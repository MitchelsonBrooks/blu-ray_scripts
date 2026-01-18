#!/usr/bin/env python3
"""
Media file renamer for disc rips.
Renames files from disc/track format to standard S01E01 format.

Usage:
    python rename_media.py /path/to/Videos              # Dry run
    python rename_media.py /path/to/Videos --execute    # Actually rename

Supports:
    - Flat structure: "Show S1/" folders directly under base path
    - Nested structure: "Show/Season 1/" folders
"""

import os
import re
import sys
from pathlib import Path
from dataclasses import dataclass
from collections import defaultdict


@dataclass
class MediaFile:
    path: Path
    show_name: str
    season: int
    part: int  # For multi-part releases (P1, P2, etc.), 0 if not applicable
    disc: int
    track: int

    def __lt__(self, other):
        return (self.part, self.disc, self.track) < (other.part, other.disc, other.track)


def normalize_for_grouping(key: str) -> str:
    """
    Normalize a merge key for grouping comparison.
    Removes spaces and lowercases to handle inconsistent naming like
    'VinlandSaga S2' vs 'Vinland Saga S2'.
    """
    return key.lower().replace(' ', '')


def parse_nested_season_folder(folder_name: str) -> int | None:
    """
    Parse a nested season folder name like "Season 1" or "Season 02".
    Returns the season number, or None if not a season folder.
    """
    match = re.search(r'^Season\s*(\d+)$', folder_name, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def parse_season_from_folder(folder_name: str) -> tuple[str, int, int, int]:
    """
    Extract show name, season number, part number, and disc number from folder name.
    Returns (canonical_show_name, season_number, part_number, disc_number)
    Part number is 0 if not applicable.
    Disc number is 0 if not specified in folder name.
    """
    # Pattern: "Show Name S1BD-1" -> season 1, part 0, disc 1
    match = re.search(r'^(.+?)\s*S(\d+)\s*BD[- ]?(\d+)$', folder_name, re.IGNORECASE)
    if match:
        return match.group(1).strip(), int(match.group(2)), 0, int(match.group(3))
    
    # Pattern: "Show Name S2P1 D1" or "Show Name S2 P1 D1" -> season 2, part 1, disc 1
    match = re.search(r'^(.+?)\s*S(\d+)\s*P(\d+)\s*D\s*(\d+)$', folder_name, re.IGNORECASE)
    if match:
        return match.group(1).strip(), int(match.group(2)), int(match.group(3)), int(match.group(4))
    
    # Pattern: "Show Name S1P1" or "Show Name S1 P2" -> season 1, part 1 or 2, disc 0
    match = re.search(r'^(.+?)\s*S(\d+)\s*P(\d+)$', folder_name, re.IGNORECASE)
    if match:
        return match.group(1).strip(), int(match.group(2)), int(match.group(3)), 0
    
    # Pattern: "Show Name S2 D1" or "Show Name S2D1" -> season 2, part 0, disc from folder
    match = re.search(r'^(.+?)\s*S(\d+)\s*D\s*(\d+)$', folder_name, re.IGNORECASE)
    if match:
        return match.group(1).strip(), int(match.group(2)), 0, int(match.group(3))
    
    # Pattern: "Show Name S2" -> season 2, part 0, disc 0
    match = re.search(r'^(.+?)\s*S(\d+)$', folder_name, re.IGNORECASE)
    if match:
        return match.group(1).strip(), int(match.group(2)), 0, 0
    
    # No season indicator, default to season 1, part 0, disc 0
    return folder_name, 1, 0, 0


def get_merge_key(folder_name: str) -> str:
    """
    Get a key for grouping folders that should be merged.
    e.g., "FRIEREN S1P1" and "FRIEREN S1P2" -> "FRIEREN S1"
    e.g., "Mob Psycho 100 S2 D1" and "Mob Psycho 100 S2 D2" -> "Mob Psycho 100 S2"
    e.g., "Vinland Saga S1BD-1" and "Vinland Saga S1BD-2" -> "Vinland Saga S1"
    
    Order matters: strip disc first, then part, then BD suffix.
    """
    key = folder_name
    # Strip disc indicator first (must come before part stripping)
    # Pattern: "Show S2 P1 D1" -> "Show S2 P1"
    key = re.sub(r'\s*D\s*\d+$', '', key, flags=re.IGNORECASE).strip()
    # Pattern: "Show S1BD-1" -> "Show S1"
    key = re.sub(r'\s*BD[- ]?\d+$', '', key, flags=re.IGNORECASE).strip()
    # Pattern: "Show S1P1" or "Show S1 P1" -> "Show S1"
    key = re.sub(r'\s*P\s*\d+$', '', key, flags=re.IGNORECASE).strip()
    return key


def parse_filename(filepath: Path, folder_name: str, show_override: str = None, season_override: int = None) -> MediaFile | None:
    """
    Parse a media filename and extract components.
    
    Args:
        filepath: Path to the media file
        folder_name: Name of the containing folder (used for pattern matching)
        show_override: If provided, use this as the show name instead of parsing from folder
        season_override: If provided, use this as the season instead of parsing from folder
    """
    filename = filepath.stem
    
    # Skip non-episode files
    if 'Bonus' in str(filepath) or 'Features' in str(filepath):
        return None
    
    # Use overrides if provided, otherwise parse from folder
    if show_override is not None and season_override is not None:
        show_name = show_override
        season = season_override
        part = 0
        folder_disc = 0
    else:
        show_name, season, part, folder_disc = parse_season_from_folder(folder_name)
    
    # Pattern 1: "split_Show Name Disc N_tXX" or "Show Name Disc N_tXX"
    match = re.search(r'(?:split_)?(.+?)\s*Disc\s*(\d+)_t(\d+)$', filename, re.IGNORECASE)
    if match:
        return MediaFile(
            path=filepath,
            show_name=show_name,
            season=season,
            part=part,
            disc=int(match.group(2)),
            track=int(match.group(3))
        )
    
    # Pattern 2: "Show Name SxPx Dx_tXX" or "Show Name Sx Px Dx_tXX" (flexible spacing)
    match = re.search(r'(.+?)\s*S\d+\s*(?:P\d+)?\s*D\s*(\d+)_t(\d+)$', filename, re.IGNORECASE)
    if match:
        return MediaFile(
            path=filepath,
            show_name=show_name,
            season=season,
            part=part,
            disc=int(match.group(2)),
            track=int(match.group(3))
        )
    
    # Pattern 3: "Show Name BD DISCx-XXX" (Gurren Lagann style)
    match = re.search(r'(.+?)\s*BD\s*DISC(\d+)-(\d+)$', filename, re.IGNORECASE)
    if match:
        return MediaFile(
            path=filepath,
            show_name=show_name,
            season=season,
            part=part,
            disc=int(match.group(2)),
            track=int(match.group(3))
        )
    
    # Pattern 4: "title_tXX" - simple track number, disc comes from folder
    match = re.search(r'^title_t(\d+)$', filename, re.IGNORECASE)
    if match and folder_disc > 0:
        return MediaFile(
            path=filepath,
            show_name=show_name,
            season=season,
            part=part,
            disc=folder_disc,
            track=int(match.group(1))
        )
    
    # Pattern 5: "ShowName Dx_tXX" - disc and track in filename (e.g., "ODDTAXI D1_t00")
    match = re.search(r'^(.+?)\s*D(\d+)_t(\d+)$', filename, re.IGNORECASE)
    if match:
        return MediaFile(
            path=filepath,
            show_name=show_name,
            season=season,
            part=part,
            disc=int(match.group(2)),
            track=int(match.group(3))
        )
    
    return None


def is_movie_folder(folder_path: Path) -> bool:
    """Check if folder contains a single movie rather than episodes."""
    mkv_files = list(folder_path.glob('*.mkv'))
    if len(mkv_files) == 1:
        # Single file, likely a movie
        filename = mkv_files[0].stem
        # Check if it's just a single _t00 file (movie)
        if re.search(r'_t00$', filename):
            return True
    return False


def has_season_subfolders(folder_path: Path) -> bool:
    """Check if a folder contains 'Season X' subfolders."""
    for item in folder_path.iterdir():
        if item.is_dir() and parse_nested_season_folder(item.name) is not None:
            return True
    return False


def scan_directory(base_path: Path) -> dict[str, list[MediaFile]]:
    """
    Scan the directory and group files by show+season.
    Returns dict mapping normalized_merge_key -> list of MediaFiles
    
    Supports three directory structures:
    1. Flat: base_path/Show S1/*.mkv
    2. Nested from parent: base_path/Show/Season 1/*.mkv
    3. Nested from show: base_path/Season 1/*.mkv (base_path name is show name)
    """
    shows = defaultdict(list)
    
    # Check if base_path itself contains Season folders (running from show directory)
    if has_season_subfolders(base_path):
        show_name = base_path.name
        
        for season_folder in base_path.iterdir():
            if not season_folder.is_dir():
                continue
            
            season_num = parse_nested_season_folder(season_folder.name)
            if season_num is None:
                continue  # Skip non-season folders
            
            merge_key = f"{show_name} S{season_num}"
            normalized_key = normalize_for_grouping(merge_key)
            
            for mkv_file in season_folder.glob('*.mkv'):
                media_file = parse_filename(
                    mkv_file,
                    season_folder.name,
                    show_override=show_name,
                    season_override=season_num
                )
                if media_file:
                    shows[normalized_key].append(media_file)
        
        return shows
    
    # Otherwise, process child folders as shows
    for item in base_path.iterdir():
        if not item.is_dir():
            continue
        
        folder_name = item.name
        
        # Skip certain folders
        if folder_name in ('Screencasts', 'scripts', 'movies', 'shows') or 'Bonus' in folder_name or 'Features' in folder_name or 'Specials' in folder_name:
            continue
        
        # Check for nested "Show/Season X" structure
        if has_season_subfolders(item):
            show_name = folder_name  # Parent folder is the show name
            
            for season_folder in item.iterdir():
                if not season_folder.is_dir():
                    continue
                
                season_num = parse_nested_season_folder(season_folder.name)
                if season_num is None:
                    continue  # Skip non-season folders (like Bonus, Specials, etc.)
                
                # Build merge key for nested structure: "ShowName S1"
                merge_key = f"{show_name} S{season_num}"
                normalized_key = normalize_for_grouping(merge_key)
                
                for mkv_file in season_folder.glob('*.mkv'):
                    media_file = parse_filename(
                        mkv_file, 
                        season_folder.name,
                        show_override=show_name,
                        season_override=season_num
                    )
                    if media_file:
                        shows[normalized_key].append(media_file)
        else:
            # Original flat structure: base_path/Show S1/*.mkv
            
            # Skip movie folders
            if is_movie_folder(item):
                continue
            
            merge_key = get_merge_key(folder_name)
            normalized_key = normalize_for_grouping(merge_key)
            
            for mkv_file in item.glob('*.mkv'):
                media_file = parse_filename(mkv_file, folder_name)
                if media_file:
                    shows[normalized_key].append(media_file)
    
    return shows


def generate_renames(shows: dict[str, list[MediaFile]], base_path: Path) -> list[tuple[Path, Path]]:
    """Generate list of (old_path, new_path) tuples."""
    renames = []
    
    for normalized_key, files in shows.items():
        if not files:
            continue
        
        # Sort files by part, disc, then track
        files.sort()
        
        # Get show info from first file
        show_name = files[0].show_name
        season = files[0].season
        
        # Target folder built from show name + season
        target_folder = base_path / f"{show_name} S{season}"
        
        for episode_num, media_file in enumerate(files, start=1):
            new_filename = f"{show_name} S{season:02d}E{episode_num:02d}.mkv"
            new_path = target_folder / new_filename
            
            if media_file.path != new_path:
                renames.append((media_file.path, new_path))
    
    return renames


def main():
    if len(sys.argv) < 2:
        print("Usage: python rename_media.py /path/to/Videos [--execute]")
        sys.exit(1)
    
    base_path = Path(sys.argv[1]).expanduser().resolve()
    execute = '--execute' in sys.argv
    
    if not base_path.exists():
        print(f"Error: Path does not exist: {base_path}")
        sys.exit(1)
    
    print(f"Scanning: {base_path}")
    print(f"Mode: {'EXECUTE' if execute else 'DRY RUN'}")
    print("-" * 60)
    
    shows = scan_directory(base_path)
    renames = generate_renames(shows, base_path)
    
    if not renames:
        print("No files to rename.")
        return
    
    # Group by target folder for cleaner output
    by_folder = defaultdict(list)
    for old_path, new_path in renames:
        by_folder[new_path.parent].append((old_path, new_path))
    
    for folder in sorted(by_folder.keys()):
        print(f"\n{folder.name}/")
        for old_path, new_path in by_folder[folder]:
            old_rel = old_path.relative_to(base_path)
            new_name = new_path.name
            print(f"  {old_rel}")
            print(f"    -> {new_name}")
        
        if execute:
            # Create target folder if needed
            folder.mkdir(parents=True, exist_ok=True)
            
            for old_path, new_path in by_folder[folder]:
                old_path.rename(new_path)
    
    print("-" * 60)
    print(f"Total: {len(renames)} files")
    
    if not execute:
        print("\nThis was a dry run. Run with --execute to apply changes.")


if __name__ == '__main__':
    main()
