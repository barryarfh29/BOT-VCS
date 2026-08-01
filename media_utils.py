"""Utility media: deteksi panjang durasi video via ffprobe (bagian dari ffmpeg)."""

import subprocess
import logging

logger = logging.getLogger(__name__)


def probe_duration_seconds(path: str):
    """Kembalikan panjang video (detik, bulat) via ffprobe, atau None kalau gagal.

    Dipakai untuk menampilkan 'berapa menit' video di admin. ffprobe ikut terpasang
    bersama ffmpeg yang sudah dipakai untuk kompres video.
    """
    if not path:
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, timeout=60, text=True,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return None
        return round(float(raw))
    except Exception as e:
        logger.warning(f"probe_duration_seconds gagal untuk {path}: {e}")
        return None


def probe_video_codec(path: str):
    """Kembalikan nama codec video stream pertama (mis. 'h264') via ffprobe, atau None.

    Dipakai untuk memutuskan perlu re-encode atau tidak: kalau sudah 'h264' (mayoritas
    file dari HP/kamera), upload apa adanya tanpa kompres ulang yang berat di CPU.
    """
    if not path:
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, timeout=60, text=True,
        )
        name = (proc.stdout or "").strip().lower()
        return name or None
    except Exception as e:
        logger.warning(f"probe_video_codec gagal untuk {path}: {e}")
        return None
