#!/usr/bin/env python3
"""
Batch MKV re-encoder for fixing timestamp issues.

Encodes video to x265 CRF 14 (10-bit) and audio to FLAC.
Preserves HDR metadata. Skips Dolby Vision content.
Keeps only lossless audio tracks, drops lossy duplicates.
Archives originals before replacing.

Usage:
    python3 reencode_x265.py /tank/media/anime/Show/
"""

import subprocess
import sys
import shutil
import json
from pathlib import Path
from datetime import datetime

# Configuration
MEDIA_BASE = Path("/tank/media")
ARCHIVE_BASE = Path("/tank/archive/originals")
LOG_FILE = Path("/tank/archive/reencode.log")

# Audio codec classification
LOSSLESS_AUDIO = {
    "DTS-HD MA", "DTS-HD HR", "TrueHD", "FLAC", "PCM", "LPCM", "ALAC",
    "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le",
    "truehd", "flac", "alac", "mlp"
}
LOSSY_AUDIO = {
    "DTS", "AC3", "EAC3", "AAC", "MP3", "Opus", "Vorbis",
    "dts", "ac3", "eac3", "aac", "mp3", "opus", "vorbis"
}


def log(message: str):
    """Log to both stdout and file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def get_archive_path(source: Path) -> Path:
    """Mirror source path structure under archive base."""
    try:
        relative = source.relative_to(MEDIA_BASE)
    except ValueError:
        relative = Path(source.name)
    return ARCHIVE_BASE / relative


def probe_video(source: Path) -> dict:
    """Get comprehensive video stream info."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,color_transfer,color_primaries,color_space,pix_fmt",
        "-show_entries", "stream_side_data",
        "-of", "json",
        str(source)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        return data.get("streams", [{}])[0]
    except Exception:
        return {}


def get_audio_tracks(source: Path) -> list:
    """Get all audio track info."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index,codec_name,profile,channels:stream_tags=language,title",
        "-of", "json",
        str(source)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        return data.get("streams", [])
    except Exception:
        return []


def select_audio_tracks(tracks: list) -> tuple:
    """
    Select audio tracks: keep lossless, drop lossy duplicates.
    Returns (selected_indices, warnings)
    """
    if not tracks:
        return [], []
    
    warnings = []
    
    # Group tracks by language + channels
    groups = {}
    for track in tracks:
        lang = track.get("tags", {}).get("language", "und")
        channels = track.get("channels", 0)
        key = (lang, channels)
        
        if key not in groups:
            groups[key] = []
        groups[key].append(track)
    
    selected = []
    
    for (lang, channels), group_tracks in groups.items():
        lossless = []
        lossy = []
        unknown = []
        
        for track in group_tracks:
            codec = track.get("codec_name", "")
            profile = track.get("profile", "")
            
            # Check profile first (more specific), then codec
            identifier = profile if profile else codec
            
            if identifier in LOSSLESS_AUDIO or codec in LOSSLESS_AUDIO:
                lossless.append(track)
            elif identifier in LOSSY_AUDIO or codec in LOSSY_AUDIO:
                lossy.append(track)
            else:
                unknown.append(track)
                warnings.append(f"Unknown audio: {identifier} ({lang}, {channels}ch) - keeping")
        
        # Select: prefer lossless, fall back to lossy, always keep unknown
        if lossless:
            selected.extend(lossless)
        elif lossy:
            selected.extend(lossy)
        
        # Always keep unknown codecs
        selected.extend(unknown)
    
    # Sort by original index to maintain order
    selected.sort(key=lambda t: t.get("index", 0))
    indices = [t.get("index") for t in selected]
    
    return indices, warnings


def is_dolby_vision(source: Path) -> bool:
    """Detect Dolby Vision content."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream_side_data=dv_profile,dv_version_major",
        "-of", "json",
        str(source)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        side_data = stream.get("side_data_list", [])
        
        for sd in side_data:
            if "dv_profile" in sd or sd.get("side_data_type") == "DOVI configuration record":
                return True
        
        # Also check via codec tag
        cmd2 = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream_tags",
            "-of", "json",
            str(source)
        ]
        result2 = subprocess.run(cmd2, capture_output=True, text=True)
        output = result2.stdout.lower()
        if "dolby vision" in output or "dovi" in output:
            return True
            
    except Exception:
        pass
    
    return False


def get_hdr_params(source: Path) -> tuple:
    """
    Detect HDR and return appropriate x265 params.
    Returns (params_list, is_hdr)
    """
    stream = probe_video(source)
    
    color_transfer = stream.get("color_transfer", "")
    color_primaries = stream.get("color_primaries", "")
    color_space = stream.get("color_space", "")
    
    # Check if HDR (PQ for HDR10/HDR10+, HLG for HLG)
    is_hdr = color_transfer in ["smpte2084", "arib-std-b67"]
    
    if not is_hdr:
        return [], False
    
    # Map ffprobe values to x265 values
    primaries_map = {
        "bt2020": "bt2020",
        "bt709": "bt709",
    }
    transfer_map = {
        "smpte2084": "smpte2084",
        "arib-std-b67": "arib-std-b67",
    }
    matrix_map = {
        "bt2020nc": "bt2020nc",
        "bt2020c": "bt2020c",
        "bt709": "bt709",
    }
    
    x265_params = [
        "hdr10-opt=1",
        "repeat-headers=1",
    ]
    
    if color_primaries in primaries_map:
        x265_params.append(f"colorprim={primaries_map[color_primaries]}")
    if color_transfer in transfer_map:
        x265_params.append(f"transfer={transfer_map[color_transfer]}")
    if color_space in matrix_map:
        x265_params.append(f"colormatrix={matrix_map[color_space]}")
    
    # Extract mastering display and content light level from side data
    side_data = stream.get("side_data_list", [])
    
    for sd in side_data:
        sd_type = sd.get("side_data_type", "")
        
        if sd_type == "Mastering display metadata":
            try:
                def parse_ratio(s):
                    if "/" in str(s):
                        num, den = str(s).split("/")
                        return int(num), int(den)
                    return int(s), 1
                
                rx, rx_d = parse_ratio(sd.get("red_x", "0/1"))
                ry, ry_d = parse_ratio(sd.get("red_y", "0/1"))
                gx, gx_d = parse_ratio(sd.get("green_x", "0/1"))
                gy, gy_d = parse_ratio(sd.get("green_y", "0/1"))
                bx, bx_d = parse_ratio(sd.get("blue_x", "0/1"))
                by, by_d = parse_ratio(sd.get("blue_y", "0/1"))
                wpx, wpx_d = parse_ratio(sd.get("white_point_x", "0/1"))
                wpy, wpy_d = parse_ratio(sd.get("white_point_y", "0/1"))
                lmax, lmax_d = parse_ratio(sd.get("max_luminance", "0/1"))
                lmin, lmin_d = parse_ratio(sd.get("min_luminance", "0/1"))
                
                def scale_coord(num, den):
                    return int(num * 50000 / den) if den else 0
                
                def scale_lum(num, den):
                    return int(num * 10000 / den) if den else 0
                
                master_display = (
                    f"G({scale_coord(gx, gx_d)},{scale_coord(gy, gy_d)})"
                    f"B({scale_coord(bx, bx_d)},{scale_coord(by, by_d)})"
                    f"R({scale_coord(rx, rx_d)},{scale_coord(ry, ry_d)})"
                    f"WP({scale_coord(wpx, wpx_d)},{scale_coord(wpy, wpy_d)})"
                    f"L({scale_lum(lmax, lmax_d)},{scale_lum(lmin, lmin_d)})"
                )
                x265_params.append(f"master-display={master_display}")
            except Exception as e:
                log(f"  Warning: Could not parse mastering display metadata: {e}")
        
        elif sd_type == "Content light level metadata":
            try:
                max_cll = int(sd.get("max_content", 0))
                max_fall = int(sd.get("max_average", 0))
                x265_params.append(f"max-cll={max_cll},{max_fall}")
            except Exception:
                pass
    
    return x265_params, True


def encode_file(source: Path, x265_params: list, audio_indices: list) -> bool:
    """Encode a single file. Returns True on success."""
    temp_output = source.with_suffix(".tmp.mkv")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i", str(source),
        # Map video
        "-map", "0:v",
    ]
    
    # Map selected audio tracks
    for idx in audio_indices:
        cmd.extend(["-map", f"0:{idx}"])
    
    # Map subtitles and attachments
    cmd.extend([
        "-map", "0:s?",
        "-map", "0:t?",
        "-map", "0:d?",
        # Preserve all metadata
        "-map_metadata", "0",
        "-map_chapters", "0",
        # Video encoding
        "-c:v", "libx265",
        "-crf", "14",
        "-preset", "slow",
        "-profile:v", "main10",
        "-pix_fmt", "yuv420p10le",
    ])
    
    # Add x265 params for HDR if needed
    if x265_params:
        cmd.extend(["-x265-params", ":".join(x265_params)])
    
    cmd.extend([
        # Audio encoding
        "-c:a", "flac",
        # Copy everything else unchanged
        "-c:s", "copy",
        "-c:t", "copy",
        "-c:d", "copy",
        # Clear subtitle default flag
        "-disposition:s", "0",
        "-y",
        str(temp_output)
    ])

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            if "frame=" in line or "speed=" in line:
                print(f"\r{line.strip()[:100]}", end="", flush=True)

        process.wait()
        print()

        if process.returncode != 0:
            log(f"FAILED: {source.name}")
            error_output = "".join(output_lines[-30:])
            log(f"  Error: {error_output}")
            temp_output.unlink(missing_ok=True)
            return False
        return True

    except Exception as e:
        log(f"FAILED: {source.name} - {e}")
        temp_output.unlink(missing_ok=True)
        return False


def process_file(source: Path) -> str:
    """
    Encode, archive original, replace with new file.
    Returns: 'success', 'skipped_dv', or 'failed'
    """
    temp_output = source.with_suffix(".tmp.mkv")
    archive_path = get_archive_path(source)

    # Check for Dolby Vision
    if is_dolby_vision(source):
        log(f"SKIPPED (Dolby Vision): {source.name}")
        log(f"  Warning: DV content requires special handling and cannot be re-encoded without losing DV metadata")
        return "skipped_dv"

    # Get video info
    stream = probe_video(source)
    codec = stream.get("codec_name", "unknown")
    
    # Get HDR params
    x265_params, is_hdr = get_hdr_params(source)
    
    # Get audio tracks and select lossless
    audio_tracks = get_audio_tracks(source)
    audio_indices, audio_warnings = select_audio_tracks(audio_tracks)
    
    hdr_status = "HDR" if is_hdr else "SDR"
    log(f"Processing: {source.name} (codec: {codec}, {hdr_status})")
    log(f"  Audio: {len(audio_tracks)} tracks -> {len(audio_indices)} selected")
    
    for warning in audio_warnings:
        log(f"  {warning}")
    
    if x265_params:
        log(f"  HDR params: {':'.join(x265_params)}")

    # Encode
    if not encode_file(source, x265_params, audio_indices):
        return "failed"

    # Create archive directory
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    # Move original to archive
    try:
        shutil.move(str(source), str(archive_path))
    except Exception as e:
        log(f"FAILED to archive: {source.name} - {e}")
        temp_output.unlink(missing_ok=True)
        return "failed"

    # Rename temp to original name
    try:
        temp_output.rename(source)
    except Exception as e:
        log(f"FAILED to rename: {source.name} - {e}")
        shutil.move(str(archive_path), str(source))
        temp_output.unlink(missing_ok=True)
        return "failed"

    # Get file sizes
    original_size = archive_path.stat().st_size / (1024**3)
    new_size = source.stat().st_size / (1024**3)
    reduction = (1 - new_size / original_size) * 100

    log(f"SUCCESS: {source.name} ({original_size:.2f}GB -> {new_size:.2f}GB, {reduction:+.1f}%)")
    return "success"


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <directory>")
        sys.exit(1)

    source_dir = Path(sys.argv[1])
    if not source_dir.is_dir():
        print(f"Error: {source_dir} is not a directory")
        sys.exit(1)

    # Find all MKV files (recursive)
    mkv_files = sorted(source_dir.glob("**/*.mkv"))

    if not mkv_files:
        print("No MKV files found")
        sys.exit(0)

    # Show files and ask for confirmation
    print(f"\nFound {len(mkv_files)} MKV files in {source_dir}:\n")
    for i, mkv in enumerate(mkv_files, 1):
        try:
            rel_path = mkv.relative_to(source_dir)
        except ValueError:
            rel_path = mkv.name
        size_gb = mkv.stat().st_size / (1024**3)
        print(f"  {i:3}. {rel_path} ({size_gb:.2f} GB)")
    
    print(f"\nArchive location: {ARCHIVE_BASE}")
    print(f"Log file: {LOG_FILE}")
    print()
    
    confirm = input("Proceed with encoding? [y/N]: ").strip().lower()
    if confirm != 'y':
        print("Aborted.")
        sys.exit(0)

    log(f"{'='*60}")
    log(f"Starting batch encode: {len(mkv_files)} files in {source_dir}")
    log(f"{'='*60}")

    success = 0
    skipped_dv = 0
    failed = 0

    for i, mkv in enumerate(mkv_files, 1):
        try:
            rel_path = mkv.relative_to(source_dir)
        except ValueError:
            rel_path = mkv.name
        log(f"[{i}/{len(mkv_files)}] {rel_path}")

        result = process_file(mkv)
        
        if result == "success":
            success += 1
        elif result == "skipped_dv":
            skipped_dv += 1
        else:
            failed += 1

    log(f"{'='*60}")
    log(f"Complete:")
    log(f"  Encoded:    {success}")
    log(f"  Skipped DV: {skipped_dv} (Dolby Vision)")
    log(f"  Failed:     {failed}")
    log(f"{'='*60}")


if __name__ == "__main__":
    main()
