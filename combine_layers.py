#!/usr/bin/env python3
"""
combine.py

Process downloaded Snapchat memories that contain zipped files with:
- media file (jpg/mp4)
- layer file (png overlay)

Combines the layer on top of the media and saves as the original media type.
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile
import tempfile
import shutil
import threading
from pathlib import Path
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
except ImportError:
    print("Error: Pillow is required. Install with: pip install Pillow")
    sys.exit(1)

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    print("Warning: opencv-python not found. Video processing will be disabled.")
    print("Install with: pip install opencv-python")


def find_media_and_layer(zip_path: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extract and identify media and layer files from a zip archive.
    
    Returns:
        (media_path, layer_path, media_type) where media_type is 'image' or 'video'
    """
    with zipfile.ZipFile(zip_path, 'r') as zf:
        files = zf.namelist()
        
        media_file = None
        layer_file = None
        media_type = None
        
        for filename in files:
            lower = filename.lower()
            
            # Skip macOS metadata files
            if filename.startswith('__MACOSX') or filename.startswith('.'):
                continue
            
            # Identify layer file (PNG overlay)
            if 'layer' in lower or 'overlay' in lower:
                if lower.endswith('.png'):
                    layer_file = filename
            # Identify media files
            elif lower.endswith(('.jpg', '.jpeg')):
                media_file = filename
                media_type = 'image'
            elif lower.endswith('.mp4'):
                media_file = filename
                media_type = 'video'
        
        # If we didn't find explicit layer file, look for any PNG
        if layer_file is None:
            for filename in files:
                if filename.lower().endswith('.png') and not filename.startswith('__MACOSX'):
                    layer_file = filename
                    break
        
        return media_file, layer_file, media_type


def combine_images(media_path: str, layer_path: str, output_path: str, progress=None, task_id=None) -> bool:
    """
    Combine a base image with a transparent PNG layer.
    
    Args:
        media_path: Path to the base image (JPG)
        layer_path: Path to the overlay layer (PNG)
        output_path: Path to save the combined image
        progress: Optional Rich Progress object
        task_id: Optional task ID for progress tracking
    
    Returns:
        True if successful, False otherwise
    """
    try:
        if progress and task_id:
            progress.update(task_id, description="[cyan]Loading images...")
        
        # Open both images
        base = Image.open(media_path).convert('RGBA')
        layer = Image.open(layer_path).convert('RGBA')
        
        # Verify dimensions match (should always be the case according to requirements)
        if base.size != layer.size:
            if progress and task_id:
                progress.update(task_id, description=f"[yellow]Resizing layer...")
            # Resize layer to match base if needed
            layer = layer.resize(base.size, Image.Resampling.LANCZOS)
        
        if progress and task_id:
            progress.update(task_id, description="[cyan]Compositing...")
        
        # Composite the images (layer on top of base)
        combined = Image.alpha_composite(base, layer)
        
        # Convert back to RGB if saving as JPG
        if output_path.lower().endswith(('.jpg', '.jpeg')):
            combined = combined.convert('RGB')
        
        if progress and task_id:
            progress.update(task_id, description="[cyan]Saving image...")
        
        # Save with high quality
        if output_path.lower().endswith(('.jpg', '.jpeg')):
            combined.save(output_path, 'JPEG', quality=95)
        else:
            combined.save(output_path, 'PNG')
        
        if progress and task_id:
            progress.update(task_id, description="[green]✓ Image complete")
        
        return True
    
    except Exception as e:
        if progress and task_id:
            progress.update(task_id, description=f"[red]✗ Error: {str(e)[:40]}")
        return False


def combine_video(media_path: str, layer_path: str, output_path: str, progress=None, task_id=None) -> bool:
    """
    Combine a base video with a transparent PNG layer overlay.
    
    Args:
        media_path: Path to the base video (MP4)
        layer_path: Path to the overlay layer (PNG)
        output_path: Path to save the combined video
        progress: Optional Rich Progress object
        task_id: Optional task ID for progress tracking
    
    Returns:
        True if successful, False otherwise
    """
    if not HAS_CV2:
        if progress and task_id:
            progress.update(task_id, description="[red]✗ opencv-python required")
        return False
    
    try:
        if progress and task_id:
            progress.update(task_id, description="[cyan]Loading video...")
        
        # Load the overlay layer
        layer_pil = Image.open(layer_path).convert('RGBA')
        layer_np = np.array(layer_pil)
        
        # Open the video
        cap = cv2.VideoCapture(media_path)
        
        # Get video properties
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        # Resize layer if dimensions don't match
        if layer_pil.size != (width, height):
            if progress and task_id:
                progress.update(task_id, description="[yellow]Resizing layer...")
            layer_pil = layer_pil.resize((width, height), Image.Resampling.LANCZOS)
            layer_np = np.array(layer_pil)
        
        if progress and task_id:
            progress.update(task_id, description="[cyan]Processing video...", total=total_frames, completed=0)
        
        # Create video writer
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
        
        frame_count = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            # Convert frame to RGBA
            frame_rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
            
            # Blend the layer onto the frame
            # Extract alpha channel from layer
            alpha = layer_np[:, :, 3] / 255.0
            
            # Apply alpha blending
            for c in range(3):  # RGB channels
                frame_rgba[:, :, c] = (
                    alpha * layer_np[:, :, c] +
                    (1 - alpha) * frame_rgba[:, :, c]
                )
            
            # Convert back to BGR for video writer
            frame_bgr = cv2.cvtColor(frame_rgba, cv2.COLOR_RGBA2BGR)
            out.write(frame_bgr)
            
            frame_count += 1
            if progress and task_id:
                progress.update(task_id, completed=frame_count, 
                              description=f"[cyan]Processing frames ({frame_count}/{total_frames})...")
        
        cap.release()
        out.release()
        
        if progress and task_id:
            progress.update(task_id, description=f"[green]✓ Video complete ({frame_count} frames)")
        
        return True
    
    except Exception as e:
        if progress and task_id:
            progress.update(task_id, description=f"[red]✗ Error: {str(e)[:40]}")
        return False


def process_zip_file(zip_path: str, output_dir: str, keep_originals: bool = True, 
                     progress=None, task_id=None) -> Tuple[bool, Optional[str], bool]:
    """
    Process a single zip file containing media and layer.
    
    Args:
        zip_path: Path to the zip file
        output_dir: Directory to save the combined result
        keep_originals: If True, keep the original zip file
        progress: Optional Rich Progress object
        task_id: Optional task ID for progress tracking
    
    Returns:
        (success, error_message, already_existed) tuple
    """
    zip_name = os.path.basename(zip_path)
    
    # Check if already processed - look for file with same name but different extension
    zip_basename = os.path.splitext(os.path.basename(zip_path))[0]
    output_dir_path = Path(output_dir)
    
    # Check for common media extensions
    for ext in ['.jpg', '.jpeg', '.mp4', '.png']:
        potential_output = output_dir_path / f"{zip_basename}{ext}"
        if potential_output.exists():
            if progress and task_id:
                progress.update(task_id, description=f"[dim]⊘ Already exists: {zip_name[:40]}")
            return True, None, True  # Success, no error, already existed
    
    if progress and task_id:
        progress.update(task_id, description=f"[cyan]Analyzing {zip_name[:40]}...")
    
    try:
        # Find media and layer files in the zip
        media_file, layer_file, media_type = find_media_and_layer(zip_path)
        
        if not media_file or not layer_file:
            error_msg = f"Missing files in {zip_name}: media={media_file}, layer={layer_file}"
            if progress and task_id:
                progress.update(task_id, description=f"[yellow]⊘ Skipped {zip_name[:40]} (missing files)")
            return False, error_msg, False
        
        # Create temporary directory for extraction
        with tempfile.TemporaryDirectory() as temp_dir:
            if progress and task_id:
                progress.update(task_id, description=f"[cyan]Extracting {zip_name[:40]}...")
            
            # Extract files
            with zipfile.ZipFile(zip_path, 'r') as zf:
                media_path = zf.extract(media_file, temp_dir)
                layer_path = zf.extract(layer_file, temp_dir)
            
            # Determine output filename (use zip filename + media extension)
            media_ext = os.path.splitext(media_path)[1]
            base_name = f"{zip_basename}{media_ext}"
            output_path = os.path.join(output_dir, base_name)
            
            # Combine based on media type
            if media_type == 'image':
                success = combine_images(media_path, layer_path, output_path, progress, task_id)
            elif media_type == 'video':
                success = combine_video(media_path, layer_path, output_path, progress, task_id)
            else:
                error_msg = f"Unknown media type for {zip_name}"
                if progress and task_id:
                    progress.update(task_id, description=f"[red]✗ Unknown media type")
                return False, error_msg, False
            
            if success:
                # Remove original zip if requested
                if not keep_originals:
                    os.remove(zip_path)
                
                return True, None, False  # Success, no error, newly processed
            else:
                error_msg = f"Failed to combine {zip_name} (type: {media_type})"
                return False, error_msg, False
    
    except Exception as e:
        error_msg = f"Exception processing {zip_name}: {type(e).__name__}: {str(e)}"
        if progress and task_id:
            progress.update(task_id, description=f"[red]✗ Error: {str(e)[:40]}")
        return False, error_msg, False


def main():
    parser = argparse.ArgumentParser(
        description='Combine media and layer files from zipped Snapchat memories'
    )
    parser.add_argument(
        '-i', '--input',
        required=True,
        help='Input directory containing zip files'
    )
    parser.add_argument(
        '-o', '--output',
        default=None,
        help='Output directory for combined files (default: same as input)'
    )
    parser.add_argument(
        '--remove-originals',
        action='store_true',
        help='Remove original zip files after processing'
    )
    parser.add_argument(
        '--pattern',
        default='*.zip',
        help='File pattern to match (default: *.zip)'
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
    
    # Set output directory
    output_dir = Path(args.output) if args.output else input_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Find all zip files
    zip_files = list(input_dir.glob(args.pattern))
    
    if not zip_files:
        console.print(f'[yellow]No files matching \'{args.pattern}\' found in {args.input}[/yellow]')
        sys.exit(0)
    
    console.print(f'[cyan]Found {len(zip_files)} zip file(s)[/cyan]')
    console.print(f'[cyan]Using {args.threads} worker thread(s)[/cyan]')
    console.print(f'[cyan]Output directory: {output_dir}[/cyan]\n')
    
    # Process files with rich progress bars and multithreading
    success_count = 0
    skip_count = 0
    already_exists_count = 0
    failures: List[Tuple[str, str]] = []  # (filename, error_message)
    lock = threading.Lock()
    
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
            total=len(zip_files)
        )
        
        # Create worker thread tasks
        for i in range(args.threads):
            task_id = progress.add_task(
                f"[dim]Worker #{i+1}: Idle", 
                total=100
            )
            thread_tasks[i] = task_id
        
        def process_with_progress(zip_path: Path, worker_id: int):
            """Wrapper to process a zip file with progress tracking."""
            nonlocal success_count, skip_count, already_exists_count
            
            task_id = thread_tasks[worker_id]
            
            result, error_msg, already_existed = process_zip_file(
                str(zip_path),
                str(output_dir),
                keep_originals=not args.remove_originals,
                progress=progress,
                task_id=task_id
            )
            
            # Update counters thread-safely
            with lock:
                if result:
                    if already_existed:
                        already_exists_count += 1
                    else:
                        success_count += 1
                else:
                    skip_count += 1
                    if error_msg:
                        failures.append((os.path.basename(str(zip_path)), error_msg))
                
                # Update overall progress
                progress.update(overall_task, advance=1,
                              description=f"[green]Overall Progress [cyan]({success_count} new, {already_exists_count} existing, {skip_count} failed)")
            
            # Mark worker as idle
            progress.update(task_id, completed=100, 
                          description=f"[dim]Worker #{worker_id+1}: Idle")
            
            return result
        
        # Process files with thread pool
        interrupted = False
        pending_count = 0
        
        try:
            with ThreadPoolExecutor(max_workers=args.threads) as executor:
                futures = {}
                worker_counter = 0
                
                # Submit all jobs
                for zip_path in zip_files:
                    worker_id = worker_counter % args.threads
                    future = executor.submit(process_with_progress, zip_path, worker_id)
                    futures[future] = (zip_path, worker_id)
                    worker_counter += 1
                
                # Wait for completion
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        zip_path, worker_id = futures[future]
                        error_msg = f"Unhandled exception: {type(e).__name__}: {str(e)}"
                        with lock:
                            skip_count += 1
                            failures.append((os.path.basename(str(zip_path)), error_msg))
        
        except KeyboardInterrupt:
            interrupted = True
            console.print('\n\n[yellow]Interrupted by user. Waiting for active jobs to finish...[/yellow]')
            
            # Calculate how many were pending
            completed_count = success_count + skip_count + already_exists_count
            pending_count = len(zip_files) - completed_count
            
            # Let running futures complete
            for future in futures:
                if not future.done():
                    try:
                        future.result(timeout=30)  # Wait up to 30s for each
                    except Exception:
                        pass  # Ignore errors during cleanup
    
    # Summary
    console.print('\n[bold cyan]Summary:[/bold cyan]')
    console.print(f'  Total files: [bold]{len(zip_files)}[/bold]')
    console.print(f'  Newly processed: [green]{success_count}[/green]')
    console.print(f'  Already existed: [blue]{already_exists_count}[/blue]')
    console.print(f'  Failed: [red]{skip_count}[/red]')
    if interrupted and pending_count > 0:
        console.print(f'  Pending (interrupted): [yellow]{pending_count}[/yellow]')
    console.print(f'  Output directory: [cyan]{output_dir}[/cyan]')
    
    # Show failures if any
    if failures:
        console.print(f'\n[bold red]Failures ({len(failures)}):[/bold red]')
        for filename, error_msg in failures[:20]:  # Show first 20
            console.print(f'  [yellow]{filename}[/yellow]')
            console.print(f'    [dim]{error_msg}[/dim]')
        
        if len(failures) > 20:
            console.print(f'\n  [dim]... and {len(failures) - 20} more failures[/dim]')
    
    if success_count > 0:
        total_processed = success_count + already_exists_count
        pct = int((total_processed / len(zip_files)) * 100)
        if interrupted:
            console.print(f'\n[yellow]Interrupted. {pct}% complete ({success_count} new, {already_exists_count} existing)[/yellow]')
        else:
            console.print(f'\n[green]✓ Done! {pct}% complete ({success_count} new, {already_exists_count} existing)[/green]')
    elif already_exists_count > 0:
        console.print(f'\n[blue]All files already processed ({already_exists_count} files)[/blue]')
    elif interrupted:
        console.print(f'\n[yellow]Interrupted before completion[/yellow]')


if __name__ == '__main__':
    main()

