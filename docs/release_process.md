# Release Process

Normal pushes to `main` create verified CI artifacts, not public releases. A public release is
created only from a stable SemVer tag such as `v0.1.0`. The tag workflow publishes one signed
Windows installer plus the Python wheel, sdist, and `SHA256SUMS`; it fails closed unless all
target-machine evidence is complete and a matching installer has already been staged in a GitHub
Draft Release.

## Release evidence

Commit exactly these privacy-safe files under `release-evidence/<tag>/` before tagging:

- `voice-acceptance.json`: exact output of `python -m companion --accept-voice-json`;
- `avatar-acceptance.json`: exact output of `python -m companion --accept-avatar-json`;
- `windows-stage.json`: generated from the signed AIRI/Godot stage and approved VRM;
- `windows-installer.json`: generated only after the signed installer and same-identity signed
  uninstaller pass install/uninstall smoke tests;
- `visual-signoff.json`: human confirmation of the installed avatar result;
- `security-signoff.json`: confirmation that exposed credentials were revoked, replacements are
  securely stored, and mutating actions remain disabled.

Reports must match the release version, be no older than 30 days, and contain only allowlisted
fields. They never contain transcripts, generated replies, audio, screenshots, credentials,
certificate subjects, usernames, or local paths. The verifier also requires `airi.exe`, the Godot
sidecar, installer, and installed uninstaller to use the same Authenticode signing identity. The
installer report
records the SHA-256 of `windows-stage.json`; publication fails if the committed stage evidence is
not byte-for-byte the evidence packaged in the installer.

Example human sign-offs:

```json
{
  "reviewer": "release owner",
  "observed_at": "2026-07-30T20:00:00+08:00",
  "intended_model_visible": true,
  "animation_healthy": true,
  "rendering_approved": true
}
```

```json
{
  "reviewer": "release owner",
  "observed_at": "2026-07-30T20:00:00+08:00",
  "exposed_credentials_revoked": true,
  "rotated_credentials_stored_securely": true,
  "mutating_actions_disabled": true
}
```

## Build the Windows candidate

Build on a trusted Windows x64 machine from a clean, committed source revision. Required inputs:

- the reviewed AIRI `win-unpacked` directory;
- local `model/8496491754682859078.vrm`, matching `managed-avatar.json` exactly;
- a currently valid code-signing certificate with private key and Code Signing EKU in
  `Cert:\CurrentUser\My`;
- Windows SDK `signtool.exe`;
- Python 3.12 with the hash-locked build tools installed.

The build downloads only the CPython 3.12.10 embeddable archive and Inno Setup 7.0.2 inputs pinned
by URL and SHA-256 in `packaging/windows/toolchain.json`. It also verifies the Inno installer's
`Pyrsys B.V.` Authenticode signature and timestamp before executing it. The official Inno Setup
[license](https://github.com/jrsoftware/issrc/blob/is-7_0_2/license.txt) permits use for any purpose,
including commercial applications. Its maintainers separately
[request](https://jrsoftware.org/isorder.php) that qualifying commercial users purchase a license
and state that purchase is not strictly required; organizations should follow their own legal and
procurement policy before a commercial launch.

```powershell
$tag = "v0.1.0"
$version = $tag.Substring(1)
$certificateThumbprint = "<CurrentUser code-signing certificate thumbprint>"

.\scripts\build_windows_installer.ps1 `
  -AiriStagePath $approvedAiriStagePath `
  -ModelPath .\model\8496491754682859078.vrm `
  -AppVersion $version `
  -SigningCertificateThumbprint $certificateThumbprint
```

The command requires CPython 3.12 x64, refuses tracked or untracked source changes (Git-ignored
local model input remains allowed), and refuses to overwrite outputs. It performs this
ordered release boundary:

1. copy the AIRI stage into an isolated build directory;
2. sign and timestamp `airi.exe` and `godot-stage.exe`;
3. generate `windows-stage.json` from their post-signing hashes;
4. extract the pinned CPython runtime and install only `requirements-runtime.lock` plus the wheel;
   all dependencies require wheels except the hash-locked `jieba` source distribution;
5. copy the approved VRM and generate a secret-free relative-path `production.yaml`;
6. inventory and SHA-256 every bundle file in `bundle-manifest.json`; bundled Python entry points
   use `-B` so runtime imports cannot add bytecode to the read-only application tree;
7. compile a per-user x64 Inno Setup installer while Inno signs and timestamps both Setup and its
   generated uninstaller with the approved identity;
8. verify the final installer signature and timestamp;
9. silently install it into a fresh temporary directory, bind the packaged stage evidence to the
   committed evidence, recheck AIRI/Godot/model hashes and signatures, validate every installed
   file, config, voice dependency import and CLI help, require the installed uninstaller to have a
   valid timestamped signature from the same identity, verify the file inventory again after those
   runtime checks, silently uninstall, and require removal of the installation directory;
10. publish the installer to `dist/` and the two Windows evidence files to
    `release-evidence/<tag>/` only after every prior step succeeds.

The model binary is included in the final installer under its approved redistribution terms but
remains Git-ignored and is never committed as source. Runtime data and credentials are not copied
into the bundle.

## Target-machine acceptance

Install the exact signed candidate normally. The installer creates a per-user installation under
`%LOCALAPPDATA%\Programs\Virtual Companion` and provisions a random local Avatar Bridge credential
without displaying it. It does not create or replace DeepSeek or Fish Audio credentials. Store newly
rotated provider credentials in Windows Credential Manager, then run the installed diagnostics and
the voice/avatar acceptance commands described in `docs/deployment_preflight.md`.

The uninstaller removes application files but deliberately retains
`%LOCALAPPDATA%\VirtualCompanion` and Windows Generic Credentials. Back up or delete that user data
explicitly according to the user's retention decision.

## Stage the Draft Release

`windows-installer.json` records the clean source commit used to build the installer. After all six
evidence files pass locally, commit only the evidence. The final tag commit may differ from the
installer source commit only by files under its own `release-evidence/<tag>/` directory.

Before pushing the tag, create a Draft Release targeted at the recorded source commit and upload
exactly the evidenced installer:

```powershell
python scripts/verify_release_version.py --tag $tag
python scripts/verify_release_evidence.py --tag $tag

$installerEvidence = Get-Content "release-evidence\$tag\windows-installer.json" -Raw |
  ConvertFrom-Json
$installer = Join-Path dist $installerEvidence.installer.filename
gh release create $tag $installer `
  --repo Achenyiyi/virtual_girl `
  --draft `
  --target $installerEvidence.source_commit `
  --title "Virtual Companion $tag" `
  --generate-notes
```

Do not add the wheel, sdist, checksums, evidence, model, or logs to this staging draft. The tag
workflow requires exactly one staged asset and independently compares its filename, size, and
GitHub-computed SHA-256 to `windows-installer.json`. If the target commit modifies workflow files
relative to `main`, the credential used by `gh release create` must also be authorized to write
workflow content; GitHub otherwise rejects draft creation.

Commit the evidence through the normal protected-branch workflow and wait for the resulting `main`
CI run to pass. Then create and push the signed annotated tag on that evidence commit. Either GPG
or SSH signing is supported, but the matching public key must be registered in GitHub as a signing
key so the Git Data API reports the tag signature as verified:

```powershell
git tag -s $tag -m "Virtual Companion $tag"
git push origin $tag
```

## Automated publication

The tag workflow requires all of the following before publication:

- the release ref is an annotated tag that directly targets the workflow commit and has a
  GitHub-verified signature; lightweight and unverified tags fail closed;
- the tag commit is on `main` and already has a successful `main` CI run;
- all six evidence files pass their strict schema, version, freshness, license, signer, and check
  requirements;
- the installer's recorded source commit is an ancestor of the tag and every later changed file is
  confined to `release-evidence/<tag>/`;
- the existing GitHub Release is a mutable draft targeted at that source commit and contains only
  the evidenced installer, with matching GitHub-reported size and SHA-256;
- the downloaded installer still has the evidenced hash, trusted signature, timestamp, complete
  bundle, and repeatable silent install/uninstall behavior;
- the rebuilt wheel and sdist pass content/version checks and `SHA256SUMS` covers all three binary
  artifacts;
- GitHub creates attestations for the installer, wheel, sdist, and checksum file;
- every final asset's remote size and GitHub-computed SHA-256 match the verified local files before
  publication and remain exact after GitHub reports the Release immutable.

A failure leaves the Draft Release in place for inspection; the workflow never deletes or silently
replaces the signed installer. Correct the cause and explicitly remove any partially uploaded
non-installer assets before rerunning. Never move an existing release tag to bypass a failed gate;
create a new version when released content must change.

## Post-publication verification

```powershell
gh attestation verify VirtualCompanion-0.1.0-windows-x64.exe `
  --repo Achenyiyi/virtual_girl
gh attestation verify virtual_companion-0.1.0-py3-none-any.whl `
  --repo Achenyiyi/virtual_girl
Get-FileHash VirtualCompanion-0.1.0-windows-x64.exe -Algorithm SHA256
Get-AuthenticodeSignature VirtualCompanion-0.1.0-windows-x64.exe
```

Repository administrators must keep `main` protected, require the relevant CI checks, block force
pushes and deletion, and retain the `Protect release tags` ruleset for `refs/tags/v*` with no bypass
actors. `Enable release immutability` must remain selected; the workflow confirms it from GitHub's
post-publication `isImmutable` result.
