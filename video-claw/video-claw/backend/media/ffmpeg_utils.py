"""ffmpeg helpers previously used only by the one-shot pipelines.

R8 and R13 can call these. ASS subtitle rendering is not included: those
helpers had no callers and were deleted with the pipelines.
"""

import logging
import os
import shutil
import subprocess
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


class FfmpegMedia:
    """ffmpeg concat, duration, and audio replacement. One class, static methods."""

    @staticmethod
    def concat_videos(video_paths: Iterable[str], output_path: str) -> Optional[str]:
        paths = [path for path in video_paths if path and os.path.exists(path)]
        if not paths:
            return None

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg not found; cannot concatenate videos")
            return None

        list_path = os.path.join(os.path.dirname(output_path), "concat.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            for path in paths:
                handle.write(f"file '{os.path.abspath(path)}'\n")

        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path]
        logger.info("Concatenating %d videos -> %s", len(paths), output_path)
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        logger.info("Video concatenation complete: %s", output_path)
        return output_path

    @staticmethod
    def concat_audios(audio_paths: Iterable[str], output_path: str) -> Optional[str]:
        paths = [path for path in audio_paths if path and os.path.exists(path)]
        if not paths:
            return None
        if len(paths) == 1:
            return paths[0]

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("ffmpeg not found; cannot concatenate audios")
            return None

        list_path = os.path.join(os.path.dirname(output_path), "concat_audio.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            for path in paths:
                handle.write(f"file '{os.path.abspath(path)}'\n")

        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path]
        logger.info("Concatenating %d audios -> %s", len(paths), output_path)
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return output_path

    @staticmethod
    def replace_video_audio(video_path: str, audio_path: str, output_path: str) -> str:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to replace video audio.")
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video not found: {video_path}")
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio not found: {audio_path}")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        logger.info("Replacing video audio: video=%s audio=%s -> %s", video_path, audio_path, output_path)
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            video_path,
            "-i",
            audio_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            output_path,
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return output_path

    @staticmethod
    def media_duration_seconds(path: str) -> Optional[float]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe or not os.path.exists(path):
            return None

        cmd = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        try:
            result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            duration = float(result.stdout.strip())
            logger.debug("Media duration: %s = %.3fs", path, duration)
            return duration
        except Exception as exc:
            logger.warning("Failed to probe media duration: %s (%s)", path, exc)
            return None

    @staticmethod
    def speed_audio_to_duration(audio_path: str, output_path: str, target_seconds: int) -> str:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to fit narration audio to a target duration.")

        duration = FfmpegMedia.media_duration_seconds(audio_path)
        if not duration:
            return audio_path
        if duration <= target_seconds:
            return audio_path

        speed = duration / float(target_seconds)
        filters = []
        remaining = speed
        while remaining > 2.0:
            filters.append("atempo=2.0")
            remaining /= 2.0
        filters.append(f"atempo={remaining:.6f}")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        logger.info(
            "Speeding audio to fit video duration: %s duration=%.2fs target=%ss speed=%.3fx -> %s",
            audio_path,
            duration,
            target_seconds,
            speed,
            output_path,
        )
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            audio_path,
            "-filter:a",
            ",".join(filters),
            "-vn",
            "-acodec",
            "libmp3lame",
            output_path,
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return output_path
