# Deployment Preflight

## Configuration contract

The packaged and repository YAML templates contain only capabilities implemented by the current
runtime. The loader rejects unsupported provider types, enabled perception/telemetry export, YAML
credentials, invalid cloud endpoints, invalid audio settings, and unsafe action configuration.

The following settings are now authoritative at runtime:

- memory SQLite path, WAL, and FTS configuration;
- `COMPANION_DB_PATH` as the explicit memory-path environment override;
- LLM retries, retry delay, timeout, model, endpoint, and credential environment name;
- Azure TTS region, voice, timeout, credential environment name, and 24 kHz PCM format;
- microphone/ASR sample rate, language, pre-roll, speech/silence limits, whole-turn timeout, TTS
  chunk timeout, playback timeout, cleanup timeout, and provider-interruption timeout;
- quiet hours, hourly proactive budgets, and per-level cooldowns;
- rotating file-log size/count, bounded in-memory event retention, avatar bridge, and Windows
  read-only action settings including pending-confirmation capacity and TTL.

Relative data/log/audit paths in the packaged default resolve under
`%LOCALAPPDATA%\VirtualCompanion`, independent of the launch directory. Set
`COMPANION_RUNTIME_DIR` to override that root. Relative paths in an explicitly supplied YAML file
resolve from that file's directory. An explicit missing config file is an error and never silently
falls back to defaults.

## Doctor commands

Run from the exact Python environment used to start the release:

```powershell
python -m companion --doctor
python -m companion --doctor --voice-input
python -m companion --doctor-online --voice-input
python -m companion --doctor-json --voice-input
python -m companion --accept-voice
python -m companion --accept-voice-json 1>voice-acceptance.json
```

`--doctor` performs no LLM/TTS network calls. `--doctor-online` validates configured remote
providers. The Azure check uses the read-only voices-list endpoint and doesn't synthesize audio.
No diagnostic message or JSON field includes credential values.

Exit codes:

- `0`: every required requested check passed; warnings and disabled optional providers are allowed;
- `1`: a required runtime, credential, database, module, device, or provider check failed;
- `2`: configuration could not be parsed or validated.

When `--voice-input` is included, doctor requires faster-whisper, NumPy, sounddevice, a usable
default input device, a usable default output device, and the Azure Speech credential. It does not
download or load the configured Whisper model.

`--doctor-voice-hardware` is an explicit deeper check. It may download/load the configured model,
runs one in-memory silence inference, opens the production microphone stream long enough to
capture several in-memory frames, and opens the production playback stream with about 20 ms of
silence. It doesn't persist captured audio or make cloud requests. On Windows without Developer
Mode, the Hugging Face cache may warn that symlink deduplication is unavailable; this increases
cache disk use but does not invalidate the model/inference check.

`--accept-voice` is the credential-backed target-machine release gate. It first asks for one real
microphone utterance and requires the durable event chain `ASR -> LLM -> streaming TTS -> audio
played -> completed`. It then asks for a second utterance and, while the companion is speaking,
asks the operator to speak again; this must stop provider work and playback, and persist exactly one
interrupted terminal event within `target_interrupt_latency_ms` of the microphone VAD speech-start
signal. The full barge-in utterance continues collecting for inspection without delaying the stop
signal. Before prompting for barge-in, the gate observes a short no-speech window; a premature VAD
edge fails as suspected speaker echo or crosstalk instead of being counted as a user interruption.
The completed turn must reach its
first played audio within `target_e2e_latency_ms`. Both targets are configured under
`providers.asr.capture` and default to 900 ms and 300 ms respectively.

The JSON variant writes interactive prompts to stderr and exactly one machine-readable report to
stdout. Redirect stdout to retain release evidence. The report contains only check identifiers,
pass/fail state, latency values, targets, event-stage failure categories, and exit code. It never
contains microphone audio, transcripts, generated replies, credentials, or raw exception text.

## Target-machine release sequence

1. Rotate any credential that was ever stored outside the environment/credential manager.
2. Install `requirements.lock` with hashes and install the wheel with `--no-deps`.
3. Verify the published wheel and sdist against the accompanying `SHA256SUMS` before installation.
4. Run `pip check` in that environment.
5. Run local doctor with `--voice-input`.
6. Inject the rotated DeepSeek and Azure credentials and run online doctor.
7. Run `python -m companion --accept-voice-json 1>voice-acceptance.json`; follow the stderr prompts
   and retain the passing JSON report with the release evidence.
8. Enable the avatar only when its bridge extension is running and test Live2D/VRM state sync.
9. Keep mutating computer actions disabled; the shipped Windows provider remains read-only.

An action provider timeout is a process-lifetime quarantine, not a retry signal. For execution and
undo, the external outcome is explicitly unknown because the provider may complete after the
application deadline. Stop issuing actions, inspect the target system and durable audit ledger,
reconcile any partial side effect, then restart the process. Preview or sandbox timeouts also
quarantine the provider because its health is no longer trustworthy. A durable-audit timeout makes
all later actions fail closed until restart.

Normal shutdown, startup failure, single-turn failure, and voice-loop failure all use the same
bounded cleanup path. If logs report a component shutdown timeout or failure, do not restart in a
tight loop: confirm the prior process has exited and that microphone, audio, database, and avatar
resources are no longer held before relaunching.

Voice work uses one cumulative whole-turn deadline across ASR, LLM generation, TTS, and playback.
The shorter TTS-chunk, playback, cleanup, and interruption limits prevent any single provider or
audio-driver operation from consuming that budget indefinitely. A provider that ignores coroutine
cancellation is detached after the observation deadline and its eventual result is consumed; this
guarantees that the application operation returns, but it does not claim to terminate a native
thread or driver owned by that provider. Treat repeated timeout logs as an unhealthy provider or
device and recycle the process only after the normal bounded shutdown path completes.

The cloud LLM timeout is also the total budget for all provider attempts and retry backoff. Only
connection-establishment failures and transient status codes (`408`, `409`, `425`, `429`, and
`5xx`) are retried. A response-read timeout is not retried because the remote service may already
have generated and billed the answer; blindly issuing another POST can duplicate cost and output.
Cancellation targets the active HTTP generation by turn ID, including the period before the first
streamed token. Custom endpoints must be HTTPS URLs without embedded credentials, query strings, or
fragments.

Azure TTS treats an absent credential, non-success HTTP response, transport failure, timeout, or
empty successful response as a failed voice stage; none may be accepted as a silent successful
turn. Cancellation is effective from connection setup onward, not only after response headers have
arrived. A duplicate active turn ID is rejected to prevent one synthesis from overwriting another's
cancellation handle. Repeated TTS transport or empty-audio failures indicate an unhealthy endpoint
and must fail the target-machine spoken-turn gate.

The event bus rejects new work after shutdown and drains any accepted event before the SQLite
memory provider closes. A persistence error is fail-closed: the event is neither delivered nor
retained for in-process replay. Treat repeated persistence or subscriber-timeout logs as a failed
runtime, investigate the database or handler, and restart only after normal cleanup completes.
Timed-out live subscribers are automatically quarantined for the remainder of the process. Both
asynchronous and synchronous subscriber/replay callbacks have hard observation deadlines;
synchronous callbacks run outside the event-loop thread and cannot prevent process exit.

Each top-level component shutdown also has a hard observation deadline. If a component ignores
coroutine cancellation, shutdown continues with the remaining components and consumes the eventual
task result instead of waiting indefinitely. This bounds application exit but cannot forcibly free
native resources held by defective third-party code, so timeout logs still require operator review
before relaunch.

## Memory backup and recovery preparation

Create a verified backup before upgrades, configuration migrations, or any repair operation:

```powershell
python -m companion --backup-memory D:\CompanionBackups\memory-before-upgrade.db
python -m companion --verify-memory-backup D:\CompanionBackups\memory-before-upgrade.db
```

The backup uses SQLite's online backup API, so WAL state is included consistently without copying
live `.db-wal` files. It is written to a random temporary file, checked with `PRAGMA quick_check`,
checked for the supported ownership marker, schema version, tables, and runtime columns, and
atomically moved into place. Existing targets are protected unless `--overwrite-backup` is
explicitly supplied. Store at least one copy outside the runtime directory and test verification
from the installed wheel before relying on it.

Current databases use SQLite `application_id` and `user_version` markers. A complete legacy
companion database with both markers at zero is adopted in place on first startup without deleting
data, and legacy markerless backups remain verifiable. The doctor and runtime reject foreign,
structurally incomplete, or future-version databases before changing their schema or journal mode.
Treat a future-version rejection as a binary rollback problem: restore the matching application
version or a verified compatible backup; never lower `user_version` manually.

Maintenance operations are isolated from live memory traffic. A rebuild holds the shared database
operation boundary until commit or rollback, including cancellation; queued dialogue writes run
only afterward. Online backup likewise waits for its SQLite worker to finish before releasing live
writes, and a cancelled backup removes its unpublished temporary snapshot.
