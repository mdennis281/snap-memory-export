#!/usr/bin/env python3
"""
download_memories_ui.py

Enhanced UI version with rich progress bars showing:
- Overall progress with skipped/failed context
- Individual progress bars for each thread
- Live updating display of active downloads
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Optional, Dict
from urllib.parse import unquote, urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter, Retry
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TaskID,
)
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout


DOWNLOAD_MEMORIES_RE = re.compile(r"downloadMemories\('(.+?)',\s*this,\s*(true|false)\)")
HREF_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.IGNORECASE)
IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
DATE_ONLY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
COORD_RE = re.compile(r"(-?\d{1,3}\.\d+)[,\s;]+(-?\d{1,3}\.\d+)")


def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = name.replace('\r', '').replace('\n', '')
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    return name[:240]


def get_filename_from_response(resp: requests.Response, url: str, index: int) -> str:
    cd = resp.headers.get('content-disposition')
    if cd:
        m = re.search(r"filename\*=UTF-8''([^;\n]+)", cd)
        if m:
            return sanitize_filename(unquote(m.group(1)))
        m = re.search(r'filename=(?:"?)([^;\n"]+)', cd)
        if m:
            return sanitize_filename(m.group(1).strip('"'))

    p = urlparse(url)
    path = os.path.basename(p.path)
    if path:
        return sanitize_filename(unquote(path))

    qs = parse_qs(p.query)
    for key in ('sid', 'id', 'file'):
        if key in qs and qs[key]:
            return sanitize_filename(qs[key][0])

    return f"download_{index}"


def extract_urls(html: str) -> List[Tuple[str, bool, int]]:
    results: List[Tuple[int, Tuple[str, bool]]] = []

    for m in DOWNLOAD_MEMORIES_RE.finditer(html):
        url = m.group(1)
        is_get = m.group(2).lower() == 'true'
        results.append((m.start(), (url, is_get)))

    for m in HREF_RE.finditer(html):
        href = m.group(1)
        if href.startswith('data:'):
            continue
        results.append((m.start(), (href, True)))

    for m in IMG_RE.finditer(html):
        src = m.group(1)
        if src.startswith('data:'):
            continue
        results.append((m.start(), (src, True)))

    results.sort(key=lambda t: t[0])
    seen = set()
    out: List[Tuple[str, bool, int]] = []
    for pos, (url, is_get) in results:
        if url in seen:
            continue
        seen.add(url)
        out.append((url, is_get, pos))

    return out


def extract_metadata(html: str, pos: int, window: int = 1200) -> Tuple[Optional[str], Optional[str]]:
    start = max(0, pos - window)
    end = pos + window
    snippet = html[start:end]

    iso = None
    coords = None

    m = ISO_DATE_RE.search(snippet)
    if m:
        iso = m.group(1).replace(':', '-')
    else:
        m2 = DATE_ONLY_RE.search(snippet)
        if m2:
            iso = m2.group(1)

    m3 = COORD_RE.search(snippet)
    if m3:
        lat = m3.group(1)
        lon = m3.group(2)
        coords = f"{lat}_{lon}"

    return iso, coords


thread_local = threading.local()


def get_thread_session() -> requests.Session:
    s = getattr(thread_local, 'session', None)
    if s is None:
        s = make_session()
        thread_local.session = s
    return s


class DownloadTracker:
    """Thread-safe tracker for download progress."""
    def __init__(self):
        self.lock = threading.Lock()
        self.thread_status: Dict[int, Dict] = {}  # thread_id -> {filename, stage, progress}
    
    def update_thread(self, thread_id: int, filename: str = None, stage: str = None, progress: int = None, total: int = None):
        with self.lock:
            if thread_id not in self.thread_status:
                self.thread_status[thread_id] = {}
            if filename is not None:
                self.thread_status[thread_id]['filename'] = filename
            if stage is not None:
                self.thread_status[thread_id]['stage'] = stage
            if progress is not None:
                self.thread_status[thread_id]['progress'] = progress
            if total is not None:
                self.thread_status[thread_id]['total'] = total
    
    def clear_thread(self, thread_id: int):
        with self.lock:
            if thread_id in self.thread_status:
                self.thread_status[thread_id] = {'filename': 'Idle', 'stage': 'waiting', 'progress': 0, 'total': 0}
    
    def get_status(self) -> Dict[int, Dict]:
        with self.lock:
            return dict(self.thread_status)


def download_one(
    session: Optional[requests.Session],
    url: str,
    is_get: bool,
    outdir: str,
    index: int,
    iso: Optional[str] = None,
    coords: Optional[str] = None,
    timeout: int = 30,
    tracker: Optional[DownloadTracker] = None,
    max_retries: int = 3
) -> Tuple[bool, str]:
    """
    Download a single file with retry logic and exponential backoff.
    
    Args:
        max_retries: Number of additional retry attempts for transient errors
    """
    thread_id = threading.get_ident()
    last_error = None
    
    for attempt in range(max_retries + 1):
        try:
            if session is None:
                session = get_thread_session()

            # Determine expected filename early
            if iso or coords:
                parts = []
                if iso:
                    parts.append(iso)
                if coords:
                    parts.append(coords)
                base_name = '-'.join(parts)
                expected_fname = sanitize_filename(base_name)
            else:
                expected_fname = f"file_{index}"
            
            retry_suffix = f" (retry {attempt}/{max_retries})" if attempt > 0 else ""
            if tracker:
                tracker.update_thread(thread_id, filename=expected_fname[:40], stage=f'connecting{retry_suffix}')

            if is_get:
                headers = {'X-Snap-Route-Tag': 'mem-dmd', 'User-Agent': 'download_memories/1.0'}
                resp = session.get(url, headers=headers, stream=True, timeout=timeout)
            else:
                parts = url.split('?', 1)
                post_url = parts[0]
                data = parts[1] if len(parts) > 1 else ''
                headers = {'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'download_memories/1.0'}
                resp = session.post(post_url, data=data, headers=headers, stream=True, timeout=timeout)

            # Check for retryable status codes
            if resp.status_code in [429, 500, 502, 503, 504]:
                if attempt < max_retries:
                    # Exponential backoff: 1s, 2s, 4s, ...
                    backoff_time = 2 ** attempt
                    last_error = f"HTTP {resp.status_code}"
                    if tracker:
                        tracker.update_thread(thread_id, stage=f'backoff {backoff_time}s')
                    time.sleep(backoff_time)
                    continue
                else:
                    if tracker:
                        tracker.clear_thread(thread_id)
                    return False, f"HTTP {resp.status_code} (after {max_retries} retries)"
            
            if resp.status_code != 200:
                if tracker:
                    tracker.clear_thread(thread_id)
                return False, f"HTTP {resp.status_code} for {url}"

            # Get content length for progress tracking
            content_length = resp.headers.get('content-length')
            total_size = int(content_length) if content_length else 0

            # Determine extension
            ext = ''
            cd_name = None
            cd = resp.headers.get('content-disposition')
            if cd:
                m = re.search(r"filename\*=UTF-8''([^;\n]+)", cd)
                if m:
                    cd_name = unquote(m.group(1))
                else:
                    m2 = re.search(r'filename=(?:"?)([^;\n"]+)', cd)
                    if m2:
                        cd_name = m2.group(1).strip('"')

            if cd_name and '.' in cd_name:
                _, ext = os.path.splitext(cd_name)

            if not ext:
                p = urlparse(url)
                _, ext = os.path.splitext(p.path)

            if not ext:
                ct = resp.headers.get('content-type', '')
                if 'jpeg' in ct or 'jpg' in ct:
                    ext = '.jpg'
                elif 'png' in ct:
                    ext = '.png'
                elif 'gif' in ct:
                    ext = '.gif'

            if iso or coords:
                parts = []
                if iso:
                    parts.append(iso)
                if coords:
                    parts.append(coords)
                base_name = '-'.join(parts)
                fname = sanitize_filename(base_name) + (ext or '')
            else:
                fname = get_filename_from_response(resp, url, index)
                if '.' not in fname and ext:
                    fname += ext

            outpath = os.path.join(outdir, fname)
            
            if tracker:
                tracker.update_thread(thread_id, filename=fname[:40], stage='downloading', progress=0, total=total_size)

            downloaded = 0
            with open(outpath, 'wb') as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if tracker and total_size > 0:
                            tracker.update_thread(thread_id, progress=downloaded)

            if tracker:
                tracker.update_thread(thread_id, stage='complete')
                tracker.clear_thread(thread_id)

            return True, outpath
            
        except (requests.exceptions.Timeout, 
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as e:
            # Transient errors - retry with backoff
            last_error = str(e)
            if attempt < max_retries:
                backoff_time = 2 ** attempt
                if tracker:
                    tracker.update_thread(thread_id, stage=f'backoff {backoff_time}s')
                time.sleep(backoff_time)
                continue
            else:
                if tracker:
                    tracker.clear_thread(thread_id)
                return False, f"{type(e).__name__}: {str(e)} (after {max_retries} retries)"
        
        except Exception as e:
            # Non-retryable errors
            if tracker:
                tracker.clear_thread(thread_id)
            return False, f"{type(e).__name__}: {str(e)}"
    
    # Should not reach here, but just in case
    if tracker:
        tracker.clear_thread(thread_id)
    return False, f"Failed after {max_retries} retries: {last_error}"


def make_session(max_retries: int = 5, backoff_factor: float = 0.5) -> requests.Session:
    """
    Create a requests session with robust retry logic.
    
    Args:
        max_retries: Maximum number of retry attempts
        backoff_factor: Exponential backoff multiplier (delay = backoff_factor * (2 ^ retry_number))
                       e.g., 0.5 gives delays of [0.5s, 1s, 2s, 4s, 8s]
    """
    s = requests.Session()
    
    # Configure retry strategy with exponential backoff
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],  # Retry on these HTTP status codes
        allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],
        raise_on_status=False,  # Don't raise exception, let us handle status codes
        respect_retry_after_header=True,  # Honor Retry-After headers from server
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    
    return s


def make_status_table(tracker: DownloadTracker, num_workers: int) -> Table:
    """Create a table showing status of each worker thread."""
    table = Table(title="Worker Status", show_header=True, header_style="bold magenta")
    table.add_column("Worker", style="cyan", width=8)
    table.add_column("File", style="green", width=40)
    table.add_column("Stage", style="yellow", width=12)
    table.add_column("Progress", style="blue", width=30)
    
    status = tracker.get_status()
    for i in range(num_workers):
        # Find a thread_id that maps to this worker (or show idle)
        worker_name = f"#{i+1}"
        thread_info = None
        
        # Get any thread_id for this worker
        all_threads = list(status.keys())
        if i < len(all_threads):
            thread_info = status.get(all_threads[i], {})
        
        if thread_info and thread_info.get('filename') != 'Idle':
            filename = thread_info.get('filename', 'Unknown')
            stage = thread_info.get('stage', 'unknown')
            progress = thread_info.get('progress', 0)
            total = thread_info.get('total', 0)
            
            if total > 0:
                pct = int((progress / total) * 100)
                progress_str = f"{pct}% ({progress:,}/{total:,} bytes)"
            else:
                progress_str = stage
            
            table.add_row(worker_name, filename, stage, progress_str)
        else:
            table.add_row(worker_name, "[dim]Idle[/dim]", "[dim]waiting[/dim]", "[dim]-[/dim]")
    
    return table


def main():
    p = argparse.ArgumentParser(description='Download media with rich UI progress tracking')
    p.add_argument('-i', '--input', required=True, help='Path to memories_history.html')
    p.add_argument('-o', '--outdir', default='memories_downloads', help='Output directory')
    p.add_argument('--dry-run', action='store_true', help='List URLs without downloading')
    p.add_argument('--delay', type=float, default=0.0, help='Delay between scheduling downloads (seconds)')
    p.add_argument('--threads', type=int, default=5, help='Number of worker threads')
    args = p.parse_args()

    console = Console()

    if not os.path.isfile(args.input):
        console.print(f'[red]Input file not found:[/red] {args.input}')
        sys.exit(2)

    os.makedirs(args.outdir, exist_ok=True)

    console.print(f'[cyan]Reading HTML file...[/cyan]')
    with open(args.input, 'r', encoding='utf-8', errors='replace') as f:
        html = f.read()

    items = extract_urls(html)
    if not items:
        console.print('[yellow]No download links found in the HTML file.[/yellow]')
        sys.exit(0)

    console.print(f'[green]Found {len(items)} candidate links.[/green]')

    if args.dry_run:
        for idx, (url, is_get, pos) in enumerate(items, start=1):
            console.print(f'{idx:3d}: [{"GET" if is_get else "POST"}] {url}')
        console.print('[yellow]Dry run; no files downloaded.[/yellow]')
        return

    # Cache existing files
    console.print(f'[cyan]Scanning output directory for existing files...[/cyan]')
    existing_files = set()
    existing_basenames = {}
    try:
        for entry in os.scandir(args.outdir):
            if entry.is_file():
                existing_files.add(entry.path)
                existing_basenames[entry.name] = entry.path
                base_no_ext = os.path.splitext(entry.name)[0]
                base_clean = re.sub(r'\(\d+\)$', '', base_no_ext)
                existing_basenames[base_no_ext] = entry.path
                if base_clean != base_no_ext:
                    existing_basenames[base_clean] = entry.path
    except FileNotFoundError:
        pass
    console.print(f'[green]Found {len(existing_files)} existing files.[/green]\n')

    # Setup progress tracking
    tracker = DownloadTracker()
    successes = 0
    failures = []
    skipped = []
    total = len(items)
    
    # Create progress bars
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("•"),
        TextColumn("[cyan]{task.completed}/{task.total}[/cyan]"),
        TimeRemainingColumn(),
        console=console,
    )

    overall_task = progress.add_task("[green]Overall Progress", total=total)
    
    interrupted = False
    
    try:
        with progress:
            with ThreadPoolExecutor(max_workers=args.threads) as ex:
                # Initialize tracker for all potential workers
                for i in range(args.threads):
                    tracker.thread_status[i] = {'filename': 'Idle', 'stage': 'waiting', 'progress': 0, 'total': 0}
                
                futures_map = {}
                scheduled_this_run = set()
                
                for idx, (url, is_get, pos) in enumerate(items, start=1):
                    iso, coords = extract_metadata(html, pos)

                    already = False
                    expected_basename = None
                    
                    if iso or coords:
                        parts = []
                        if iso:
                            parts.append(iso)
                        if coords:
                            parts.append(coords)
                        base_name = sanitize_filename('-'.join(parts))
                        expected_basename = base_name
                        base_name_clean = re.sub(r'\(\d+\)$', '', base_name)
                        if base_name in existing_basenames or base_name_clean in existing_basenames:
                            already = True
                        elif base_name in scheduled_this_run or base_name_clean in scheduled_this_run:
                            already = True

                    if not already:
                        p_url = urlparse(url)
                        url_name = os.path.basename(p_url.path)
                        if expected_basename is None:
                            expected_basename = os.path.splitext(url_name)[0]
                        if url_name and url_name in existing_basenames:
                            already = True

                    if already:
                        skipped.append(idx)
                        progress.update(overall_task, advance=1, 
                                      description=f"[green]Overall [yellow]({len(skipped)} skipped, {len(failures)} failed)")
                        continue

                    if expected_basename:
                        scheduled_this_run.add(expected_basename)
                        scheduled_this_run.add(re.sub(r'\(\d+\)$', '', expected_basename))

                    fut = ex.submit(download_one, None, url, is_get, args.outdir, idx, iso, coords, 30, tracker)
                    futures_map[fut] = (idx, url, iso, coords)
                    
                    if args.delay:
                        time.sleep(args.delay)

                # Collect results
                for fut in as_completed(futures_map):
                    idx, url, iso, coords = futures_map[fut]
                    ok, message = fut.result()
                    
                    if ok:
                        successes += 1
                        progress.update(overall_task, advance=1,
                                      description=f"[green]Overall [cyan]({successes} downloaded, [yellow]{len(skipped)} skipped, [red]{len(failures)} failed)")
                    else:
                        failures.append((idx, url, message))
                        progress.update(overall_task, advance=1,
                                      description=f"[green]Overall [cyan]({successes} downloaded, [yellow]{len(skipped)} skipped, [red]{len(failures)} failed)")

    except KeyboardInterrupt:
        interrupted = True
        console.print('\n\n[yellow]Interrupted by user. Waiting for active downloads to finish...[/yellow]')
        for fut in futures_map:
            if fut.done():
                try:
                    ok, message = fut.result()
                    if ok:
                        successes += 1
                    else:
                        idx, url, _, _ = futures_map[fut]
                        failures.append((idx, url, message))
                except Exception:
                    pass

    # Summary
    console.print('\n[bold cyan]Summary:[/bold cyan]')
    summary_table = Table(show_header=False, box=None)
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Count", style="green", justify="right")
    
    summary_table.add_row("Total", str(total))
    summary_table.add_row("Downloaded", f"[green]{successes}[/green]")
    summary_table.add_row("Skipped", f"[yellow]{len(skipped)}[/yellow]")
    summary_table.add_row("Failed", f"[red]{len(failures)}[/red]")
    if interrupted:
        pending = total - (successes + len(skipped) + len(failures))
        summary_table.add_row("Pending", f"[yellow]{pending}[/yellow]")
    
    console.print(summary_table)

    if failures and len(failures) <= 20:
        console.print('\n[bold red]Failures:[/bold red]')
        for idx, url, reason in failures[:20]:
            short_url = url[:60] + '...' if len(url) > 60 else url
            short_reason = reason[:60] + '...' if len(reason) > 60 else reason
            console.print(f'  [dim]{idx:3d}:[/dim] {short_reason} -> {short_url}')
        if len(failures) > 20:
            console.print(f'  [dim]... and {len(failures) - 20} more[/dim]')
    elif failures:
        console.print(f'\n[red]{len(failures)} failures occurred (too many to display)[/red]')

    if interrupted:
        console.print(f"\n[yellow]Interrupted. {successes} of {total} files downloaded to {args.outdir}[/yellow]")
        sys.exit(130)
    else:
        pct = int((successes/total)*100) if total > 0 else 0
        console.print(f"\n[green]Done! {successes} of {total} files downloaded to {args.outdir} ({pct}%)[/green]")


if __name__ == '__main__':
    main()
