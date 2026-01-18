#!/usr/bin/env python3
"""
Interactive MKV remuxer to fix timestamp discontinuities.
Remuxes MKV files with mkvmerge to regenerate clean timestamps,
fixing HLS streaming issues (freezing, seek failures) in Jellyfin.

This is a lossless operation - video, audio, subtitles, and chapters
are copied bit-for-bit. Only container timestamps are regenerated.

Usage:
    python fix_timestamps.py                 # Current directory
    python fix_timestamps.py /path/to/folder # Specific directory

Requirements:
    - mkvmerge (from mkvtoolnix)
"""

import os
import subprocess
import sys
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


def remux_file(filepath: Path) -> bool:
    """
    Remux a single MKV file to fix timestamps.
    Returns True on success.
    """
    directory = filepath.parent
    temp_output = directory / f"_remuxing_{filepath.name}"

    try:
        # Remux with mkvmerge - this regenerates timestamps
        result = subprocess.run(
            ['mkvmerge', '-o', str(temp_output), str(filepath)],
            capture_output=True,
            text=True,
            check=True
        )

        # Check for warnings (mkvmerge returns 1 for warnings, 2 for errors)
        # But check=True only raises on non-zero, so we handle warnings separately

    except subprocess.CalledProcessError as e:
        # Return code 1 = warnings (still succeeded)
        # Return code 2 = errors (failed)
        if e.returncode == 1:
            # Warnings are okay, file was still created
            pass
        else:
            print(f"    Error: mkvmerge failed")
            if e.stderr:
                # Print first few lines of error
                for line in e.stderr.strip().split('\n')[:5]:
                    print(f"    {line}")
            # Clean up temp file if it exists
            if temp_output.exists():
                temp_output.unlink()
            return False

    # Verify output was created
    if not temp_output.exists():
        print(f"    Error: Output file was not created")
        return False

    # Replace original with remuxed version
    try:
        filepath.unlink()
        temp_output.rename(filepath)
    except OSError as e:
        print(f"    Error replacing original: {e}")
        # Try to clean up
        if temp_output.exists():
            temp_output.unlink()
        return False

    return True


def process_files(files: list[MkvFile]):
    """Process all selected files."""
    selected = [f for f in files if f.selected]

    print()
    print(f"{'='*70}")
    print(f"Remuxing {len(selected)} files to fix timestamps...")
    print(f"{'='*70}")
    print()

    success_count = 0
    for i, mkv in enumerate(selected):
        print(f"[{i + 1}/{len(selected)}] {mkv.path.name}")
        print(f"  Remuxing ({mkv.size_str()})...")

        if remux_file(mkv.path):
            success_count += 1
            print(f"  Done!")
        else:
            print(f"  FAILED")
        print()

    print(f"{'='*70}")
    print(f"Completed: {success_count}/{len(selected)} files remuxed successfully")
    print(f"{'='*70}")


def check_dependencies():
    """Verify required tools are installed."""
    try:
        result = subprocess.run(
            ['mkvmerge', '--version'],
            capture_output=True,
            check=True
        )
    except FileNotFoundError:
        print("Error: 'mkvmerge' not found. Please install mkvtoolnix.")
        print()
        print("  Debian/Ubuntu: sudo apt install mkvtoolnix")
        print("  Fedora:        sudo dnf install mkvtoolnix")
        print("  Arch:          sudo pacman -S mkvtoolnix-cli")
        print("  macOS:         brew install mkvtoolnix")
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
