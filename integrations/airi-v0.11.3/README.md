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
- real WebSocket, lifecycle, concurrency, validation, and renderer-evidence tests.

The renderer runtime deliberately cannot advance `rendered_state_sequence` or `frame_sequence`
from a request handler. AIRI must call `notifyPresentedFrame()` from the successful Live2D Pixi or
VRM render hook and `notifyModelLoaded()` from the actual model-loaded callback. The remaining
integration work is wiring these hooks and the concrete Live2D/VRM store operations into AIRI's
Electron/Eventa application at the pinned commit. Until that wiring is built and exercised, this
directory does not claim that AIRI rendering acceptance has passed.

Run its checks with:

```powershell
npm ci --ignore-scripts
npm test
npm run typecheck
```

Runtime dependencies are pinned to the same H3/CrossWS versions used by AIRI v0.11.3. The server
must be started only when explicitly enabled, with its token injected by the launcher rather than
stored in AIRI configuration or logs.
