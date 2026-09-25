"""Bounded audio decoding shared by references and the editor."""

from contextlib import contextmanager
from pathlib import Path
import subprocess
import tempfile

import soundfile as sf


@contextmanager
def open_audio(path, max_seconds=None):
    """Open SoundFile audio, using an on-disk FFmpeg decode for M4A."""
    path = Path(path)
    if path.suffix.lower() != ".m4a":
        with sf.SoundFile(str(path)) as stream:
            yield stream
        return
    with tempfile.TemporaryDirectory(prefix="tts_decode_") as directory:
        decoded = Path(directory) / "decoded.wav"
        command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path)]
        if max_seconds is not None:
            command += ["-t", str(max_seconds)]
        command += ["-vn", "-c:a", "pcm_f32le", str(decoded)]
        subprocess.run(command, check=True, capture_output=True, timeout=600)
        with sf.SoundFile(str(decoded)) as stream:
            yield stream


def read_audio(path, max_seconds=None):
    with open_audio(path, max_seconds) as stream:
        frames = -1 if max_seconds is None else max(1, int(stream.samplerate * max_seconds))
        return stream.read(frames=frames, dtype="float32"), stream.samplerate
