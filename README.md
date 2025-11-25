# snap-memory-export
Bulk export Snapchat memories with EXIF data

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
python download_memories.py -i path/to/memories_history.html -o output_folder --threads 5
```

<img width="1055" alt="Downloader" src="https://github.com/user-attachments/assets/be2d9375-39d2-4798-b643-75854908c289" />

### 4. Combine layers (for zipped files with overlays)
```bash
python combine.py -i output_folder --threads 4
```

<img width="909" alt="Combiner" src="https://github.com/user-attachments/assets/0a80aa47-d1cb-4a00-be65-67332e0f41c4" />

### 5. Add EXIF data (date/GPS from filenames)
```bash
python add_exif.py -i output_folder
```

## Options

**download_memories.py**
- `--threads`: Concurrent downloads (default: 5)
- `--dry-run`: Preview without downloading

**combine.py**
- `--threads`: Concurrent processing (default: 4)
- `--remove-originals`: Delete zip files after combining

**add_exif.py**
- `--dry-run`: Preview EXIF data without modifying files

## Requirements
- Python 3.7+
- ffmpeg (optional, for video metadata)


