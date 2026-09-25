# audio_assembler.py
"""Final audio assembly and format conversion."""

import shutil
import subprocess
import logging
from pathlib import Path

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


def assemble_chunks(chunk_files: list[str | Path], output_path: str | Path,
                    sr: int, inter_pause: float = 0.25,
                    front_pad: float = 0.5, end_pad: float = 0.5,
                    allow_partial: bool = False) -> str:
    """Concatenate chunk WAV files with silence padding between them.

    Args:
        chunk_files: Ordered list of WAV file paths.
        output_path: Where to write the assembled WAV.
        sr: Sample rate.
        inter_pause: Seconds of silence between chunks.
        front_pad: Seconds of silence at the beginning.
        end_pad: Seconds of silence at the end.
        allow_partial: If False (default), raise RuntimeError when any chunk
            fails to load/resample so a truncated assembly is reported as a
            failure rather than a silent success. Set True to deliberately
            assemble only the chunks that loaded.

    Returns:
        The output_path as a string.
    """
    if sr <= 0:
        raise ValueError(f"Invalid sample rate: {sr}")

    parts = []
    failed_indices = []
    for idx, f in enumerate(chunk_files):
        try:
            data, file_sr = sf.read(str(f))
            # Convert stereo to mono for consistency
            if data.ndim > 1:
                data = data.mean(axis=1)
            if file_sr != sr:
                logger.warning("Chunk %s has sample rate %d (expected %d), resampling",
                               f, file_sr, sr)
                try:
                    import librosa
                except ImportError:
                    # librosa is a required dependency on the assembly path;
                    # fail loudly rather than silently dropping the chunk.
                    raise RuntimeError(
                        f"librosa not available to resample chunk {f} "
                        f"({file_sr} Hz -> {sr} Hz); cannot assemble audio"
                    )
                data = librosa.resample(data, orig_sr=file_sr, target_sr=sr)
            parts.append(data)
        except Exception as e:
            logger.warning("Failed to read chunk %s: %s", f, e)
            failed_indices.append(idx)

    if failed_indices:
        logger.warning("%d of %d chunks failed to load", len(failed_indices), len(chunk_files))

    if not parts:
        raise RuntimeError(
            f"No audio chunks to assemble ({len(failed_indices)} of {len(chunk_files)} failed)"
        )

    if failed_indices and not allow_partial:
        raise RuntimeError(
            f"Audio assembly incomplete: {len(failed_indices)} of "
            f"{len(chunk_files)} chunks failed to load (indices "
            f"{failed_indices}); refusing to write truncated audio. "
            f"Pass allow_partial=True to assemble the remaining chunks anyway."
        )

    # Apply a short linear fade-in/fade-out (~8 ms) to each chunk's head and
    # tail so a hard cut from an independently-normalized chunk into the
    # surrounding zero-silence (front/inter/end) is not a step discontinuity
    # that produces an audible click/pop at every seam.
    fade_len = int(sr * 0.008)
    if fade_len > 1:
        for p in parts:
            n = min(fade_len, len(p))
            if n > 1:
                p[:n] *= np.linspace(0.0, 1.0, n, dtype=p.dtype)
                p[-n:] *= np.linspace(1.0, 0.0, n, dtype=p.dtype)

    inter = np.zeros(int(sr * inter_pause), dtype=np.float32)
    front = np.zeros(int(sr * front_pad), dtype=np.float32)
    end = np.zeros(int(sr * end_pad), dtype=np.float32)

    segments = [front, parts[0]]
    for p in parts[1:]:
        segments.append(inter)
        segments.append(p)
    segments.append(end)

    final_wav = np.concatenate(segments)
    sf.write(str(output_path), final_wav, sr, subtype="PCM_16")
    logger.info("Assembled %d chunks -> %s (%.1fs)", len(parts), output_path,
                len(final_wav) / sr)
    return str(output_path)


def _ffmpeg_args(fmt: str) -> list[str]:
    """Return ffmpeg encoding arguments for common output formats."""
    return {
        "mp3": ["-c:a", "libmp3lame", "-q:a", "0"],
        "ogg": ["-c:a", "libvorbis", "-q:a", "6"],
        "flac": ["-c:a", "flac", "-compression_level", "12"],
        "m4a": ["-c:a", "aac", "-b:a", "320k"],
    }.get(fmt, [])


def convert_format(input_path: str | Path, output_path: str | Path,
                   fmt: str = "wav", ffmpeg_path: str = "ffmpeg") -> str:
    """Convert audio file to the requested format via FFmpeg.

    Args:
        input_path: Source WAV file.
        output_path: Destination file path (should have the correct extension).
        fmt: Target format (wav, mp3, ogg, flac, m4a).
        ffmpeg_path: Path to the ffmpeg executable.

    Returns:
        The output_path as a string.
    """
    if fmt == "wav":
        # No conversion needed, just copy if different paths
        if str(input_path) != str(output_path):
            shutil.copy2(str(input_path), str(output_path))
        return str(output_path)

    codec_args = _ffmpeg_args(fmt)
    if not codec_args:
        logger.warning("No codec args for format '%s', ffmpeg will guess", fmt)

    cmd = [
        ffmpeg_path, "-y", "-i", str(input_path),
        *codec_args,
        str(output_path)
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True,
                       timeout=max(120, sf.info(str(input_path)).duration * 2 + 60), stdin=subprocess.DEVNULL)
        logger.info("Converted %s -> %s", input_path, output_path)
    except FileNotFoundError:
        raise RuntimeError(f"FFmpeg not found at '{ffmpeg_path}' and no fallback is available")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg conversion to {fmt} failed and no fallback is available: {e.stderr}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"FFmpeg conversion to {fmt} exceeded its duration-based timeout and no fallback is available")

    return str(output_path)
