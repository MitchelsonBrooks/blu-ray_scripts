#!/usr/bin/env python3
"""
Interactive batch MKV re-encoder for fixing timestamp issues.

Encodes video to x265 CRF 14 (10-bit) and audio to FLAC.
Preserves HDR metadata. Skips Dolby Vision content.
Configurable audio track selection with lossless preference.
Archives originals before replacing.

Usage:
    python3 reencode_x265.py /tank/media/anime/Show/
"""

import subprocess
import sys
import shutil
import json
from dataclasses import dataclass, field
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


@dataclass
class AudioTrack:
    """Represents a single audio track in a file."""
    index: int
    language: str
    channels: int
    codec: str
    profile: str
    is_lossless: bool
    is_lossy: bool
    selected: bool = False
    
    @property
    def codec_display(self) -> str:
        """Human-readable codec name."""
        if self.profile and self.profile not in ["", "unknown"]:
            return self.profile
        return self.codec.upper()
    
    @property
    def channel_layout(self) -> str:
        """Human-readable channel layout."""
        layouts = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}
        return layouts.get(self.channels, f"{self.channels}ch")
    
    @property
    def quality_tag(self) -> str:
        """Tag indicating lossless/lossy/unknown."""
        if self.is_lossless:
            return "lossless"
        elif self.is_lossy:
            return "lossy"
        return "unknown"
    
    @property
    def signature(self) -> tuple:
        """Signature for comparing track layouts across files."""
        return (self.language, self.channels, self.is_lossless, self.is_lossy)
    
    def __str__(self) -> str:
        return f"{self.language.upper()} {self.channel_layout} ({self.codec_display}) [{self.quality_tag}]"


@dataclass
class SubtitleTrack:
    """Represents a single subtitle track in a file."""
    index: int
    language: str
    codec: str
    title: str
    is_forced: bool
    is_hearing_impaired: bool
    is_default: bool = False
    
    @property
    def codec_display(self) -> str:
        """Human-readable codec name."""
        codec_names = {
            "subrip": "SRT",
            "srt": "SRT",
            "ass": "ASS",
            "ssa": "SSA",
            "hdmv_pgs_subtitle": "PGS",
            "pgssub": "PGS",
            "dvd_subtitle": "VobSub",
            "dvdsub": "VobSub",
            "mov_text": "TX3G",
            "webvtt": "WebVTT",
        }
        return codec_names.get(self.codec.lower(), self.codec.upper())
    
    @property
    def signature(self) -> tuple:
        """Signature for comparing track layouts across files."""
        return (self.language, self.codec, self.is_forced, self.is_hearing_impaired)
    
    @property
    def flags(self) -> list[str]:
        """List of flag strings for display."""
        f = []
        if self.is_forced:
            f.append("forced")
        if self.is_hearing_impaired:
            f.append("SDH")
        return f
    
    def __str__(self) -> str:
        parts = [f"{self.language.upper()} ({self.codec_display})"]
        if self.title:
            parts.append(f'"{self.title}"')
        if self.flags:
            parts.append(f"[{', '.join(self.flags)}]")
        return " ".join(parts)


@dataclass
class ReencodeFile:
    """Represents an MKV file to be re-encoded."""
    path: Path
    codec: str
    is_hdr: bool
    is_dv: bool
    audio_tracks: list[AudioTrack]
    subtitle_tracks: list[SubtitleTrack]
    x265_params: list[str]
    size_gb: float
    selected: bool = True
    skip_reason: str = ""
    default_audio_lang: str = ""      # Language code for default audio
    default_subtitle_lang: str = ""   # Language code for default subtitle ("" = none)
    
    @property
    def audio_signature(self) -> tuple:
        """Signature of all audio tracks for comparison."""
        return tuple(t.signature for t in self.audio_tracks)
    
    @property
    def subtitle_signature(self) -> tuple:
        """Signature of all subtitle tracks for comparison."""
        return tuple(t.signature for t in self.subtitle_tracks)
    
    @property
    def selected_audio_tracks(self) -> list[AudioTrack]:
        """List of selected audio tracks."""
        return [t for t in self.audio_tracks if t.selected]
    
    @property
    def selected_audio_indices(self) -> list[int]:
        """Indices of selected audio tracks."""
        return [t.index for t in self.audio_tracks if t.selected]
    
    @property
    def hdr_status(self) -> str:
        """Human-readable HDR status."""
        if self.is_dv:
            return "Dolby Vision"
        elif self.is_hdr:
            return "HDR"
        return "SDR"
    
    def get_default_audio_index(self) -> int | None:
        """Get the output stream index for default audio track."""
        selected = self.selected_audio_tracks
        if not selected:
            return None
        if not self.default_audio_lang:
            return 0  # First selected track
        for i, track in enumerate(selected):
            if track.language == self.default_audio_lang:
                return i
        return 0  # Fallback to first
    
    def get_default_subtitle_index(self) -> int | None:
        """Get the output stream index for default subtitle track."""
        if not self.default_subtitle_lang or not self.subtitle_tracks:
            return None
        for i, track in enumerate(self.subtitle_tracks):
            if track.language == self.default_subtitle_lang:
                return i
        return None


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


def get_audio_tracks(source: Path) -> list[AudioTrack]:
    """Get all audio track info as AudioTrack objects."""
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
        streams = data.get("streams", [])
    except Exception:
        return []
    
    tracks = []
    for stream in streams:
        codec = stream.get("codec_name", "")
        profile = stream.get("profile", "")
        identifier = profile if profile else codec
        
        is_lossless = identifier in LOSSLESS_AUDIO or codec in LOSSLESS_AUDIO
        is_lossy = identifier in LOSSY_AUDIO or codec in LOSSY_AUDIO
        
        track = AudioTrack(
            index=stream.get("index", 0),
            language=stream.get("tags", {}).get("language", "und"),
            channels=stream.get("channels", 0),
            codec=codec,
            profile=profile,
            is_lossless=is_lossless,
            is_lossy=is_lossy
        )
        tracks.append(track)
    
    return tracks


def get_subtitle_tracks(source: Path) -> list[SubtitleTrack]:
    """Get all subtitle track info as SubtitleTrack objects."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "s",
        "-show_entries", "stream=index,codec_name:stream_tags=language,title:stream_disposition=default,forced,hearing_impaired",
        "-of", "json",
        str(source)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
    except Exception:
        return []
    
    tracks = []
    for stream in streams:
        disposition = stream.get("disposition", {})
        
        track = SubtitleTrack(
            index=stream.get("index", 0),
            language=stream.get("tags", {}).get("language", "und"),
            codec=stream.get("codec_name", "unknown"),
            title=stream.get("tags", {}).get("title", ""),
            is_forced=disposition.get("forced", 0) == 1,
            is_hearing_impaired=disposition.get("hearing_impaired", 0) == 1,
        )
        tracks.append(track)
    
    return tracks


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


def get_hdr_params(source: Path) -> tuple[list[str], bool]:
    """
    Detect HDR and return appropriate x265 params.
    Returns (params_list, is_hdr)
    """
    stream = probe_video(source)
    
    color_transfer = stream.get("color_transfer", "")
    color_primaries = stream.get("color_primaries", "")
    color_space = stream.get("color_space", "")
    
    is_hdr = color_transfer in ["smpte2084", "arib-std-b67"]
    
    if not is_hdr:
        return [], False
    
    primaries_map = {"bt2020": "bt2020", "bt709": "bt709"}
    transfer_map = {"smpte2084": "smpte2084", "arib-std-b67": "arib-std-b67"}
    matrix_map = {"bt2020nc": "bt2020nc", "bt2020c": "bt2020c", "bt709": "bt709"}
    
    x265_params = ["hdr10-opt=1", "repeat-headers=1"]
    
    if color_primaries in primaries_map:
        x265_params.append(f"colorprim={primaries_map[color_primaries]}")
    if color_transfer in transfer_map:
        x265_params.append(f"transfer={transfer_map[color_transfer]}")
    if color_space in matrix_map:
        x265_params.append(f"colormatrix={matrix_map[color_space]}")
    
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
            except Exception:
                pass
        
        elif sd_type == "Content light level metadata":
            try:
                max_cll = int(sd.get("max_content", 0))
                max_fall = int(sd.get("max_average", 0))
                x265_params.append(f"max-cll={max_cll},{max_fall}")
            except Exception:
                pass
    
    return x265_params, True


# =============================================================================
# Phase 1: Scanning
# =============================================================================

def scan_files(source_dir: Path) -> list[ReencodeFile]:
    """Scan all MKV files and analyze them."""
    mkv_paths = sorted(source_dir.glob("**/*.mkv"))
    
    if not mkv_paths:
        return []
    
    print(f"Scanning {len(mkv_paths)} MKV files...")
    print()
    
    files = []
    for i, mkv_path in enumerate(mkv_paths, 1):
        print(f"\r  Analyzing [{i}/{len(mkv_paths)}] {mkv_path.name[:50]}...", end="", flush=True)
        
        # Get video info
        stream = probe_video(mkv_path)
        codec = stream.get("codec_name", "unknown")
        
        # Check DV first
        dv = is_dolby_vision(mkv_path)
        
        # Get HDR params
        x265_params, is_hdr = get_hdr_params(mkv_path)
        
        # Get audio tracks
        audio_tracks = get_audio_tracks(mkv_path)
        
        # Get subtitle tracks
        subtitle_tracks = get_subtitle_tracks(mkv_path)
        
        # File size
        size_gb = mkv_path.stat().st_size / (1024**3)
        
        rf = ReencodeFile(
            path=mkv_path,
            codec=codec,
            is_hdr=is_hdr,
            is_dv=dv,
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks,
            x265_params=x265_params,
            size_gb=size_gb,
            selected=not dv,  # Auto-deselect DV files
            skip_reason="Dolby Vision" if dv else ""
        )
        files.append(rf)
    
    print("\r" + " " * 80 + "\r", end="")  # Clear line
    print(f"Scanned {len(files)} files.")
    
    return files


# =============================================================================
# Phase 2: Audio Configuration
# =============================================================================

def apply_default_audio_selection(tracks: list[AudioTrack]):
    """Apply default selection: lossless preferred, lossy fallback."""
    # Group by language + channels
    groups: dict[tuple, list[AudioTrack]] = {}
    for track in tracks:
        key = (track.language, track.channels)
        if key not in groups:
            groups[key] = []
        groups[key].append(track)
    
    # For each group, select lossless if available, else lossy
    for group_tracks in groups.values():
        lossless = [t for t in group_tracks if t.is_lossless]
        lossy = [t for t in group_tracks if t.is_lossy]
        unknown = [t for t in group_tracks if not t.is_lossless and not t.is_lossy]
        
        # Reset all in group
        for t in group_tracks:
            t.selected = False
        
        if lossless:
            for t in lossless:
                t.selected = True
        elif lossy:
            for t in lossy:
                t.selected = True
        
        # Always keep unknown
        for t in unknown:
            t.selected = True


def apply_audio_selection_to_all(files: list[ReencodeFile], template_tracks: list[AudioTrack]):
    """Apply selection from template to all files with matching layout."""
    template_selection = [t.selected for t in template_tracks]
    
    for rf in files:
        if len(rf.audio_tracks) == len(template_tracks):
            for i, track in enumerate(rf.audio_tracks):
                track.selected = template_selection[i]
        else:
            # Fallback to default for mismatched files
            apply_default_audio_selection(rf.audio_tracks)


def display_audio_tracks(tracks: list[AudioTrack], indent: str = "  "):
    """Display audio tracks with selection status."""
    for i, track in enumerate(tracks):
        sel = "[x]" if track.selected else "[ ]"
        print(f"{indent}{i+1}. {sel} {track}")


def configure_audio(files: list[ReencodeFile]) -> bool:
    """
    Interactive audio configuration.
    Returns True to continue, False to quit.
    """
    # Get files that will actually be processed (not DV)
    processable = [f for f in files if not f.is_dv]
    
    if not processable:
        print("No processable files (all are Dolby Vision).")
        return False
    
    # Check if all files share the same audio layout
    signatures = {}
    for rf in processable:
        sig = rf.audio_signature
        if sig not in signatures:
            signatures[sig] = []
        signatures[sig].append(rf)
    
    print()
    print("=" * 60)
    print("Audio Configuration")
    print("=" * 60)
    print()
    
    if len(signatures) == 1:
        # All files share same layout
        template_file = processable[0]
        apply_default_audio_selection(template_file.audio_tracks)
        
        print(f"All {len(processable)} processable files share the same audio layout:")
        print()
        display_audio_tracks(template_file.audio_tracks)
        print()
        
        while True:
            selected_count = sum(1 for t in template_file.audio_tracks if t.selected)
            print(f"Selected: {selected_count}/{len(template_file.audio_tracks)} tracks")
            print()
            print("Commands:")
            print("  [a]ccept  - Use this selection for all files")
            print("  [1-9]     - Toggle track by number")
            print("  [q]uit    - Exit without processing")
            print()
            
            try:
                cmd = input("audio> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            
            if cmd == 'q':
                return False
            elif cmd == 'a':
                if selected_count == 0:
                    print("Must select at least one audio track!")
                    continue
                apply_audio_selection_to_all(files, template_file.audio_tracks)
                return True
            elif cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(template_file.audio_tracks):
                    template_file.audio_tracks[idx].selected = not template_file.audio_tracks[idx].selected
                    print()
                    display_audio_tracks(template_file.audio_tracks)
                    print()
                else:
                    print(f"Invalid number. Enter 1-{len(template_file.audio_tracks)}")
            else:
                print("Unknown command")
    
    else:
        # Multiple layouts detected
        print(f"Warning: Audio layouts differ across files!")
        print()
        
        sorted_sigs = sorted(signatures.items(), key=lambda x: -len(x[1]))
        
        for i, (sig, sig_files) in enumerate(sorted_sigs):
            label = chr(ord('A') + i)
            print(f"  Layout {label} ({len(sig_files)} files):")
            # Show tracks from first file in this group
            for track in sig_files[0].audio_tracks:
                print(f"    - {track}")
            print()
        
        print("  Inconsistent files:")
        # Show files not in the largest group
        main_sig = sorted_sigs[0][0]
        for rf in processable:
            if rf.audio_signature != main_sig:
                print(f"    - {rf.path.name}")
        print()
        
        while True:
            print("Commands:")
            print("  [c]ontinue - Use automatic selection per-file")
            print("  [e]xclude  - Exclude inconsistent files from selection")
            print("  [q]uit     - Exit to review manually")
            print()
            
            try:
                cmd = input("audio> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            
            if cmd == 'q':
                return False
            elif cmd == 'c':
                # Apply default selection to each file individually
                for rf in files:
                    apply_default_audio_selection(rf.audio_tracks)
                return True
            elif cmd == 'e':
                # Apply default and exclude inconsistent files
                for rf in files:
                    apply_default_audio_selection(rf.audio_tracks)
                    if rf.audio_signature != main_sig and not rf.is_dv:
                        rf.selected = False
                        rf.skip_reason = "Inconsistent audio layout"
                return True
            else:
                print("Unknown command")
    
    return True


# =============================================================================
# Phase 2b: Default Track Configuration
# =============================================================================

def configure_defaults(files: list[ReencodeFile]) -> bool:
    """
    Configure default audio and subtitle tracks.
    Returns True to continue, False to quit.
    """
    processable = [f for f in files if not f.is_dv and f.selected]
    
    if not processable:
        return True
    
    # Use first file as reference for display
    ref_file = processable[0]
    selected_audio = ref_file.selected_audio_tracks
    subtitle_tracks = ref_file.subtitle_tracks
    
    # Check if subtitle layouts are consistent
    sub_signatures = {}
    for rf in processable:
        sig = rf.subtitle_signature
        if sig not in sub_signatures:
            sub_signatures[sig] = []
        sub_signatures[sig].append(rf)
    
    subtitles_consistent = len(sub_signatures) == 1
    
    # Initialize defaults: first audio track, no subtitle default
    default_audio_idx = 0
    default_subtitle_idx = None  # None means no default
    
    print()
    print("=" * 60)
    print("Default Track Configuration")
    print("=" * 60)
    
    while True:
        print()
        print("Selected audio tracks:")
        for i, track in enumerate(selected_audio):
            marker = " *" if i == default_audio_idx else ""
            print(f"  {i+1}. {track}{marker}")
        
        if default_audio_idx is not None and default_audio_idx < len(selected_audio):
            audio_lang = selected_audio[default_audio_idx].language
            print(f"\n  Default audio: Track {default_audio_idx + 1} ({audio_lang.upper()})")
        
        print()
        
        if not subtitle_tracks:
            print("Subtitle tracks: None")
        else:
            print("Subtitle tracks:")
            if not subtitles_consistent:
                print("  [!] Warning: Subtitle layouts differ across files")
                print(f"      ({len(sub_signatures)} different layouts detected)")
                print("      Default will apply by language where available")
                print()
            
            for i, track in enumerate(subtitle_tracks):
                marker = " *" if i == default_subtitle_idx else ""
                print(f"  {i+1}. {track}{marker}")
            
            if default_subtitle_idx is not None:
                sub_lang = subtitle_tracks[default_subtitle_idx].language
                print(f"\n  Default subtitle: Track {default_subtitle_idx + 1} ({sub_lang.upper()})")
            else:
                print(f"\n  Default subtitle: None")
        
        print()
        print("Commands:")
        print("  a <num>       - Set default audio (e.g., 'a 1')")
        if subtitle_tracks:
            print("  s <num|none>  - Set default subtitle (e.g., 's 1' or 's none')")
        print("  [c]ontinue    - Accept and continue")
        print("  [q]uit        - Exit without processing")
        print()
        
        try:
            cmd = input("defaults> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        
        if cmd == 'q':
            return False
        elif cmd == 'c':
            # Apply defaults to all files
            if default_audio_idx is not None and default_audio_idx < len(selected_audio):
                audio_lang = selected_audio[default_audio_idx].language
                for rf in files:
                    rf.default_audio_lang = audio_lang
            
            if default_subtitle_idx is not None and default_subtitle_idx < len(subtitle_tracks):
                sub_lang = subtitle_tracks[default_subtitle_idx].language
                for rf in files:
                    rf.default_subtitle_lang = sub_lang
            else:
                for rf in files:
                    rf.default_subtitle_lang = ""
            
            return True
        elif cmd.startswith('a '):
            try:
                idx = int(cmd[2:].strip()) - 1
                if 0 <= idx < len(selected_audio):
                    default_audio_idx = idx
                else:
                    print(f"Invalid track number. Enter 1-{len(selected_audio)}")
            except ValueError:
                print("Invalid input. Use 'a <number>'")
        elif cmd.startswith('s '):
            if not subtitle_tracks:
                print("No subtitle tracks available")
                continue
            arg = cmd[2:].strip()
            if arg == 'none':
                default_subtitle_idx = None
            else:
                try:
                    idx = int(arg) - 1
                    if 0 <= idx < len(subtitle_tracks):
                        default_subtitle_idx = idx
                    else:
                        print(f"Invalid track number. Enter 1-{len(subtitle_tracks)} or 'none'")
                except ValueError:
                    print("Invalid input. Use 's <number>' or 's none'")
        else:
            print("Unknown command")


# =============================================================================
# Phase 3: File Selection
# =============================================================================

def display_files(files: list[ReencodeFile], source_dir: Path):
    """Display file list with status."""
    print()
    print("=" * 60)
    print("File Selection")
    print("=" * 60)
    print()
    
    total_size = sum(f.size_gb for f in files if f.selected)
    
    for i, rf in enumerate(files):
        try:
            rel_path = rf.path.relative_to(source_dir)
        except ValueError:
            rel_path = rf.path.name
        
        sel = "[x]" if rf.selected else "[ ]"
        
        # Build flags
        flags = []
        if rf.is_dv:
            flags.append("[DV]")
        elif rf.is_hdr:
            flags.append("[HDR]")
        if rf.skip_reason and not rf.is_dv:
            flags.append(f"[!]")
        if rf.codec == "hevc":
            flags.append("[x265]")
        
        flag_str = " ".join(flags)
        
        print(f"  {i+1:2}. {sel} {flag_str:12} {rel_path}")
        
        # Info line
        audio_summary = f"{len(rf.selected_audio_indices)}/{len(rf.audio_tracks)} audio"
        info = f"{rf.codec} | {rf.hdr_status} | {audio_summary} | {rf.size_gb:.2f} GB"
        
        if rf.skip_reason:
            info += f" | Skip: {rf.skip_reason}"
        
        print(f"          {info}")
    
    print()
    print(f"Archive location: {ARCHIVE_BASE}")
    print(f"Log file: {LOG_FILE}")
    print()


def interactive_selection(files: list[ReencodeFile], source_dir: Path) -> bool:
    """
    Interactive file selection loop.
    Returns True to proceed, False to quit.
    """
    while True:
        display_files(files, source_dir)
        
        selected = [f for f in files if f.selected]
        selected_count = len(selected)
        total_size = sum(f.size_gb for f in selected)
        dv_count = sum(1 for f in files if f.is_dv)
        
        print(f"Selected: {selected_count}/{len(files)} files ({total_size:.2f} GB)")
        if dv_count > 0:
            print(f"Dolby Vision (excluded): {dv_count}")
        print()
        print("Commands:")
        print("  [a]ll      - Select all (except DV)")
        print("  [n]one     - Deselect all")
        print("  [i]nvert   - Invert selection")
        print("  [1-99]     - Toggle file by number")
        print("  [g]o       - Start encoding")
        print("  [q]uit     - Exit without processing")
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
                if not f.is_dv:
                    f.selected = True
        elif cmd == 'n':
            for f in files:
                f.selected = False
        elif cmd == 'i':
            for f in files:
                if not f.is_dv:
                    f.selected = not f.selected
        elif cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(files):
                rf = files[idx]
                if rf.is_dv:
                    print("Cannot select Dolby Vision files (DV metadata would be lost)")
                else:
                    rf.selected = not rf.selected
            else:
                print(f"Invalid number. Enter 1-{len(files)}")
        else:
            print("Unknown command")


# =============================================================================
# Phase 4: Encoding
# =============================================================================

def encode_file(
    source: Path,
    x265_params: list[str],
    audio_indices: list[int],
    default_audio_idx: int | None = 0,
    default_subtitle_idx: int | None = None
) -> bool:
    """Encode a single file. Returns True on success."""
    temp_output = source.with_suffix(".tmp.mkv")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i", str(source),
        "-map", "0:v",
    ]
    
    for idx in audio_indices:
        cmd.extend(["-map", f"0:{idx}"])
    
    cmd.extend([
        "-map", "0:s?",
        "-map", "0:t?",
        "-map", "0:d?",
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-c:v", "libx265",
        "-crf", "14",
        "-preset", "slow",
        "-profile:v", "main10",
        "-pix_fmt", "yuv420p10le",
    ])
    
    if x265_params:
        cmd.extend(["-x265-params", ":".join(x265_params)])
    
    cmd.extend([
        "-c:a", "flac",
        "-c:s", "copy",
        "-c:t", "copy",
        "-c:d", "copy",
    ])
    
    # Set audio track dispositions
    for i in range(len(audio_indices)):
        if i == default_audio_idx:
            cmd.extend([f"-disposition:a:{i}", "default"])
        else:
            cmd.extend([f"-disposition:a:{i}", "0"])
    
    # Set subtitle track dispositions
    if default_subtitle_idx is not None:
        # Clear all first, then set the default
        cmd.extend(["-disposition:s", "0"])
        cmd.extend([f"-disposition:s:{default_subtitle_idx}", "default"])
    else:
        # Clear all subtitle defaults
        cmd.extend(["-disposition:s", "0"])
    
    cmd.extend([
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
                print(f"\r  {line.strip()[:100]}", end="", flush=True)

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


def process_file(rf: ReencodeFile) -> str:
    """
    Encode, archive original, replace with new file.
    Returns: 'success', 'skipped', or 'failed'
    """
    source = rf.path
    temp_output = source.with_suffix(".tmp.mkv")
    archive_path = get_archive_path(source)

    audio_indices = rf.selected_audio_indices
    default_audio_idx = rf.get_default_audio_index()
    default_subtitle_idx = rf.get_default_subtitle_index()
    
    log(f"Processing: {source.name} (codec: {rf.codec}, {rf.hdr_status})")
    log(f"  Audio: {len(rf.audio_tracks)} tracks -> {len(audio_indices)} selected (default: {default_audio_idx})")
    log(f"  Subtitles: {len(rf.subtitle_tracks)} tracks (default: {default_subtitle_idx})")
    
    if rf.x265_params:
        log(f"  HDR params: {':'.join(rf.x265_params)}")

    # Encode
    if not encode_file(source, rf.x265_params, audio_indices, default_audio_idx, default_subtitle_idx):
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


def process_files(files: list[ReencodeFile], source_dir: Path):
    """Process all selected files."""
    selected = [f for f in files if f.selected]
    
    total_size = sum(f.size_gb for f in selected)
    
    print()
    print("=" * 60)
    print(f"Starting encode: {len(selected)} files ({total_size:.2f} GB)")
    print("=" * 60)
    print()
    
    confirm = input("Final confirmation - proceed? [y/N]: ").strip().lower()
    if confirm != 'y':
        print("Aborted.")
        return
    
    log("=" * 60)
    log(f"Starting batch encode: {len(selected)} files in {source_dir}")
    log("=" * 60)

    success = 0
    failed = 0

    for i, rf in enumerate(selected, 1):
        try:
            rel_path = rf.path.relative_to(source_dir)
        except ValueError:
            rel_path = rf.path.name
        log(f"[{i}/{len(selected)}] {rel_path}")

        result = process_file(rf)
        
        if result == "success":
            success += 1
        else:
            failed += 1

    log("=" * 60)
    log(f"Complete:")
    log(f"  Encoded: {success}")
    log(f"  Failed:  {failed}")
    log("=" * 60)


# =============================================================================
# Main
# =============================================================================

def check_dependencies():
    """Verify required tools are installed."""
    for tool in ['ffmpeg', 'ffprobe']:
        try:
            subprocess.run([tool, '-version'], capture_output=True, check=True)
        except FileNotFoundError:
            print(f"Error: '{tool}' not found. Please install ffmpeg.")
            sys.exit(1)


def main():
    check_dependencies()
    
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <directory>")
        sys.exit(1)

    source_dir = Path(sys.argv[1]).expanduser().resolve()
    
    if not source_dir.exists():
        print(f"Error: Directory does not exist: {source_dir}")
        sys.exit(1)
    
    if not source_dir.is_dir():
        print(f"Error: {source_dir} is not a directory")
        sys.exit(1)

    print(f"Directory: {source_dir}")
    print()

    # Phase 1: Scan
    files = scan_files(source_dir)
    
    if not files:
        print("No MKV files found")
        sys.exit(0)

    # Phase 2: Audio configuration
    if not configure_audio(files):
        print("Cancelled.")
        sys.exit(0)

    # Phase 2b: Default track configuration
    if not configure_defaults(files):
        print("Cancelled.")
        sys.exit(0)

    # Phase 3: File selection
    if not interactive_selection(files, source_dir):
        print("Cancelled.")
        sys.exit(0)

    # Phase 4: Process
    process_files(files, source_dir)


if __name__ == "__main__":
    main()
