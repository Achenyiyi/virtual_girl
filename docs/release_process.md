# Source-Only Release Process

## Current publication boundary

The public release surface is the Git repository source code only. GitHub Releases are not created.
The repository and the source archives that GitHub generates from the **Code** page are the only
public downloads maintained by this project.

Do not publish or upload any of the following:

- Windows installers, executables, unpacked AIRI applications, or archives containing runnable
  program files;
- Python wheels, source distributions, package-index releases, or checksum bundles for them;
- VRM/model binaries, credentials, acceptance media, local databases, logs, or evidence reports;
- GitHub Actions artifacts, Release assets, or provenance attestations for program binaries.

This boundary matches the current personal-use deployment. It removes Authenticode as a blocker for
publishing the source repository because no Windows binary is being distributed.

## Continuous integration boundary

CI may build a wheel, sdist, AIRI stage, or other temporary output inside an ephemeral runner when
that is necessary to validate packaging and integration behavior. Those files must remain inside the
runner and must not be uploaded, attached to a Release, published to a package registry, or otherwise
made downloadable.

Repository tests enforce this policy by rejecting known artifact-upload, GitHub Release,
attestation, and Python-package publication actions or commands in every workflow. The tag-triggered
binary publication workflow is intentionally absent.

## Personal source deployment

Use a reviewed commit from protected `main` on the Windows machine that will run the companion:

```powershell
git clone https://github.com/Achenyiyi/virtual_girl.git
Set-Location virtual_girl
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install --disable-pip-version-check --require-hashes -r requirements.lock
python -m pip install --disable-pip-version-check --no-deps -e .
python -m companion --validate-config
```

Keep `production.yaml`, provider credentials, the managed VRM, AIRI executables, runtime data, and
acceptance reports local and Git-ignored. Store provider and Avatar Bridge credentials through the
Windows Generic Credential targets documented in `docs/deployment_preflight.md`. The current Fish
Audio model remains `s2.1-pro-free`; a later paid-mode migration only changes the configured model
name and requires a fresh local voice acceptance run.

Before relying on a new source revision locally, run the doctor and the voice/avatar acceptance
checks in `docs/deployment_preflight.md`. These checks are local operational evidence and are not
uploaded to GitHub.

## Publishing source changes

1. Inspect the complete diff and verify that local secrets, models, databases, logs, build outputs,
   and acceptance evidence are absent.
2. Run the focused tests for changed behavior, then the full test/coverage, Ruff, mypy, configuration,
   secret-scan, workflow syntax, PowerShell parse, and diff checks used by CI.
3. Commit the coherent source change on a non-default branch and push it to GitHub.
4. Merge through a pull request after the protected `quality` check passes. Do not bypass branch
   protection.
5. Confirm the public repository has no GitHub Release, no program artifact, and no workflow capable
   of uploading or publishing one. Do not create a release tag for the source-only launch.

## Dormant Windows installer tooling

The Windows installer, verification, and evidence scripts remain in the repository for a possible
future binary-distribution decision. Keeping those tools does not authorize their output for public
distribution and does not weaken their fail-closed behavior.

If public Windows binary distribution is restored later, it is a separate release project. Before
publishing any executable output, obtain a trusted Authenticode code-signing certificate, retain the
existing signature and timestamp requirements for AIRI, Godot, Setup, and Uninstall, regenerate
target-machine evidence, review third-party redistribution rights, and introduce a separately
reviewed publishing workflow. Do not add an unsigned-installer bypass.
