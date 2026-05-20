import subprocess


def capture_frame_rtsp(rtsp_uri: str, timeout: int = 10) -> bytes:
    """Capture a single JPEG frame from an RTSP stream using ffmpeg.

    Args:
        rtsp_uri: Full RTSP URI with credentials (e.g. rtsp://admin:pass@host:554/...)
        timeout: Seconds to wait before killing ffmpeg

    Returns:
        Raw JPEG bytes

    Raises:
        RuntimeError: If ffmpeg fails or returns no data
    """
    cmd = [
        "ffmpeg",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_uri,
        "-vframes",
        "1",
        "-f",
        "image2",
        "-timeout",
        str(timeout * 1000000),
        "-y",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout + 5,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg exited with {result.returncode}: {result.stderr.decode(errors='replace')}"
            )
        if not result.stdout:
            raise RuntimeError("ffmpeg returned empty frame")
        return result.stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s")
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found in PATH")
