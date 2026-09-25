# audio_editor.py
"""Non-destructive audio editing pipeline.

Applies an ordered list of edits to a source WAV and writes the result.
Each edit is a dict ``{"type": "<name>", "params": {...}}``.

Supported edit types
--------------------
Range ops (use sample-time fields ``start_sec``/``end_sec``):
    cut          remove the selected range
    trim         keep only the selected range
    silence      mute the selected range
    fade_in      linear ramp 0 -> 1 across the range
    fade_out     linear ramp 1 -> 0 across the range

Effects (full-track):
    gain         {db}
    tempo        {factor}                            (rubberband time stretch)
    pitch        {semitones}                         (rubberband pitch shift)
    eq_highpass  {cutoff_hz}
    eq_lowpass   {cutoff_hz}
    eq_shelf_low {cutoff_hz, gain_db}
    eq_shelf_high{cutoff_hz, gain_db}
    reverb       {room_size, damping, wet}           (impulse-response reverb)
    echo         {delay_sec, feedback, wet}
    compressor   {threshold_db, ratio, attack_ms, release_ms, makeup_db}
    pad          {front_sec, end_sec}
    de_reverb    {strength}
    de_ess       {strength}
    noise_gate   {threshold_db, attack_ms, release_ms}
    normalize_lufs {target_lufs}
    normalize_peak {target}

Requested effects fail explicitly if required dependencies are unavailable.
"""

from __future__ import annotations

import logging
import math
import os
import uuid
from pathlib import Path
from typing import Iterable

import numpy as np
import soundfile as sf
from audio_io import open_audio, read_audio

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FORMAT_SUBTYPE = {
    "wav":  ("WAV",  "PCM_16"),
    "flac": ("FLAC", "PCM_16"),
    "ogg":  ("OGG",  "VORBIS"),
}


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _to_mono(data: np.ndarray) -> np.ndarray:
    if data.ndim > 1:
        return data.mean(axis=1)
    return data


def _idx(rate: int, t_sec: float, n_samples: int) -> int:
    """Convert seconds → sample index, clamped to [0, n_samples]."""
    i = int(round(max(0.0, t_sec) * rate))
    return min(i, n_samples)


# ---------------------------------------------------------------------------
# Range-based edits (apply on numpy data + rate)
# ---------------------------------------------------------------------------

def _op_cut(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    s = _idx(rate, float(p.get("start_sec", 0)), len(data))
    e = _idx(rate, float(p.get("end_sec", 0)),   len(data))
    if e <= s:
        return data
    return np.concatenate([data[:s], data[e:]])


def _op_trim(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    s = _idx(rate, float(p.get("start_sec", 0)), len(data))
    e = _idx(rate, float(p.get("end_sec", 0)),   len(data))
    if e <= s:
        return data
    return data[s:e].copy()


def _op_silence(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    s = _idx(rate, float(p.get("start_sec", 0)), len(data))
    e = _idx(rate, float(p.get("end_sec", 0)),   len(data))
    if e <= s:
        return data
    out = data.copy()
    out[s:e] = 0.0
    return out


def _op_fade_in(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    """Linear ramp 0→1 across [start, end]. Audio outside the range is unchanged."""
    s = _idx(rate, float(p.get("start_sec", 0)), len(data))
    e = _idx(rate, float(p.get("end_sec",   0)), len(data))
    if e <= s:
        return data
    n = e - s
    ramp = np.linspace(0.0, 1.0, n, dtype=data.dtype)
    out = data.copy()
    out[s:e] = out[s:e] * ramp
    return out


def _op_fade_out(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    """Linear ramp 1→0 across [start, end]. Audio outside the range is unchanged."""
    s = _idx(rate, float(p.get("start_sec", 0)), len(data))
    e = _idx(rate, float(p.get("end_sec",   0)), len(data))
    if e <= s:
        return data
    n = e - s
    ramp = np.linspace(1.0, 0.0, n, dtype=data.dtype)
    out = data.copy()
    out[s:e] = out[s:e] * ramp
    return out


# ---------------------------------------------------------------------------
# Full-track effects
# ---------------------------------------------------------------------------

def _op_gain(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    db = float(p.get("db", 0))
    return data * (10.0 ** (db / 20.0))


def _op_pad(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    front_sec = _clip(float(p.get("front_sec", p.get("front_pad_sec", 0.0))), 0.0, 60.0)
    end_sec = _clip(float(p.get("end_sec", p.get("end_pad_sec", 0.0))), 0.0, 60.0)
    front_n = int(round(front_sec * rate))
    end_n = int(round(end_sec * rate))
    if front_n <= 0 and end_n <= 0:
        return data
    if data.ndim == 1:
        front = np.zeros(front_n, dtype=data.dtype)
        end = np.zeros(end_n, dtype=data.dtype)
    else:
        front = np.zeros((front_n, data.shape[1]), dtype=data.dtype)
        end = np.zeros((end_n, data.shape[1]), dtype=data.dtype)
    return np.concatenate([front, data, end], axis=0)


def _op_tempo(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    factor = _clip(float(p.get("factor", 1.0)), 0.25, 4.0)
    if abs(factor - 1.0) < 1e-4:
        return data
    try:
        import pyrubberband as pyrb
        return pyrb.time_stretch(data, rate, factor)
    except Exception as e:
        raise RuntimeError("tempo requires working pyrubberband and rubberband") from e


def _op_pitch(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    semitones = _clip(float(p.get("semitones", 0)), -24, 24)
    if abs(semitones) < 1e-4:
        return data
    try:
        import pyrubberband as pyrb
        return pyrb.pitch_shift(data, rate, semitones)
    except Exception as e:
        raise RuntimeError("pitch requires working pyrubberband and rubberband") from e


def _butter_filter(data: np.ndarray, rate: int, kind: str,
                   cutoff_hz: float | tuple[float, float], order: int = 4) -> np.ndarray:
    try:
        from scipy.signal import butter, sosfiltfilt
    except ImportError:
        raise RuntimeError("Requested filter requires scipy")
    if isinstance(cutoff_hz, tuple):
        wn = (cutoff_hz[0], cutoff_hz[1])
    else:
        wn = float(cutoff_hz)
    sos = butter(order, wn, kind, fs=rate, output='sos')
    return sosfiltfilt(sos, data)


def _op_eq_highpass(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    cutoff = _clip(float(p.get("cutoff_hz", 80)), 20, rate / 2 - 1)
    return _butter_filter(data, rate, "high", cutoff)


def _op_eq_lowpass(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    cutoff = _clip(float(p.get("cutoff_hz", 12000)), 100, rate / 2 - 1)
    return _butter_filter(data, rate, "low", cutoff)


def _op_eq_shelf_low(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    cutoff = _clip(float(p.get("cutoff_hz", 200)), 20, rate / 2 - 1)
    gain_db = _clip(float(p.get("gain_db", 0)), -24, 24)
    if abs(gain_db) < 1e-3:
        return data
    g = 10.0 ** (gain_db / 20.0)
    low = _butter_filter(data, rate, "low", cutoff, order=2)
    high = data - low
    return low * g + high


def _op_eq_shelf_high(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    cutoff = _clip(float(p.get("cutoff_hz", 4000)), 100, rate / 2 - 1)
    gain_db = _clip(float(p.get("gain_db", 0)), -24, 24)
    if abs(gain_db) < 1e-3:
        return data
    g = 10.0 ** (gain_db / 20.0)
    low = _butter_filter(data, rate, "low", cutoff, order=2)
    high = data - low
    return low + high * g


def _op_reverb(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    """Impulse-response reverb. The impulse is exponentially-decaying noise."""
    room_size = _clip(float(p.get("room_size", 0.5)), 0.0, 1.0)
    damping   = _clip(float(p.get("damping",   0.5)), 0.0, 1.0)
    wet       = _clip(float(p.get("wet",       0.3)), 0.0, 1.0)
    if wet < 1e-3:
        return data
    try:
        from scipy.signal import fftconvolve
    except ImportError:
        raise RuntimeError("reverb requires scipy")

    duration = 0.25 + room_size * 2.5  # 0.25s -> 2.75s tail
    n = int(duration * rate)
    if n < 8:
        return data
    rng = np.random.default_rng(seed=42)  # deterministic IR for reproducibility
    ir = rng.standard_normal(n).astype(np.float32) * 0.5
    decay_rate = 2.0 + damping * 8.0
    decay = np.exp(-np.linspace(0.0, decay_rate, n)).astype(np.float32)
    ir *= decay

    # Predelay
    predelay = int(_clip(0.005 + room_size * 0.04, 0, 0.08) * rate)
    if predelay > 0:
        ir = np.concatenate([np.zeros(predelay, dtype=ir.dtype), ir])

    # Damping: low-pass the IR by the damping amount
    if damping > 0.05:
        from scipy.signal import butter, sosfiltfilt
        cutoff = max(800.0, 12000.0 * (1.0 - damping))
        sos = butter(2, cutoff, "low", fs=rate, output='sos')
        ir = sosfiltfilt(sos, ir).astype(np.float32)

    wet_signal = fftconvolve(data, ir, mode='full').astype(np.float32)
    # Keep the natural convolution length so the reverb tail isn't chopped,
    # but cap at input + 3s to avoid runaway durations on huge IRs.
    max_tail = int(rate * 3.0)
    keep = len(data) + min(len(ir), max_tail)
    if len(wet_signal) > keep:
        wet_signal = wet_signal[:keep]

    # Normalize wet so the convolution's amplitude is bounded
    peak = float(np.max(np.abs(wet_signal)))
    if peak > 1e-9:
        wet_signal = wet_signal / peak * 0.9

    # Pad data to match wet length
    if len(wet_signal) > len(data):
        dry = np.concatenate([data, np.zeros(len(wet_signal) - len(data), dtype=data.dtype)])
    else:
        dry = data
    return (1.0 - wet) * dry + wet * wet_signal


def _op_echo(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    """Feedback delay implemented as a comb filter via scipy.lfilter.

    Equivalent to ``y[n] = x[n] + feedback * y[n-delay]`` but vectorized,
    which keeps long files fast.
    """
    delay_sec = _clip(float(p.get("delay_sec", 0.25)), 0.01, 2.0)
    feedback  = _clip(float(p.get("feedback",  0.4)),  0.0, 0.95)
    wet       = _clip(float(p.get("wet",       0.4)),  0.0, 1.0)
    if wet < 1e-3:
        return data
    delay = max(1, int(delay_sec * rate))

    # Pad with a tail so trailing echoes aren't truncated
    tail_samples = int(rate * delay_sec * 4)
    padded = np.concatenate([
        data.astype(np.float32, copy=False),
        np.zeros(tail_samples, dtype=np.float32),
    ])

    try:
        from scipy.signal import lfilter
        # Transfer function: Y(z)/X(z) = 1 / (1 - feedback * z^-delay)
        a = np.zeros(delay + 1, dtype=np.float64)
        a[0] = 1.0
        a[delay] = -feedback
        wet_signal = lfilter([1.0], a, padded).astype(np.float32)
    except Exception as e:
        # Slow Python fallback if scipy is unavailable
        logger.warning("echo: scipy unavailable (%s), using Python loop fallback", e)
        wet_signal = padded.copy()
        for i in range(delay, len(wet_signal)):
            wet_signal[i] += feedback * wet_signal[i - delay]

    # Pad dry to match
    if len(wet_signal) > len(data):
        dry = np.concatenate([data, np.zeros(len(wet_signal) - len(data), dtype=np.float32)])
    else:
        dry = data
    return (1.0 - wet) * dry + wet * wet_signal


def _envelope_iir(abs_data: np.ndarray, rate: int, attack_ms: float,
                  release_ms: float) -> np.ndarray:
    """Single-pole envelope follower (symmetric, vectorized via scipy.lfilter).

    Uses the geometric mean of attack/release as the time constant. Good
    enough for compressors/gates without burning seconds in a Python loop.
    """
    try:
        from scipy.signal import lfilter
    except ImportError:
        # Fallback: rolling max with a small window
        return abs_data
    tau = math.sqrt(max(0.1, attack_ms) * max(0.1, release_ms)) * rate / 1000.0
    if tau < 1:
        return abs_data
    alpha = math.exp(-1.0 / tau)
    # y[n] = (1-alpha) * x[n] + alpha * y[n-1]   →   b=[1-alpha], a=[1, -alpha]
    return lfilter([1 - alpha], [1.0, -alpha], abs_data).astype(np.float32)


def _op_compressor(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    threshold_db = _clip(float(p.get("threshold_db", -20)), -60, 0)
    ratio        = _clip(float(p.get("ratio", 4)), 1.0, 30.0)
    attack_ms    = _clip(float(p.get("attack_ms", 10)), 0.1, 500)
    release_ms   = _clip(float(p.get("release_ms", 150)), 1.0, 2000)
    makeup_db    = _clip(float(p.get("makeup_db", 0)), 0, 30)

    abs_data = np.abs(data).astype(np.float32)
    env = _envelope_iir(abs_data, rate, attack_ms, release_ms)

    threshold = 10.0 ** (threshold_db / 20.0)
    over = env > threshold
    gain_db = np.zeros_like(env)
    gain_db[over] = 20.0 * np.log10(env[over] / threshold + 1e-12) * (1.0 / ratio - 1.0)
    makeup = 10.0 ** (makeup_db / 20.0)
    gain = (10.0 ** (gain_db / 20.0)) * makeup
    return data * gain


def _op_de_reverb(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    strength = _clip(float(p.get("strength", 0.5)), 0.0, 1.0)
    if strength < 1e-3 or len(data) <= int(rate * 0.2):
        return data
    try:
        import noisereduce as nr
    except ImportError:
        raise RuntimeError("de_reverb requires noisereduce")
    noise_clip = data[:int(rate * 0.2)]
    return nr.reduce_noise(y=data, sr=rate, y_noise=noise_clip, prop_decrease=strength)


def _op_de_ess(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    """Multiband de-esser: Hilbert envelope on 3kHz+ band, 4:1 compression."""
    strength = _clip(float(p.get("strength", 0.5)), 0.0, 1.0)
    if strength < 1e-3 or len(data) < 30:
        return data
    try:
        from scipy.signal import butter, sosfiltfilt, hilbert
        from scipy.ndimage import gaussian_filter1d
    except ImportError:
        raise RuntimeError("de_ess requires scipy")
    cutoff = 3000
    sos_high = butter(4, cutoff, 'high', fs=rate, output='sos')
    high = sosfiltfilt(sos_high, data)
    env = np.abs(hilbert(high))
    sigma = (rate * 5 / 1000) / 2.355
    env = gaussian_filter1d(env, sigma)
    env_db = 20 * np.log10(env + 1e-6)
    gain_db = np.where(env_db > -20, (env_db + 20) * (1 / 4 - 1), 0.0)
    gain = 10 ** (gain_db / 20.0)
    high_compressed = high * gain
    sos_low = butter(4, cutoff, 'low', fs=rate, output='sos')
    low = sosfiltfilt(sos_low, data)
    return (1 - strength) * data + strength * (low + high_compressed)


def _op_noise_gate(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    threshold_db = _clip(float(p.get("threshold_db", -50)), -90, 0)
    attack_ms    = _clip(float(p.get("attack_ms",    5)), 0.1, 100)
    release_ms   = _clip(float(p.get("release_ms", 100)), 1.0, 1000)

    threshold = 10.0 ** (threshold_db / 20.0)
    abs_data = np.abs(data).astype(np.float32)
    env = _envelope_iir(abs_data, rate, attack_ms, release_ms)
    gate = (env > threshold).astype(np.float32)
    # Smooth the gate so transitions aren't clicks
    try:
        from scipy.ndimage import gaussian_filter1d
        gate = gaussian_filter1d(gate, sigma=max(1.0, rate * 0.005))
    except ImportError:
        pass
    return data * gate


def _op_normalize_lufs(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    target = _clip(float(p.get("target_lufs", -16)), -36, -6)
    try:
        import pyloudnorm as pyln
    except ImportError:
        raise RuntimeError("LUFS normalization requires pyloudnorm")
    if len(data) < int(0.4 * rate) + 1:
        raise ValueError("LUFS normalization requires at least 0.4 seconds of audio")
    meter = pyln.Meter(rate)
    loud = meter.integrated_loudness(data)
    if math.isinf(loud):
        return data
    return pyln.normalize.loudness(data, loud, target).astype(np.float32)


def _op_normalize_peak(data: np.ndarray, rate: int, p: dict) -> np.ndarray:
    target = _clip(float(p.get("target", 0.95)), 0.1, 1.0)
    peak = float(np.max(np.abs(data)) or 0.0)
    if peak <= 1e-9:
        return data
    return (data / peak * target).astype(np.float32)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

OPS = {
    # Range ops
    "cut":            _op_cut,
    "trim":           _op_trim,
    "silence":        _op_silence,
    "fade_in":        _op_fade_in,
    "fade_out":       _op_fade_out,
    # Effects
    "gain":           _op_gain,
    "pad":            _op_pad,
    "tempo":          _op_tempo,
    "pitch":          _op_pitch,
    "eq_highpass":    _op_eq_highpass,
    "eq_lowpass":     _op_eq_lowpass,
    "eq_shelf_low":   _op_eq_shelf_low,
    "eq_shelf_high":  _op_eq_shelf_high,
    "reverb":         _op_reverb,
    "echo":           _op_echo,
    "compressor":     _op_compressor,
    "de_reverb":      _op_de_reverb,
    "de_ess":         _op_de_ess,
    "noise_gate":     _op_noise_gate,
    "normalize_lufs": _op_normalize_lufs,
    "normalize_peak": _op_normalize_peak,
}


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def apply_edits(data: np.ndarray, rate: int, edits: Iterable[dict]) -> np.ndarray:
    """Apply a sequence of edit dicts to a numpy audio array."""
    out = data.astype(np.float32, copy=False)
    for spec in edits or ():
        kind = spec.get("type")
        op = OPS.get(kind)
        if op is None:
            raise ValueError(f"Unknown edit type: {kind}")
        try:
            out = op(out, rate, spec.get("params", {}) or {})
            out = np.nan_to_num(out, nan=0.0)
        except Exception as e:
            raise ValueError(f"Edit {kind} failed: {e}") from e
    if not len(out):
        raise ValueError("Edits remove all audio; keep at least one sample")
    # Final safety clamp to prevent clipping from cascade gain
    peak = float(np.max(np.abs(out)) or 0.0)
    if peak > 1.0:
        out = out / peak * 0.99
    return out.astype(np.float32, copy=False)


def render_to_file(src_path: str | Path, dst_path: str | Path,
                   edits: list[dict], output_format: str = "wav") -> dict:
    """Read src, apply edits, write dst (atomically via temp file + rename).

    Returns a small status dict with duration_sec / sample_rate / size_bytes.
    """
    src_path = Path(src_path)
    dst_path = Path(dst_path)
    if not src_path.exists():
        raise FileNotFoundError(f"Source not found: {src_path}")

    data, rate = read_audio(src_path)
    if data.ndim == 1:
        out = apply_edits(data, rate, edits)
    elif not edits:
        out = data
    else:
        out = np.stack([apply_edits(data[:, ch], rate, edits)
                        for ch in range(data.shape[1])], axis=1)

    fmt = output_format.lower().lstrip(".")
    # Reject unknown formats instead of silently writing WAV-encoded bytes into
    # a file named with the requested extension (e.g. a *.m4a holding PCM WAV).
    if fmt != "mp3" and fmt not in _FORMAT_SUBTYPE:
        raise ValueError(f"Unsupported output_format: {fmt!r}")
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a sibling temp file first, then atomically rename. This way a
    # half-written file never appears at dst_path if the encoder crashes.
    tmp_dst = dst_path.with_name(f".tmp.{uuid.uuid4().hex}.{dst_path.name}")
    try:
        if fmt == "mp3":
            # soundfile can't emit mp3; bounce through a temp WAV via pydub/ffmpeg.
            tmp_wav = tmp_dst.with_name(tmp_dst.name + ".wav")
            sf.write(str(tmp_wav), out, rate, subtype="PCM_16")
            try:
                from pydub import AudioSegment
                seg = AudioSegment.from_wav(str(tmp_wav))
                seg.export(str(tmp_dst), format="mp3", bitrate="192k")
            finally:
                try:
                    tmp_wav.unlink()
                except OSError:
                    pass
        else:
            kind, subtype = _FORMAT_SUBTYPE[fmt]
            sf.write(str(tmp_dst), out, rate, format=kind, subtype=subtype)
        # os.replace is atomic on POSIX/NTFS; overwrites if dst exists
        os.replace(str(tmp_dst), str(dst_path))
    except Exception:
        try:
            if tmp_dst.exists():
                tmp_dst.unlink()
        except OSError:
            pass
        raise

    duration_sec = float(len(out) / rate) if rate else 0.0
    return {
        "duration_sec": round(duration_sec, 3),
        "sample_rate": int(rate),
        "size_bytes": dst_path.stat().st_size if dst_path.exists() else 0,
        "samples": int(len(out)),
    }


# ---------------------------------------------------------------------------
# Peaks extractor (for waveform display)
# ---------------------------------------------------------------------------

def extract_peaks(src_path: str | Path, buckets: int = 1024) -> dict:
    """Return min/max envelope buckets for a fast waveform render.

    The output is shaped so the client can draw vertical bars per pixel column.
    """
    src_path = Path(src_path)
    if not src_path.exists():
        raise FileNotFoundError(f"Source not found: {src_path}")
    buckets = max(64, min(4096, int(buckets)))

    with open_audio(src_path) as stream:
        n, rate = len(stream), stream.samplerate
        count = min(n, buckets)
        if not count:
            return {"duration_sec": 0.0, "sample_rate": rate, "buckets": 0, "peaks": [], "rms": []}
        # Integer boundaries include every sample, including the final remainder.
        edges = np.linspace(0, n, count + 1, dtype=np.int64)
        peaks, rms = [], []
        for lo, hi in zip(edges[:-1], edges[1:]):
            remaining = int(hi - lo)
            low, high, squares = float("inf"), float("-inf"), 0.0
            while remaining:
                data = _to_mono(stream.read(min(65536, remaining), dtype="float32"))
                if not len(data):
                    raise ValueError("Audio ended before its declared duration")
                low, high = min(low, float(data.min())), max(high, float(data.max()))
                squares += float(np.sum(data.astype(np.float64) ** 2))
                remaining -= len(data)
            peaks.append([round(low, 4), round(high, 4)])
            rms.append(round(math.sqrt(squares / int(hi - lo)), 4))
    return {"duration_sec": n / rate, "sample_rate": rate, "buckets": count,
            "peaks": peaks, "rms": rms}


# ---------------------------------------------------------------------------
# Defaults manifest (for the frontend)
# ---------------------------------------------------------------------------

EFFECT_DEFAULTS: dict[str, dict] = {
    "gain":           {"db": 0.0},
    "pad":            {"front_sec": 0.0, "end_sec": 0.0},
    "tempo":          {"factor": 1.0},
    "pitch":          {"semitones": 0.0},
    "eq_highpass":    {"cutoff_hz": 80.0},
    "eq_lowpass":     {"cutoff_hz": 12000.0},
    "eq_shelf_low":   {"cutoff_hz": 200.0,  "gain_db": 0.0},
    "eq_shelf_high":  {"cutoff_hz": 4000.0, "gain_db": 0.0},
    "reverb":         {"room_size": 0.5, "damping": 0.5, "wet": 0.3},
    "echo":           {"delay_sec": 0.25, "feedback": 0.4, "wet": 0.4},
    "compressor":     {"threshold_db": -20.0, "ratio": 4.0,
                       "attack_ms": 10.0, "release_ms": 150.0, "makeup_db": 0.0},
    "de_reverb":      {"strength": 0.5},
    "de_ess":         {"strength": 0.5},
    "noise_gate":     {"threshold_db": -50.0, "attack_ms": 5.0, "release_ms": 100.0},
    "normalize_lufs": {"target_lufs": -16.0},
    "normalize_peak": {"target": 0.95},
}
