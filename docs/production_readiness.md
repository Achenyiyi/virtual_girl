# Production Readiness Specification

## Objective

Deliver a Windows virtual-companion application that is safe for real user data,
recovers cleanly after restart or failure, provides a complete interruptible voice
experience, and fails closed when a required dependency is unavailable.

## Release gates

### Security and privacy

- [ ] No plaintext API credentials exist in the project or release artifacts.
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
- [x] Unit, integration, recovery, security, and packaged-wheel suites pass in Windows CI.
- [x] Ruff and mypy pass for production code; critical production paths meet agreed coverage.
- [x] Public releases require matching source/tag/wheel versions, prior green main CI, real voice
      and avatar evidence, human/security sign-off, checksums, and GitHub provenance attestations.
- [x] The Windows installer build pins CPython/Inno inputs, requires one valid Code Signing identity
      for AIRI, Godot, Setup, and Uninstall,
      binds the approved AIRI/VRM stage into a full-file manifest, and proves silent install,
      immutable runtime smoke checks, and uninstall before evidence can be emitted.
- [x] A no-bypass repository ruleset prevents `v*` release tags from being updated or deleted;
      release publication fails closed unless GitHub marks the complete asset set immutable.
- [x] GitHub CodeQL default setup covers Python and Actions, and Dependabot vulnerability alerts
      and security updates are enabled.

## Current status

Status: **not production ready**.

Completed in the current milestone: environment/Credential Manager credential resolution,
authoritative event persistence, causal fact references, atomic memory rebuild, stable
time-versioned preference keys, single-use action confirmation, provider timeouts, strict
LLM readiness status, unknown no-data telemetry, and standards-compliant WAV generation.

Active milestone: revoke every credential disclosed in chat, validate the interruptible voice path
with never-disclosed DeepSeek and Fish Audio credentials, provision Authenticode signing, produce
the first signed installer from the real AIRI stage, and complete human security/visual sign-off.
The unified installer pipeline itself is implemented and has passed an unsigned lifecycle test.
Mutating actions remain out of release scope until a separate OS isolation boundary exists.

Voice implementation status: the code path now includes optional local faster-whisper ASR,
contextual runtime generation, network-streamed Fish Audio PCM, gapless sounddevice playback,
played-audio confirmation, and barge-in cancellation. Voice turn admission is serialized while
the explicit barge-in path remains concurrent; ASR/LLM/TTS/playback failures emit sanitized durable
terminal events, cancellation cannot leave a started turn dangling, and interruption checks prevent
late provider or event commits from resuming generation or playback. The gate remains open until
this exact path passes latency and device tests on the target Windows machine with real credentials.

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

Avatar acceptance status: the release CLI now has an LLM-independent `--accept-avatar` gate. A
production stage extension must expose renderer-owned inspection evidence showing the configured
Live2D/VRM model is loaded and visible, the full state was consumed by the render loop, the expected
expression/gesture/proactive level was applied, and a later frame was presented. The inspection
schema is strict and privacy-safe. On the target Windows machine, the real
`--accept-avatar-json` command passed all six checks against the managed VRM and the captured frames
received operator visual review. Release-specific JSON evidence and sign-off must still be generated
for each tag as required by `docs/release_process.md`.

Managed VRM status: the user-supplied `Nemesia_pajamas` model is now pinned by local path, size,
SHA-256, fixed model ID, GLB 2.0 structure, and VRM metadata. Python validates it before process
creation; AIRI independently validates it again, serves only the approved file through a private
Electron protocol, registers it without IndexedDB persistence, and selects it as the default main
stage model. The model binary remains a Git-ignored local user asset. Its embedded VRoid Hub license
and the owner-confirmed model page allow corporate/personal commercial use, redistribution, and
modification without attribution; the exact permissions are hash-bound in `managed-avatar.json`
and documented in `docs/third_party_assets.md`. The patched renderer build completes, includes a
real humanoid nod, and passed managed-model acceptance. Public distribution remains blocked until
the AIRI-owned executables are Authenticode-signed and the per-tag evidence record is complete.

Windows AIRI build status: the repository now has a separate 90-minute Windows workflow plus
PowerShell build and artifact-verification scripts, with the AIRI commit, Node, pnpm, Electron,
electron-builder, Godot, and verified download digests recorded in a machine-readable manifest.
The workflow deliberately labels its output an unsigned acceptance candidate. It is not a release
artifact and cannot pass the release verifier with `-RequireAuthenticode` until a real code-signing
identity is provisioned.

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
both response and client closure even when third-party close operations ignore cancellation.

Target-machine voice acceptance status: the release CLI now provides an interactive, credential-
backed gate that separately proves one complete microphone-to-playback turn and one real-microphone
barge-in. Runtime barge-in now fires on the VAD speech-start edge rather than waiting for the
interrupting utterance to finish. The gate consumes the same configured providers and durable
event ledger as production, checks
the 900 ms voice-turn-to-first-audio and 300 ms interruption targets, returns deterministic exit
codes, and can emit privacy-safe JSON evidence without transcripts, responses, audio, credentials,
or raw exception messages. The gate remains unpassed until run with rotated credentials.

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

## Verification evidence (2026-07-30)

- `ruff check companion tests scripts`: passed.
- `mypy companion scripts`: passed in strict mode for 78 source files.
- `pytest -q --cov=companion --cov-report=term --cov-fail-under=70`: 478 tests passed with 78.96%
  total coverage; the Windows read-only provider is 92%, WebSocket avatar provider 83%, voice
  pipeline 85%, cloud LLM 64%, cloud TTS 81%, avatar acceptance 80%, action service 82%, and the
  action audit store 94%.
- Windows Credential Manager integration passed focused resolution, precedence, validation, and
  native missing-target tests. Environment overrides take precedence; absent or unreadable Generic
  Credentials fail closed without enumerating the vault or exposing credential content.
- Configuration loading rejects unknown fields at every supported nesting level, preventing
  misspelled security, timeout, audit, provider, and proactive-policy options from silently falling
  back to defaults. The side-effect-free `--validate-config` command is enforced by both CI and the
  tag release workflow.
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
- The avatar release gate passed all six checks against the real managed AIRI process and pinned
  Nemesia VRM, including authenticated health, model availability/load, visible renderer evidence,
  applied state and command sequences, and presented-frame advancement. Operator visual review of
  the intended model, textures, framing, happy expression, animation, and nod also passed.
- A real Win32 integration test executed the capability-confined system-status action and verified
  the persisted SQLite hash chain. Boundary tests proved `open_app`, parameter injection, forged
  risk/method values, and unknown actions cannot reach provider execution.
- On the target machine's isolated release environment, doctor found faster-whisper, NumPy,
  sounddevice, a usable default microphone, and a usable default output device. A separate online
  doctor resolved DeepSeek from `VirtualCompanion/DeepSeek` and completed the provider health check
  without exposing the credential. This proves current credential usability, not revocation of any
  previously exposed key. Voice preflight currently fails only because
  `VirtualCompanion/FishAudio` has not yet passed credential-backed target-machine acceptance.
- The explicit hardware doctor first downloaded/loaded the configured faster-whisper `base` model
  in 34.3 seconds. With the cached model, the latest run loaded it in 4.8 seconds,
  completed a real CTranslate2 in-memory silence inference in 0.2 seconds, captured 16 microphone
  frames through the production stream, and opened the production playback stream with 20 ms of
  silence. Credential-backed Fish Audio synthesis remains pending.
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
- `.github/workflows/ci.yml` reproduces lint, type, coverage, secret, vulnerability, artifact,
  checksum, and isolated-wheel-install gates on Windows. Third-party Actions are pinned to full
  immutable commit SHAs and the job has read-only repository permissions. GitHub Actions run
  `30410669222` passed every gate on the remote `main` branch and uploaded verified artifacts.
- Git history now has a clean `main` baseline commit. Ignored local credentials, memory data,
  virtual environments, and build artifacts were excluded before staging.
- Release-source `detect-secrets` reported zero candidates. `pip-audit` reported no known
  vulnerabilities. A hash-locked isolated environment passed `pip check`; the newly built wheel
  installed there and loaded packaged configuration from an unrelated working directory.
- AIRI's bridge suite passed 39 Node tests and TypeScript type checking. GitHub workflow syntax and
  expressions passed checksum-verified `actionlint 1.7.12` in addition to PowerShell parser and YAML
  parser checks.

## Open release blockers

- `deepseek_key.txt` is ignored and never read by the application. The DeepSeek credential most
  recently supplied in chat was also disclosed before it was stored in Windows Credential Manager,
  so it must be revoked again. Release sign-off must prove the stored credential is a later
  replacement that never appeared in conversation, files, logs, or command arguments.
- The repository owner enabled `Enable release immutability` on 2026-07-29. GitHub currently
  exposes this as a web setting rather than a public REST or GraphQL read field, so the first real
  Release must still prove it through GitHub's `isImmutable` result. The workflow fails closed and
  leaves the mutable Draft Release in place for explicit inspection; it never silently deletes or
  replaces staged assets.
- The initial CodeQL default-setup analysis completed successfully on 2026-07-29. Its two
  clear-text-logging findings in the application startup path were addressed by keeping configured
  credential source identifiers out of those terminal messages; a subsequent main analysis must
  confirm that those high-severity alerts close.
- Dependabot vulnerability alerts and security updates are enabled. The initial dependency-graph
  job exposed an incompatible `requirements.txt` indirection and that compatibility wrapper was
  removed; a subsequent main-branch graph job must confirm successful ingestion of `pyproject.toml`
  and the audited `requirements.lock`.
- Local voice modules and default devices now pass doctor in the isolated release environment, but
  no credential-backed test has yet proven microphone capture, faster-whisper model loading,
  Fish Audio streaming TTS, gapless playback, and barge-in latency together.
- The pinned AIRI candidate, `app.asar`, Godot sidecar, and managed VRM now have approved hashes and
  pass the real 6/6 avatar gate. Public release is still blocked because `airi.exe` and
  `godot-stage.exe` lack Authenticode signatures and trusted timestamps. The release gate now
  requires a privacy-safe per-tag `windows-stage.json`; the current unsigned candidate is proven to
  fail closed without emitting that evidence.
- The unified Windows installer and immutable Draft Release promotion pipeline now exist and the
  runtime/VRM lifecycle passes unsigned validation. Public release remains blocked until a trusted
  certificate signs and timestamps the real AIRI executable, Godot sidecar, final installer, and
  generated uninstaller, producing matching `windows-stage.json` and `windows-installer.json`
  evidence.
- Human security review and production sign-off remain pending.
- File changes, messages, application control, input automation, and other mutating actions remain
  deliberately unavailable. This does not block the current read-only release scope; enabling them
  later requires a separate OS isolation boundary and target-device acceptance tests.
- End users run the bundled CPython runtime, not the global Anaconda interpreter. Trusted source
  builders require CPython 3.12 x64 plus the full hash lock. DeepSeek online health passed in an
  isolated environment, while Fish Audio credential-backed voice acceptance remains pending.
- Dependency hashes were generated and verified on Windows/Python 3.12. Cross-platform releases
  are not claimed; regenerate and verify the lock on every newly supported OS/Python target.
