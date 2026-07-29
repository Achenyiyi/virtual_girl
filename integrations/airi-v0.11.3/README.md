# AIRI v0.11.3 Avatar Bridge

This directory contains the pinned AIRI-side implementation of Companion Avatar Protocol v1.
It targets AIRI `v0.11.3` (`dbf812488829a61cc2e95909e021b215704d066c`) because AIRI's remote
plugin bootstrap is still empty in that release. The bridge must therefore be integrated into the
desktop Electron main/renderer boundary instead of claiming compatibility with the unfinished
remote plugin API.

The current implementation includes:

- strict protocol envelopes, size limits, authenticated version negotiation, and sanitized errors;
- a loopback-only H3/CrossWS server with bounded per-connection concurrency and deterministic
  shutdown of active peers;
- an Eventa adapter boundary for forwarding authenticated operations to AIRI's main renderer;
- a renderer-owned runtime that validates complete state snapshots and tracks model, state,
  expression, gesture, proactive, visibility, and presented-frame evidence;
- a reproducible patch for the pinned AIRI checkout that starts the bridge from Electron main,
  invokes the renderer over Eventa, writes Live2D/VRM state, and sources model/frame evidence from
  the real renderer callbacks;
- real WebSocket, lifecycle, concurrency, validation, and renderer-evidence tests.

The renderer runtime deliberately cannot advance `rendered_state_sequence` or `frame_sequence`
from a request handler. The supplied patch calls `notifyPresentedFrame()` only after the successful
Live2D Pixi or VRM render callback and `notifyModelLoaded()` only from the actual model-loaded
callback. This concrete wiring is not equivalent to runtime acceptance: the patched AIRI build must
still compile, start with an injected bridge token, pass `--accept-avatar-json`, and receive human
visual sign-off on the target machine.

Apply the pinned patch from the root of a clean AIRI v0.11.3 checkout:

```powershell
git checkout dbf812488829a61cc2e95909e021b215704d066c
git apply --check C:\path\to\virtual_girl\integrations\airi-v0.11.3\airi-v0.11.3-avatar-bridge.patch
git apply C:\path\to\virtual_girl\integrations\airi-v0.11.3\airi-v0.11.3-avatar-bridge.patch
```

The bridge remains disabled unless `COMPANION_AVATAR_TOKEN` is injected into the AIRI process.
Unsupported renderer operations fail closed; in particular, VRM one-shot gestures and proactive
level changes are not reported as successful because AIRI v0.11.3 exposes no matching public API.

For the companion's managed Windows launcher, build or install the unpacked application and pin
both `airi.exe` and `resources/app.asar` in `providers.avatar.launch`. Pinning only the executable
is insufficient because electron-builder packages the reviewed main/renderer bridge code inside
`app.asar`.

Run its checks with:

```powershell
npm ci --ignore-scripts
npm test
npm run typecheck
```

Runtime dependencies are pinned to the same H3/CrossWS versions used by AIRI v0.11.3. The server
must be started only when explicitly enabled, with its token injected by the launcher rather than
stored in AIRI configuration or logs.

The patch leaves AIRI's existing `@mediapipe/tasks-vision` patched dependency and pnpm metadata
unchanged. With the repository-declared pnpm `10.33.0`, a frozen filtered installation of the stage
app and its 31 dependency workspaces succeeds with that upstream patch still enabled. Validation
must distinguish bridge-file errors from unrelated upstream workspace typecheck failures.
