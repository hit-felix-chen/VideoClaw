import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


class VideoFrameExtractor:
    """Single-frame ffmpeg commands. Callers keep using extract_last_frame()."""

    @staticmethod
    def extract_last_frame_command(ffmpeg: str, video_path: str, output_path: str) -> list[str]:
        return [
            ffmpeg,
            "-y",
            "-sseof",
            "-0.1",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            output_path,
        ]

    @staticmethod
    def extract_last_frame(video_path: str, output_path: str) -> str:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required to extract the previous video tail frame.")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        logger.info("Extracting tail frame: %s -> %s", video_path, output_path)
        cmd = VideoFrameExtractor.extract_last_frame_command(ffmpeg, video_path, output_path)
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return output_path


extract_last_frame = VideoFrameExtractor.extract_last_frame
