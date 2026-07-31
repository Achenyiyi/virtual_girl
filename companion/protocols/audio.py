"""Audio playback confirmation protocol.

Only audio that was actually played through the speakers enters
shared conversation history. This protocol tracks:

1. Which audio segments were generated (TTS output)
2. Which audio segments were actually played (speaker output)
3. Whether playback was interrupted mid-segment
4. The exact fraction of each segment that was played

This enables:
- Accurate conversation history (what user actually heard)
- Barge-in accounting (what was cut off)
- Replay/verification of past audio
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from companion.providers.tts import TTSTimingSegment


@dataclass
class AudioPlaybackRecord:
    """Record of a single audio segment being played."""

    turn_id: str
    segment_index: int
    audio_hash: str = ""  # SHA-256 of audio bytes
    audio_duration_ms: int = 0
    played_duration_ms: int = 0
    was_interrupted: bool = False
    played_fraction: float = 1.0  # 0.0 to 1.0
    time_to_first_byte_ms: int = 0
    text: str = ""  # The text this audio corresponds to
    audio_start_ms: int = 0
    alignment: tuple[TTSTimingSegment, ...] = ()

    @property
    def was_fully_played(self) -> bool:
        return not self.was_interrupted and self.played_fraction >= 0.95

    @property
    def effective_text(self) -> str:
        """Return the text that was effectively communicated.

        If fully played, returns the full text.
        If interrupted near the end (>80% played), returns the text
        with a note.
        If heavily interrupted, returns an approximation.
        """
        if self.alignment and self.played_duration_ms > 0:
            played_until_ms = self.audio_start_ms + self.played_duration_ms
            return "".join(
                segment.text for segment in self.alignment if segment.end_ms <= played_until_ms
            )
        if self.was_fully_played:
            return self.text
        if self.played_fraction > 0.8:
            return self.text  # Near-complete: count as communicated
        # Estimate based on fraction
        char_count = int(len(self.text) * self.played_fraction)
        return self.text[:char_count] if char_count > 0 else "[interrupted]"


class AudioConfirmationProtocol:
    """Protocol tracking what audio the user actually heard.

    Key invariant: conversation history MUST only contain text
    whose audio was actually played. The LLM may generate more text
    than was spoken (if interrupted), but only the played portion
    is committed.
    """

    def __init__(self) -> None:
        self._records: dict[str, list[AudioPlaybackRecord]] = {}
        self._alignments: dict[str, dict[int, tuple[TTSTimingSegment, ...]]] = {}

    def record_synthesis(
        self,
        turn_id: str,
        segment_index: int,
        audio_bytes: bytes,
        duration_ms: int,
        text: str,
        *,
        audio_start_ms: int = 0,
        alignment: tuple[TTSTimingSegment, ...] = (),
    ) -> AudioPlaybackRecord:
        """Record that TTS produced an audio segment."""
        audio_hash = hashlib.sha256(audio_bytes).hexdigest()
        record = AudioPlaybackRecord(
            turn_id=turn_id,
            segment_index=segment_index,
            audio_hash=audio_hash,
            audio_duration_ms=duration_ms,
            text=text,
            audio_start_ms=audio_start_ms,
            alignment=alignment,
        )
        self._records.setdefault(turn_id, []).append(record)
        if alignment:
            by_chunk: dict[int, list[TTSTimingSegment]] = {}
            for segment in alignment:
                by_chunk.setdefault(segment.chunk_seq, []).append(segment)
            snapshots = self._alignments.setdefault(turn_id, {})
            for chunk_seq, segments in by_chunk.items():
                snapshots[chunk_seq] = tuple(segments)
        return record

    def confirm_played(
        self,
        turn_id: str,
        segment_index: int,
        played_duration_ms: int,
        was_interrupted: bool = False,
    ) -> AudioPlaybackRecord | None:
        """Confirm that an audio segment was (partially) played.

        Returns the updated record, or None if the segment wasn't found.
        """
        records = self._records.get(turn_id, [])
        for r in records:
            if r.segment_index == segment_index:
                r.played_duration_ms = played_duration_ms
                r.was_interrupted = was_interrupted
                fraction = (
                    played_duration_ms / r.audio_duration_ms if r.audio_duration_ms > 0 else 1.0
                )
                r.played_fraction = max(0.0, min(1.0, fraction))
                return r
        return None

    def get_played_text(self, turn_id: str) -> str:
        """Get the text that was actually communicated in this turn.

        Concatenates effective_text from all played segments.
        """
        records = self._records.get(turn_id, [])
        played = [r for r in records if r.played_duration_ms > 0]
        snapshots = self._alignments.get(turn_id, {})
        if played and snapshots:
            played_until_ms = max(r.audio_start_ms + r.played_duration_ms for r in played)
            communicated: list[tuple[int, str]] = [
                (segment.start_ms, segment.text)
                for chunk_seq in sorted(snapshots)
                for segment in snapshots[chunk_seq]
                if segment.end_ms <= played_until_ms
            ]
            communicated.extend(
                (record.audio_start_ms, record.effective_text)
                for record in played
                if not record.alignment and record.text
            )
            return "".join(text for _, text in sorted(communicated, key=lambda item: item[0]))
        return "".join(r.effective_text for r in sorted(played, key=lambda r: r.segment_index))

    def get_all_text(self, turn_id: str) -> str:
        """Get the full generated text for this turn (including unplayed)."""
        records = self._records.get(turn_id, [])
        return "".join(r.text for r in sorted(records, key=lambda r: r.segment_index))

    def next_segment_index(self, turn_id: str) -> int:
        records = self._records.get(turn_id, [])
        return max((record.segment_index for record in records), default=-1) + 1

    def generated_audio_duration_ms(self, turn_id: str) -> int:
        return sum(record.audio_duration_ms for record in self._records.get(turn_id, []))

    def played_audio_duration_ms(self, turn_id: str) -> int:
        return sum(record.played_duration_ms for record in self._records.get(turn_id, []))

    def was_any_audio_played(self, turn_id: str) -> bool:
        """Check if any audio was played for this turn."""
        records = self._records.get(turn_id, [])
        return any(r.played_duration_ms > 0 for r in records)
