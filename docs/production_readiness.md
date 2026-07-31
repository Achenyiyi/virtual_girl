# Production Readiness Specification

## Objective

Deliver a Windows virtual-companion application that is safe for real user data,
recovers cleanly after restart or failure, provides a complete interruptible voice
experience, and fails closed when a required dependency is unavailable.

## Source-deployment gates

### Security and privacy

- [x] No plaintext API credentials exist in tracked source or public downloads; CI secret scanning
      reports zero candidates, and local credentials remain outside Git.
- [x] Production credentials come from an environment override or native Windows Credential
      Manager lookup; YAML and key-file credentials are rejected.
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
- [x] Offline restore validates the source, checkpoints the stopped live WAL, atomically publishes
      the replacement, and preserves the prior file set for rollback.

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
- [x] AIRI/avatar rendering and emotion synchronization pass an end-to-end scenario.

### Operations and quality

- [x] Readiness fails for invalid credentials, unavailable required providers, or database errors.
- [x] No-data telemetry reports `unknown`, never `passing`.
- [x] Runtime dependencies are locked and reproducible; package and clean-machine install pass.
- [x] A credential-safe deployment doctor validates configuration, SQLite integrity, local voice
      modules/devices, and optionally remote provider health with deterministic exit codes.
- [x] Runtime logs and diagnostic histories have explicit memory/disk bounds and rotation.
- [x] A Windows session-scoped single-instance boundary prevents duplicate runtimes from sharing
      one profile, microphone, playback device, or avatar stage.
- [x] Runtime databases and logs require a genuinely writable local Windows volume with at least
      512 MiB free before providers start.
- [x] An unclean-exit marker forces full SQLite integrity and ownership/schema validation before
      cloud startup, and is cleared only after every runtime/provider shutdown succeeds.
- [x] Unit, integration, recovery, security, and package-validation suites pass in Windows CI.
- [x] Ruff and mypy pass for production code; critical production paths meet agreed coverage.
- [x] Public GitHub distribution is source-only: no Releases, uploaded Actions artifacts, Windows
      binaries, wheel/sdist downloads, package-registry publication, or binary attestations.
- [x] The Windows installer build pins CPython/Inno inputs, requires one valid Code Signing identity
      for AIRI, Godot, Setup, and Uninstall,
      binds the approved AIRI/VRM stage into a full-file manifest, and proves silent install,
      immutable runtime smoke checks, and uninstall before evidence can be emitted. It is dormant
      under source-only publication and retains no unsigned bypass.
- [x] Protected `main` requires the `quality` check and blocks force pushes/deletion; source changes
      merge only through the reviewed branch workflow.
- [x] GitHub CodeQL default setup covers Python and Actions, and Dependabot vulnerability alerts
      and security updates are enabled.

## Current status

Status: **ready for source-only GitHub publication and personal local use**.

Completed in the current milestone: environment/Credential Manager credential resolution,
authoritative event persistence, causal fact references, atomic memory rebuild, stable
time-versioned preference keys, single-use action confirmation, provider timeouts, strict
LLM readiness status, unknown no-data telemetry, and standards-compliant WAV generation.

Current milestone: publish only reviewed source through protected `main`, retain the user's current
DeepSeek and Fish Audio credentials solely in the local Windows credential boundary, and keep all
program binaries and build artifacts off GitHub. The unified installer pipeline remains dormant and
still requires Authenticode if binary distribution is reconsidered. Mutating actions remain out of
scope until a separate OS isolation boundary exists.

Voice implementation status: the code path now includes optional local faster-whisper ASR,
contextual runtime generation, network-streamed Fish Audio PCM, gapless sounddevice playback,
played-audio confirmation, and barge-in cancellation. Voice turn admission is serialized while
the explicit barge-in path remains concurrent; ASR/LLM/TTS/playback failures emit sanitized durable
terminal events, cancellation cannot leave a started turn dangling, and interruption checks prevent
late provider or event commits from resuming generation or playback. On the target Windows machine,
the final credential-backed path passed all eight `--accept-voice-json` checks with the current free
Fish budget: first audio 2,248 ms against a 30,000 ms target and interruption 19 ms against a 300 ms
target. This proves current connectivity, incremental playback, history accounting, and barge-in
behavior; paid-model low-latency targets must be revalidated after switching away from the free
model.

Avatar implementation status: the Python runtime now owns a versioned, authenticated WebSocket
bridge with bounded messages and timeouts, concurrent request correlation, model validation/load,
full affect-derived state snapshots, proactive-level synchronization, reconnect, and clean
shutdown. It is disabled by default and fails startup closed when explicitly enabled but unhealthy.
The AIRI `v0.11.3` protocol core, loopback H3/CrossWS server, Eventa forwarding boundary, and
renderer-owned evidence state machine are implemented and tested under `integrations/airi-v0.11.3`.
A pinned patch now wires Electron startup/shutdown, Eventa renderer invocation, Live2D/VRM state
operations, actual model-loaded callbacks, successful render callbacks, and main-window visibility.
Renderer-side loading now invalidates stale model evidence. The patch leaves AIRI's existing
MediaPipe patched dependency and pnpm metadata unchanged, passes `git apply --check`, and supports a
frozen filtered install of the pinned stage application plus its 31 dependency workspaces with pnpm
`10.33.0`. The Python runtime can now optionally supervise that Windows build: configuration pins
the Electron executable, `resources/app.asar`, and executable Godot sidecar; child credentials are
allowlisted, startup
uses a suspended process assigned to a kill-on-close Job Object, and runtime/acceptance cleanup owns
the process tree. Managed bridge mode also disables AIRI's startup update check and every manual
check/download/install path, preventing the reviewed pinned build from replacing itself with an
unreviewed upstream release. Doctor validates the installation without launching a GUI. The real
managed VRM run now passes all six machine checks and the captured frames pass visual review for
model identity, textures, alpha, framing, animation, expression, and gesture. AIRI's
remote plugin bootstrap is unfinished at the pinned upstream commit, so no undocumented remote API
is claimed. See `docs/avatar_bridge_protocol.md`.

Natural gesture status: each successfully committed text or voice turn may now trigger at most one
affect-derived one-shot gesture. Scheduling uses monotonic global and per-gesture cooldowns, skips a
recently repeated gesture in favor of the next eligible suggestion, and quarantines a failed motion
for five minutes. Startup and full-state synchronization never trigger one-shot gestures, and an
unsupported model motion is logged without changing the already-completed dialogue result.

Avatar acceptance status: the runtime CLI now has an LLM-independent `--accept-avatar` gate. A
production stage extension must expose renderer-owned inspection evidence showing the configured
Live2D/VRM model is loaded and visible, the full state was consumed by the render loop, the expected
expression/gesture/proactive level was applied, and a later frame was presented. The inspection
schema is strict and privacy-safe. On the target Windows machine, the real
`--accept-avatar-json` command passed all six checks against the managed VRM and the captured frames
received operator visual review. The privacy-safe report remains a local, Git-ignored personal-use
record and is not a GitHub release asset.

Managed VRM status: the user-supplied `Nemesia_pajamas` model is now pinned by local path, size,
SHA-256, fixed model ID, GLB 2.0 structure, and VRM metadata. Python validates it before process
creation; AIRI independently validates it again, serves only the approved file through a private
Electron protocol, registers it without IndexedDB persistence, and selects it as the default main
stage model. The model binary remains a Git-ignored local user asset. Its embedded VRoid Hub license
and the owner-confirmed model page allow corporate/personal commercial use, redistribution, and
modification without attribution; the exact permissions are hash-bound in `managed-avatar.json`
and documented in `docs/third_party_assets.md`. The patched renderer build completes, includes a
real humanoid nod, and passed managed-model acceptance. The VRM and AIRI executables remain local and
are not part of the source-only GitHub publication.

Windows AIRI build status: the repository now has a separate 90-minute Windows workflow plus
PowerShell build and artifact-verification scripts, with the AIRI commit, Node, pnpm, Electron,
electron-builder, Godot, and verified download digests recorded in a machine-readable manifest.
The workflow builds an unsigned acceptance candidate only inside its ephemeral runner and does not
upload it. It is not a public artifact. If binary distribution is restored, it cannot pass the
release verifier with `-RequireAuthenticode` until a real code-signing identity is provisioned.

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
voice capture parameters, quiet hours, proactive budgets/cooldowns, Fish Audio TTS settings, and event-log
retention) are now wired into runtime construction. Unsupported advertised providers were removed
from the default YAML and unsafe or unimplemented configuration fails closed. The new `--doctor`
family provides human and JSON preflight reports without exposing credential values; online Fish Audio
health uses the read-only wallet credit endpoint. Production configuration now separates read-only
config-relative AIRI/model assets from `%LOCALAPPDATA%` databases, logs, and audit state, so a
Program Files installation does not require write access or redirect executable paths when the data
root is overridden. See `docs/deployment_preflight.md`.

Credential storage status: LLM, Fish Audio TTS, and avatar token resolution now uses one constrained
security boundary. A process environment value is an explicit temporary override; otherwise the
runtime reads only the configured Windows Generic Credential through `CredReadW` and never
enumerates the vault. Legacy LLM key-file loading was removed, and YAML rejects both inline and
file credential fields.

Cloud LLM status: the configured timeout is a cumulative budget covering all attempts and backoff,
not a fresh allowance per HTTP request. Retries are limited to connection-establishment failures and
explicitly transient HTTP statuses; response-read timeouts are not retried because the provider may
already have completed and billed the generation. Active non-streaming and streaming calls are
tracked by turn ID, so barge-in cancels the actual HTTP operation even before the first token.
Streaming remains incremental rather than buffering the entire response. Custom endpoints reject
URL user information, queries, and fragments so credentials cannot be embedded in routable URLs.

Cloud TTS status: missing credentials, HTTP failures, transport failures, hard timeouts, and
successful responses with empty audio all propagate as sanitized synthesis failures instead of
silently completing an inaudible turn. Active non-streaming and streaming synthesis is tracked by
turn ID, allowing cancellation during connection setup and before the first PCM chunk. Duplicate
active turn IDs are rejected, response streams are always released, and provider shutdown bounds
both response and client closure even when third-party close operations ignore cancellation. Fish
Audio requests now use the official conversation-friendly parameter surface: `latency: balanced`,
24 kHz PCM, explicit sampling/prosody controls, cross-chunk consistency, and 480-byte text
segmentation for earlier playback, responsive cancellation, and smaller request failure domains.
The complete response is synthesized across all segments; the chunk size is not a free-tier
character-limit workaround.

Dialogue output safety status: assistant replies are sanitized before they reach UI, memory, voice
synthesis, or avatar follow-on behavior. Tool-call-shaped JSON, internal tool directives, and
analysis/final leakage are stripped or replaced with a safe companion utterance so internal agent
mechanics cannot become spoken character dialogue.

Target-machine voice acceptance status: the release CLI now provides an interactive, credential-
backed gate that separately proves one complete microphone-to-playback turn and one real-microphone
barge-in. Runtime barge-in now fires on the VAD speech-start edge rather than waiting for the
interrupting utterance to finish. The gate consumes the same configured providers and durable
event ledger as production, checks
the configured first-audio and 300 ms interruption targets, measured incremental playback, one-stream
PCM continuity without underflow, and exact completed/interrupted history accounting. It returns
deterministic exit codes and can emit privacy-safe JSON evidence without transcripts, responses,
audio, credentials, or raw exception messages. The earlier target-machine run passed latency and
interruption checks, but fresh evidence is required because the current gate contains these stronger
streaming, continuity, and history checks.

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

Single-instance status: the interactive runtime and real voice/avatar acceptance gates acquire a
named Windows mutex derived from a SHA-256 hash of the resolved memory path before constructing
providers. A duplicate profile exits deterministically without opening user data or devices. The
mutex is released on normal and exceptional exits and is abandoned automatically by Windows after
a crash. Doctor and consistent online backup are intentionally not blocked.

Storage readiness status: memory, rotating logs, and durable action audit paths pass one shared
fail-closed boundary before provider construction. It rejects UNC and Windows remote volumes,
performs a real exclusive create/write/fsync/delete probe, verifies existing files can be opened
read/write, and requires 512 MiB free. Doctor uses the same memory-path check. Live SQLite/WAL data
is therefore not claimed safe on mapped/network storage; verified online backups are the supported
transfer mechanism.

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
Offline restore uses the same profile mutex, full integrity/schema validation before and after
copy, a checkpointed live generation, atomic replacement, and a timestamped rollback directory.

The action and perception features must remain disabled until their corresponding gates pass.

## Verification evidence (2026-07-31)

- `ruff check companion tests scripts`: passed.
- `mypy companion scripts`: passed in strict mode for 79 source files.
- `pytest -q --cov=companion --cov-report=term --cov-fail-under=70`: 530 tests passed with 79.35%
  total coverage; the Windows read-only provider is 92%, WebSocket avatar provider 83%, voice
  pipeline 81%, cloud LLM 65%, cloud TTS 84%, avatar acceptance 80%, action service 82%, and the
  action audit store 94%.
- Windows Credential Manager integration passed focused resolution, precedence, validation, and
  native missing-target tests. Environment overrides take precedence; absent or unreadable Generic
  Credentials fail closed without enumerating the vault or exposing credential content.
- Configuration loading rejects unknown fields at every supported nesting level, preventing
  misspelled security, timeout, audit, provider, and proactive-policy options from silently falling
  back to defaults. The side-effect-free `--validate-config` command is enforced by CI.
- The Windows single-instance boundary passed same-process and real child-process contention tests,
  profile-path normalization, independent-profile, idempotent release, and pre-provider CLI exit
  cases. The named mutex discloses only a truncated SHA-256 digest.
- Runtime storage readiness passed real default-path write/flush/delete probing with 5,802 MiB free,
  plus low-space, remote-volume, existing-file permission, no-residue, doctor, and pre-provider CLI
  failure tests.
- The avatar integration test exercised a real loopback WebSocket server: authenticated version
  negotiation, health, model list/validate/load, full state mapping, timeout, disconnect,
  reconnect, and shutdown all passed. Orchestrator readiness fails for an unhealthy configured
  avatar.
- The avatar acceptance gate passed all six checks against the real managed AIRI process and pinned
  Nemesia VRM, including authenticated health, model availability/load, visible renderer evidence,
  applied state and command sequences, and presented-frame advancement. Operator visual review of
  the intended model, textures, framing, happy expression, animation, and nod also passed.
- A real Win32 integration test executed the capability-confined system-status action and verified
  the persisted SQLite hash chain. Boundary tests proved `open_app`, parameter injection, forged
  risk/method values, and unknown actions cannot reach provider execution.
- On the target machine's isolated local environment, doctor found faster-whisper, NumPy,
  sounddevice, a usable default microphone, and a usable default output device. A separate online
  doctor resolved DeepSeek from `VirtualCompanion/DeepSeek` and Fish Audio from
  `VirtualCompanion/FishAudio`, then completed provider health checks without exposing credentials.
  This proves the current locally accepted credentials are usable.
- The explicit hardware doctor first downloaded/loaded the configured faster-whisper `base` model
  in 34.3 seconds. With the cached model, the latest run loaded it in 4.8 seconds,
  completed a real CTranslate2 in-memory silence inference in 0.2 seconds, captured 16 microphone
  frames through the production stream, and opened the production playback stream with 20 ms of
  silence. Credential-backed Fish Audio voice acceptance then passed the full microphone,
  faster-whisper, DeepSeek, Fish streaming TTS, playback, and barge-in path.
- `requirements.lock` pins runtime, voice, development, and release tooling for CI/build hosts;
  `requirements-runtime.lock` is a constrained runtime-only subset used inside the installer and
  isolated wheel checks. There is no unhashed `requirements.txt` dependency entry point.
- The hash-locked `build 1.5.0`/`setuptools 83.0.0` toolchain built the wheel and sdist successfully.
  Automated archive checks
  require the packaged YAML, metadata, and license; reject databases, tests, key-named files,
  bytecode, unsafe archive paths, and runtime data; and emit `SHA256SUMS`.
- `pip-audit -r requirements.lock` and `pip-audit -r requirements-runtime.lock`: no known
  vulnerabilities in either locked dependency boundary.
- A fresh Python 3.12 virtual environment installed `requirements.lock` with `--require-hashes`,
  installed the wheel with `--no-deps`, passed `pip check`, imported `companion`,
  `faster_whisper`, `sounddevice`, and `numpy`, and completed a CLI help smoke test.
- The pinned CPython 3.12.10 embeddable archive was assembled with the runtime-only lock, current
  wheel, and approved 30,688,684-byte Nemesia VRM. Its isolated interpreter imported `companion`,
  CTranslate2, faster-whisper, NumPy, and sounddevice, validated the generated production config,
  ran CLI help, and left the complete 5,110-file bundle unchanged after runtime checks.
- Inno Setup 7.0.2 passed its pinned digest, `Pyrsys B.V.` signature, and timestamp checks. It
  compiled that real runtime/VRM bundle from and into paths containing spaces; the resulting
  103,882,173-byte unsigned lifecycle fixture installed silently, passed full bundle verification
  both before and after runtime imports, uninstalled silently, and removed its installation
  directory. Synthetic AIRI placeholder files were used because no release signing certificate is
  currently available; this is not final signed-installer evidence.
- A wheel-only installation loaded its packaged default YAML from an unrelated empty working
  directory; the sdist excludes tests and includes the hash-locked runtime requirements.
- A wheel-only installation created a live WAL-backed memory database in a previously missing
  directory, produced and verified an online backup from an unrelated working directory, and then
  verified the backup again after its source YAML was removed. Corrupt, incomplete, same-path,
  in-memory, and accidental-overwrite cases are rejected by tests.
- `.github/workflows/ci.yml` reproduces lint, type, coverage, secret, vulnerability, package-content,
  checksum, and isolated-wheel-install gates on Windows. Third-party Actions are pinned to full
  immutable commit SHAs and the job has read-only repository permissions. Package outputs remain
  ephemeral and are not uploaded for download.
- Git history now has a clean `main` baseline commit. Ignored local credentials, memory data,
  virtual environments, and build artifacts were excluded before staging.
- Tracked-source `detect-secrets` reported zero candidates. `pip-audit` reported no known
  vulnerabilities. A hash-locked isolated environment passed `pip check`; the newly built wheel
  installed there and loaded packaged configuration from an unrelated working directory.
- AIRI's bridge suite passed 39 Node tests and TypeScript type checking. GitHub workflow syntax and
  expressions passed checksum-verified `actionlint 1.7.12` in addition to PowerShell parser and YAML
  parser checks.

## Remaining boundaries and future blockers

- The current public surface is source-only. GitHub has no Release, tag, uploaded program artifact,
  package publication, or binary attestation. The 42 historical downloadable program artifacts
  were removed after the source-only workflow reached `main`; the three CodeQL SARIF artifacts were
  deliberately retained.
- The current DeepSeek and Fish Audio credentials are accepted for personal local use and remain in
  Windows Credential Manager. Their values must never enter source, reports, logs, or command
  arguments; revoke them immediately if compromise is suspected.
- Local voice modules, default devices, Fish Audio synthesis, gapless playback, history accounting,
  and barge-in passed together 8/8 on the target machine. The local privacy-safe report is ignored by
  Git and is not public release evidence.
- The pinned AIRI candidate, `app.asar`, Godot sidecar, and managed VRM have approved hashes and pass
  the real 6/6 avatar gate plus operator visual confirmation. They remain local assets and are not
  uploaded to GitHub.
- Authenticode is not required for publishing source or running it personally. It becomes mandatory
  again before any future public Windows executable or installer distribution; the dormant build
  and verifier scripts intentionally provide no unsigned bypass.
- File changes, messages, application control, input automation, and other mutating actions remain
  deliberately unavailable. Enabling them later requires a separate OS isolation boundary and
  target-device acceptance tests.
- Dependency hashes were generated and verified on Windows/Python 3.12. Cross-platform binary
  deployment is not claimed; regenerate and verify the lock on every newly supported OS/Python
  target.
