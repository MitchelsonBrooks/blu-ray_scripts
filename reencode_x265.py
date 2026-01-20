#!/usr/bin/env python3
"""
Interactive batch MKV re-encoder for fixing timestamp issues.

Encodes video to x265 CRF 12 (10-bit) and audio to FLAC.
Preserves HDR metadata. Skips Dolby Vision content.
Configurable audio track selection with lossless preference.
Archives originals before replacing.

Usage:
    python3 reencode_x265.py /tank/media/anime/Show/
    python3 reencode_x265.py -t /tank/media/anime/Show/

Options:
    -t, --timestamps    Analyze files for timestamp issues (slower but thorough).
                        Detects non-monotonic DTS/PTS, discontinuities, and
                        negative timestamps in video and audio streams.
"""

import subprocess
import sys
import shutil
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
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
    is_default: bool = False
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
    original_width: int = 0
    original_height: int = 0
    duration_seconds: float = 0.0
    detected_crop: str = ""           # Detected crop string (e.g., "1920:800:0:140")
    enable_crop: bool = False         # Whether to apply crop during encode
    selected: bool = True
    skip_reason: str = ""
    default_audio_lang: str = ""      # Language code for default audio
    default_subtitle_lang: str = ""   # Language code for default subtitle ("" = none)
    timestamp_issues: list = None     # List of TimestampIssue objects (None = not checked)

    @property
    def has_timestamp_issues(self) -> bool:
        """Check if file has any detected timestamp issues."""
        return self.timestamp_issues is not None and len(self.timestamp_issues) > 0

    @property
    def timestamp_issue_count(self) -> int:
        """Total count of timestamp issues across all streams."""
        if not self.timestamp_issues:
            return 0
        return sum(issue.count for issue in self.timestamp_issues)

    @property
    def timestamp_summary(self) -> str:
        """Brief summary of timestamp issues."""
        if not self.timestamp_issues:
            return "OK"

        video_issues = [i for i in self.timestamp_issues if i.stream_type == "video"]
        audio_issues = [i for i in self.timestamp_issues if i.stream_type == "audio"]

        parts = []
        if video_issues:
            total = sum(i.count for i in video_issues)
            parts.append(f"video:{total}")
        if audio_issues:
            total = sum(i.count for i in audio_issues)
            parts.append(f"audio:{total}")

        return ", ".join(parts) if parts else "OK"

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
    
    @property
    def crop_dimensions(self) -> tuple[int, int, int, int] | None:
        """Parse crop string into (w, h, x, y) tuple."""
        if not self.detected_crop:
            return None
        try:
            parts = self.detected_crop.split(":")
            return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
        except (ValueError, IndexError):
            return None
    
    @property
    def resolution_str(self) -> str:
        """Current resolution as string."""
        if self.original_width and self.original_height:
            return f"{self.original_width}x{self.original_height}"
        return "?"
    
    @property
    def output_resolution_str(self) -> str:
        """Output resolution after crop (or same if no crop)."""
        if self.enable_crop and self.crop_dimensions:
            w, h, _, _ = self.crop_dimensions
            return f"{w}x{h}"
        return self.resolution_str
    
    @property
    def is_already_x265(self) -> bool:
        """Check if file is already x265/HEVC encoded."""
        return self.codec in ("hevc", "h265")
    
    @property
    def needs_video_reencode(self) -> bool:
        """Check if video needs to be re-encoded (not just remuxed)."""
        # Need re-encode if: not hevc, or crop enabled
        if not self.is_already_x265:
            return True
        if self.enable_crop:
            return True
        return False
    
    @property
    def needs_audio_processing(self) -> bool:
        """Check if audio tracks need processing (selection change or codec conversion)."""
        # Check if any selected tracks are not already FLAC
        for track in self.selected_audio_tracks:
            if track.codec.upper() != "FLAC":
                return True
        # Check if track count changed
        if len(self.selected_audio_tracks) != len(self.audio_tracks):
            return True
        return False

    @property
    def needs_disposition_update(self) -> bool:
        """Check if default track dispositions need updating."""
        selected = self.selected_audio_tracks
        if not selected:
            return False

        # Check audio default
        current_default_audio = None
        for i, track in enumerate(selected):
            if track.is_default:
                current_default_audio = track.language
                break

        target_default_audio = self.default_audio_lang or (selected[0].language if selected else None)
        if current_default_audio != target_default_audio:
            return True

        # Check subtitle default
        current_default_sub = None
        for track in self.subtitle_tracks:
            if track.is_default:
                current_default_sub = track.language
                break

        if current_default_sub != self.default_subtitle_lang:
            return True

        return False

    @property
    def processing_mode(self) -> str:
        """Determine processing mode: 'reencode', 'remux', or 'skip'."""
        if self.needs_video_reencode:
            return "reencode"
        elif self.needs_audio_processing or self.needs_disposition_update:
            return "remux"
        else:
            return "skip"
    
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
    """Get comprehensive video stream info including resolution."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,color_transfer,color_primaries,color_space,pix_fmt",
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
        "-show_entries", "stream=index,codec_name,profile,channels:stream_tags=language,title:stream_disposition=default",
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
        disposition = stream.get("disposition", {})

        is_lossless = identifier in LOSSLESS_AUDIO or codec in LOSSLESS_AUDIO
        is_lossy = identifier in LOSSY_AUDIO or codec in LOSSY_AUDIO

        track = AudioTrack(
            index=stream.get("index", 0),
            language=stream.get("tags", {}).get("language", "und"),
            channels=stream.get("channels", 0),
            codec=codec,
            profile=profile,
            is_lossless=is_lossless,
            is_lossy=is_lossy,
            is_default=disposition.get("default", 0) == 1
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
            is_default=disposition.get("default", 0) == 1,
        )
        tracks.append(track)

    return tracks


def get_duration(source: Path) -> float:
    """Get video duration in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(source)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
        return 0.0


@dataclass
class TimestampIssue:
    """Represents a detected timestamp issue."""
    stream_type: str  # "video" or "audio"
    stream_index: int
    issue_type: str   # "non_monotonic", "discontinuity", "negative", "invalid"
    count: int
    sample_messages: list[str]

    def __str__(self) -> str:
        return f"{self.stream_type}:{self.stream_index} - {self.issue_type} ({self.count}x)"


def detect_timestamp_issues(source: Path, duration: float, sample_duration: float = 60.0) -> list[TimestampIssue]:
    """
    Detect timestamp issues in video and audio streams.

    Decodes portions of the file and captures FFmpeg warnings about:
    - Non-monotonic DTS/PTS
    - Timestamp discontinuities
    - Negative timestamps
    - Invalid timestamps

    Uses accurate seeking (-ss after -i) to ensure all frames are processed
    and timestamp issues are not skipped due to keyframe seeking.

    Args:
        source: Path to the video file
        duration: Total duration in seconds
        sample_duration: How many seconds to sample per point

    Returns:
        List of TimestampIssue objects describing problems found
    """
    import re

    # Patterns to match timestamp issues in FFmpeg output
    # These patterns are matched case-insensitively
    patterns = {
        "non_monotonic": [
            r"DTS .* < .* out of order",
            r"non monotonically increasing dts",
            r"Non-monotonous DTS",
            r"Application provided invalid.*timestamps",
            r"pts .* < .* invalid",
            r"dts .* < .* invalid",
            r"out of order dts",
            r"packet with invalid pts",
        ],
        "discontinuity": [
            r"discont.*detected",
            r"discontinuity.*detected",
            r"DTS discontinuity",
            r"PTS discontinuity",
            r"discarding .* with pts=",
            r"discarding frame due to timestamp",
        ],
        "negative": [
            r"discarding.*negative",
            r"negative.*timestamp",
            r"clipping.*timestamp",
            r"discarding packet.*before start",
        ],
        "invalid": [
            r"discarding corrupted packet",
            r"Invalid NAL unit",
            r"decode_slice_header error",
            r"Invalid timestamp",
            r"discarding.*packet",
        ],
    }

    # Compile patterns
    compiled_patterns = {}
    for issue_type, pattern_list in patterns.items():
        compiled_patterns[issue_type] = [re.compile(p, re.IGNORECASE) for p in pattern_list]

    # Pattern to extract stream info from FFmpeg output
    stream_pattern = re.compile(r"Stream #\d+:(\d+)|Audio:|Video:")

    issues_by_key: dict[tuple[str, int, str], list[str]] = {}

    def analyze_output(stderr_text: str):
        """Parse FFmpeg stderr for timestamp issues."""
        lines = stderr_text.split('\n')

        # Track which stream we're looking at
        current_stream_type = "video"
        current_stream_idx = 0

        for line in lines:
            # Try to identify stream context from the line itself
            line_lower = line.lower()
            if "video" in line_lower or ":v" in line_lower or "v:" in line_lower:
                current_stream_type = "video"
            elif "audio" in line_lower or ":a" in line_lower or "a:" in line_lower:
                current_stream_type = "audio"

            stream_match = stream_pattern.search(line)
            if stream_match and stream_match.group(1):
                current_stream_idx = int(stream_match.group(1))

            # Check for timestamp issues
            for issue_type, pattern_list in compiled_patterns.items():
                for pattern in pattern_list:
                    if pattern.search(line):
                        key = (current_stream_type, current_stream_idx, issue_type)
                        if key not in issues_by_key:
                            issues_by_key[key] = []
                        if len(issues_by_key[key]) < 3:  # Keep sample messages
                            issues_by_key[key].append(line.strip()[:100])
                        else:
                            issues_by_key[key].append("")  # Count only
                        break

    # For short files (< 5 min), scan the entire file
    # For longer files, sample at multiple points with accurate seeking
    if duration <= 300:
        # Short file: scan entirely
        sample_configs = [(None, None)]  # No seeking, no duration limit
    else:
        # Longer file: sample at start, 25%, 50%, 75%, and end
        # Use longer sample duration (90s) for better coverage
        sample_duration = max(sample_duration, 90.0)
        sample_configs = [
            (None, sample_duration),  # Start (no seek, accurate)
            (duration * 0.25, sample_duration),
            (duration * 0.50, sample_duration),
            (duration * 0.75, sample_duration),
            (max(0, duration - sample_duration), sample_duration),  # End
        ]

    for start_time, duration_limit in sample_configs:
        # Build FFmpeg command
        # IMPORTANT: -ss AFTER -i for accurate frame-by-frame decoding
        # This ensures we don't skip timestamp issues due to keyframe seeking
        cmd = [
            "ffmpeg",
            "-v", "warning",
            "-i", str(source),
        ]

        # Add accurate seek if specified (after input for frame-accurate)
        if start_time is not None:
            cmd.extend(["-ss", str(start_time)])

        # Add duration limit if specified
        if duration_limit is not None:
            cmd.extend(["-t", str(duration_limit)])

        cmd.extend([
            "-f", "null",
            "-"
        ])

        try:
            # Increase timeout for accurate seeking which is slower
            timeout = 300 if start_time is not None else 600
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            analyze_output(result.stderr)
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            continue

    # Convert to TimestampIssue objects
    issues = []
    for (stream_type, stream_idx, issue_type), messages in issues_by_key.items():
        sample_msgs = [m for m in messages if m][:3]
        issues.append(TimestampIssue(
            stream_type=stream_type,
            stream_index=stream_idx,
            issue_type=issue_type,
            count=len(messages),
            sample_messages=sample_msgs
        ))

    return issues


def detect_crop(source: Path, duration: float, num_samples: int = 32) -> str:
    """
    Detect crop values by sampling multiple points in the video.
    Returns crop string like "1920:800:0:140" or empty string if no crop needed.

    Filters out invalid crops (letterboxed scenes, credits) by requiring
    at least one dimension to stay at 90%+ of original.
    """
    if duration <= 0:
        return ""

    # Guard against invalid num_samples
    if num_samples < 2:
        num_samples = 2
    
    # Get original resolution first
    res_cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(source)
    ]
    try:
        res_result = subprocess.run(res_cmd, capture_output=True, text=True)
        res_data = json.loads(res_result.stdout)
        res_stream = res_data.get("streams", [{}])[0]
        orig_w = res_stream.get("width", 0)
        orig_h = res_stream.get("height", 0)
    except Exception:
        return ""
    
    if orig_w == 0 or orig_h == 0:
        return ""
    
    # Generate sample points, avoiding first/last 5% of video
    start_pct = 0.05
    end_pct = 0.95
    sample_points = []
    for i in range(num_samples):
        pct = start_pct + (end_pct - start_pct) * i / (num_samples - 1)
        sample_points.append(duration * pct)
    
    crop_values = []
    
    for seek_time in sample_points:
        cmd = [
            "ffmpeg",
            "-ss", str(seek_time),
            "-i", str(source),
            "-vf", "cropdetect=round=2:limit=24",
            "-frames:v", "3",
            "-f", "null",
            "-"
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            # Parse cropdetect output from stderr
            for line in result.stderr.split('\n'):
                if "crop=" in line:
                    # Extract crop value: crop=1920:800:0:140
                    match = line.split("crop=")[-1].split()[0]
                    if match and ":" in match:
                        crop_values.append(match)
        except (subprocess.TimeoutExpired, Exception):
            continue
    
    if not crop_values:
        return ""
    
    # Filter to valid crops: at least one dimension must be >= 90% of original
    # This filters out letterboxed scenes within content while keeping:
    # - Pillarboxing (4:3 in 16:9): width reduced, height ~100%
    # - Letterboxing (2.35:1 in 16:9): height reduced, width ~100%
    valid_crops = []
    for crop in crop_values:
        try:
            parts = crop.split(":")
            w, h = int(parts[0]), int(parts[1])
            width_pct = w / orig_w
            height_pct = h / orig_h
            if width_pct >= 0.90 or height_pct >= 0.90:
                valid_crops.append(crop)
        except (ValueError, IndexError):
            continue
    
    if not valid_crops:
        return ""

    # Find most common valid crop value
    crop_counter = Counter(valid_crops)
    most_common_crop, count = crop_counter.most_common(1)[0]
    
    # Parse the crop to check if it's meaningful
    try:
        parts = most_common_crop.split(":")
        w, h, x, y = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
        
        # Only report crop if it removes at least 20 pixels on any edge
        # and at least 70% agreement among valid samples
        if (x > 10 or y > 10 or (orig_w - w - x) > 10 or (orig_h - h - y) > 10):
            if count >= len(valid_crops) * 0.7:
                return most_common_crop
    except (ValueError, IndexError):
        pass
    
    return ""


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

    # hdr10-opt is only valid for PQ (SMPTE 2084), not HLG
    if color_transfer == "smpte2084":
        x265_params = ["hdr10-opt=1", "repeat-headers=1"]
    else:
        x265_params = ["repeat-headers=1"]
    
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

def scan_files(source_dir: Path, check_timestamps: bool = False) -> list[ReencodeFile]:
    """Scan all MKV files and analyze them."""
    mkv_paths = sorted(source_dir.glob("**/*.mkv"))

    if not mkv_paths:
        return []

    print(f"Scanning {len(mkv_paths)} MKV files...")
    if check_timestamps:
        print("  (includes crop detection and timestamp analysis)")
    else:
        print("  (includes crop detection)")
    print()

    files = []
    for i, mkv_path in enumerate(mkv_paths, 1):
        print(f"\r  [{i}/{len(mkv_paths)}] {mkv_path.name[:50]:<50}", end="", flush=True)

        # Get video info (includes codec and resolution)
        stream = probe_video(mkv_path)
        codec = stream.get("codec_name", "unknown")
        orig_width = stream.get("width", 0)
        orig_height = stream.get("height", 0)

        # Check DV first
        dv = is_dolby_vision(mkv_path)

        # Get HDR params
        x265_params, is_hdr = get_hdr_params(mkv_path)

        # Get audio tracks
        audio_tracks = get_audio_tracks(mkv_path)

        # Get subtitle tracks
        subtitle_tracks = get_subtitle_tracks(mkv_path)

        # Get duration
        duration = get_duration(mkv_path)

        # Detect crop
        detected_crop = detect_crop(mkv_path, duration)

        # File size
        size_gb = mkv_path.stat().st_size / (1024**3)

        # Detect timestamp issues (only if requested)
        timestamp_issues = None
        if check_timestamps:
            print(f"\r  [{i}/{len(mkv_paths)}] {mkv_path.name[:40]:<40} [timestamps]", end="", flush=True)
            timestamp_issues = detect_timestamp_issues(mkv_path, duration)

        rf = ReencodeFile(
            path=mkv_path,
            codec=codec,
            is_hdr=is_hdr,
            is_dv=dv,
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks,
            x265_params=x265_params,
            size_gb=size_gb,
            original_width=orig_width,
            original_height=orig_height,
            duration_seconds=duration,
            detected_crop=detected_crop,
            selected=not dv,  # Auto-deselect DV files
            skip_reason="Dolby Vision" if dv else "",
            timestamp_issues=timestamp_issues
        )
        files.append(rf)

    print("\r" + " " * 80 + "\r", end="")  # Clear line
    print(f"Scanned {len(files)} files.")

    return files


# =============================================================================
# Phase 1b: Crop Configuration
# =============================================================================

def validate_crop(crop_str: str, max_width: int = 0, max_height: int = 0) -> tuple[bool, str]:
    """
    Validate a crop string format and optionally check against max dimensions.
    Returns (is_valid, error_message).
    """
    if not crop_str:
        return False, "Empty crop value"

    if crop_str.count(":") != 3:
        return False, "Invalid format. Expected W:H:X:Y (e.g., 1440:1080:240:0)"

    try:
        parts = crop_str.split(":")
        w, h, x, y = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return False, "All values must be integers"

    if w <= 0 or h <= 0:
        return False, "Width and height must be positive"

    if x < 0 or y < 0:
        return False, "X and Y offsets cannot be negative"

    if max_width > 0 and max_height > 0:
        if w + x > max_width:
            return False, f"Crop width + X offset ({w + x}) exceeds source width ({max_width})"
        if h + y > max_height:
            return False, f"Crop height + Y offset ({h + y}) exceeds source height ({max_height})"

    return True, ""


def format_crop_info(crop_str: str) -> str:
    """Format crop string with aspect ratio info."""
    if not crop_str:
        return "None"
    
    try:
        parts = crop_str.split(":")
        w, h = int(parts[0]), int(parts[1])

        # Calculate aspect ratio
        ar_decimal = w / h
        
        # Common aspect ratio names
        ar_name = ""
        if abs(ar_decimal - 2.40) < 0.05:
            ar_name = "2.40:1 Scope"
        elif abs(ar_decimal - 2.35) < 0.05:
            ar_name = "2.35:1 Scope"
        elif abs(ar_decimal - 1.85) < 0.05:
            ar_name = "1.85:1"
        elif abs(ar_decimal - 1.78) < 0.05:
            ar_name = "16:9"
        elif abs(ar_decimal - 1.33) < 0.05:
            ar_name = "4:3"
        elif abs(ar_decimal - 2.0) < 0.05:
            ar_name = "2:1"
        else:
            ar_name = f"{ar_decimal:.2f}:1"
        
        return f"{crop_str} -> {w}x{h} ({ar_name})"
    except (ValueError, IndexError):
        return crop_str


def configure_crop(files: list[ReencodeFile]) -> bool | str:
    """
    Configure crop settings based on detected values.
    Returns True to continue, False to quit, or "perfile" for per-file mode.
    """
    processable = [f for f in files if not f.is_dv]
    
    # Find files with detected crops
    files_with_crop = [f for f in processable if f.detected_crop]
    
    if not files_with_crop:
        # No crop detected on any file, skip this phase
        return True
    
    # Group by crop value
    crop_groups: dict[str, list[ReencodeFile]] = defaultdict(list)
    for rf in files_with_crop:
        crop_groups[rf.detected_crop].append(rf)
    
    print()
    print("=" * 60)
    print("Crop Detection Results")
    print("=" * 60)
    print()

    # Get original resolution from first file for context (already stored)
    first_file = files_with_crop[0]
    if first_file.original_width and first_file.original_height:
        print(f"Original resolution: {first_file.original_width}x{first_file.original_height}")
        print()

    files_without_crop = [f for f in processable if not f.detected_crop]
    
    if len(crop_groups) == 1:
        # Consistent crop across all files with crop detected
        crop_value = list(crop_groups.keys())[0]
        print(f"Detected crop: {format_crop_info(crop_value)}")
        print()
        print(f"Consistent across {len(files_with_crop)} file(s): Yes")
        if files_without_crop:
            print(f"Files without crop detected: {len(files_without_crop)}")
        print()
        
        print("Files with detected crop:")
        for rf in files_with_crop[:10]:  # Show first 10
            print(f"  - {rf.path.name}")
        if len(files_with_crop) > 10:
            print(f"  ... and {len(files_with_crop) - 10} more")
        print()
        
        while True:
            print("Commands:")
            print("  [e]nable   - Enable crop for files with detected crop")
            if files_without_crop:
                print(f"  [a]ll      - Apply detected crop to ALL {len(processable)} files")
            print("  [o]verride - Set manual crop value for all files")
            print("  [p]erfile  - Configure crop per-file (manual mode)")
            print("  [d]isable  - Disable crop (keep black bars)")
            print("  [q]uit     - Exit")
            print()
            
            try:
                cmd = input("crop> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            
            if cmd == 'q':
                return False
            elif cmd == 'e':
                for rf in files_with_crop:
                    rf.enable_crop = True
                print(f"Crop enabled for {len(files_with_crop)} files.")
                return True
            elif cmd == 'a' and files_without_crop:
                for rf in processable:
                    rf.detected_crop = crop_value
                    rf.enable_crop = True
                print(f"Crop {crop_value} applied to all {len(processable)} files.")
                return True
            elif cmd == 'o':
                manual_crop = input("Enter crop value (W:H:X:Y): ").strip()
                is_valid, error = validate_crop(
                    manual_crop,
                    first_file.original_width,
                    first_file.original_height
                )
                if not is_valid:
                    print(error)
                    continue
                confirm = input(f"Apply {manual_crop} to all {len(processable)} files? [y/N]: ").strip().lower()
                if confirm == 'y':
                    for rf in processable:
                        rf.detected_crop = manual_crop
                        rf.enable_crop = True
                    print(f"Crop {manual_crop} applied to all {len(processable)} files.")
                    return True
            elif cmd == 'p':
                # Skip to per-file mode (handled after audio config)
                return "perfile"
            elif cmd == 'd':
                for rf in files:
                    rf.enable_crop = False
                print("Crop disabled.")
                return True
            else:
                print("Unknown command")
    
    else:
        # Inconsistent crop values
        print("Warning: Inconsistent crop values detected!")
        print()
        
        sorted_crops = sorted(crop_groups.items(), key=lambda x: -len(x[1]))
        
        for i, (crop_val, crop_files) in enumerate(sorted_crops):
            label = chr(ord('A') + i)
            print(f"  Crop {label}: {format_crop_info(crop_val)} ({len(crop_files)} files)")
            for rf in crop_files[:3]:
                print(f"    - {rf.path.name}")
            if len(crop_files) > 3:
                print(f"    ... and {len(crop_files) - 3} more")
            print()
        
        if files_without_crop:
            print(f"  No crop detected: {len(files_without_crop)} files")
            print()
        
        majority_crop = sorted_crops[0][0]
        majority_files = sorted_crops[0][1]
        
        while True:
            print("Commands:")
            print(f"  [m]ajority - Enable crop using majority value ({len(majority_files)} files)")
            print(f"  [a]ll      - Apply majority crop to ALL {len(processable)} files")
            print("  [o]verride - Set manual crop value for all files")
            print("  [p]erfile  - Configure crop per-file (manual mode)")
            print("  [d]isable  - Disable crop for all files")
            print("  [e]xclude  - Exclude files with non-majority crop from selection")
            print("  [q]uit     - Exit")
            print()
            
            try:
                cmd = input("crop> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            
            if cmd == 'q':
                return False
            elif cmd == 'm':
                for rf in majority_files:
                    rf.enable_crop = True
                print(f"Crop enabled for {len(majority_files)} files with majority crop value.")
                return True
            elif cmd == 'a':
                for rf in processable:
                    rf.detected_crop = majority_crop
                    rf.enable_crop = True
                print(f"Crop {majority_crop} applied to all {len(processable)} files.")
                return True
            elif cmd == 'o':
                manual_crop = input("Enter crop value (W:H:X:Y): ").strip()
                is_valid, error = validate_crop(
                    manual_crop,
                    first_file.original_width,
                    first_file.original_height
                )
                if not is_valid:
                    print(error)
                    continue
                confirm = input(f"Apply {manual_crop} to all {len(processable)} files? [y/N]: ").strip().lower()
                if confirm == 'y':
                    for rf in processable:
                        rf.detected_crop = manual_crop
                        rf.enable_crop = True
                    print(f"Crop {manual_crop} applied to all {len(processable)} files.")
                    return True
            elif cmd == 'p':
                return "perfile"
            elif cmd == 'd':
                for rf in files:
                    rf.enable_crop = False
                print("Crop disabled for all files.")
                return True
            elif cmd == 'e':
                for rf in majority_files:
                    rf.enable_crop = True
                # Exclude non-majority files
                for crop_val, crop_files in sorted_crops[1:]:
                    for rf in crop_files:
                        rf.selected = False
                        rf.skip_reason = "Inconsistent crop"
                print(f"Crop enabled for majority. {sum(len(cf) for _, cf in sorted_crops[1:])} files excluded.")
                return True
            else:
                print("Unknown command")
    
    return True


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


def configure_audio(files: list[ReencodeFile]) -> bool | str:
    """
    Interactive audio configuration.
    Returns True to continue, False to quit, or "perfile" for per-file mode.
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
            print("  [m]anual  - Configure each file individually")
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
            elif cmd == 'm':
                return "perfile"
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
        
        # Check if there's a clear majority
        total_files = len(processable)
        largest_group = len(sorted_sigs[0][1])
        clear_majority = largest_group / total_files > 0.5
        
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
        
        # Auto-recommend per-file mode if no clear majority
        if not clear_majority:
            print("  No clear majority layout detected.")
            print("  Recommend: [m]anual per-file configuration")
            print()
        
        while True:
            print("Commands:")
            print("  [c]ontinue - Use automatic selection per-file")
            print("  [m]anual   - Configure each file individually")
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
            elif cmd == 'm':
                return "perfile"
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
# Phase 2c: Per-File Configuration (Manual Mode)
# =============================================================================

def configure_file_individually(rf: ReencodeFile, file_num: int, total_files: int) -> str | None:
    """
    Configure a single file's settings interactively.
    Returns: 'next', 'prev', 'done', or None (quit)
    """
    while True:
        print()
        print("=" * 60)
        print(f"File {file_num}/{total_files}: {rf.path.name}")
        print("=" * 60)
        print(f"Now: {rf.codec} | {rf.resolution_str} | {rf.size_gb:.2f} GB")
        print()
        
        # Audio tracks
        print("Audio tracks:")
        selected_audio = rf.selected_audio_tracks
        
        # Find which track is the default (first selected track with default_audio_lang)
        default_audio_track_idx = None
        if rf.default_audio_lang:
            for i, track in enumerate(rf.audio_tracks):
                if track.selected and track.language == rf.default_audio_lang:
                    default_audio_track_idx = i
                    break
        elif selected_audio:
            # If no default_audio_lang set, first selected track is default
            for i, track in enumerate(rf.audio_tracks):
                if track.selected:
                    default_audio_track_idx = i
                    break
        
        for i, track in enumerate(rf.audio_tracks):
            sel = "[x]" if track.selected else "[ ]"
            default_marker = " <- default" if i == default_audio_track_idx else ""
            print(f"  {i+1}. {sel} {track}{default_marker}")

        print()
        
        # Subtitle tracks
        if rf.subtitle_tracks:
            print("Subtitle tracks:")
            default_sub_marked = False
            for i, track in enumerate(rf.subtitle_tracks):
                # Only mark the first track with matching language as default
                if rf.default_subtitle_lang == track.language and not default_sub_marked:
                    default_marker = " <- default"
                    default_sub_marked = True
                else:
                    default_marker = ""
                print(f"  {i+1}. {track}{default_marker}")
            print()
        else:
            print("Subtitle tracks: None")
            print()
        
        # Crop
        if rf.enable_crop:
            print(f"Crop: ENABLED ({rf.detected_crop})")
        elif rf.detected_crop:
            print(f"Crop: disabled (detected: {rf.detected_crop})")
        else:
            print("Crop: disabled (none detected)")
        
        print()
        print("Commands:")
        print("  a <num>       - Toggle audio track")
        print("  ad <num>      - Set default audio (among selected)")
        if rf.subtitle_tracks:
            print("  s <num|none>  - Set default subtitle")
        if rf.detected_crop:
            print("  crop on       - Enable detected crop")
        print("  crop off      - Disable crop")
        print("  crop W:H:X:Y  - Set manual crop")
        print("  [n]ext        - Next file")
        if file_num > 1:
            print("  [p]rev        - Previous file")
        print("  [d]one        - Finish, go to file selection")
        print("  [q]uit        - Exit")
        print()
        
        try:
            cmd = input(f"[{file_num}/{total_files}]> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        
        if cmd == 'q':
            return None
        elif cmd == 'n':
            return 'next'
        elif cmd == 'p' and file_num > 1:
            return 'prev'
        elif cmd == 'd':
            return 'done'
        elif cmd.startswith('a ') and not cmd.startswith('ad '):
            try:
                idx = int(cmd[2:].strip()) - 1
                if 0 <= idx < len(rf.audio_tracks):
                    rf.audio_tracks[idx].selected = not rf.audio_tracks[idx].selected
                    # Ensure at least one track selected
                    if not any(t.selected for t in rf.audio_tracks):
                        rf.audio_tracks[idx].selected = True
                        print("Must keep at least one audio track selected")
                else:
                    print(f"Invalid track number. Enter 1-{len(rf.audio_tracks)}")
            except ValueError:
                print("Invalid input. Use 'a <number>'")
        elif cmd.startswith('ad '):
            try:
                idx = int(cmd[3:].strip()) - 1
                selected = [t for t in rf.audio_tracks if t.selected]
                if 0 <= idx < len(selected):
                    rf.default_audio_lang = selected[idx].language
                else:
                    print(f"Invalid track number. Enter 1-{len(selected)}")
            except ValueError:
                print("Invalid input. Use 'ad <number>'")
        elif cmd.startswith('s '):
            if not rf.subtitle_tracks:
                print("No subtitle tracks available")
                continue
            arg = cmd[2:].strip()
            if arg == 'none':
                rf.default_subtitle_lang = ""
            else:
                try:
                    idx = int(arg) - 1
                    if 0 <= idx < len(rf.subtitle_tracks):
                        rf.default_subtitle_lang = rf.subtitle_tracks[idx].language
                    else:
                        print(f"Invalid track number. Enter 1-{len(rf.subtitle_tracks)} or 'none'")
                except ValueError:
                    print("Invalid input. Use 's <number>' or 's none'")
        elif cmd == 'crop on':
            if rf.detected_crop:
                rf.enable_crop = True
            else:
                print("No crop detected. Use 'crop W:H:X:Y' to set manually.")
        elif cmd == 'crop off':
            rf.enable_crop = False
        elif cmd.startswith('crop '):
            crop_val = cmd[5:].strip()
            is_valid, error = validate_crop(
                crop_val,
                rf.original_width,
                rf.original_height
            )
            if is_valid:
                rf.detected_crop = crop_val
                rf.enable_crop = True
            else:
                print(error)
        else:
            print("Unknown command")


def configure_per_file(files: list[ReencodeFile]) -> bool:
    """
    Per-file configuration mode.
    Returns True to continue, False to quit.
    """
    processable = [f for f in files if not f.is_dv]
    
    if not processable:
        return True
    
    # Apply default audio selection to all files first
    for rf in processable:
        apply_default_audio_selection(rf.audio_tracks)
        # Set default_audio_lang to first selected track's language
        selected = [t for t in rf.audio_tracks if t.selected]
        if selected:
            rf.default_audio_lang = selected[0].language
    
    print()
    print("=" * 60)
    print("Per-File Configuration Mode")
    print("=" * 60)
    print(f"Configuring {len(processable)} files individually.")
    print("Use [n]ext/[p]rev to navigate, [d]one when finished.")
    
    idx = 0
    while True:
        rf = processable[idx]
        result = configure_file_individually(rf, idx + 1, len(processable))
        
        if result is None:
            return False
        elif result == 'next':
            if idx < len(processable) - 1:
                idx += 1
            else:
                print("Already at last file. Use [d]one to finish.")
        elif result == 'prev':
            if idx > 0:
                idx -= 1
        elif result == 'done':
            return True


# =============================================================================
# Phase 3: File Selection
# =============================================================================

def display_files(files: list[ReencodeFile], source_dir: Path, timestamps_checked: bool = False):
    """Display file list with current state and intended changes."""
    print()
    print("=" * 60)
    print("File Selection")
    print("=" * 60)
    print()

    for i, rf in enumerate(files):
        try:
            rel_path = rf.path.relative_to(source_dir)
        except ValueError:
            rel_path = rf.path.name

        sel = "[x]" if rf.selected else "[ ]"

        # Build status flags
        flags = []
        if rf.is_dv:
            flags.append("DV")
        if rf.is_hdr and not rf.is_dv:
            flags.append("HDR")
        if timestamps_checked and rf.has_timestamp_issues:
            flags.append("TS!")
        if rf.skip_reason:
            flags.append("!")

        flag_str = f"[{','.join(flags)}] " if flags else ""

        print(f"  {i+1:2}. {sel} {flag_str}{rel_path}")

        # Now line - current state
        now_parts = [
            rf.codec,
            rf.resolution_str,
            f"{len(rf.audio_tracks)} audio",
            f"{rf.size_gb:.2f} GB"
        ]

        # Add timestamp status only if checked
        if timestamps_checked:
            if rf.has_timestamp_issues:
                now_parts.append(f"TS:{rf.timestamp_summary}")
            else:
                now_parts.append("TS:OK")

        print(f"        Now:   {' | '.join(now_parts)}")
        
        # After line - what will happen based on processing_mode
        if rf.is_dv:
            print(f"        After: [SKIP - Dolby Vision]")
        elif rf.skip_reason:
            print(f"        After: [SKIP - {rf.skip_reason}]")
        else:
            mode = rf.processing_mode
            after_audio = f"{len(rf.selected_audio_indices)} audio (FLAC)"
            
            if mode == "skip":
                print(f"        After: [SKIP - no changes needed]")
            elif mode == "remux":
                # Audio-only remux, video copied
                print(f"        After: [REMUX] hevc (copy) | {rf.resolution_str} | {after_audio}")
            else:
                # Full re-encode
                after_res = rf.output_resolution_str
                changes = []
                
                # Codec change
                if not rf.is_already_x265:
                    changes.append(f"{rf.codec}->hevc")
                else:
                    changes.append("hevc")
                
                # Resolution change (only show arrow if actually changing)
                if rf.enable_crop and rf.resolution_str != after_res:
                    changes.append(f"{rf.resolution_str}->{after_res}")
                else:
                    changes.append(after_res)
                
                changes.append(after_audio)
                
                print(f"        After: [ENCODE] {' | '.join(changes)}")
    
    print()
    
    # Summary
    selected = [f for f in files if f.selected and not f.is_dv and not f.skip_reason]
    will_encode = [f for f in selected if f.processing_mode == "reencode"]
    will_remux = [f for f in selected if f.processing_mode == "remux"]
    will_skip = [f for f in selected if f.processing_mode == "skip"]
    with_crop = [f for f in will_encode if f.enable_crop]
    
    print(f"Archive location: {ARCHIVE_BASE}")
    print(f"Log file: {LOG_FILE}")
    print()
    print(f"Summary:")
    print(f"  Selected: {len(selected)}/{len(files)} files")
    if will_encode:
        print(f"  Will encode (video+audio): {len(will_encode)} files ({sum(f.size_gb for f in will_encode):.2f} GB)")
    if with_crop:
        print(f"    With crop: {len(with_crop)} files")
    if will_remux:
        print(f"  Will remux (audio only): {len(will_remux)} files")
    if will_skip:
        print(f"  Will skip (no changes): {len(will_skip)} files")

    # Timestamp summary (only if checked)
    if timestamps_checked:
        files_with_ts_issues = [f for f in files if f.has_timestamp_issues]
        print()
        print(f"Timestamp issues:")
        print(f"  Files with issues: {len(files_with_ts_issues)}/{len(files)}")
        if files_with_ts_issues:
            total_issues = sum(f.timestamp_issue_count for f in files_with_ts_issues)
            print(f"  Total issue count: {total_issues}")

    print()


def show_timestamp_details(rf: ReencodeFile):
    """Display detailed timestamp issues for a file."""
    print()
    print(f"Timestamp details for: {rf.path.name}")
    print("-" * 60)

    if not rf.timestamp_issues:
        print("  No timestamp issues detected.")
    else:
        for issue in rf.timestamp_issues:
            print(f"\n  {issue.stream_type.upper()} stream {issue.stream_index}:")
            print(f"    Issue type: {issue.issue_type}")
            print(f"    Occurrences: {issue.count}")
            if issue.sample_messages:
                print("    Sample messages:")
                for msg in issue.sample_messages:
                    print(f"      - {msg}")

    print()


def interactive_selection(files: list[ReencodeFile], source_dir: Path, timestamps_checked: bool = False) -> bool:
    """
    Interactive file selection loop.
    Returns True to proceed, False to quit.
    """
    files_with_ts_issues = [f for f in files if f.has_timestamp_issues] if timestamps_checked else []

    while True:
        display_files(files, source_dir, timestamps_checked)

        selected = [f for f in files if f.selected]
        selected_count = len(selected)
        not_x265_count = sum(1 for f in files if not f.is_already_x265 and not f.is_dv)

        print("Commands:")
        print("  [a]ll      - Select all (except DV)")
        print("  [n]one     - Deselect all")
        print(f"  [u]nproc   - Select only non-hevc files ({not_x265_count} files)")
        if timestamps_checked:
            print(f"  [t]s       - Select only files with timestamp issues ({len(files_with_ts_issues)} files)")
            print("  ts <num>   - Show timestamp details for file")
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
        elif cmd == 'u':
            for f in files:
                f.selected = not f.is_already_x265 and not f.is_dv
        elif cmd == 't' and timestamps_checked:
            # Select only files with timestamp issues
            for f in files:
                f.selected = f.has_timestamp_issues and not f.is_dv
        elif cmd.startswith('ts ') and timestamps_checked:
            try:
                idx = int(cmd[3:].strip()) - 1
                if 0 <= idx < len(files):
                    show_timestamp_details(files[idx])
                    input("Press Enter to continue...")
                else:
                    print(f"Invalid number. Enter 1-{len(files)}")
            except ValueError:
                print("Invalid input. Use 'ts <number>'")
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
    audio_tracks: list[AudioTrack],
    default_audio_idx: int | None = 0,
    default_subtitle_idx: int | None = None,
    crop: str = ""
) -> bool:
    """Encode a single file. Returns True on success."""
    temp_output = source.with_suffix(".tmp.mkv")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i", str(source),
        "-map", "0:v",
    ]

    for track in audio_tracks:
        cmd.extend(["-map", f"0:{track.index}"])

    cmd.extend([
        "-map", "0:s?",
        "-map", "0:t?",
        "-map", "0:d?",
        "-map_metadata", "0",
        "-map_chapters", "0",
    ])

    # Add crop filter if specified
    if crop:
        cmd.extend(["-vf", f"crop={crop}"])

    cmd.extend([
        "-c:v", "libx265",
        "-crf", "12",
        "-preset", "slower",
        "-profile:v", "main10",
        "-pix_fmt", "yuv420p10le",
    ])

    if x265_params:
        cmd.extend(["-x265-params", ":".join(x265_params)])

    # Set per-stream audio codec: copy FLAC, encode others to FLAC
    for i, track in enumerate(audio_tracks):
        if track.codec.upper() == "FLAC":
            cmd.extend([f"-c:a:{i}", "copy"])
        else:
            cmd.extend([f"-c:a:{i}", "flac"])

    cmd.extend([
        "-c:s", "copy",
        "-c:t", "copy",
        "-c:d", "copy",
    ])

    # Set audio track dispositions
    for i in range(len(audio_tracks)):
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


def remux_file(
    source: Path,
    audio_tracks: list[AudioTrack],
    default_audio_idx: int | None = 0,
    default_subtitle_idx: int | None = None
) -> bool:
    """
    Remux file: copy video, convert audio to FLAC (or copy if already FLAC), copy subtitles.
    Much faster than re-encoding when video doesn't need changes.
    Returns True on success.
    """
    temp_output = source.with_suffix(".tmp.mkv")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i", str(source),
        "-map", "0:v",
    ]

    for track in audio_tracks:
        cmd.extend(["-map", f"0:{track.index}"])

    cmd.extend([
        "-map", "0:s?",
        "-map", "0:t?",
        "-map", "0:d?",
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-c:v", "copy",      # Copy video stream unchanged
    ])

    # Set per-stream audio codec: copy FLAC, encode others to FLAC
    for i, track in enumerate(audio_tracks):
        if track.codec.upper() == "FLAC":
            cmd.extend([f"-c:a:{i}", "copy"])
        else:
            cmd.extend([f"-c:a:{i}", "flac"])

    cmd.extend([
        "-c:s", "copy",
        "-c:t", "copy",
        "-c:d", "copy",
    ])

    # Set audio track dispositions
    for i in range(len(audio_tracks)):
        if i == default_audio_idx:
            cmd.extend([f"-disposition:a:{i}", "default"])
        else:
            cmd.extend([f"-disposition:a:{i}", "0"])
    
    # Set subtitle track dispositions
    if default_subtitle_idx is not None:
        cmd.extend(["-disposition:s", "0"])
        cmd.extend([f"-disposition:s:{default_subtitle_idx}", "default"])
    else:
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
            if "frame=" in line or "speed=" in line or "size=" in line:
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
    Process file based on its processing_mode.
    Returns: 'success', 'skipped', or 'failed'
    """
    source = rf.path
    temp_output = source.with_suffix(".tmp.mkv")
    archive_path = get_archive_path(source)
    
    mode = rf.processing_mode
    
    if mode == "skip":
        log(f"SKIPPED: {source.name} (no changes needed)")
        return "skipped"

    audio_tracks = rf.selected_audio_tracks
    default_audio_idx = rf.get_default_audio_index()
    default_subtitle_idx = rf.get_default_subtitle_index()
    crop = rf.detected_crop if rf.enable_crop else ""

    if mode == "remux":
        log(f"Remuxing: {source.name} (audio only, video copied)")
        log(f"  Audio: {len(rf.audio_tracks)} tracks -> {len(audio_tracks)} selected (default: {default_audio_idx})")
        log(f"  Subtitles: {len(rf.subtitle_tracks)} tracks (default: {default_subtitle_idx})")

        if not remux_file(source, audio_tracks, default_audio_idx, default_subtitle_idx):
            return "failed"
    else:
        # Full re-encode
        log(f"Encoding: {source.name} (codec: {rf.codec}, {rf.hdr_status})")
        log(f"  Audio: {len(rf.audio_tracks)} tracks -> {len(audio_tracks)} selected (default: {default_audio_idx})")
        log(f"  Subtitles: {len(rf.subtitle_tracks)} tracks (default: {default_subtitle_idx})")

        if crop:
            log(f"  Crop: {crop}")

        if rf.x265_params:
            log(f"  HDR params: {':'.join(rf.x265_params)}")

        if not encode_file(source, rf.x265_params, audio_tracks, default_audio_idx, default_subtitle_idx, crop):
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
    selected = [f for f in files if f.selected and not f.is_dv and not f.skip_reason]
    
    # Categorize by processing mode
    to_encode = [f for f in selected if f.processing_mode == "reencode"]
    to_remux = [f for f in selected if f.processing_mode == "remux"]
    to_skip = [f for f in selected if f.processing_mode == "skip"]
    
    if not to_encode and not to_remux:
        print("No files need processing.")
        return
    
    print()
    print("=" * 60)
    print("Ready to process")
    print("=" * 60)
    if to_encode:
        print(f"  Encode (video+audio): {len(to_encode)} files ({sum(f.size_gb for f in to_encode):.2f} GB)")
    if to_remux:
        print(f"  Remux (audio only):   {len(to_remux)} files")
    if to_skip:
        print(f"  Skip (no changes):    {len(to_skip)} files")
    print()
    
    confirm = input("Final confirmation - proceed? [y/N]: ").strip().lower()
    if confirm != 'y':
        print("Aborted.")
        return
    
    # Process only files that need it
    to_process = to_encode + to_remux
    
    log("=" * 60)
    log(f"Starting batch: {len(to_encode)} encode, {len(to_remux)} remux in {source_dir}")
    log("=" * 60)

    success = 0
    failed = 0
    skipped = 0

    for i, rf in enumerate(to_process, 1):
        try:
            rel_path = rf.path.relative_to(source_dir)
        except ValueError:
            rel_path = rf.path.name
        
        mode_str = "ENCODE" if rf.processing_mode == "reencode" else "REMUX"
        log(f"[{i}/{len(to_process)}] [{mode_str}] {rel_path}")

        result = process_file(rf)
        
        if result == "success":
            success += 1
        elif result == "skipped":
            skipped += 1
        else:
            failed += 1

    log("=" * 60)
    log(f"Complete:")
    log(f"  Success: {success}")
    if skipped:
        log(f"  Skipped: {skipped}")
    if failed:
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


def parse_args():
    """Parse command line arguments."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Interactive batch MKV re-encoder for fixing timestamp issues.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s /tank/media/anime/Show/
    %(prog)s --timestamps /tank/media/anime/Show/
    %(prog)s -t /tank/media/movies/
        """
    )

    parser.add_argument(
        "directory",
        type=Path,
        help="Directory containing MKV files to process"
    )

    parser.add_argument(
        "-t", "--timestamps",
        action="store_true",
        help="Analyze files for timestamp issues (slower but thorough)"
    )

    return parser.parse_args()


def main():
    check_dependencies()
    args = parse_args()

    source_dir = args.directory.expanduser().resolve()

    if not source_dir.exists():
        print(f"Error: Directory does not exist: {source_dir}")
        sys.exit(1)

    if not source_dir.is_dir():
        print(f"Error: {source_dir} is not a directory")
        sys.exit(1)

    print(f"Directory: {source_dir}")
    if args.timestamps:
        print("Timestamp analysis: enabled")
    print()

    # Phase 1: Scan
    files = scan_files(source_dir, check_timestamps=args.timestamps)
    
    if not files:
        print("No MKV files found")
        sys.exit(0)

    # Track if we need per-file mode
    use_perfile_mode = False

    # Phase 1b: Crop configuration (if crop detected)
    crop_result = configure_crop(files)
    if crop_result is False:
        print("Cancelled.")
        sys.exit(0)
    elif crop_result == "perfile":
        use_perfile_mode = True

    # Phase 2: Audio configuration (skip if going to per-file mode)
    if not use_perfile_mode:
        audio_result = configure_audio(files)
        if audio_result is False:
            print("Cancelled.")
            sys.exit(0)
        elif audio_result == "perfile":
            use_perfile_mode = True

    # Phase 2b/2c: Default track configuration OR per-file mode
    if use_perfile_mode:
        if not configure_per_file(files):
            print("Cancelled.")
            sys.exit(0)
    else:
        if not configure_defaults(files):
            print("Cancelled.")
            sys.exit(0)

    # Phase 3: File selection
    if not interactive_selection(files, source_dir, timestamps_checked=args.timestamps):
        print("Cancelled.")
        sys.exit(0)

    # Phase 4: Process
    process_files(files, source_dir)


if __name__ == "__main__":
    main()
