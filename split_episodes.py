#!/usr/bin/env python3
"""
Interactive episode splitter for MKV files.
Analyzes chapter structure and splits multi-episode MKV files into individual episodes.

Usage:
    python split_episodes.py                 # Current directory
    python split_episodes.py /path/to/folder # Specific directory

Requirements:
    - mkvmerge (from mkvtoolnix)
    - mkvextract (from mkvtoolnix)
    - mkvpropedit (from mkvtoolnix)
"""

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median


@dataclass
class Chapter:
    index: int
    start_time: float  # seconds
    end_time: float    # seconds
    name: str

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time

    def time_str(self, seconds: float) -> str:
        """Format time as H:MM:SS or MM:SS"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    @property
    def start_str(self) -> str:
        return self.time_str(self.start_time)

    @property
    def duration_str(self) -> str:
        return self.time_str(self.duration)


@dataclass
class Episode:
    number: int
    chapters: list[Chapter]
    
    @property
    def start_time(self) -> float:
        return self.chapters[0].start_time if self.chapters else 0
    
    @property
    def end_time(self) -> float:
        return self.chapters[-1].end_time if self.chapters else 0
    
    @property
    def duration(self) -> float:
        return self.end_time - self.start_time
    
    @property
    def chapter_range(self) -> str:
        if not self.chapters:
            return "N/A"
        first = self.chapters[0].index
        last = self.chapters[-1].index
        return f"Ch {first}-{last}"
    
    @property
    def chapter_count(self) -> int:
        return len(self.chapters)

    def time_str(self, seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    @property
    def start_str(self) -> str:
        return self.time_str(self.start_time)

    @property
    def end_str(self) -> str:
        return self.time_str(self.end_time)

    @property
    def duration_str(self) -> str:
        return self.time_str(self.duration)


@dataclass
class MkvFile:
    path: Path
    chapters: list[Chapter]
    total_duration: float
    episodes: list[Episode] = field(default_factory=list)
    skip: bool = False
    
    @property
    def num_chapters(self) -> int:
        return len(self.chapters)
    
    def duration_str(self) -> str:
        hours = int(self.total_duration // 3600)
        minutes = int((self.total_duration % 3600) // 60)
        secs = int(self.total_duration % 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"


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


def get_chapter_timestamps(filepath: Path) -> list[tuple[float, str]] | None:
    """
    Get chapter start timestamps and names using mkvextract.
    Returns list of (start_time_seconds, name) or None on failure.
    """
    try:
        result = subprocess.run(
            ['mkvextract', str(filepath), 'chapters', '-s'],
            capture_output=True,
            text=True,
            check=True
        )
        
        timestamps = []
        names = {}
        
        for line in result.stdout.strip().split('\n'):
            if not line or '=' not in line:
                continue
                
            key, value = line.split('=', 1)
            
            if 'NAME' in key:
                # Extract chapter number from key like CHAPTER01NAME
                ch_num = ''.join(filter(str.isdigit, key.replace('NAME', '')))
                if ch_num:
                    names[int(ch_num)] = value
            elif key.startswith('CHAPTER'):
                # Extract chapter number and timestamp
                ch_num = ''.join(filter(str.isdigit, key))
                if ch_num:
                    time_str = value
                    parts = time_str.split(':')
                    hours = int(parts[0])
                    minutes = int(parts[1])
                    seconds = float(parts[2])
                    total_seconds = hours * 3600 + minutes * 60 + seconds
                    timestamps.append((int(ch_num), total_seconds))
        
        # Sort by chapter number and combine with names
        timestamps.sort(key=lambda x: x[0])
        result_list = []
        for ch_num, ts in timestamps:
            name = names.get(ch_num, f"Chapter {ch_num:02d}")
            result_list.append((ts, name))
        
        return result_list if result_list else None
        
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


def analyze_file(filepath: Path) -> MkvFile | None:
    """Analyze a single MKV file and return MkvFile or None if unsuitable."""
    timestamps = get_chapter_timestamps(filepath)
    if not timestamps or len(timestamps) < 2:
        return None
    
    duration = get_container_duration(filepath)
    if not duration:
        return None
    
    # Build chapter list with durations
    chapters = []
    for i, (start_time, name) in enumerate(timestamps):
        # End time is start of next chapter, or total duration for last chapter
        if i + 1 < len(timestamps):
            end_time = timestamps[i + 1][0]
        else:
            end_time = duration
        
        chapters.append(Chapter(
            index=i + 1,
            start_time=start_time,
            end_time=end_time,
            name=name
        ))
    
    return MkvFile(
        path=filepath,
        chapters=chapters,
        total_duration=duration
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


def auto_detect_episodes(mkv: MkvFile) -> list[Episode] | None:
    """
    Attempt to auto-detect episode boundaries for a single file.
    
    Strategy:
    1. Estimate episode count based on typical durations (20-28 min)
    2. Try duration-based splitting for each candidate count
    3. Pick the split with most consistent episode lengths
    """
    total_duration = mkv.total_duration
    
    # Estimate likely episode counts based on common episode durations
    candidates = []
    
    for target_minutes in [20, 22, 24, 26, 28, 30]:
        target_seconds = target_minutes * 60
        estimated_eps = round(total_duration / target_seconds)
        
        if estimated_eps < 1:
            continue
        
        # Avoid duplicates
        if any(c['num_episodes'] == estimated_eps for c in candidates):
            continue
        
        # Try this episode count
        episodes = split_by_duration_target(mkv, target_seconds, start_episode=1)
        
        if not episodes:
            continue
        
        # Calculate consistency score (coefficient of variation)
        durations = [ep.duration for ep in episodes]
        if len(durations) < 2:
            continue
        
        avg = sum(durations) / len(durations)
        variance = sum((d - avg) ** 2 for d in durations) / len(durations)
        std_dev = variance ** 0.5
        cv = std_dev / avg if avg > 0 else float('inf')
        
        # Also check if average is in reasonable range
        avg_minutes = avg / 60
        if not (18 <= avg_minutes <= 35):
            continue
        
        candidates.append({
            'num_episodes': len(episodes),
            'target_duration': target_seconds,
            'avg_duration': avg,
            'cv': cv,
            'episodes': episodes
        })
    
    if not candidates:
        return None
    
    # Sort by coefficient of variation (lower = more consistent)
    candidates.sort(key=lambda x: x['cv'])
    
    return candidates[0]['episodes']


def auto_detect_all(files: list[MkvFile]) -> bool:
    """
    Auto-detect episodes across all files using duration-based approach.
    
    Strategy:
    1. Calculate total runtime
    2. Estimate total episode count based on typical durations
    3. Try duration-based splitting for different episode counts
    4. Pick the count with most consistent results
    """
    total_runtime = sum(f.total_duration for f in files)
    
    # Try different episode counts based on typical durations
    candidates = []
    
    for target_minutes in [20, 22, 24, 26, 28, 30]:
        target_seconds = target_minutes * 60
        estimated_total = round(total_runtime / target_seconds)
        
        if estimated_total < len(files):  # At least one episode per file
            continue
        
        # Avoid duplicates
        if any(c['total_episodes'] == estimated_total for c in candidates):
            continue
        
        # Try this count
        test_files = files.copy()
        episode_number = 1
        all_episodes = []
        
        for mkv in test_files:
            eps = split_by_duration_target(mkv, target_seconds, start_episode=episode_number)
            all_episodes.extend(eps)
            episode_number += len(eps)
        
        if not all_episodes:
            continue
        
        # Calculate consistency
        durations = [ep.duration for ep in all_episodes]
        if len(durations) < 2:
            continue
        
        avg = sum(durations) / len(durations)
        variance = sum((d - avg) ** 2 for d in durations) / len(durations)
        std_dev = variance ** 0.5
        cv = std_dev / avg if avg > 0 else float('inf')
        
        avg_minutes = avg / 60
        if not (18 <= avg_minutes <= 35):
            continue
        
        candidates.append({
            'total_episodes': len(all_episodes),
            'target_duration': target_seconds,
            'avg_duration': avg,
            'cv': cv
        })
    
    if not candidates:
        return False
    
    # Sort by coefficient of variation
    candidates.sort(key=lambda x: x['cv'])
    best = candidates[0]
    
    # Apply the best candidate
    target_duration = best['target_duration']
    episode_number = 1
    
    for mkv in files:
        mkv.episodes = split_by_duration_target(mkv, target_duration, start_episode=episode_number)
        episode_number += len(mkv.episodes)
    
    return True


def split_by_chapter_count(mkv: MkvFile, chapters_per_episode: int, start_episode: int = 1) -> list[Episode]:
    """Split chapters into episodes with fixed chapter count per episode."""
    episodes = []
    ep_num = start_episode
    
    for i in range(0, len(mkv.chapters), chapters_per_episode):
        ep_chapters = mkv.chapters[i:i + chapters_per_episode]
        if ep_chapters:
            episodes.append(Episode(number=ep_num, chapters=ep_chapters))
            ep_num += 1
    
    return episodes


def split_by_episode_count(mkv: MkvFile, num_episodes: int, start_episode: int = 1) -> list[Episode]:
    """Split chapters into a specific number of episodes (chapters divided as evenly as possible)."""
    if num_episodes <= 0:
        return []
    
    chapters_per_episode = len(mkv.chapters) // num_episodes
    remainder = len(mkv.chapters) % num_episodes
    
    episodes = []
    ep_num = start_episode
    idx = 0
    
    for i in range(num_episodes):
        # Distribute remainder chapters to first episodes
        count = chapters_per_episode + (1 if i < remainder else 0)
        ep_chapters = mkv.chapters[idx:idx + count]
        if ep_chapters:
            episodes.append(Episode(number=ep_num, chapters=ep_chapters))
            ep_num += 1
        idx += count
    
    return episodes


def split_by_duration_target(mkv: MkvFile, target_duration: float, tolerance: float = 0.20, start_episode: int = 1) -> list[Episode]:
    """
    Split chapters into episodes based on target duration.
    
    Walks through chapters accumulating duration. When accumulated time reaches
    the acceptable range (target +/- tolerance), decides whether to split based
    on which choice gets closer to the target.
    
    Handles outliers naturally:
    - Double episodes: No good break point near target -> stays as one long episode
    - Short episodes: Break point found early -> accepted if within tolerance
    - Finale padding: Won't split if remaining content is too short to be an episode
    """
    if not mkv.chapters:
        return []
    
    episodes = []
    ep_num = start_episode
    current_chapters = []
    accumulated = 0.0
    
    min_duration = target_duration * (1 - tolerance)
    max_overshoot = target_duration * (1 + tolerance * 2)  # Allow significant overshoot for double eps
    min_valid_episode = target_duration * 0.5  # Minimum duration to be considered a real episode
    
    i = 0
    while i < len(mkv.chapters):
        chapter = mkv.chapters[i]
        current_chapters.append(chapter)
        accumulated += chapter.duration
        
        is_last_chapter = (i == len(mkv.chapters) - 1)
        
        if is_last_chapter:
            # Must end here - final episode
            if current_chapters:
                episodes.append(Episode(number=ep_num, chapters=current_chapters.copy()))
            break
        
        # Calculate remaining duration after this chapter
        remaining_duration = mkv.total_duration - (current_chapters[0].start_time + accumulated)
        
        # Look ahead to next chapter
        next_chapter = mkv.chapters[i + 1]
        accumulated_with_next = accumulated + next_chapter.duration
        
        # Decision logic: should we split here?
        should_split = False
        
        if accumulated >= min_duration:
            # We've reached minimum acceptable duration
            
            # Don't split if remaining content would be too short to be a valid episode
            if remaining_duration < min_valid_episode:
                # Absorb remaining chapters into this episode
                i += 1
                continue
            
            # Calculate distances to target
            distance_if_split = abs(accumulated - target_duration)
            distance_if_continue = abs(accumulated_with_next - target_duration)
            
            # Split if:
            # 1. Splitting gets us closer (or equal) to target, OR
            # 2. Continuing would significantly overshoot
            if distance_if_split <= distance_if_continue:
                should_split = True
            elif accumulated_with_next > max_overshoot:
                should_split = True
        
        elif accumulated > max_overshoot:
            # We've overshot significantly without finding a good break
            # This is likely a double episode - accept it
            # But still check if remaining would be too short
            if remaining_duration >= min_valid_episode:
                should_split = True
        
        if should_split:
            episodes.append(Episode(number=ep_num, chapters=current_chapters.copy()))
            ep_num += 1
            current_chapters = []
            accumulated = 0.0
        
        i += 1
    
    return episodes


def split_all_by_duration(files: list[MkvFile], total_episodes: int) -> bool:
    """
    Split all files using duration-based detection.
    Returns True if successful.
    """
    # Calculate total runtime
    total_runtime = sum(f.total_duration for f in files)
    target_duration = total_runtime / total_episodes
    
    # Display what we're working with
    target_min = int(target_duration // 60)
    target_sec = int(target_duration % 60)
    print(f"Total runtime: {int(total_runtime // 3600)}:{int((total_runtime % 3600) // 60):02d}:{int(total_runtime % 60):02d}")
    print(f"Target episode duration: ~{target_min}:{target_sec:02d}")
    print()
    
    episode_number = 1
    
    for mkv in files:
        mkv.episodes = split_by_duration_target(mkv, target_duration, start_episode=episode_number)
        episode_number += len(mkv.episodes)
    
    # Verify we got close to expected count
    actual_count = sum(len(f.episodes) for f in files)
    if actual_count != total_episodes:
        print(f"Warning: Detected {actual_count} episodes, expected {total_episodes}")
        print(f"This may indicate unusual episode lengths or incorrect episode count.")
        print()
    
    return True


def prompt_show_info(directory: Path) -> tuple[str, int]:
    """Prompt user for show name and season number."""
    # Suggest show name from directory
    suggested_name = directory.name
    
    # Try to extract season number if directory looks like "Season X"
    suggested_season = 1
    parent_name = directory.name.lower()
    if 'season' in parent_name:
        parts = parent_name.split()
        for i, p in enumerate(parts):
            if p == 'season' and i + 1 < len(parts):
                try:
                    suggested_season = int(parts[i + 1])
                    # Use parent directory for show name
                    suggested_name = directory.parent.name
                except ValueError:
                    pass
    
    print("Output naming:")
    show_name = input(f"  Show name [{suggested_name}]: ").strip()
    if not show_name:
        show_name = suggested_name
    
    season_input = input(f"  Season number [{suggested_season}]: ").strip()
    if season_input:
        try:
            season = int(season_input)
        except ValueError:
            print(f"  Invalid number, using {suggested_season}")
            season = suggested_season
    else:
        season = suggested_season
    
    print(f"  Format: {show_name} S{season:02d}E{{n:02d}}.mkv")
    print()
    
    return show_name, season


def prompt_detection_method() -> tuple[str, int | None]:
    """
    Prompt user for detection method.
    Returns (method, value) where method is 'auto', 'total_episodes', or 'chapter_count'
    and value is the user-provided count (or None for auto).
    """
    print("How to determine episode boundaries?")
    print("  [1] Auto-detect")
    print("  [2] I know total episode count (across all files)")
    print("  [3] I know chapters per episode (fixed)")
    print()
    
    while True:
        choice = input("> ").strip()
        
        if choice == '1':
            return ('auto', None)
        elif choice == '2':
            while True:
                try:
                    count = int(input("Total episodes: ").strip())
                    if count > 0:
                        return ('total_episodes', count)
                    print("Must be a positive number")
                except ValueError:
                    print("Please enter a number")
        elif choice == '3':
            while True:
                try:
                    count = int(input("Chapters per episode: ").strip())
                    if count > 0:
                        return ('chapter_count', count)
                    print("Must be a positive number")
                except ValueError:
                    print("Please enter a number")
        else:
            print("Please enter 1, 2, or 3")


def apply_detection(files: list[MkvFile], method: str, value: int | None) -> bool:
    """
    Apply the selected detection method to all files.
    Returns True if successful, False if auto-detect failed.
    """
    if method == 'total_episodes':
        return split_all_by_duration(files, value)
    
    if method == 'auto':
        return auto_detect_all(files)
    
    # chapter_count method
    episode_number = 1
    for mkv in files:
        mkv.episodes = split_by_chapter_count(mkv, value, start_episode=episode_number)
        episode_number += len(mkv.episodes)
    
    return True


def display_analysis(files: list[MkvFile]) -> int:
    """Display full episode breakdown for all files. Returns total episode count."""
    total_episodes = 0
    
    for i, mkv in enumerate(files):
        if mkv.skip:
            continue
            
        print()
        print("=" * 60)
        print(f"File {i+1}/{len(files)}: {mkv.path.name}")
        print(f"Total: {mkv.duration_str()} | {mkv.num_chapters} chapters -> {len(mkv.episodes)} episodes")
        print("=" * 60)
        
        if not mkv.episodes:
            print("  No episodes detected!")
            continue
        
        # Calculate median duration for outlier detection
        durations = [ep.duration for ep in mkv.episodes]
        med_duration = median(durations) if durations else 0
        
        for ep in mkv.episodes:
            # Flag significant outliers (>15% deviation from median)
            deviation = ((ep.duration - med_duration) / med_duration * 100) if med_duration > 0 else 0
            flag = f" [!] {deviation:+.0f}%" if abs(deviation) > 15 else ""
            
            print(f"  E{ep.number:02d}: {ep.chapter_range:10} ({ep.chapter_count} ch) "
                  f"{ep.start_str} -> {ep.end_str}  {ep.duration_str}{flag}")
        
        total_episodes += len(mkv.episodes)
    
    return total_episodes


def find_episode_location(files: list[MkvFile], episode_num: int) -> tuple[MkvFile, int] | None:
    """
    Find which file and episode index contains a given episode number.
    Returns (mkv_file, episode_index_within_file) or None if not found.
    """
    for mkv in files:
        if mkv.skip:
            continue
        for i, ep in enumerate(mkv.episodes):
            if ep.number == episode_num:
                return (mkv, i)
    return None


def edit_episode(files: list[MkvFile], episode_num: int, target_duration: float):
    """
    Allow user to manually adjust chapter range for a specific episode.
    Remaining episodes in the file are recalculated.
    """
    location = find_episode_location(files, episode_num)
    if not location:
        print(f"Episode {episode_num} not found")
        return
    
    mkv, ep_idx = location
    ep = mkv.episodes[ep_idx]
    
    print()
    print(f"Editing E{episode_num:02d} in {mkv.path.name}")
    print(f"Current: {ep.chapter_range} ({ep.chapter_count} ch) {ep.duration_str}")
    print(f"File has {mkv.num_chapters} chapters total")
    print()
    print("Enter new chapter range (e.g., '17-22') or [c]ancel:")
    
    cmd = input("> ").strip().lower()
    
    if cmd == 'c' or not cmd:
        return
    
    # Parse chapter range
    try:
        if '-' in cmd:
            parts = cmd.split('-')
            start_ch = int(parts[0])
            end_ch = int(parts[1])
        else:
            print("Invalid format. Use 'start-end' (e.g., '17-22')")
            return
    except ValueError:
        print("Invalid chapter numbers")
        return
    
    # Validate range
    if start_ch < 1 or end_ch > mkv.num_chapters or start_ch > end_ch:
        print(f"Invalid range. Chapters must be between 1 and {mkv.num_chapters}")
        return
    
    # Check that start_ch matches expected position
    # (must start where previous episode ended, or at chapter 1 for first episode)
    if ep_idx == 0:
        expected_start = 1
    else:
        prev_ep = mkv.episodes[ep_idx - 1]
        expected_start = prev_ep.chapters[-1].index + 1
    
    if start_ch != expected_start:
        print(f"Start chapter must be {expected_start} (where previous episode ends)")
        return
    
    # Build new chapter list for this episode
    new_chapters = [ch for ch in mkv.chapters if start_ch <= ch.index <= end_ch]
    
    if not new_chapters:
        print("No chapters in specified range")
        return
    
    # Update this episode
    ep.chapters = new_chapters
    
    # Recalculate remaining episodes in this file
    remaining_start_ch = end_ch + 1
    remaining_chapters = [ch for ch in mkv.chapters if ch.index >= remaining_start_ch]
    
    # Remove old episodes after this one
    mkv.episodes = mkv.episodes[:ep_idx + 1]
    
    # If there are remaining chapters, re-detect episodes from them
    if remaining_chapters:
        # Create a temporary structure for detection
        temp_mkv = MkvFile(
            path=mkv.path,
            chapters=remaining_chapters,
            total_duration=remaining_chapters[-1].end_time - remaining_chapters[0].start_time
        )
        
        # Use duration-based detection on remaining chapters
        new_episodes = split_by_duration_target(
            temp_mkv, 
            target_duration, 
            start_episode=ep.number + 1
        )
        
        mkv.episodes.extend(new_episodes)
    
    print(f"Updated E{episode_num:02d} to {ep.chapter_range}")


def renumber_all_episodes(files: list[MkvFile]):
    """Renumber all episodes sequentially across files."""
    ep_num = 1
    for mkv in files:
        if mkv.skip:
            continue
        for ep in mkv.episodes:
            ep.number = ep_num
            ep_num += 1


def interactive_review(files: list[MkvFile], target_duration: float) -> bool:
    """Interactive review loop. Returns True if user wants to proceed."""
    while True:
        total_episodes = display_analysis(files)
        
        print()
        print("=" * 60)
        active_files = sum(1 for f in files if not f.skip)
        print(f"Summary: {total_episodes} episodes from {active_files} files")
        print("=" * 60)
        print()
        print("[g]o | [e]dit episode # | [s]kip file # | [q]uit")
        print()
        
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        
        if cmd == 'q':
            return False
        elif cmd == 'g':
            if total_episodes == 0:
                print("No episodes to process!")
                continue
            return True
        elif cmd.startswith('e'):
            # Edit episode by number
            parts = cmd.split()
            if len(parts) == 2 and parts[1].isdigit():
                ep_num = int(parts[1])
                edit_episode(files, ep_num, target_duration)
                renumber_all_episodes(files)
            else:
                print("Usage: e <episode number> (e.g., 'e 18' to edit episode 18)")
        elif cmd.startswith('s'):
            # Skip file by number
            parts = cmd.split()
            if len(parts) == 2 and parts[1].isdigit():
                idx = int(parts[1]) - 1
                if 0 <= idx < len(files):
                    files[idx].skip = not files[idx].skip
                    status = "skipped" if files[idx].skip else "included"
                    print(f"File {idx + 1} {status}")
                    renumber_all_episodes(files)
                else:
                    print(f"Invalid file number. Enter 1-{len(files)}")
            else:
                print("Usage: s <file number> (e.g., 's 2' to skip file 2)")
        else:
            print("Unknown command")


def renumber_chapters(filepath: Path, num_chapters: int) -> bool:
    """
    Renumber chapters in an MKV file to start from 1.
    Creates a simple chapter file and applies it with mkvpropedit.
    Returns True on success.
    """
    # First, extract existing chapter timestamps
    try:
        result = subprocess.run(
            ['mkvextract', str(filepath), 'chapters', '-s'],
            capture_output=True,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError:
        return False
    
    # Parse timestamps from the output
    timestamps = []
    for line in result.stdout.strip().split('\n'):
        if line.startswith('CHAPTER') and '=' in line and 'NAME' not in line:
            time_str = line.split('=')[1]
            timestamps.append(time_str)
    
    if not timestamps:
        return False
    
    # Create new chapter file with renumbered chapters
    chapter_file = filepath.parent / f"_temp_chapters_{filepath.stem}.txt"
    
    try:
        with open(chapter_file, 'w') as f:
            for i, ts in enumerate(timestamps):
                ch_num = i + 1
                f.write(f"CHAPTER{ch_num:02d}={ts}\n")
                f.write(f"CHAPTER{ch_num:02d}NAME=Chapter {ch_num:02d}\n")
        
        # Apply new chapters with mkvpropedit
        result = subprocess.run(
            ['mkvpropedit', str(filepath), '--chapters', str(chapter_file)],
            capture_output=True,
            text=True,
            check=True
        )
        
        return True
        
    except (subprocess.CalledProcessError, IOError):
        return False
    finally:
        # Clean up temp file
        if chapter_file.exists():
            chapter_file.unlink()


def process_file(mkv: MkvFile, show_name: str, season: int) -> bool:
    """
    Process a single file: split into episodes.
    Uses mkvmerge --split chapters:X,Y,Z to split at episode boundaries,
    then renames the output files.
    Returns True on success.
    """
    directory = mkv.path.parent
    
    if not mkv.episodes:
        return False
    
    # Build list of chapter numbers to split at
    # We split BEFORE the first chapter of each episode (except the first episode)
    split_points = []
    for ep in mkv.episodes[1:]:  # Skip first episode
        first_ch = ep.chapters[0].index
        split_points.append(str(first_ch))
    
    # Create temporary output path
    temp_base = directory / f"_split_temp_{mkv.path.stem}"
    
    try:
        if split_points:
            # Multiple episodes: split at chapter boundaries
            split_arg = ','.join(split_points)
            print(f"  Splitting at chapters: {split_arg}")
            
            result = subprocess.run(
                [
                    'mkvmerge',
                    '-o', f"{temp_base}.mkv",
                    '--split', f'chapters:{split_arg}',
                    str(mkv.path)
                ],
                capture_output=True,
                text=True,
                check=True
            )
        else:
            # Single episode: just copy/remux the file
            print(f"  Single episode, remuxing...")
            result = subprocess.run(
                [
                    'mkvmerge',
                    '-o', f"{temp_base}-001.mkv",
                    str(mkv.path)
                ],
                capture_output=True,
                text=True,
                check=True
            )
        
        # Rename output files to proper episode names and renumber chapters
        for i, ep in enumerate(mkv.episodes):
            part_num = i + 1
            temp_file = directory / f"_split_temp_{mkv.path.stem}-{part_num:03d}.mkv"
            output_name = f"{show_name} S{season:02d}E{ep.number:02d}.mkv"
            output_path = directory / output_name
            
            if temp_file.exists():
                print(f"  Renaming part {part_num} -> {output_name}")
                temp_file.rename(output_path)
                
                # Renumber chapters to start from 1
                if renumber_chapters(output_path, len(ep.chapters)):
                    print(f"    Renumbered {len(ep.chapters)} chapters")
                else:
                    print(f"    Warning: Could not renumber chapters")
            else:
                print(f"    Error: Expected file not found: {temp_file.name}")
                return False
        
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"    Error: {e}")
        print(f"    stderr: {e.stderr}")
        # Clean up any temp files
        for temp_file in directory.glob(f"_split_temp_{mkv.path.stem}*.mkv"):
            temp_file.unlink()
        return False


def process_files(files: list[MkvFile], show_name: str, season: int):
    """Process all non-skipped files."""
    active_files = [f for f in files if not f.skip]
    
    print()
    print("=" * 60)
    print(f"Processing {len(active_files)} files...")
    print("=" * 60)
    
    success_count = 0
    for i, mkv in enumerate(active_files):
        print()
        print(f"[{i + 1}/{len(active_files)}] {mkv.path.name}")
        
        if process_file(mkv, show_name, season):
            success_count += 1
            print(f"  Done!")
        else:
            print(f"  FAILED")
    
    print()
    print("=" * 60)
    print(f"Completed: {success_count}/{len(active_files)} files processed successfully")
    print("=" * 60)


def check_dependencies():
    """Verify required tools are installed."""
    for tool in ['mkvmerge', 'mkvextract', 'mkvpropedit']:
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
    
    print(f"Found {len(files)} MKV files with chapters")
    print()
    
    # Get show info
    show_name, season = prompt_show_info(directory)
    
    # Get detection method
    method, value = prompt_detection_method()
    print()
    
    # Apply detection
    print("Analyzing files...")
    if not apply_detection(files, method, value):
        print()
        print("Auto-detection failed. Falling back to manual input.")
        print()
        method, value = prompt_detection_method()
        if not apply_detection(files, method, value):
            print("Detection failed.")
            sys.exit(1)
    
    # Calculate target duration for use in editing
    total_runtime = sum(f.total_duration for f in files)
    total_episodes = sum(len(f.episodes) for f in files if not f.skip)
    target_duration = total_runtime / total_episodes if total_episodes > 0 else 24 * 60
    
    # Interactive review
    if interactive_review(files, target_duration):
        process_files(files, show_name, season)
    else:
        print("Cancelled.")


if __name__ == '__main__':
    main()
