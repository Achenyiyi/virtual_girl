# Production Readiness Specification

## Objective

Deliver a Windows virtual-companion application that is safe for real user data,
recovers cleanly after restart or failure, provides a complete interruptible voice
experience, and fails closed when a required dependency is unavailable.

## Release gates

### Security and privacy

- [ ] No plaintext API credentials exist in the project or release artifacts.
- [x] Production credentials come from an environment secret or Windows credential store.
- [x] Secret scanning and dependency vulnerability scanning pass for release sources and the
      locked dependency tree.
- [x] Logs and action audit records redact credentials and sensitive parameters.

### Event ledger and memory

- [x] Every domain event is recorded before subscribers process it, including events with
      no subscribers.
- [x] Completed conversation turns are persisted and extracted facts reference real event IDs.
- [x] Every started text turn reaches one durable terminal event; failed configuration,
      generation, persistence, and cancellation paths are recorded without storing exception text.
- [x] Voice turns use the same durable terminal invariant across ASR, LLM, TTS, playback,
      persistence, cancellation, and barge-in paths.
- [x] Memory consistency checks pass after normal conversation, restart, forget, and rebuild.
- [x] Rebuild is atomic: failure leaves the previous derived memory intact.
- [x] Live memory can be backed up consistently, verified independently, and protected from
      accidental overwrite before upgrade or repair.

### Computer actions

- [x] PolicyGate is the single authorization path.
- [x] Irreversible actions always require a fresh explicit confirmation and preview.
- [x] Confirmation is single-use and concurrent duplicate confirmation is idempotent.
- [x] Pending confirmations expire, have a fixed capacity, and cannot authorize stale actions.
- [x] Preview, sandbox verification, execution, undo, and durable audit writes have hard observation
      deadlines; a timed-out provider or audit store is quarantined and later actions fail closed.
- [x] The Windows provider is capability-confined to three parameterless read-only Win32 queries;
      real execution and the durable audit chain pass on the target Windows machine.
- [ ] A separate OS-isolated provider is required before enabling any mutating action.

### Voice and presence

- [ ] ASR -> LLM -> streaming TTS -> playback works end to end on the target machine.
- [ ] Playback and in-flight provider requests can be interrupted within the latency target.
- [x] Generated audio is standards-compliant and device failures are reported accurately.
- [x] The authenticated, versioned avatar bridge passes a local WebSocket integration scenario,
      including model operations, state synchronization, timeouts, reconnect, and shutdown.
- [ ] AIRI/avatar rendering and emotion synchronization pass an end-to-end scenario.

### Operations and quality

- [x] Readiness fails for invalid credentials, unavailable required providers, or database errors.
- [x] No-data telemetry reports `unknown`, never `passing`.
- [x] Runtime dependencies are locked and reproducible; package and clean-machine install pass.
- [x] A credential-safe deployment doctor validates configuration, SQLite integrity, local voice
      modules/devices, and optionally remote provider health with deterministic exit codes.
- [x] Runtime logs and diagnostic histories have explicit memory/disk bounds and rotation.
- [ ] Unit, integration, recovery, security, and end-to-end suites pass in CI.
- [x] Ruff and mypy pass for production code; critical production paths meet agreed coverage.

## Current status

Status: **not production ready**.

Completed in the current milestone: environment-only default credential configuration,
authoritative event persistence, causal fact references, atomic memory rebuild, stable
time-versioned preference keys, single-use action confirmation, provider timeouts, strict
LLM readiness status, unknown no-data telemetry, and standards-compliant WAV generation.

Active milestone: rotate the exposed local credential, validate the interruptible voice path and
mutating-action isolation, implement the AIRI-side stage extension, and obtain the first green
remote CI run.

Voice implementation status: the code path now includes optional local faster-whisper ASR,
contextual runtime generation, network-streamed Azure PCM, gapless sounddevice playback,
played-audio confirmation, and barge-in cancellation. Voice turn admission is serialized while
the explicit barge-in path remains concurrent; ASR/LLM/TTS/playback failures emit sanitized durable
terminal events, cancellation cannot leave a started turn dangling, and interruption checks prevent
late provider or event commits from resuming generation or playback. The gate remains open until
this exact path passes latency and device tests on the target Windows machine with real credentials.

Avatar implementation status: the Python runtime now owns a versioned, authenticated WebSocket
bridge with bounded messages and timeouts, concurrent request correlation, model validation/load,
full affect-derived state snapshots, proactive-level synchronization, reconnect, and clean
shutdown. It is disabled by default and fails startup closed when explicitly enabled but unhealthy.
The remaining renderer gate is an AIRI-side extension and a real Live2D/VRM end-to-end run; AIRI's
remote plugin bootstrap is unfinished at the inspected upstream commit, so no undocumented Eventa
wire format is hard-coded. See `docs/avatar_bridge_protocol.md`.

Action implementation status: a real Windows provider now exposes only `check_system_status`,
`read_window_title`, and `read_active_app`. It has no shell or generic command surface and rejects
parameters, unknown actions, method/risk mismatches, and foreign sandbox IDs. Enabling it requires
the immutable capability allowlist and a valid durable audit chain; otherwise startup fails. It
remains disabled by default because foreground metadata has privacy implications. Mutating actions
remain unavailable until a separate OS-isolated provider is designed and validated. See
`docs/windows_readonly_actions.md`.

Action timeout status: provider and durable-audit operations use hard observation deadlines rather
than cancellation-dependent waits. Any provider timeout quarantines that provider for the process;
execution or undo timeout is reported as an unknown side-effect outcome, and no later request or
pending confirmation may cross the provider boundary. An audit timeout similarly quarantines the
store and makes all subsequent actions fail closed. Detached tasks have eventual exceptions
consumed, but operators must reconcile any unknown external side effect before restart.

Deployment status: configuration values that were previously documentary-only (memory path,
voice capture parameters, quiet hours, proactive budgets/cooldowns, Azure region, and event-log
retention) are now wired into runtime construction. Unsupported advertised providers were removed
from the default YAML and unsafe or unimplemented configuration fails closed. The new `--doctor`
family provides human and JSON preflight reports without exposing credential values; online Azure
health uses the read-only voices-list endpoint. See `docs/deployment_preflight.md`.

Cloud LLM status: the configured timeout is a cumulative budget covering all attempts and backoff,
not a fresh allowance per HTTP request. Retries are limited to connection-establishment failures and
explicitly transient HTTP statuses; response-read timeouts are not retried because the provider may
already have completed and billed the generation. Active non-streaming and streaming calls are
tracked by turn ID, so barge-in cancels the actual HTTP operation even before the first token.
Streaming remains incremental rather than buffering the entire response. Custom endpoints reject
URL user information, queries, and fragments so credentials cannot be embedded in routable URLs.

Long-running resource status: file logs rotate at 10 MiB with five backups by default; replay,
action, audit, proactive, latency, and audio queues are bounded. The durable SQLite event ledger is
not truncated by these in-memory limits. Confirmation-requiring actions have a ten-item pending
limit and a two-minute TTL by default, with expired requests revoked and audited.

Lifecycle status: all runtime entry paths now converge on one idempotent shutdown task. Startup,
single-turn, voice-loop, and microphone-cleanup failures cannot bypass application cleanup. Audio,
voice, provider, memory, avatar, action, and audit shutdowns run independently with per-component
timeouts; one failure cannot prevent later cleanup, and caller cancellation is propagated only
after the underlying shutdown task finishes. Shutdown drains the event bus before closing its
memory persistence provider.

Event delivery status: concurrent publishes are serialized as durable commit units. Persistence
must succeed before an event enters the bounded replay log or reaches subscribers; a cancelled
caller waits for the accepted commit to resolve. Subscriber failures are isolated, hung handlers
time out at a hard observation deadline even if they ignore cancellation, and timed-out live
subscribers are quarantined. Synchronous handlers run on disposable daemon threads so they cannot
freeze the event loop or prevent process exit; replay handlers use the same bounded invocation.
Shutdown drains an in-flight publish, and the closed bus rejects publishing, replay, new
subscriptions, and persistence-handler replacement.

Text conversation lifecycle status: text turns are serialized so sequence numbers, prompt history,
affect updates, and terminal events cannot interleave. Configuration, LLM generation, pre-completion
persistence, and cancellation failures emit a typed `conversation.turn.failed` event with a
sanitized error category. Cancellation during a committed completion waits for conversation history
to catch up, preserving exactly one terminal state. Voice turns retain their separate completed and
interrupted lifecycle and still require the target-device end-to-end gate below.

Voice conversation lifecycle status: one admitted voice/text-to-speech turn owns the pipeline until
it reaches `completed`, `interrupted`, or `failed`; concurrent callers queue instead of silently
replacing the active turn. Barge-in also cancels active ASR and re-checks ownership after ASR, LLM,
TTS-event, and playback boundaries. A completion claim is made before its durable publish so an
accepted completion cannot race an interrupt into two terminal events. Post-completion derived
history failures are logged as degradation rather than creating a contradictory failed terminal.
ASR and LLM share a cumulative whole-turn deadline with TTS and playback. Separate TTS-chunk,
playback, cleanup, and interruption limits keep stalled providers and drivers from blocking public
operations or shutdown indefinitely. Non-cooperative coroutines are cancelled and detached at the
observation deadline with eventual exceptions consumed; this bounds the application operation but
does not forcibly terminate third-party native threads.

Memory operations status: the SQLite service creates its own configured parent directory and
provides CLI-level online backup and independent verification. Backup publication is atomic,
existing targets are protected by default, WAL-backed live state is captured through SQLite's
backup API, and corrupt or structurally incomplete snapshots are rejected. SQLite ownership and
schema-version markers now prevent foreign files and future schemas from being opened or silently
downgraded. Complete legacy markerless databases are adopted without data loss, and old verified
backups remain readable. Shared-connection access is serialized with same-task transaction
re-entry: concurrent first use creates one connection, rebuilds exclude ordinary reads/writes,
failed or cancelled rebuilds roll back before queued work proceeds, and online backups hold the
database boundary until their worker has finished even if the caller is cancelled.

The action and perception features must remain disabled until their corresponding gates pass.

## Verification evidence (2026-07-29)

- `ruff check companion tests scripts`: passed.
- `mypy companion scripts`: passed in strict mode for 66 source files.
- `pytest -q --cov=companion --cov-fail-under=70`: 307 tests passed with 78.71% total
  coverage; the Windows read-only provider is 92%, WebSocket avatar provider 81%, voice pipeline
  85%, cloud LLM 63%, action service 82%, and the action audit store 94%.
- The avatar integration test exercised a real loopback WebSocket server: authenticated version
  negotiation, health, model list/validate/load, full state mapping, timeout, disconnect,
  reconnect, and shutdown all passed. Orchestrator readiness fails for an unhealthy configured
  avatar.
- A real Win32 integration test executed the capability-confined system-status action and verified
  the persisted SQLite hash chain. Boundary tests proved `open_app`, parameter injection, forged
  risk/method values, and unknown actions cannot reach provider execution.
- On the target machine's isolated release environment, doctor found faster-whisper, NumPy,
  sounddevice, a usable default microphone, and a usable default output device. It currently fails
  only the required DeepSeek and Azure credential checks, as expected before rotated credentials
  are injected.
- The explicit hardware doctor first downloaded/loaded the configured faster-whisper `base` model
  in 34.3 seconds. With the cached model, the final implementation loaded it in 2.9 seconds,
  completed a real CTranslate2 in-memory silence inference in 0.2 seconds, captured 15 microphone
  frames through the production stream, and opened the production playback stream with 20 ms of
  silence. Credential-backed Azure synthesis remains pending.
- The single hash lock now covers runtime, voice, development, and release tooling. CI installs
  only that lock plus the project with `--no-deps`; `requirements.txt` delegates to the lock
  instead of maintaining a second dependency definition.
- `python -m build --no-isolation`: wheel and sdist built successfully. Automated archive checks
  require the packaged YAML, metadata, and license; reject databases, tests, key-named files,
  bytecode, unsafe archive paths, and runtime data; and emit `SHA256SUMS`.
- `pip-audit -r requirements.lock`: no known vulnerabilities in the complete locked dependency
  tree.
- A fresh Python 3.12 virtual environment installed `requirements.lock` with `--require-hashes`,
  installed the wheel with `--no-deps`, passed `pip check`, imported `companion`,
  `faster_whisper`, `sounddevice`, and `numpy`, and completed a CLI help smoke test.
- A wheel-only installation loaded its packaged default YAML from an unrelated empty working
  directory; the sdist excludes tests and includes the hash-locked runtime requirements.
- A wheel-only installation created a live WAL-backed memory database in a previously missing
  directory, produced and verified an online backup from an unrelated working directory, and then
  verified the backup again after its source YAML was removed. Corrupt, incomplete, same-path,
  in-memory, and accidental-overwrite cases are rejected by tests.
- `.github/workflows/ci.yml` reproduces lint, type, coverage, secret, vulnerability, artifact,
  checksum, and isolated-wheel-install gates on Windows. Third-party Actions are pinned to full
  immutable commit SHAs and the job has read-only repository permissions. It has not yet run on a
  remote GitHub repository.
- Git history now has a clean `main` baseline commit. Ignored local credentials, memory data,
  virtual environments, and build artifacts were excluded before staging.
- Release-source `detect-secrets` reported zero candidates. `pip-audit` reported no known
  vulnerabilities. A hash-locked isolated environment passed `pip check`; the newly built wheel
  installed there and loaded packaged configuration from an unrelated working directory.

## Open release blockers

- `deepseek_key.txt` is ignored and never read by the application, but the credential it once
  contained must be revoked/rotated outside this repository before release.
- Local voice modules and default devices now pass doctor in the isolated release environment, but
  no credential-backed test has yet proven microphone capture, faster-whisper model loading,
  Azure streaming TTS, gapless playback, and barge-in latency together.
- The Python avatar bridge is complete and locally integration-tested, but AIRI still needs a thin
  stage extension implementing the documented contract, followed by a real Live2D/VRM rendering
  and emotion-synchronization test. AIRI's currently inspected remote-plugin bootstrap is not a
  stable implementation target.
- The real Windows read-only provider is wired and validated, but file changes, messages,
  application control, input automation, installation, and other mutating actions remain
  unavailable. They require a separate OS isolation boundary and target-device acceptance tests.
- The current system Python has an unrelated pre-existing `paddlex`/PyYAML version conflict; the
  project's isolated hash-locked release environment passes `pip check`. Azure/DeepSeek
  environment variables are not present, so hardware- and credential-backed end-to-end testing
  remains pending.
- No Git remote is configured, so the checked-in GitHub Actions workflow cannot produce its first
  remote green run until a repository URL and push authorization are provided.
- Dependency hashes were generated and verified on Windows/Python 3.12. Cross-platform releases
  are not claimed; regenerate and verify the lock on every newly supported OS/Python target.
