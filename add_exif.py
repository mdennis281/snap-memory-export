#!/usr/bin/env python3
"""
add_exif.py

Extract date and GPS coordinates from filenames and add them as EXIF data to JPG and MP4 files.
Expects filenames in format: <isodate>-<lat>_<lon>.<ext>
Example: 2024-01-15T14-30-00-37.7749_-122.4194.jpg
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    TaskProgressColumn,
)

try:
    from PIL import Image
    import piexif
except ImportError:
    print("Error: Pillow and piexif are required. Install with: pip install Pillow piexif")
    sys.exit(1)

# Check for video metadata support
try:
    import subprocess
    HAS_FFMPEG = True
    # Check if ffmpeg is available
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=False)
    except FileNotFoundError:
        HAS_FFMPEG = False
        print("Warning: ffmpeg not found. Video EXIF writing will be disabled.")
        print("Install ffmpeg and add it to PATH for video support.")
except ImportError:
    HAS_FFMPEG = False


# Regex patterns for filename parsing
# ISO date format: 2024-01-15T14-30-00 or 2024-01-15
ISO_DATE_PATTERN = re.compile(r'^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})-(\d{2})-(\d{2}))?')
# Coordinates: after date separator, look for lat_lon pattern
# Match: -29.80406_-94.8984 (note the leading - is separator, not sign)
COORDS_PATTERN = re.compile(r'-(\d{1,3}\.\d+)_(-?\d{1,3}\.\d+)')


def parse_filename(filename: str) -> Tuple[Optional[datetime], Optional[float], Optional[float]]:
    """
    Parse filename to extract date and GPS coordinates.
    
    Args:
        filename: Filename to parse (without extension)
    
    Returns:
        (datetime, latitude, longitude) tuple, any can be None if not found
    """
    dt = None
    lat = None
    lon = None
    
    # Extract date
    date_match = ISO_DATE_PATTERN.match(filename)
    if date_match:
        year = int(date_match.group(1))
        month = int(date_match.group(2))
        day = int(date_match.group(3))
        hour = int(date_match.group(4)) if date_match.group(4) else 0
        minute = int(date_match.group(5)) if date_match.group(5) else 0
        second = int(date_match.group(6)) if date_match.group(6) else 0
        
        try:
            dt = datetime(year, month, day, hour, minute, second)
        except ValueError:
            pass
    
    # Extract coordinates
    coords_match = COORDS_PATTERN.search(filename)
    if coords_match:
        try:
            lat = float(coords_match.group(1))
            lon = float(coords_match.group(2))
            
            # Validate coordinate ranges
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                lat = None
                lon = None
        except ValueError:
            pass
    
    return dt, lat, lon


def decimal_to_dms(decimal: float, is_latitude: bool = True) -> Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int], str]:
    """
    Convert decimal degrees to degrees, minutes, seconds format for EXIF.
    
    Args:
        decimal: Decimal degrees
        is_latitude: True if latitude, False if longitude
    
    Returns:
        ((deg_num, deg_den), (min_num, min_den), (sec_num, sec_den), ref)
    """
    # Determine reference (N/S for lat, E/W for lon)
    if is_latitude:
        ref = 'N' if decimal >= 0 else 'S'
    else:
        ref = 'E' if decimal >= 0 else 'W'
    
    # Work with absolute value
    decimal = abs(decimal)
    
    # Extract degrees, minutes, seconds
    degrees = int(decimal)
    minutes_decimal = (decimal - degrees) * 60
    minutes = int(minutes_decimal)
    seconds = (minutes_decimal - minutes) * 60
    
    # Convert to rational format (numerator, denominator)
    # Use high precision for seconds
    seconds_rational = (int(seconds * 1000), 1000)
    
    return ((degrees, 1), (minutes, 1), seconds_rational, ref)


def add_exif_to_image(image_path: str, dt: Optional[datetime], lat: Optional[float], lon: Optional[float]) -> bool:
    """
    Add EXIF data to a JPEG image.
    
    Args:
        image_path: Path to the image file
        dt: DateTime to add
        lat: Latitude to add
        lon: Longitude to add
    
    Returns:
        True if successful, False otherwise
    """
    if not dt and not lat:
        return False  # Nothing to add
    
    try:
        # Load existing EXIF data or create new
        img = Image.open(image_path)
        
        try:
            exif_dict = piexif.load(image_path)
        except:
            # No existing EXIF data, create new
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        
        # Add date/time if available
        if dt:
            datetime_str = dt.strftime("%Y:%m:%d %H:%M:%S")
            exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = datetime_str
            exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = datetime_str
            exif_dict["0th"][piexif.ImageIFD.DateTime] = datetime_str
        
        # Add GPS if available
        if lat is not None and lon is not None:
            lat_dms = decimal_to_dms(lat, True)
            lon_dms = decimal_to_dms(lon, False)
            
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = lat_dms[3]
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = (lat_dms[0], lat_dms[1], lat_dms[2])
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = lon_dms[3]
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = (lon_dms[0], lon_dms[1], lon_dms[2])
        
        # Convert to bytes and save
        exif_bytes = piexif.dump(exif_dict)
        img.save(image_path, exif=exif_bytes, quality=95)
        
        return True
    
    except Exception as e:
        return False


def add_metadata_to_video(video_path: str, dt: Optional[datetime], lat: Optional[float], lon: Optional[float]) -> bool:
    """
    Add metadata to an MP4 video file using ffmpeg.
    
    Args:
        video_path: Path to the video file
        dt: DateTime to add
        lat: Latitude to add
        lon: Longitude to add
    
    Returns:
        True if successful, False otherwise
    """
    if not HAS_FFMPEG:
        return False
    
    if not dt and not lat:
        return False  # Nothing to add
    
    try:
        # Create temporary output file
        temp_output = video_path + ".temp.mp4"
        
        # Build ffmpeg command
        cmd = ['ffmpeg', '-i', video_path, '-c', 'copy', '-y']
        
        # Add metadata
        if dt:
            creation_time = dt.strftime("%Y-%m-%dT%H:%M:%S")
            cmd.extend(['-metadata', f'creation_time={creation_time}'])
        
        if lat is not None and lon is not None:
            # Add GPS as metadata (not all players support this, but it's the best we can do)
            cmd.extend(['-metadata', f'location={lat:+.6f}{lon:+.6f}/'])
            cmd.extend(['-metadata', f'location-eng={lat:+.6f}{lon:+.6f}/'])
        
        cmd.append(temp_output)
        
        # Run ffmpeg
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )
        
        if result.returncode == 0 and os.path.exists(temp_output):
            # Replace original with new file
            os.replace(temp_output, video_path)
            return True
        else:
            # Clean up temp file if it exists
            if os.path.exists(temp_output):
                os.remove(temp_output)
            return False
    
    except Exception as e:
        # Clean up temp file if it exists
        temp_output = video_path + ".temp.mp4"
        if os.path.exists(temp_output):
            os.remove(temp_output)
        return False


def process_file(file_path: str, progress=None, task_id=None) -> Tuple[bool, str]:
    """
    Process a single media file to add EXIF data.
    
    Args:
        file_path: Path to the file
        progress: Optional Rich Progress object
        task_id: Optional task ID for progress tracking
    
    Returns:
        (success, message) tuple
    """
    filename = os.path.basename(file_path)
    name_without_ext = os.path.splitext(filename)[0]
    ext = os.path.splitext(filename)[1].lower()
    
    if progress and task_id:
        progress.update(task_id, description=f"[cyan]Parsing {filename[:50]}...")
    
    # Parse filename
    dt, lat, lon = parse_filename(name_without_ext)
    
    if not dt and not lat:
        if progress and task_id:
            progress.update(task_id, description=f"[yellow]⊘ Skipped {filename[:50]} (no metadata)")
        return False, "No metadata found in filename"
    
    # Process based on file type
    if ext in ['.jpg', '.jpeg']:
        if progress and task_id:
            progress.update(task_id, description=f"[cyan]Adding EXIF to {filename[:50]}...")
        
        success = add_exif_to_image(file_path, dt, lat, lon)
        
        if success:
            parts = []
            if dt:
                parts.append(f"date: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
            if lat is not None:
                parts.append(f"GPS: {lat:.6f}, {lon:.6f}")
            message = f"Added {', '.join(parts)}"
            
            if progress and task_id:
                progress.update(task_id, description=f"[green]✓ {filename[:50]}")
            return True, message
        else:
            if progress and task_id:
                progress.update(task_id, description=f"[red]✗ Failed {filename[:50]}")
            return False, "Failed to add EXIF"
    
    elif ext in ['.mp4']:
        if not HAS_FFMPEG:
            if progress and task_id:
                progress.update(task_id, description=f"[yellow]⊘ Skipped {filename[:50]} (no ffmpeg)")
            return False, "ffmpeg not available"
        
        if progress and task_id:
            progress.update(task_id, description=f"[cyan]Adding metadata to {filename[:50]}...")
        
        success = add_metadata_to_video(file_path, dt, lat, lon)
        
        if success:
            parts = []
            if dt:
                parts.append(f"date: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
            if lat is not None:
                parts.append(f"GPS: {lat:.6f}, {lon:.6f}")
            message = f"Added {', '.join(parts)}"
            
            if progress and task_id:
                progress.update(task_id, description=f"[green]✓ {filename[:50]}")
            return True, message
        else:
            if progress and task_id:
                progress.update(task_id, description=f"[red]✗ Failed {filename[:50]}")
            return False, "Failed to add metadata"
    
    else:
        if progress and task_id:
            progress.update(task_id, description=f"[yellow]⊘ Skipped {filename[:50]} (unsupported)")
        return False, "Unsupported file type"


def main():
    parser = argparse.ArgumentParser(
        description='Add EXIF data to media files based on filename (date and GPS coordinates)'
    )
    parser.add_argument(
        '-i', '--input',
        required=True,
        help='Input directory containing media files'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Parse filenames and show what would be added without modifying files'
    )
    parser.add_argument(
        '--pattern',
        default='*',
        help='File pattern to match (default: all files)'
    )
    parser.add_argument(
        '--threads',
        type=int,
        default=4,
        help='Number of worker threads (default: 4)'
    )
    
    args = parser.parse_args()
    
    console = Console()
    
    # Validate input directory
    input_dir = Path(args.input)
    if not input_dir.exists() or not input_dir.is_dir():
        console.print(f'[red]Error: Input directory not found:[/red] {args.input}')
        sys.exit(1)
    
    # Find all media files
    image_files = list(input_dir.glob('*.jpg')) + list(input_dir.glob('*.jpeg'))
    video_files = list(input_dir.glob('*.mp4'))
    all_files = image_files + video_files
    
    if not all_files:
        console.print(f'[yellow]No JPG or MP4 files found in {args.input}[/yellow]')
        sys.exit(0)
    
    console.print(f'[cyan]Found {len(image_files)} image(s) and {len(video_files)} video(s)[/cyan]')
    console.print(f'[cyan]Using {args.threads} worker thread(s)[/cyan]')
    console.print(f'[cyan]Input directory: {input_dir}[/cyan]')
    
    if args.dry_run:
        console.print('[yellow]DRY RUN - No files will be modified[/yellow]\n')
    else:
        console.print()
    
    # Process files
    success_count = 0
    skip_count = 0
    failures: List[Tuple[str, str]] = []  # (filename, error_message)
    lock = threading.Lock()
    interrupted = False
    pending_count = 0
    
    if args.dry_run:
        # Dry run - just show what would be done
        for file_path in all_files:
            filename = os.path.basename(str(file_path))
            name_without_ext = os.path.splitext(filename)[0]
            
            dt, lat, lon = parse_filename(name_without_ext)
            
            if dt or lat:
                console.print(f'[cyan]{filename}[/cyan]')
                if dt:
                    console.print(f'  Date: [green]{dt.strftime("%Y-%m-%d %H:%M:%S")}[/green]')
                if lat is not None and lon is not None:
                    console.print(f'  GPS: [green]{lat:.6f}, {lon:.6f}[/green]')
                success_count += 1
            else:
                console.print(f'[yellow]{filename}[/yellow]')
                console.print(f'  [dim]No metadata found in filename[/dim]')
                skip_count += 1
            console.print()
    else:
        # Actually process files with progress bars and multithreading
        # Create per-thread tasks for progress tracking
        thread_tasks: Dict[int, int] = {}
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            
            # Overall progress task
            overall_task = progress.add_task(
                "[green]Overall Progress", 
                total=len(all_files)
            )
            
            # Create worker thread tasks
            for i in range(args.threads):
                task_id = progress.add_task(
                    f"[dim]Worker #{i+1}: Idle", 
                    total=100
                )
                thread_tasks[i] = task_id
            
            def process_with_progress(file_path: Path, worker_id: int):
                """Wrapper to process a file with progress tracking."""
                nonlocal success_count, skip_count
                
                task_id = thread_tasks[worker_id]
                filename = os.path.basename(str(file_path))
                
                # Reset task
                progress.update(task_id, completed=0, total=100)
                
                success, error_msg = process_file(
                    str(file_path),
                    progress=progress,
                    task_id=task_id
                )
                
                # Update counters thread-safely
                with lock:
                    if success:
                        success_count += 1
                    else:
                        skip_count += 1
                        if error_msg:
                            failures.append((filename, error_msg))
                    
                    # Update overall progress
                    progress.update(overall_task, advance=1,
                                  description=f"[green]Overall Progress [cyan]({success_count} done, {skip_count} skipped)")
                
                # Mark worker as idle
                progress.update(task_id, completed=100, 
                              description=f"[dim]Worker #{worker_id+1}: Idle")
                
                return success
            
            # Process files with thread pool
            interrupted = False
            pending_count = 0
            
            try:
                with ThreadPoolExecutor(max_workers=args.threads) as executor:
                    futures = {}
                    worker_counter = 0
                    
                    # Submit all jobs
                    for file_path in all_files:
                        worker_id = worker_counter % args.threads
                        future = executor.submit(process_with_progress, file_path, worker_id)
                        futures[future] = (file_path, worker_id)
                        worker_counter += 1
                    
                    # Wait for completion
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            file_path, worker_id = futures[future]
                            error_msg = f"Unhandled exception: {type(e).__name__}: {str(e)}"
                            filename = os.path.basename(str(file_path))
                            with lock:
                                skip_count += 1
                                failures.append((filename, error_msg))
            
            except KeyboardInterrupt:
                interrupted = True
                console.print('\n\n[yellow]Interrupted by user. Waiting for active jobs to finish...[/yellow]')
                
                # Calculate how many were pending
                completed_count = success_count + skip_count
                pending_count = len(all_files) - completed_count
                
                # Let running futures complete
                for future in futures:
                    if not future.done():
                        try:
                            future.result(timeout=30)  # Wait up to 30s for each
                        except Exception:
                            pass  # Ignore errors during cleanup
    
    # Summary
    console.print('\n[bold cyan]Summary:[/bold cyan]')
    console.print(f'  Total files: [bold]{len(all_files)}[/bold]')
    console.print(f'  Successfully processed: [green]{success_count}[/green]')
    console.print(f'  Skipped: [yellow]{skip_count}[/yellow]')
    if not args.dry_run and interrupted and pending_count > 0:
        console.print(f'  Pending (interrupted): [yellow]{pending_count}[/yellow]')
    
    # Show failures if any
    if failures and not args.dry_run:
        console.print(f'\n[bold red]Failures ({len(failures)}):[/bold red]')
        for filename, error_msg in failures[:20]:  # Show first 20
            console.print(f'  [yellow]{filename}[/yellow]')
            console.print(f'    [dim]{error_msg}[/dim]')
        
        if len(failures) > 20:
            console.print(f'\n  [dim]... and {len(failures) - 20} more failures[/dim]')
    
    if success_count > 0 and not args.dry_run:
        pct = int((success_count / len(all_files)) * 100)
        if interrupted:
            console.print(f'\n[yellow]Interrupted. {pct}% complete[/yellow]')
        else:
            console.print(f'\n[green]✓ Done! {pct}% success rate[/green]')
    elif args.dry_run:
        console.print(f'\n[yellow]Dry run complete. Use without --dry-run to apply changes.[/yellow]')
    elif interrupted:
        console.print(f'\n[yellow]Interrupted before completion[/yellow]')


if __name__ == '__main__':
    main()
