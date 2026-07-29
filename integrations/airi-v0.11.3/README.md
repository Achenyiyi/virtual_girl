# AIRI v0.11.3 Avatar Bridge

This directory contains the pinned AIRI-side implementation of Companion Avatar Protocol v1.
It targets AIRI `v0.11.3` (`dbf812488829a61cc2e95909e021b215704d066c`) because AIRI's remote
plugin bootstrap is still empty in that release. The bridge must therefore be integrated into the
desktop Electron main/renderer boundary instead of claiming compatibility with the unfinished
remote plugin API.

The current vertical slice implements and tests the protocol core: strict envelopes and size
limits, authenticated version negotiation, method routing, and sanitized failures. It deliberately
does not manufacture `stage.inspect` values. The renderer adapter must source model readiness,
visibility, applied state sequences, and frame advancement from AIRI's actual Live2D/VRM render
loop before this integration can close the production avatar gate.

Run its checks with:

```powershell
npm ci
npm test
npm run typecheck
```
