# Fish Audio S2.1 Timestamp Streaming

## Objective

Replace the voice turn's full-response TTS wait with one safe streaming path:

1. Build the LLM request through `CompanionOrchestrator`, preserving identity, memory,
   affect, relationship state, and conversation history.
2. Consume the LLM stream incrementally, but release only sanitized, stable phrase prefixes.
3. Synthesize each released phrase with Fish Audio S2.1 through
   `POST /v1/tts/stream/with-timestamp`.
4. Play PCM progressively and commit only text confirmed by timestamp alignment and actual
   playback duration.

The non-streaming LLM and `/v1/tts` APIs remain available for text-only turns, direct synthesis,
diagnostics, and compatibility.

## Protocol Decisions

- Supported streaming models are `s2.1-pro` and `s2.1-pro-free` only.
- The shipped default remains `s2.1-pro-free`; moving to the paid mode only changes the Fish
  `model` header value and requires fresh acceptance evidence.
- The default reference voice is the public trained model "AD学姐"
  (`7f92f8afb8ec43bf81429cc1c9199cb1`).
- Output stays mono 16-bit PCM at 24 kHz to match the existing gapless playback path.
- Each SSE `data:` payload is decoded independently. Malformed events, invalid base64, invalid
  alignment shapes, empty streams, and non-2xx responses fail closed with sanitized errors.
- Audio packets are transport chunks, not text boundaries. Alignment snapshots replace older
  snapshots for the same `chunk_seq`.
- Alignment segment times are converted to absolute stream times with
  `chunk_audio_offset_sec` before playback accounting.
- `max_text_bytes` remains configurable (480 by default) as an engineering chunk size. Smaller
  requests reduce time to first audio, improve cancellation responsiveness, and contain failures;
  the full response is always synthesized across all segments and is never truncated.

## Incremental Safety

Raw LLM tokens never reach TTS. A stateful speech buffer repeatedly sanitizes the complete prefix
seen so far and emits only punctuation-bounded, prefix-monotonic text. It holds incomplete JSON,
Markdown fences, internal tool-call lines, and `analysis:` / `final:` wrappers until they can be
classified safely. The final flush uses the same whole-response sanitizer and emits only the
remaining safe suffix.

If the sanitized prefix changes text that has already been released, generation fails closed
instead of speaking a contradictory or leaked continuation.

## Playback Accounting

Every emitted audio chunk carries the latest global alignment timeline known at that point. The
audio confirmation protocol stores that timeline with the synthesized PCM. On confirmation it
uses the chunk's absolute audio start plus the measured played duration to include only alignment
segments whose end time was reached. If alignment is temporarily unavailable, it falls back to
the existing conservative duration fraction behavior.

Repeated alignment snapshots never duplicate committed text: each synthesized record receives
only the newly aligned suffix after the preceding record's timeline boundary.

## Cancellation

Barge-in cancels the active LLM stream, closes the active Fish Audio HTTP response, cancels any
pending iterator task, aborts the audio output, and prevents later persistence or playback from
resuming. Existing 300 ms interruption and bounded cleanup contracts remain in force.

## Acceptance Criteria

- S2.1 streaming requests use the official timestamp endpoint and required headers.
- SSE framing works across arbitrary network chunk boundaries and multi-line events.
- Latest-wins alignment snapshots produce a monotonic, de-duplicated global text timeline.
- Voice playback begins before the final LLM token when the first safe phrase is available.
- The release report derives incremental playback from the measured device-write time and the
  actual LLM-stream completion time, rather than event publication order.
- All PCM chunks for a turn use one output-stream generation and report zero device underflows.
- Tool-call/reasoning leakage split across token boundaries is never sent to TTS.
- Completed turns persist full generated text and exactly confirmed communicated text.
- Interrupted turns do not publish a completed terminal event and do not commit unheard text.
- Cancellation works before response headers, during SSE consumption, during playback, and during
  event persistence.
- Existing text-only, direct TTS, configuration, diagnostics, and voice regression suites pass.
- Ruff, mypy, configuration validation, and `git diff --check` pass.
