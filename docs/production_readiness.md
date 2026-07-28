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
- [x] Memory consistency checks pass after normal conversation, restart, forget, and rebuild.
- [x] Rebuild is atomic: failure leaves the previous derived memory intact.

### Computer actions

- [x] PolicyGate is the single authorization path.
- [x] Irreversible actions always require a fresh explicit confirmation and preview.
- [x] Confirmation is single-use and concurrent duplicate confirmation is idempotent.
- [x] Provider execution has an enforced timeout and durable redacted audit records.
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
played-audio confirmation, and barge-in cancellation. The gate remains open until this exact
path passes latency and device tests on the target Windows machine with real credentials.

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

The action and perception features must remain disabled until their corresponding gates pass.

## Verification evidence (2026-07-29)

- `ruff check companion tests`: passed.
- `mypy companion`: passed in strict mode for 64 source files.
- `pytest -q --cov=companion --cov-fail-under=70`: 177 tests passed with 74.52% total
  coverage; the Windows read-only provider is 92%, WebSocket avatar provider 81%, voice pipeline
  83%, action service 80%, and the action audit store 94%.
- The avatar integration test exercised a real loopback WebSocket server: authenticated version
  negotiation, health, model list/validate/load, full state mapping, timeout, disconnect,
  reconnect, and shutdown all passed. Orchestrator readiness fails for an unhealthy configured
  avatar.
- A real Win32 integration test executed the capability-confined system-status action and verified
  the persisted SQLite hash chain. Boundary tests proved `open_app`, parameter injection, forged
  risk/method values, and unknown actions cannot reach provider execution.
- `python -m build`: wheel and sdist built successfully; the wheel contains the MIT license and
  contains no database, test credential, or key-named file.
- `pip-audit -r requirements.lock`: no known vulnerabilities in the locked runtime and voice
  dependency tree.
- A fresh Python 3.12 virtual environment installed `requirements.lock` with `--require-hashes`,
  installed the wheel with `--no-deps`, passed `pip check`, imported `companion`,
  `faster_whisper`, `sounddevice`, and `numpy`, and completed a CLI help smoke test.
- A wheel-only installation loaded its packaged default YAML from an unrelated empty working
  directory; the sdist excludes tests and includes the hash-locked runtime requirements.
- `.github/workflows/ci.yml` reproduces lint, type, coverage, vulnerability, and build gates on
  Windows; it has not yet run on a remote GitHub repository.
- Release-source `detect-secrets` reported zero candidates. `pip-audit` reported no known
  vulnerabilities. A hash-locked isolated environment passed `pip check`; the newly built wheel
  installed there and loaded packaged configuration from an unrelated working directory.

## Open release blockers

- `deepseek_key.txt` is ignored and never read by the application, but the credential it once
  contained must be revoked/rotated outside this repository before release.
- No real-device test has yet proven microphone capture, faster-whisper model loading, Azure TTS,
  gapless playback, and barge-in latency together on the target Windows machine.
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
