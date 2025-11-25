# snap-memory-export
Bulk export Snapchat memories with EXIF data

> **NOTE:** this was heavily vibe coded, but it exported my snap memories for me, will hopefully do the same for you.

## Quick Start

### 1. Get your data
- Export memories from https://accounts.snapchat.com/v2/download-my-data
- Unzip and find `memories_history.html`

### 2. Install
```bash
pip install -r requirements.txt
```

### 3. Download memories
```bash
python download.py -i path/to/memories_history.html -o output_folder --threads 5
```

<img width="1055" alt="Downloader" src="https://github.com/user-attachments/assets/be2d9375-39d2-4798-b643-75854908c289" />

### 4. Combine layers (for zipped files with overlays)
```bash
python combine_layers.py -i output_folder --threads 4
```

<img width="909" alt="Combiner" src="https://github.com/user-attachments/assets/0a80aa47-d1cb-4a00-be65-67332e0f41c4" />

### 5. Add EXIF data (date/GPS from filenames)
```bash
python add_exif.py -i working_folder
```

## Options

### download.py
- `-i, --input` (required): Path to `memories_history.html`
- `-o, --outdir` (default: `memories_downloads`): Output directory
- `--threads` (default: 5): Number of concurrent download threads
- `--delay` (default: 0.0): Delay between downloads in seconds
- `--dry-run`: Preview URLs without downloading

### combine_layers.py
- `-i, --input` (required): Input directory containing zip files
- `-o, --output` (default: same as input): Output directory for combined files
- `--threads` (default: 4): Number of concurrent worker threads
- `--pattern` (default: `*.zip`): File pattern to match
- `--remove-originals`: Delete zip files after successful processing

### add_exif.py
- `-i, --input` (required): Input directory containing media files
- `--pattern` (default: all files): File pattern to match
- `--dry-run`: Preview EXIF data without modifying files

## Requirements
- Python 3.7+
- ffmpeg (optional, for video metadata)


