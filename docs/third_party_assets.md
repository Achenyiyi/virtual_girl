# Third-party assets

## Nemesia pajamas VRM

The managed character image is the exact local file pinned by
`integrations/airi-v0.11.3/managed-avatar.json`:

- display title: `Nemesia_pajamas`;
- author: `awa`;
- managed ID: `managed-nemesia-pajamas`;
- SHA-256: `6c093fb4e37cda43e2bc89df36c9a93d1f42741fbd6ea7dd57a893e32a6fe31d`.

The pinned VRM 0.x metadata contains an official `hub.vroid.com/license` URL. Its permission
parameters allow use by everyone, corporate commercial use, personal commercial use for profit,
redistribution, and modification; attribution is unnecessary. The project owner independently
confirmed the same permissions on the model's VRoid Hub page on 2026-07-30.

The Windows AIRI verifier compares the exact model digest, size, embedded author/title and license
metadata against the manifest. It also parses the official license URL and fails closed unless the
commercial-use, redistribution, modification, and attribution values match the approved record.

Permission to redistribute does not make an arbitrary replacement model distributable. Any model
change requires a new digest, a new manifest license review, and a new visual acceptance run. The
VRM binary remains a Git-ignored local asset and is not included in the Python wheel or source
distribution. The Windows installer build includes it only after the manifest, embedded metadata,
size, digest, release evidence, and installed bundle inventory all match this approved record.
