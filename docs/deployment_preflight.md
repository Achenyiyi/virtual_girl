# Deployment Preflight

## Configuration contract

The packaged and repository YAML templates contain only capabilities implemented by the current
runtime. The loader rejects unsupported provider types, enabled perception/telemetry export, YAML
credentials, invalid cloud endpoints, invalid audio settings, and unsafe action configuration.

The following settings are now authoritative at runtime:

- memory SQLite path, WAL, and FTS configuration;
- `COMPANION_DB_PATH` as an absolute explicit memory-path environment override;
- LLM retries, retry delay, timeout, model, endpoint, and credential environment name;
- Fish Audio TTS model, endpoint, latency mode, timeout, credential source, and 24 kHz PCM format;
- microphone/ASR sample rate, language, pre-roll, speech/silence limits, whole-turn timeout, TTS
  chunk timeout, playback timeout, cleanup timeout, and provider-interruption timeout;
- quiet hours, hourly proactive budgets, and per-level cooldowns;
- rotating file-log size/count, bounded in-memory event retention, avatar bridge, and Windows
  read-only action settings including pending-confirmation capacity and TTL.

Relative data/log/audit paths in the packaged default resolve under
`%LOCALAPPDATA%\VirtualCompanion`, independent of the launch directory. A production YAML should
set `runtime.data_root: user_local` to preserve that boundary when the configuration is supplied
explicitly from a read-only installation. `COMPANION_RUNTIME_DIR` may override the writable data
root with an absolute path. Relative AIRI executable and model paths always resolve from the
explicit configuration file's directory, so moving user data never redirects executable assets.
For compatibility, an explicit YAML that omits `runtime.data_root` keeps resolving relative data
paths from its own directory. An explicit missing config file is an error and never silently falls
back to defaults.

## Doctor commands

Run from the exact Python environment used to start the release:

```powershell
python -m companion --doctor
python -m companion --doctor --voice-input
python -m companion --doctor-online --voice-input
python -m companion --doctor-json --voice-input
python -m companion --accept-voice
python -m companion --accept-voice-json 1>voice-acceptance.json
python -m companion --config production.yaml --accept-avatar
python -m companion --config production.yaml --accept-avatar-json 1>avatar-acceptance.json
```

`--doctor` performs no LLM/TTS network calls. `--doctor-online` validates configured remote
providers. The Fish Audio check uses the read-only wallet credit endpoint and does not synthesize
audio.
No diagnostic message or JSON field includes credential values.

## Windows credential setup

For an installed desktop release, store each rotated secret as a Windows **Generic Credential**
for the same Windows account that runs the companion. Open **Credential Manager > Windows
Credentials > Add a generic credential** and use these default Internet or network addresses:

| Secret | Default target | Temporary environment override |
| --- | --- | --- |
| DeepSeek | `VirtualCompanion/DeepSeek` | `DEEPSEEK_API_KEY` |
| Fish Audio TTS | `VirtualCompanion/FishAudio` | `FISH_API_KEY` |
| Avatar bridge | `VirtualCompanion/AvatarBridge` | `COMPANION_AVATAR_TOKEN` |

The username field is unused; put the secret in the password field. A custom target can be set
with `credential_target` beside the corresponding `api_key_env` or `auth_token_env`. Environment
variables take precedence for CI and one-off acceptance runs. Remove temporary variables after the
run.

The Avatar Bridge credential is a local random token, not a third-party API key. Initialize it
without exposing the value in a shell, process argument, or log:

```powershell
python -m companion --config production.yaml --provision-avatar-token

# Only when deliberately invalidating the prior local bridge token:
python -m companion --config production.yaml --rotate-avatar-token
```

Provisioning refuses to overwrite an existing target. Rotation is explicit; both commands reject
an active `COMPANION_AVATAR_TOKEN` environment override so the stored credential is immediately
authoritative.

Never put a secret in YAML, `.env`, a command argument, a key file, or a credential target name.
After rotation, delete the superseded Generic Credential and create its replacement before running
`--doctor-online`. The runtime reads only the configured Generic Credential; it does not enumerate
the user's credential vault.

## Single-instance boundary

The interactive runtime, voice acceptance gate, and avatar acceptance gate acquire a Windows
session-scoped mutex derived from the resolved memory database path. A second process using the same
companion profile exits before constructing providers or opening microphone, playback, avatar, or
action resources. Windows releases must not work around this guard.

Doctor and online memory backup remain available while the runtime is active: doctor is read-only,
and backup uses SQLite's consistent online backup boundary. A separate development instance must
use a separate `providers.memory.db_path`; sharing only a different config filename is not enough.
The mutex name contains only a hash of the resolved path and does not disclose the Windows username
or database location. Windows abandons the mutex automatically after a process crash.

## Runtime storage boundary

Before constructing providers or opening the rotating log, the runtime checks the resolved memory,
log, and durable action-audit locations. Each location must resolve to a local Windows volume, its
nearest existing parent must accept an exclusive create/write/flush/delete probe, an existing file
must be openable for read/write, and the volume must have at least 512 MiB free. The same memory
check appears in doctor output.

Do not place live SQLite/WAL files on UNC paths, mapped network drives, or cloud-sync directories.
UNC and Windows remote volumes are rejected automatically; a sync directory may appear as local,
so avoiding it remains an operator requirement. Their locking and rename semantics are not a
supported durability boundary. Keep the live profile under local application data and publish
verified online backups to a separate disk or controlled sync destination. Low-space and
permission failures are startup blockers, not warnings.

The runtime also publishes a minimal generation marker beside the memory database after the local
memory health check and before any cloud provider startup. If that marker remains after a crash or
unclean shutdown, the next launch runs a full SQLite `PRAGMA integrity_check` and validates the
companion ownership/schema before starting providers. The marker contains only a schema version,
random run ID, process ID, and UTC start time; it contains no path, credential, model data, or user
content. It is removed only after every top-level component and every orchestrated provider reports
a clean shutdown. Preserve a leftover marker for diagnosis; do not delete it to bypass a failed
recovery check. Restore a verified backup when integrity or schema validation fails.

Exit codes:

- `0`: every required requested check passed; warnings and disabled optional providers are allowed;
- `1`: a required runtime, credential, database, module, device, or provider check failed;
- `2`: configuration could not be parsed or validated.

When `--voice-input` is included, doctor requires faster-whisper, NumPy, sounddevice, a usable
default input device, a usable default output device, and the Fish Audio credential. It does not
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

`--accept-avatar` is independent of LLM and voice readiness. It requires an explicitly enabled
avatar bridge, a non-empty `identity.avatar_model_id`, the configured token credential source,
and a real stage implementing the optional `stage.inspect` release-gate extension documented in
`docs/avatar_bridge_protocol.md`. The command proves authenticated health, Live2D/VRM model
enumeration/validation/load, full-state application, expression and gesture commands, proactive
level, renderer-consumed state sequence, and a newly presented frame. Its JSON output contains
only check identifiers and pass/fail messages; it contains no token, model path, state payload,
user content, screenshot, or raw exception text.

For managed AIRI startup, enable `providers.avatar.launch` and configure the installed
`airi.exe`, adjacent `resources/app.asar`, `resources/godot-stage/godot-stage.exe`, local `.vrm`,
all four SHA-256 values, a fixed `model_id`, and a display name. The launch `model_id` must exactly match
`identity.avatar_model_id`. Relative paths in an explicit production YAML resolve from that YAML's
directory. Local doctor validates all four pinned files plus the VRM/GLB structure without starting
AIRI or opening a GUI. Runtime and `--accept-avatar` launch AIRI before
bridge health checks and always stop it after the WebSocket provider disconnects. Managed launch
requires `ws://127.0.0.1:6122/ws` and `COMPANION_AVATAR_TOKEN`; an existing listener is rejected so
acceptance cannot accidentally pass against an unrelated stage.

The avatar JSON report is necessary but not sufficient for visual release approval. While the gate
runs, an operator must confirm the intended character/model is visible, the happy expression and
nod are visibly applied, animation does not freeze, and there is no release-blocking clipping,
missing texture, broken alpha, or severe lip/expression mismatch. Record that human sign-off next
to the machine report. A self-reported `stage.inspect` result without this visual observation does
not close the AIRI/Live2D/VRM release gate.

## Target-machine release sequence

Follow `docs/release_process.md` for version, evidence, tag, provenance, and GitHub Release gates.

Configuration parsing rejects unknown fields at every supported nesting level. Treat an unknown
field error as a deployment defect; correct the spelling or remove the unsupported option instead
of attempting to bypass validation.

Run `python -m companion --config production.yaml --validate-config` in packaging and deployment
automation. It validates the complete supported schema, types, ranges, provider choices, endpoint
rules, and safety constraints, then exits without reading credentials or touching runtime storage,
devices, network providers, or the single-instance boundary.

1. Revoke every credential that ever appeared in chat, a file, a log, or a command argument.
2. Store only never-disclosed replacements using the Generic Credential targets above.
3. Verify the signed Windows installer's Authenticode signature, timestamp, attestation, and
   `SHA256SUMS` entry, then install it for the current user. It already contains the pinned CPython
   runtime, `requirements-runtime.lock` environment, AIRI stage, and approved VRM.
4. For Python-artifact acceptance only, install `requirements-runtime.lock` with hashes, install the
   wheel with `--no-deps`, verify the wheel/sdist against `SHA256SUMS`, and run `pip check`. Do not
   use the development/release-tool lock as the installed desktop runtime.
5. Run local doctor with `--voice-input` from the exact installed runtime.
6. Run online doctor with the rotated DeepSeek and Fish Audio credentials available from Credential
   Manager or temporary environment overrides.
7. Run `python -m companion --accept-voice-json 1>voice-acceptance.json`; follow the stderr prompts
   and retain the passing JSON report with the release evidence.
8. Enable managed launch for the pinned AIRI build (or supervise the same pinned files externally),
   run `--accept-avatar-json`, retain its passing report, and record the visual sign-off above.
9. Confirm `windows-installer.json` binds the installed bundle to the exact `windows-stage.json`,
   and record the security sign-off required by `docs/release_process.md`.
10. Keep mutating computer actions disabled; the shipped Windows provider remains read-only.

Before building the installer, verify the unpacked AIRI directory with
`scripts/verify_airi_windows.ps1`. For a
release candidate, pass `-RequireAuthenticode`; this must report `Valid` for both `airi.exe` and
`resources/godot-stage/godot-stage.exe`, with signer and timestamp certificates present. Also pass
`-AppVersion` and `-EvidenceJson release-evidence\<tag>\windows-stage.json`; evidence mode refuses
to run without `-RequireAuthenticode` and writes only public hashes, certificate fingerprints, and
approved model-license fields. An unsigned build is suitable only for local visual acceptance and
must not be attached to a public GitHub Release.

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

Fish Audio TTS treats an absent credential, non-success HTTP response, transport failure, timeout, or
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
python -m companion --restore-memory-backup D:\CompanionBackups\memory-before-upgrade.db
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

`--restore-memory-backup` is an offline, single-instance operation. It performs a full SQLite
integrity and ownership/schema check on the source, copies and verifies a private temporary file,
checkpoints the stopped live WAL, preserves the previous database files in a timestamped hidden
rollback directory beside the live database, and then publishes the replacement atomically. It
rejects an active runtime and removes an unpublished replacement after failure. Keep the reported
rollback directory until the restored runtime has passed doctor and a representative conversation;
do not manually mix its `.db`, `-wal`, or `-shm` files with another generation.

Maintenance operations are isolated from live memory traffic. A rebuild holds the shared database
operation boundary until commit or rollback, including cancellation; queued dialogue writes run
only afterward. Online backup likewise waits for its SQLite worker to finish before releasing live
writes, and a cancelled backup removes its unpublished temporary snapshot.
