"""Video -> real MP3 conversion helpers (ffmpeg / libmp3lame)."""
import os, subprocess

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024   # Telegram getFile bot download cap
MAX_SEND_BYTES = 49 * 1024 * 1024       # stay under 50MB sendAudio cap
FFMPEG_TIMEOUT = 240                    # seconds


class ConvertError(Exception):
    pass


def has_audio_stream(path: str) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return bool(out.stdout.strip())


def video_to_mp3(src_path: str, dst_path: str) -> int:
    """Encode src video's audio track to a real MP3 (libmp3lame VBR ~190kbps).
    Returns output size in bytes. Raises ConvertError with a user-safe message."""
    if not has_audio_stream(src_path):
        raise ConvertError("this video has no audio track")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src_path,
        "-vn", "-sn", "-dn",
        "-codec:a", "libmp3lame", "-q:a", "2",
        dst_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ConvertError("conversion took too long (try a shorter video)")
    if proc.returncode != 0:
        raise ConvertError("ffmpeg could not read this file")
    size = os.path.getsize(dst_path) if os.path.exists(dst_path) else 0
    if size == 0:
        raise ConvertError("conversion produced an empty file")
    if size > MAX_SEND_BYTES:
        raise ConvertError("the MP3 is too large for Telegram to send back")
    return size


def probe_audio_codec(path: str) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return out.stdout.strip()


def probe_format(path: str) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
         "-of", "csv=p=0", path], capture_output=True, text=True)
    return out.stdout.strip()
