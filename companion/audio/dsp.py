"""Shared audio DSP helpers."""

from __future__ import annotations

import math
from array import array


def pcm_int16_rms(audio_bytes: bytes) -> float:
    """Return normalized RMS (0..1) for little-endian int16 PCM audio."""
    if not audio_bytes:
        return 0.0
    usable = audio_bytes[: len(audio_bytes) - len(audio_bytes) % 2]
    try:
        samples = array("h")
        samples.frombytes(usable)
    except (ValueError, OverflowError):
        return 0.0
    if not samples:
        return 0.0
    sum_squares = 0.0
    for value in samples:
        sum_squares += value * value
    return min(1.0, math.sqrt(sum_squares / len(samples)) / 32768.0)
