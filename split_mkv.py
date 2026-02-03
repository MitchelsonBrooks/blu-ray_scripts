#!/usr/bin/env python3
import os
import subprocess
import sys


def get_chapter_timestamps(input_file):
    """Get chapter timestamps and names using mkvextract."""
    result = subprocess.run(
        ["mkvextract", input_file, "chapters", "-s"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []

    timestamps = []
    names = {}
    for line in result.stdout.strip().split('\n'):
        if not line or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if 'NAME' in key:
            ch_num = ''.join(filter(str.isdigit, key.replace('NAME', '')))
            if ch_num:
                names[int(ch_num)] = value
        elif key.startswith('CHAPTER'):
            ch_num = ''.join(filter(str.isdigit, key))
            if ch_num:
                parts = value.split(':')
                total_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                timestamps.append((int(ch_num), total_seconds, value))

    timestamps.sort(key=lambda x: x[0])
    result_list = []
    for ch_num, secs, time_str in timestamps:
        name = names.get(ch_num, f"Chapter {ch_num:02d}")
        result_list.append((secs, name, time_str))
    return result_list


def format_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}"


def renumber_chapters(filepath):
    """Renumber chapters in an MKV file to start from 1 using mkvpropedit."""
    result = subprocess.run(
        ["mkvextract", filepath, "chapters", "-s"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return False

    timestamps = []
    for line in result.stdout.strip().split('\n'):
        if line.startswith('CHAPTER') and '=' in line and 'NAME' not in line:
            timestamps.append(line.split('=')[1])

    if not timestamps:
        return False

    chapter_file = filepath + ".tmp_chapters.txt"
    try:
        with open(chapter_file, 'w') as f:
            for i, ts in enumerate(timestamps):
                ch_num = i + 1
                f.write(f"CHAPTER{ch_num:02d}={ts}\n")
                f.write(f"CHAPTER{ch_num:02d}NAME=Chapter {ch_num:02d}\n")

        result = subprocess.run(
            ["mkvpropedit", filepath, "--chapters", chapter_file],
            capture_output=True, text=True
        )
        return result.returncode == 0
    finally:
        if os.path.exists(chapter_file):
            os.unlink(chapter_file)


def split_mkv(input_file, num_chapters, split_chapters, output_names):
    input_dir = os.path.dirname(os.path.abspath(input_file))
    base = os.path.splitext(os.path.basename(input_file))[0]
    temp_base = os.path.join(input_dir, f"_split_temp_{base}")

    # Split with mkvmerge at chapter boundaries
    split_arg = ",".join(str(ch) for ch in split_chapters)
    print(f"\nSplitting at chapters: {split_arg}")
    cmd = [
        "mkvmerge", "-o", f"{temp_base}.mkv",
        "--split", f"chapters:{split_arg}",
        input_file
    ]
    result = subprocess.run(cmd)
    if result.returncode > 1:
        print(f"Error: mkvmerge split failed (exit code {result.returncode})")
        sys.exit(1)

    # Rename temp files and renumber chapters
    for i, name in enumerate(output_names):
        temp_file = f"{temp_base}-{i + 1:03d}.mkv"
        if not os.path.exists(temp_file):
            print(f"Error: Expected file not found: {temp_file}")
            sys.exit(1)

        if os.path.exists(name):
            os.unlink(name)
        os.rename(temp_file, name)

        print(f"Renumbering chapters in {os.path.basename(name)}...")
        if renumber_chapters(name):
            print(f"  Chapters renumbered.")
        else:
            print(f"  Warning: Could not renumber chapters.")


def main():
    if len(sys.argv) < 2:
        print("Usage: python split_mkv.py <input.mkv>")
        sys.exit(1)

    input_file = sys.argv[1]
    chapter_timestamps = get_chapter_timestamps(input_file)

    if not chapter_timestamps:
        print("No chapters found in file.")
        sys.exit(1)

    num_chapters = len(chapter_timestamps)
    print(f"\nFound {num_chapters} chapters:\n")
    for i, (secs, name, _) in enumerate(chapter_timestamps):
        print(f"  {i + 1:2d}. {name:20s}  {format_time(secs)}")

    print("\nEnter chapter numbers where each new movie starts (comma-separated).")
    print("Example: 9,15 splits into 3 parts: Ch1-8, Ch9-14, Ch15-end")
    raw = input("\nSplit at chapters: ").strip()
    if not raw:
        print("No chapters entered.")
        sys.exit(1)
    try:
        split_chapters = sorted(set(int(x.strip()) for x in raw.split(",")))
    except ValueError:
        print("Invalid input. Enter chapter numbers separated by commas (e.g. 9,15).")
        sys.exit(1)

    for ch_num in split_chapters:
        if ch_num < 2 or ch_num > num_chapters:
            print(f"Invalid chapter number: {ch_num} (must be 2-{num_chapters})")
            sys.exit(1)

    num_parts = len(split_chapters) + 1
    base = os.path.splitext(os.path.basename(input_file))[0]
    ext = os.path.splitext(input_file)[1]
    input_dir = os.path.dirname(os.path.abspath(input_file))

    defaults = []
    boundaries = [1] + split_chapters + [num_chapters + 1]
    for i in range(num_parts):
        ch_start = boundaries[i]
        ch_end = boundaries[i + 1] - 1
        defaults.append(os.path.join(input_dir, f"{base}_part{i + 1}_ch{ch_start}-{ch_end}{ext}"))

    print(f"\nThis will create {num_parts} files. Enter output filenames (press Enter for default):")
    output_names = []
    for i in range(num_parts):
        name = input(f"  Part {i + 1} [{defaults[i]}]: ").strip()
        output_names.append(name if name else defaults[i])

    split_mkv(input_file, num_chapters, split_chapters, output_names)
    print("\nDone.")


if __name__ == "__main__":
    main()
