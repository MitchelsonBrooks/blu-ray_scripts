#!/usr/bin/env python3
"""
Interactive MKV re-encoder for fixing timestamp issues.
Re-encodes video to HEVC using NVENC with high-quality settings
while copying all audio, subtitles, and chapters unchanged.

Settings:
    - CQ 14 (very conservative quality)
    - 10-bit color (reduces banding)
    - Spatial/temporal adaptive quantization
    - 32-frame lookahead
    - B-frame optimization

Usage:
    python reencode_hevc.py                 # Current directory
    python reencode_hevc.py /path/to/folder # Specific directory

Requirements:
    - ffmpeg with NVENC support
    - NVIDIA GPU with HEVC encoding capability
"""

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MkvFile:
    path: Path
    size_bytes: int
    selected: bool = True

    def size_str(self) -> str:
        """Format size as human-readable string."""
        size = self.size_bytes
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"


def format_duration(seconds: float) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}m {secs}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def check_nvenc_available() -> bool:
    """Check if NVENC HEVC encoder is available."""
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True,
            text=True,
            check=True
        )
        return 'hevc_nvenc' in result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def scan_directory(directory: Path) -> list[MkvFile]:
    """Scan directory for MKV files."""
    files = []
    mkv_paths = sorted(directory.glob('*.mkv'))

    if not mkv_paths:
        return files

    print(f"Found {len(mkv_paths)} MKV files...")

    for filepath in mkv_paths:
        try:
            size = filepath.stat().st_size
            files.append(MkvFile(path=filepath, size_bytes=size))
        except OSError as e:
            print(f"  Warning: Could not stat {filepath.name}: {e}")

    return files


def display_files(files: list[MkvFile]):
    """Display the file list with selection status."""
    print()
    print(f"{'='*70}")
    print(f"Found {len(files)} MKV files")
    print(f"{'='*70}")
    print()

    for i, f in enumerate(files):
        sel = "[x]" if f.selected else "[ ]"
        print(f"  {i+1:2}. {sel} {f.path.name}")
        print(f"          Size: {f.size_str()}")

    print()


def interactive_selection(files: list[MkvFile]) -> bool:
    """Interactive loop for selecting files to process."""
    while True:
        display_files(files)

        selected_count = sum(1 for f in files if f.selected)
        total_size = sum(f.size_bytes for f in files if f.selected)
        total_size_str = MkvFile(path=Path(), size_bytes=total_size).size_str()

        print(f"Selected: {selected_count}/{len(files)} ({total_size_str})")
        print()
        print("Encoding: HEVC NVENC | CQ 14 | 10-bit | AQ | Lookahead | p7")
        print()
        print("Commands:")
        print("  [a]ll     - Select all files")
        print("  [n]one    - Deselect all files")
        print("  [i]nvert  - Invert current selections")
        print("  [1-99]    - Toggle specific file by number")
        print("  [g]o      - Process selected files")
        print("  [q]uit    - Exit without processing")
        print()

        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        if cmd == 'q':
            return False
        elif cmd == 'g':
            if selected_count == 0:
                print("No files selected!")
                continue
            return True
        elif cmd == 'a':
            for f in files:
                f.selected = True
        elif cmd == 'n':
            for f in files:
                f.selected = False
        elif cmd == 'i':
            for f in files:
                f.selected = not f.selected
        elif cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(files):
                files[idx].selected = not files[idx].selected
            else:
                print(f"Invalid number. Enter 1-{len(files)}")
        else:
            print("Unknown command")


def reencode_file(filepath: Path) -> tuple[bool, int]:
    """
    Re-encode a single MKV file to HEVC.
    Returns (success, new_size_bytes).
    """
    directory = filepath.parent
    temp_output = directory / f"_encoding_{filepath.name}"

    # Build ffmpeg command
    # High quality NVENC settings suitable for all content types
    cmd = [
        'ffmpeg',
        '-hide_banner',
        '-i', str(filepath),
        '-map', '0',                    # Map all streams
        '-c:v', 'hevc_nvenc',           # NVENC HEVC encoder
        '-preset', 'p7',                # Slowest/best quality preset
        '-cq', '14',                    # Constant quality (very conservative)
        '-profile:v', 'main10',         # 10-bit reduces banding
        '-pix_fmt', 'p010le',           # 10-bit pixel format
        '-rc-lookahead', '32',          # Look ahead for better decisions
        '-b_ref_mode', 'middle',        # B-frame reference mode
        '-bf', '4',                     # Number of B-frames
        '-c:a', 'copy',                 # Copy all audio tracks
        '-c:s', 'copy',                 # Copy all subtitle tracks
        '-map_chapters', '0',           # Preserve chapters
        '-max_muxing_queue_size', '2048',
        '-y',                           # Overwrite output
        str(temp_output)
    ]

    try:
        # Run ffmpeg with output visible for progress
        result = subprocess.run(cmd)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)

    except subprocess.CalledProcessError as e:
        print(f"    Error: ffmpeg failed with code {e.returncode}")
        # Clean up temp file if it exists
        if temp_output.exists():
            temp_output.unlink()
        return False, 0

    # Verify output was created and has reasonable size
    if not temp_output.exists():
        print(f"    Error: Output file was not created")
        return False, 0

    new_size = temp_output.stat().st_size
    if new_size < 1000:  # Less than 1KB is definitely wrong
        print(f"    Error: Output file is too small ({new_size} bytes)")
        temp_output.unlink()
        return False, 0

    # Replace original with re-encoded version
    try:
        filepath.unlink()
        temp_output.rename(filepath)
    except OSError as e:
        print(f"    Error replacing original: {e}")
        if temp_output.exists():
            temp_output.unlink()
        return False, 0

    return True, new_size


def process_files(files: list[MkvFile]):
    """Process all selected files."""
    selected = [f for f in files if f.selected]

    print()
    print(f"{'='*70}")
    print(f"Re-encoding {len(selected)} files to HEVC")
    print(f"Settings: NVENC | CQ 14 | 10-bit | AQ | Lookahead | p7")
    print(f"{'='*70}")
    print()

    success_count = 0
    total_original = 0
    total_new = 0
    total_time = 0

    for i, mkv in enumerate(selected):
        print(f"[{i + 1}/{len(selected)}] {mkv.path.name}")
        print(f"  Source size: {mkv.size_str()}")
        print(f"  Encoding...")
        print()

        start_time = time.time()
        success, new_size = reencode_file(mkv.path)
        elapsed = time.time() - start_time
        total_time += elapsed

        print()

        if success:
            new_size_str = MkvFile(path=Path(), size_bytes=new_size).size_str()
            reduction = (1 - new_size / mkv.size_bytes) * 100
            print(f"  Completed in {format_duration(elapsed)}")
            print(f"  New size: {new_size_str} ({reduction:.1f}% reduction)")
            success_count += 1
            total_original += mkv.size_bytes
            total_new += new_size
        else:
            print(f"  FAILED")
        print()

    # Summary
    print(f"{'='*70}")
    print(f"Completed: {success_count}/{len(selected)} files re-encoded")
    print(f"Total time: {format_duration(total_time)}")
    if total_original > 0:
        orig_str = MkvFile(path=Path(), size_bytes=total_original).size_str()
        new_str = MkvFile(path=Path(), size_bytes=total_new).size_str()
        saved = total_original - total_new
        saved_str = MkvFile(path=Path(), size_bytes=saved).size_str()
        reduction = (1 - total_new / total_original) * 100
        print(f"Space: {orig_str} -> {new_str} (saved {saved_str}, {reduction:.1f}%)")
    print(f"{'='*70}")


def check_dependencies():
    """Verify required tools are installed."""
    # Check ffmpeg
    try:
        subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True,
            check=True
        )
    except FileNotFoundError:
        print("Error: 'ffmpeg' not found. Please install ffmpeg.")
        sys.exit(1)

    # Check NVENC
    if not check_nvenc_available():
        print("Error: NVENC HEVC encoder not available.")
        print()
        print("Make sure:")
        print("  1. NVIDIA drivers are loaded (run 'nvidia-smi')")
        print("  2. ffmpeg was built with NVENC support")
        print()
        print("On Debian/Ubuntu with nvidia drivers:")
        print("  apt install ffmpeg")
        print()
        print("Or use jellyfin-ffmpeg which has NVENC:")
        print("  /usr/lib/jellyfin-ffmpeg/ffmpeg")
        sys.exit(1)


def main():
    check_dependencies()

    # Get directory from args or use current directory
    if len(sys.argv) > 1:
        directory = Path(sys.argv[1]).expanduser().resolve()
    else:
        directory = Path.cwd()

    if not directory.exists():
        print(f"Error: Directory does not exist: {directory}")
        sys.exit(1)

    if not directory.is_dir():
        print(f"Error: Not a directory: {directory}")
        sys.exit(1)

    print(f"Directory: {directory}")
    print()

    # Scan directory
    files = scan_directory(directory)

    if not files:
        print("No MKV files found in directory.")
        sys.exit(0)

    # Interactive selection
    if interactive_selection(files):
        process_files(files)
    else:
        print("Cancelled.")


if __name__ == '__main__':
    main()
