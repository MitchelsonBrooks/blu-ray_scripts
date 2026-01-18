#!/usr/bin/env python3
"""
Interactive preview/outro remover for MKV files.
Removes the final chapter from MKV files (typically next-episode previews).
Uses statistical analysis to flag potential outliers that might be post-credits scenes.

Usage:
    python remove_previews.py                 # Current directory
    python remove_previews.py /path/to/folder # Specific directory

Requirements:
    - mkvmerge (from mkvtoolnix)
    - jq is NOT required (uses Python's json module)
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median


# Threshold below which a chapter is considered a "stub"
STUB_THRESHOLD_SECONDS = 1.0


@dataclass
class MkvFile:
    path: Path
    num_chapters: int
    final_chapter_duration: float  # in seconds (of last non-stub chapter)
    stub_chapter_indices: list[int]  # 0-indexed positions of stub chapters
    selected: bool = True
    is_outlier: bool = False
    deviation_percent: float = 0.0

    def duration_str(self) -> str:
        """Format duration as M:SS"""
        minutes = int(self.final_chapter_duration // 60)
        seconds = int(self.final_chapter_duration % 60)
        return f"{minutes}:{seconds:02d}"

    @property
    def has_stubs(self) -> bool:
        """Check if file has any stub chapters."""
        return len(self.stub_chapter_indices) > 0

    @property
    def effective_num_chapters(self) -> int:
        """Number of chapters after stub removal."""
        return self.num_chapters - len(self.stub_chapter_indices)


def run_mkvmerge_json(filepath: Path) -> dict | None:
    """Run mkvmerge -J and return parsed JSON."""
    try:
        result = subprocess.run(
            ['mkvmerge', '-J', str(filepath)],
            capture_output=True,
            text=True,
            check=True
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"  Warning: mkvmerge failed on {filepath.name}: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"  Warning: Failed to parse JSON for {filepath.name}: {e}")
        return None


def get_final_chapter_duration(info: dict) -> tuple[int, float] | None:
    """
    Extract number of chapters and final chapter duration from mkvmerge JSON.
    Returns (num_chapters, duration_in_seconds) or None if no chapters.
    """
    chapters = info.get('chapters', [])
    if not chapters:
        return None
    
    # chapters is a list of chapter editions, we want the first one
    edition = chapters[0]
    entries = edition.get('num_entries', 0)
    
    if entries < 2:
        # Need at least 2 chapters to remove the last one
        return None
    
    # Get the chapter list to calculate final chapter duration
    # We need to get the actual chapter timestamps
    # mkvmerge -J gives us chapter entries with timestamps
    
    # Actually, let's get more detailed chapter info
    # The 'chapters' array contains editions, each with 'num_entries'
    # but not the actual timestamps in the -J output
    
    # We need to use a different approach: get container duration
    # and the start time of the last chapter
    container_duration = info.get('container', {}).get('properties', {}).get('duration', 0)
    # Duration is in nanoseconds
    container_duration_sec = container_duration / 1_000_000_000
    
    # Unfortunately mkvmerge -J doesn't give chapter timestamps directly
    # We need to use mkvextract or parse differently
    # Let's use mkvinfo or a different approach
    
    return entries, container_duration_sec


def get_chapter_timestamps(filepath: Path) -> list[float] | None:
    """
    Get chapter start timestamps using mkvextract.
    Returns list of start times in seconds, or None on failure.
    """
    try:
        # Use mkvmerge --identify-for-mmg for chapter info, but that's deprecated
        # Better: use mkvextract chapters
        result = subprocess.run(
            ['mkvextract', str(filepath), 'chapters', '-s'],
            capture_output=True,
            text=True,
            check=True
        )
        
        # Parse simple chapter format: CHAPTER01=00:00:00.000
        timestamps = []
        for line in result.stdout.strip().split('\n'):
            if line.startswith('CHAPTER') and '=' in line and 'NAME' not in line:
                time_str = line.split('=')[1]
                # Parse HH:MM:SS.mmm
                parts = time_str.split(':')
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
                total_seconds = hours * 3600 + minutes * 60 + seconds
                timestamps.append(total_seconds)
        
        return timestamps if timestamps else None
        
    except subprocess.CalledProcessError:
        return None
    except (ValueError, IndexError):
        return None


def get_container_duration(filepath: Path) -> float | None:
    """Get total duration of the file in seconds."""
    info = run_mkvmerge_json(filepath)
    if not info:
        return None
    
    duration_ns = info.get('container', {}).get('properties', {}).get('duration', 0)
    return duration_ns / 1_000_000_000


def get_all_chapter_durations(timestamps: list[float], container_duration: float) -> list[float]:
    """
    Calculate duration of each chapter.
    Returns list of durations in seconds, same length as timestamps.
    """
    durations = []
    for i, start in enumerate(timestamps):
        if i + 1 < len(timestamps):
            # Duration is gap to next chapter
            duration = timestamps[i + 1] - start
        else:
            # Last chapter: duration to end of file
            duration = container_duration - start
        durations.append(duration)
    return durations


def find_stub_chapters(timestamps: list[float], container_duration: float) -> list[int]:
    """
    Find indices of stub chapters (duration < STUB_THRESHOLD_SECONDS).
    Returns 0-indexed list of stub positions.
    """
    durations = get_all_chapter_durations(timestamps, container_duration)
    stubs = []
    for i, dur in enumerate(durations):
        if dur < STUB_THRESHOLD_SECONDS:
            stubs.append(i)
    return stubs


def extract_chapters_xml(filepath: Path) -> str | None:
    """
    Extract chapters from MKV file as XML string.
    Returns XML string or None on failure.
    """
    try:
        result = subprocess.run(
            ['mkvextract', str(filepath), 'chapters', '-'],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout if result.stdout.strip() else None
    except subprocess.CalledProcessError:
        return None


def remove_chapters_from_xml(xml_content: str, indices_to_remove: list[int]) -> str:
    """
    Remove specific chapter entries from XML by index.
    indices_to_remove should be 0-indexed.
    Returns modified XML string.
    """
    import re
    
    # Find all ChapterAtom elements
    # Pattern matches <ChapterAtom>...</ChapterAtom> including nested content
    pattern = r'<ChapterAtom>.*?</ChapterAtom>'
    atoms = list(re.finditer(pattern, xml_content, re.DOTALL))
    
    if not atoms:
        return xml_content
    
    # Build new XML by excluding specified indices
    result = xml_content
    # Process in reverse order to maintain correct positions
    for i in sorted(indices_to_remove, reverse=True):
        if 0 <= i < len(atoms):
            match = atoms[i]
            # Remove the atom and any trailing whitespace/newline
            start = match.start()
            end = match.end()
            # Also remove trailing newline if present
            if end < len(result) and result[end] == '\n':
                end += 1
            result = result[:start] + result[end:]
    
    return result


def cleanup_stub_chapters(filepath: Path, stub_indices: list[int]) -> bool:
    """
    Remove stub chapter markers from a file.
    Returns True on success.
    """
    if not stub_indices:
        return True
    
    # Extract current chapters as XML
    xml_content = extract_chapters_xml(filepath)
    if not xml_content:
        print(f"    Warning: Could not extract chapters from {filepath.name}")
        return False
    
    # Remove stub entries from XML
    cleaned_xml = remove_chapters_from_xml(xml_content, stub_indices)
    
    # Write cleaned XML to temp file
    directory = filepath.parent
    xml_path = directory / f"_temp_chapters_{filepath.stem}.xml"
    temp_output = directory / f"_temp_cleaned_{filepath.name}"
    
    try:
        xml_path.write_text(cleaned_xml)
        
        # Remux file with cleaned chapters
        # --no-chapters prevents copying chapters from source file
        result = subprocess.run(
            [
                'mkvmerge',
                '-o', str(temp_output),
                '--no-chapters',
                str(filepath),
                '--chapters', str(xml_path)
            ],
            capture_output=True,
            text=True,
            check=True
        )
        
        # Replace original with cleaned version
        filepath.unlink()
        temp_output.rename(filepath)
        
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"    Error during chapter cleanup: {e}")
        print(f"    stderr: {e.stderr}")
        return False
    finally:
        # Clean up temp files
        if xml_path.exists():
            xml_path.unlink()
        if temp_output.exists():
            temp_output.unlink()


def analyze_file(filepath: Path) -> MkvFile | None:
    """Analyze a single MKV file and return MkvFile or None if unsuitable."""
    timestamps = get_chapter_timestamps(filepath)
    if not timestamps or len(timestamps) < 2:
        return None
    
    duration = get_container_duration(filepath)
    if not duration:
        return None
    
    # Find stub chapters
    stub_indices = find_stub_chapters(timestamps, duration)
    
    # Calculate chapter durations
    chapter_durations = get_all_chapter_durations(timestamps, duration)
    
    # Find the last non-stub chapter's duration
    # We need at least one non-stub chapter to remove
    non_stub_indices = [i for i in range(len(timestamps)) if i not in stub_indices]
    
    if len(non_stub_indices) < 2:
        # Need at least 2 non-stub chapters to remove the last one
        return None
    
    # The last non-stub chapter is what we'd remove
    last_non_stub_idx = non_stub_indices[-1]
    final_chapter_duration = chapter_durations[last_non_stub_idx]
    
    # Sanity check
    if final_chapter_duration <= 0:
        return None
    
    return MkvFile(
        path=filepath,
        num_chapters=len(timestamps),
        final_chapter_duration=final_chapter_duration,
        stub_chapter_indices=stub_indices
    )


def scan_directory(directory: Path) -> list[MkvFile]:
    """Scan directory for MKV files and analyze them."""
    files = []
    mkv_paths = sorted(directory.glob('*.mkv'))
    
    if not mkv_paths:
        return files
    
    print(f"Scanning {len(mkv_paths)} MKV files...")
    
    for filepath in mkv_paths:
        mkv = analyze_file(filepath)
        if mkv:
            files.append(mkv)
        else:
            print(f"  Skipping {filepath.name} (no chapters or < 2 chapters)")
    
    return files


def mark_outliers(files: list[MkvFile], threshold_percent: float = 10.0) -> float:
    """
    Mark outliers based on deviation from median.
    Returns the median duration.
    """
    if not files:
        return 0.0
    
    durations = [f.final_chapter_duration for f in files]
    med = median(durations)
    
    for f in files:
        if med > 0:
            deviation = ((f.final_chapter_duration - med) / med) * 100
            f.deviation_percent = deviation
            f.is_outlier = abs(deviation) > threshold_percent
            # Outliers are deselected by default
            if f.is_outlier:
                f.selected = False
    
    return med


def display_files(files: list[MkvFile], median_duration: float):
    """Display the file list with status indicators."""
    print()
    print(f"{'='*60}")
    print(f"Scanned {len(files)} files | Median final chapter: {int(median_duration//60)}:{int(median_duration%60):02d}")
    print(f"{'='*60}")
    print()
    
    for i, f in enumerate(files):
        # Selection indicator
        if f.selected:
            sel = "[x]"
        else:
            sel = "[ ]"
        
        # Outlier indicator
        if f.is_outlier:
            flag = "[!]"
            deviation = f"({f.deviation_percent:+.0f}% OUTLIER)"
        else:
            flag = "   "
            deviation = ""
        
        print(f"  {i+1:2}. {sel} {flag} {f.path.name}")
        
        # Build info line
        info_parts = [f"Final chapter: {f.duration_str()}"]
        if deviation:
            info_parts.append(deviation)
        if f.has_stubs:
            info_parts.append(f"[{len(f.stub_chapter_indices)} stub(s)]")
        
        print(f"          {' '.join(info_parts)}")
    
    print()


def interactive_selection(files: list[MkvFile], median_duration: float, directory: Path):
    """Interactive loop for selecting files to process."""
    while True:
        display_files(files, median_duration)
        
        selected_count = sum(1 for f in files if f.selected)
        stubs_count = sum(1 for f in files if f.has_stubs)
        print(f"Selected: {selected_count}/{len(files)}")
        if stubs_count > 0:
            print(f"Files with stub chapters: {stubs_count}")
        print()
        print("Commands:")
        print("  [a]ll     - Select all files")
        print("  [n]one    - Deselect all files")
        print("  [o]utliers- Select only outliers")
        print("  [r]egular - Select only non-outliers")
        print("  [i]nvert  - Invert current selections")
        print("  [1-99]    - Toggle specific file by number")
        print("  [c]leanup - Cleanup mode (remove stub chapters only)")
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
        elif cmd == 'o':
            for f in files:
                f.selected = f.is_outlier
        elif cmd == 'r':
            for f in files:
                f.selected = not f.is_outlier
        elif cmd == 'i':
            for f in files:
                f.selected = not f.selected
        elif cmd == 'c':
            cleanup_mode(directory)
            # Re-scan after cleanup in case files changed
            print("\nRe-scanning files after cleanup...")
            files.clear()
            files.extend(scan_directory(directory))
            if files:
                mark_outliers(files)
        elif cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(files):
                files[idx].selected = not files[idx].selected
            else:
                print(f"Invalid number. Enter 1-{len(files)}")
        else:
            print("Unknown command")


def process_file(mkv: MkvFile) -> bool:
    """
    Process a single file: cleanup stubs, split at last chapter, remove the last segment, rename.
    Returns True on success.
    """
    filepath = mkv.path
    directory = filepath.parent
    
    # Step 0: Clean up stub chapters if present
    if mkv.has_stubs:
        print(f"  Cleaning up {len(mkv.stub_chapter_indices)} stub chapter(s)...")
        if not cleanup_stub_chapters(filepath, mkv.stub_chapter_indices):
            print(f"    Failed to clean up stub chapters")
            return False
    
    # After cleanup, effective chapter count is what we work with
    effective_chapters = mkv.effective_num_chapters
    
    # Step 1: Split at the last chapter
    split_name = f"split_{filepath.name}"
    split_path = directory / split_name
    
    print(f"  Splitting {filepath.name}...")
    
    try:
        result = subprocess.run(
            [
                'mkvmerge',
                '-o', str(split_path),
                '--split', f'chapters:{effective_chapters}',
                str(filepath)
            ],
            capture_output=True,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"    Error splitting: {e}")
        print(f"    stderr: {e.stderr}")
        return False
    
    # Step 2: Find and remove the -002 file (last chapter)
    # mkvmerge creates: split_filename-001.mkv, split_filename-002.mkv
    stem = split_path.stem  # split_filename
    part1 = directory / f"{stem}-001.mkv"
    part2 = directory / f"{stem}-002.mkv"
    
    if not part1.exists():
        print(f"    Error: Expected file not found: {part1.name}")
        return False
    
    if part2.exists():
        print(f"  Removing {part2.name} (final chapter)...")
        part2.unlink()
    
    # Step 3: Remove the original file
    print(f"  Removing original {filepath.name}...")
    filepath.unlink()
    
    # Step 4: Rename part1 to original name (removing split_ and -001)
    final_name = filepath.name  # Original name without split_ prefix
    final_path = directory / final_name
    
    print(f"  Renaming {part1.name} -> {final_name}...")
    part1.rename(final_path)
    
    return True


def scan_for_stubs(directory: Path) -> list[MkvFile]:
    """Scan directory for MKV files with stub chapters."""
    files = []
    mkv_paths = sorted(directory.glob('*.mkv'))
    
    if not mkv_paths:
        return files
    
    print(f"Scanning {len(mkv_paths)} MKV files for stub chapters...")
    
    for filepath in mkv_paths:
        timestamps = get_chapter_timestamps(filepath)
        if not timestamps:
            continue
        
        duration = get_container_duration(filepath)
        if not duration:
            continue
        
        stub_indices = find_stub_chapters(timestamps, duration)
        if stub_indices:
            # Create a minimal MkvFile for cleanup purposes
            chapter_durations = get_all_chapter_durations(timestamps, duration)
            non_stub_indices = [i for i in range(len(timestamps)) if i not in stub_indices]
            
            if non_stub_indices:
                last_non_stub_idx = non_stub_indices[-1]
                final_dur = chapter_durations[last_non_stub_idx]
            else:
                final_dur = 0.0
            
            mkv = MkvFile(
                path=filepath,
                num_chapters=len(timestamps),
                final_chapter_duration=final_dur,
                stub_chapter_indices=stub_indices,
                selected=True
            )
            files.append(mkv)
    
    return files


def display_cleanup_files(files: list[MkvFile]):
    """Display files with stub chapters for cleanup mode."""
    print()
    print(f"{'='*60}")
    print(f"Found {len(files)} files with stub chapters")
    print(f"{'='*60}")
    print()
    
    for i, f in enumerate(files):
        sel = "[x]" if f.selected else "[ ]"
        print(f"  {i+1:2}. {sel} {f.path.name}")
        print(f"          {len(f.stub_chapter_indices)} stub chapter(s) at indices: {f.stub_chapter_indices}")
    
    print()


def cleanup_mode(directory: Path):
    """Interactive cleanup mode for removing stub chapters only."""
    files = scan_for_stubs(directory)
    
    if not files:
        print("No files with stub chapters found.")
        return
    
    while True:
        display_cleanup_files(files)
        
        selected_count = sum(1 for f in files if f.selected)
        print(f"Selected: {selected_count}/{len(files)}")
        print()
        print("Commands:")
        print("  [a]ll     - Select all files")
        print("  [n]one    - Deselect all files")
        print("  [i]nvert  - Invert current selections")
        print("  [1-99]    - Toggle specific file by number")
        print("  [g]o      - Clean up selected files")
        print("  [b]ack    - Return to main menu")
        print()
        
        try:
            cmd = input("cleanup> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        
        if cmd == 'b':
            return
        elif cmd == 'g':
            if selected_count == 0:
                print("No files selected!")
                continue
            process_cleanup(files)
            return
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


def process_cleanup(files: list[MkvFile]):
    """Process cleanup for all selected files."""
    selected = [f for f in files if f.selected]
    
    print()
    print(f"{'='*60}")
    print(f"Cleaning up {len(selected)} files...")
    print(f"{'='*60}")
    print()
    
    success_count = 0
    for i, mkv in enumerate(selected):
        print(f"[{i + 1}/{len(selected)}] {mkv.path.name}")
        print(f"  Removing {len(mkv.stub_chapter_indices)} stub chapter(s)...")
        if cleanup_stub_chapters(mkv.path, mkv.stub_chapter_indices):
            success_count += 1
            print(f"  Done!")
        else:
            print(f"  FAILED")
        print()
    
    print(f"{'='*60}")
    print(f"Completed: {success_count}/{len(selected)} files cleaned successfully")
    print(f"{'='*60}")


def process_files(files: list[MkvFile]):
    """Process all selected files."""
    selected = [f for f in files if f.selected]
    
    print()
    print(f"{'='*60}")
    print(f"Processing {len(selected)} files...")
    print(f"{'='*60}")
    print()
    
    success_count = 0
    for mkv in selected:
        print(f"[{success_count + 1}/{len(selected)}] {mkv.path.name}")
        if process_file(mkv):
            success_count += 1
            print(f"  Done!")
        else:
            print(f"  FAILED")
        print()
    
    print(f"{'='*60}")
    print(f"Completed: {success_count}/{len(selected)} files processed successfully")
    print(f"{'='*60}")


def check_dependencies():
    """Verify required tools are installed."""
    for tool in ['mkvmerge', 'mkvextract']:
        try:
            subprocess.run([tool, '--version'], capture_output=True, check=True)
        except FileNotFoundError:
            print(f"Error: '{tool}' not found. Please install mkvtoolnix.")
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
    
    # Scan and analyze
    files = scan_directory(directory)
    
    if not files:
        print("No suitable MKV files found (need files with 2+ chapters)")
        sys.exit(0)
    
    # Mark outliers
    median_duration = mark_outliers(files)
    
    # Interactive selection
    if interactive_selection(files, median_duration, directory):
        process_files(files)
    else:
        print("Cancelled.")


if __name__ == '__main__':
    main()
