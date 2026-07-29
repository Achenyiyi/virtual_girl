# Release Process

Normal pushes to `main` create verified CI artifacts, not public releases. A public release is
created only from a stable SemVer tag such as `v0.1.0`, and the tag workflow fails closed unless
all target-machine evidence below is committed in `release-evidence/<tag>/` before tagging. These
files form the auditable release record. Acceptance reports include their schema version,
application version, and UTC generation time; evidence must match the tag and be no older than 30
days. The verifier requires the complete successful check set emitted by each acceptance command
and uses strict field and directory allowlists, so transcripts, generated replies, audio,
screenshots, credentials, paths, and arbitrary notes cannot be committed as release evidence.

Required files:

- `voice-acceptance.json`: exact output of `python -m companion --accept-voice-json`;
- `avatar-acceptance.json`: exact output of `python -m companion --accept-avatar-json`;
- `visual-signoff.json`: human confirmation of the visible AIRI/Live2D/VRM result;
- `security-signoff.json`: confirmation that exposed credentials were revoked, replacements are in
  approved secure storage, and mutating actions remain disabled.

Example sign-off files:

```json
{
  "reviewer": "release owner",
  "observed_at": "2026-07-29T08:00:00+08:00",
  "intended_model_visible": true,
  "animation_healthy": true,
  "rendering_approved": true
}
```

```json
{
  "reviewer": "release owner",
  "observed_at": "2026-07-29T08:00:00+08:00",
  "exposed_credentials_revoked": true,
  "rotated_credentials_stored_securely": true,
  "mutating_actions_disabled": true
}
```

Before creating a tag:

```powershell
python scripts/verify_release_version.py --tag v0.1.0
python scripts/verify_release_evidence.py --tag v0.1.0
git status --short
git add release-evidence/v0.1.0
git commit -m "Record v0.1.0 release acceptance"
gh run watch --exit-status
git tag -s v0.1.0 -m "Virtual Companion v0.1.0"
git push origin v0.1.0
```

The version in `pyproject.toml`, `companion.__version__`, Git tag, and built wheel must match. Push
the evidence commit and wait for its push-triggered `CI` run to pass before creating the tag. The
tagged commit must already be on `main` with that successful CI run. The Release
workflow rebuilds from the tag, verifies archives and checksums, creates GitHub build-provenance
attestations, creates a draft Release with the wheel, sdist, and `SHA256SUMS`, then publishes it.
It verifies the exact asset set while the Release is still a removable draft, then publishes and
immediately requires GitHub to report the Release as immutable with the same assets. If repository
release immutability is disabled, it removes the mutable Release and fails while preserving the
protected tag for a safe rerun. Reruns accept an already-published Release only when GitHub reports
it immutable and its asset set is exact. Do not create or move a release tag to bypass a failed
gate; fix the evidence or source and create a new version.

After publication, verify provenance and checksums before target-machine installation:

```powershell
gh attestation verify virtual_companion-0.1.0-py3-none-any.whl --repo Achenyiyi/virtual_girl
Get-FileHash virtual_companion-0.1.0-py3-none-any.whl -Algorithm SHA256
```

Repository administrators must keep `main` protected: require the `quality` status check, require
pull requests for non-administrative changes, and block force pushes and deletion. The repository
ruleset named `Protect release tags` must remain active for `refs/tags/v*`, with no bypass actors
and update, deletion, and non-fast-forward rules. In repository Settings, the Releases section must
have `Enable release immutability` selected before running a release; this protects assets and the
associated tag after publication and generates a GitHub release attestation.
