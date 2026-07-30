[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$AiriStagePath,

    [Parameter(Mandatory = $true)]
    [string]$ModelPath,

    [Parameter(Mandatory = $true)]
    [string]$AppVersion,

    [Parameter(Mandatory = $true)]
    [string]$SigningCertificateThumbprint,

    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),

    [string]$WheelPath = "",

    [string]$PythonEmbedArchive = "",

    [string]$InnoSetupInstaller = "",

    [string]$SignToolPath = "",

    [string]$OutputDirectory = "",

    [string]$EvidenceDirectory = ""
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

if ($AppVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "AppVersion must be stable SemVer"
}
$thumbprint = $SigningCertificateThumbprint.Replace(" ", "").ToUpperInvariant()
if ($thumbprint -notmatch '^[0-9A-F]{40}$') {
    throw "SigningCertificateThumbprint must be a SHA-1 certificate thumbprint"
}

$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$stageInput = (Resolve-Path -LiteralPath $AiriStagePath).Path
$model = (Resolve-Path -LiteralPath $ModelPath).Path
$toolchainPath = Join-Path $repository "packaging\windows\toolchain.json"
$innoScript = Join-Path $repository "packaging\windows\installer.iss"
$runtimeLock = Join-Path $repository "requirements-runtime.lock"
$fullLock = Join-Path $repository "requirements.lock"
$modelManifestPath = Join-Path $repository "integrations\airi-v0.11.3\managed-avatar.json"
$stageVerifier = Join-Path $repository "scripts\verify_airi_windows.ps1"
$bundleAssembler = Join-Path $repository "scripts\assemble_windows_bundle.py"
$bundleVerifier = Join-Path $repository "scripts\verify_windows_bundle.py"
$installerVerifier = Join-Path $repository "scripts\verify_windows_installer.ps1"

foreach ($requiredFile in @(
    $toolchainPath,
    $innoScript,
    $runtimeLock,
    $fullLock,
    $modelManifestPath,
    $stageVerifier,
    $bundleAssembler,
    $bundleVerifier,
    $installerVerifier
)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required installer input is unavailable: $requiredFile"
    }
}
if (-not (Test-Path -LiteralPath $stageInput -PathType Container)) {
    throw "AIRI stage is unavailable"
}
if (-not (Test-Path -LiteralPath $model -PathType Leaf)) {
    throw "Managed avatar is unavailable"
}
$reparseEntries = @(
    Get-ChildItem -LiteralPath $stageInput -Recurse -Force |
        Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }
)
if ($reparseEntries.Count -ne 0) {
    throw "AIRI stage must not contain links or junctions"
}

$toolchain = Get-Content -LiteralPath $toolchainPath -Raw | ConvertFrom-Json
if (
    $toolchain.schema_version -ne 1 -or
    $toolchain.target.os -ne "windows" -or
    $toolchain.target.architecture -ne "x86_64"
) {
    throw "Windows installer toolchain manifest is unsupported"
}

$trackedChanges = @(git -C $repository status --porcelain --untracked-files=all)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the source worktree"
}
if ($trackedChanges.Count -ne 0) {
    throw "The source worktree must be clean before building an installer"
}
$sourceCommit = (git -C $repository rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $sourceCommit -cnotmatch '^[0-9a-f]{40}$') {
    throw "Unable to resolve the installer source commit"
}

$hostPython = Get-Command python -CommandType Application -ErrorAction Stop
$script:ResolvedPython = (Resolve-Path -LiteralPath $hostPython.Source).Path
$pythonProbeJson = & $script:ResolvedPython -I -s -c @'
import json, platform, struct, sys
print(json.dumps({"implementation": sys.implementation.name, "version": list(sys.version_info[:3]), "machine": platform.machine().lower(), "bits": struct.calcsize("P") * 8}))
'@
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the host Python runtime"
}
$pythonProbe = $pythonProbeJson | ConvertFrom-Json
$requiredPython = [Version]$toolchain.python.version
if (
    $pythonProbe.implementation -cne "cpython" -or
    $pythonProbe.version.Count -ne 3 -or
    $pythonProbe.version[0] -ne $requiredPython.Major -or
    $pythonProbe.version[1] -ne $requiredPython.Minor -or
    $pythonProbe.bits -ne 64 -or
    $pythonProbe.machine -notin @("amd64", "x86_64")
) {
    throw "Installer builds require CPython $($requiredPython.Major).$($requiredPython.Minor) x64"
}

& $script:ResolvedPython (Join-Path $repository "scripts\verify_release_version.py") `
    --tag "v$AppVersion"
if ($LASTEXITCODE -ne 0) {
    throw "Installer version does not match the source version"
}
$modelManifest = Get-Content -LiteralPath $modelManifestPath -Raw | ConvertFrom-Json
$expectedModelSha256 = $modelManifest.sha256
if (
    $expectedModelSha256 -notmatch '^[0-9a-f]{64}$' -or
    (Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash.ToLowerInvariant() -ne (
        $expectedModelSha256
    ) -or
    (Get-Item -LiteralPath $model).Length -ne $modelManifest.size_bytes
) {
    throw "Managed avatar does not match its approved manifest"
}

if (-not $OutputDirectory.Trim()) {
    $OutputDirectory = Join-Path $repository "dist"
}
if (-not $EvidenceDirectory.Trim()) {
    $EvidenceDirectory = Join-Path $repository "release-evidence\v$AppVersion"
}
$output = [IO.Path]::GetFullPath($OutputDirectory)
$evidence = [IO.Path]::GetFullPath($EvidenceDirectory)
New-Item -ItemType Directory -Force -Path $output | Out-Null
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
$installerName = "VirtualCompanion-$AppVersion-windows-x64.exe"
$finalInstaller = Join-Path $output $installerName
$finalStageEvidence = Join-Path $evidence "windows-stage.json"
$finalInstallerEvidence = Join-Path $evidence "windows-installer.json"
foreach ($newOutput in @($finalInstaller, $finalStageEvidence, $finalInstallerEvidence)) {
    if (Test-Path -LiteralPath $newOutput) {
        throw "Refusing to overwrite an existing installer output: $newOutput"
    }
}

function Get-VerifiedFile(
    [string]$ProvidedPath,
    [string]$Url,
    [string]$ExpectedSha256,
    [string]$Destination,
    [string]$Name
) {
    if ($ExpectedSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "$Name manifest SHA-256 is invalid"
    }
    if ($ProvidedPath.Trim()) {
        $resolved = (Resolve-Path -LiteralPath $ProvidedPath).Path
    }
    else {
        if (-not $Url.StartsWith("https://", [StringComparison]::Ordinal)) {
            throw "$Name download URL must use HTTPS"
        }
        Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
        $resolved = $Destination
    }
    $actual = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256) {
        throw "$Name SHA-256 mismatch"
    }
    return $resolved
}

function Get-CertificateSha256(
    [Security.Cryptography.X509Certificates.X509Certificate2]$Certificate
) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $algorithm.ComputeHash($Certificate.RawData)
        return [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Assert-ValidSignature([string]$Path, [string]$AssetName) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if (
        $signature.Status.ToString() -ne "Valid" -or
        $null -eq $signature.SignerCertificate -or
        $null -eq $signature.TimeStamperCertificate
    ) {
        throw "$AssetName requires a valid Authenticode signature and timestamp"
    }
    return $signature
}

function Sign-ReleaseFile([string]$Path, [string]$AssetName) {
    & $script:ResolvedSignTool sign `
        /sha1 $script:SigningCertificate.Thumbprint `
        /fd $toolchain.signing.file_digest_algorithm `
        /tr $toolchain.signing.timestamp_url `
        /td $toolchain.signing.timestamp_digest_algorithm `
        $Path
    if ($LASTEXITCODE -ne 0) {
        throw "$AssetName signing failed"
    }
    $signature = Assert-ValidSignature $Path $AssetName
    if ($signature.SignerCertificate.Thumbprint -ne $script:SigningCertificate.Thumbprint) {
        throw "$AssetName was signed by an unexpected certificate"
    }
}

$certificateCandidates = @(
    Get-ChildItem -LiteralPath "Cert:\CurrentUser\My" |
        Where-Object { $_.Thumbprint -eq $thumbprint }
)
if ($certificateCandidates.Count -ne 1 -or -not $certificateCandidates[0].HasPrivateKey) {
    throw "Exactly one CurrentUser code-signing certificate with a private key is required"
}
$script:SigningCertificate = $certificateCandidates[0]
$now = [DateTime]::UtcNow
if (
    $script:SigningCertificate.NotBefore.ToUniversalTime() -gt $now -or
    $script:SigningCertificate.NotAfter.ToUniversalTime() -le $now
) {
    throw "The code-signing certificate is not currently valid"
}
$ekuExtensions = @(
    $script:SigningCertificate.Extensions |
        Where-Object { $_.Oid.Value -eq "2.5.29.37" }
)
if ($ekuExtensions.Count -ne 1) {
    throw "The signing certificate must declare an Enhanced Key Usage extension"
}
$codeSigningUsages = @(
    $ekuExtensions[0].EnhancedKeyUsages |
        Where-Object { $_.Value -eq "1.3.6.1.5.5.7.3.3" }
)
if ($codeSigningUsages.Count -ne 1) {
    throw "The signing certificate must include the Code Signing EKU"
}

if ($SignToolPath.Trim()) {
    $script:ResolvedSignTool = (Resolve-Path -LiteralPath $SignToolPath).Path
}
else {
    $windowsKits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    $signTools = @(
        Get-ChildItem -LiteralPath $windowsKits -Recurse -File -Filter "signtool.exe" `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.Directory.Name -eq "x64" } |
            Sort-Object FullName -Descending
    )
    if ($signTools.Count -eq 0) {
        throw "Windows SDK x64 signtool.exe is required"
    }
    $script:ResolvedSignTool = $signTools[0].FullName
}

$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$work = Join-Path $tempRoot ("virtual-companion-installer-build-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work | Out-Null
$createdOutputs = [Collections.Generic.List[string]]::new()
try {
    $downloads = Join-Path $work "downloads"
    New-Item -ItemType Directory -Path $downloads | Out-Null
    $pythonEmbed = Get-VerifiedFile `
        $PythonEmbedArchive `
        $toolchain.python.embed_url `
        $toolchain.python.embed_sha256 `
        (Join-Path $downloads "python-embed.zip") `
        "CPython embeddable runtime"
    $innoInstaller = Get-VerifiedFile `
        $InnoSetupInstaller `
        $toolchain.inno_setup.installer_url `
        $toolchain.inno_setup.installer_sha256 `
        (Join-Path $downloads "inno-setup.exe") `
        "Inno Setup installer"
    $innoSignature = Assert-ValidSignature $innoInstaller "Inno Setup installer"
    $innoPublisher = $innoSignature.SignerCertificate.GetNameInfo(
        [Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
        $false
    )
    if ($innoPublisher -cne $toolchain.inno_setup.publisher) {
        throw "Inno Setup installer publisher is not approved"
    }

    $signedStage = Join-Path $work "signed-stage"
    Copy-Item -LiteralPath $stageInput -Destination $signedStage -Recurse
    $airi = Join-Path $signedStage "airi.exe"
    $appAsar = Join-Path $signedStage "resources\app.asar"
    $godot = Join-Path $signedStage "resources\godot-stage\godot-stage.exe"
    foreach ($stageFile in @($airi, $appAsar, $godot)) {
        if (-not (Test-Path -LiteralPath $stageFile -PathType Leaf)) {
            throw "Signed AIRI stage input is incomplete: $stageFile"
        }
    }
    Sign-ReleaseFile $airi "airi.exe"
    Sign-ReleaseFile $godot "godot-stage.exe"
    $airiSha256 = (Get-FileHash -LiteralPath $airi -Algorithm SHA256).Hash.ToLowerInvariant()
    $appAsarSha256 = (
        Get-FileHash -LiteralPath $appAsar -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $godotSha256 = (Get-FileHash -LiteralPath $godot -Algorithm SHA256).Hash.ToLowerInvariant()
    $stageEvidence = Join-Path $work "windows-stage.json"
    & $stageVerifier `
        -InstallationPath $signedStage `
        -ExpectedExeSha256 $airiSha256 `
        -ExpectedAppAsarSha256 $appAsarSha256 `
        -ExpectedGodotSha256 $godotSha256 `
        -ModelPath $model `
        -ExpectedModelSha256 $expectedModelSha256 `
        -ManagedAvatarManifestPath $modelManifestPath `
        -EvidenceJson $stageEvidence `
        -AppVersion $AppVersion `
        -RequireAuthenticode | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $stageEvidence)) {
        throw "Signed AIRI stage verification failed"
    }

    if ($WheelPath.Trim()) {
        $wheel = (Resolve-Path -LiteralPath $WheelPath).Path
    }
    else {
        $wheelDirectory = Join-Path $work "wheel"
        New-Item -ItemType Directory -Path $wheelDirectory | Out-Null
        & $script:ResolvedPython -m build --wheel --no-isolation `
            --outdir $wheelDirectory $repository
        if ($LASTEXITCODE -ne 0) {
            throw "Project wheel build failed"
        }
        $wheels = @(Get-ChildItem -LiteralPath $wheelDirectory -File -Filter "*.whl")
        if ($wheels.Count -ne 1) {
            throw "Project wheel build did not produce exactly one wheel"
        }
        $wheel = $wheels[0].FullName
    }
    & $script:ResolvedPython (Join-Path $repository "scripts\verify_release_version.py") `
        --tag "v$AppVersion" --wheel $wheel
    if ($LASTEXITCODE -ne 0) {
        throw "Project wheel version verification failed"
    }

    $bundle = Join-Path $work "bundle"
    & $script:ResolvedPython $bundleAssembler `
        --stage $signedStage `
        --model $model `
        --wheel $wheel `
        --python-embed $pythonEmbed `
        --runtime-lock $runtimeLock `
        --full-lock $fullLock `
        --stage-evidence $stageEvidence `
        --model-manifest $modelManifestPath `
        --toolchain $toolchainPath `
        --default-config (Join-Path $repository "companion\resources\default.yaml") `
        --project-license (Join-Path $repository "LICENSE") `
        --third-party-assets (Join-Path $repository "docs\third_party_assets.md") `
        --output $bundle `
        --app-version $AppVersion `
        --source-commit $sourceCommit `
        --pip-python $script:ResolvedPython
    if ($LASTEXITCODE -ne 0) {
        throw "Windows application bundle assembly failed"
    }
    & $script:ResolvedPython $bundleVerifier $bundle `
        --expected-version $AppVersion --expected-commit $sourceCommit
    if ($LASTEXITCODE -ne 0) {
        throw "Windows application bundle verification failed"
    }

    $innoRoot = Join-Path $work "inno"
    $innoInstall = Start-Process -FilePath $innoInstaller -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        ('/DIR="{0}"' -f $innoRoot)
    ) -WorkingDirectory $work -WindowStyle Hidden -Wait -PassThru
    if ($innoInstall.ExitCode -ne 0) {
        throw "Verified Inno Setup installation failed"
    }
    $iscc = Join-Path $innoRoot $toolchain.inno_setup.compiler_relative_path
    if (-not (Test-Path -LiteralPath $iscc -PathType Leaf)) {
        throw "Inno Setup compiler is unavailable after installation"
    }
    $installerOutput = Join-Path $work "installer"
    New-Item -ItemType Directory -Path $installerOutput | Out-Null
    $innoSignToolName = "virtualcompanion"
    $escapedSignToolPath = $script:ResolvedSignTool.Replace('$', '$$')
    $innoSignCommand = (
        '$q{0}$q sign /sha1 {1} /fd {2} /tr $q{3}$q /td {4} $f' -f
            $escapedSignToolPath,
            $script:SigningCertificate.Thumbprint,
            $toolchain.signing.file_digest_algorithm,
            $toolchain.signing.timestamp_url,
            $toolchain.signing.timestamp_digest_algorithm
    )
    & $iscc `
        "/S${innoSignToolName}=$innoSignCommand" `
        "/DBundleRoot=$bundle" `
        "/DAppVersion=$AppVersion" `
        "/DOutputDir=$installerOutput" `
        "/DSignToolName=$innoSignToolName" `
        $innoScript
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup compiler failed"
    }
    $compiledInstaller = Join-Path $installerOutput $installerName
    if (-not (Test-Path -LiteralPath $compiledInstaller -PathType Leaf)) {
        throw "Inno Setup did not produce the expected installer"
    }
    $compiledInstallerSignature = Assert-ValidSignature $compiledInstaller "Windows installer"
    if (
        $compiledInstallerSignature.SignerCertificate.Thumbprint -ne (
            $script:SigningCertificate.Thumbprint
        )
    ) {
        throw "Windows installer was signed by an unexpected certificate"
    }
    $installerEvidence = Join-Path $work "windows-installer.json"
    & $installerVerifier `
        -InstallerPath $compiledInstaller `
        -AppVersion $AppVersion `
        -SourceCommit $sourceCommit `
        -StageEvidenceJson $stageEvidence `
        -RepositoryRoot $repository `
        -EvidenceJson $installerEvidence | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $installerEvidence)) {
        throw "Signed Windows installer verification failed"
    }

    Copy-Item -LiteralPath $compiledInstaller -Destination $finalInstaller
    $createdOutputs.Add($finalInstaller)
    Copy-Item -LiteralPath $stageEvidence -Destination $finalStageEvidence
    $createdOutputs.Add($finalStageEvidence)
    Copy-Item -LiteralPath $installerEvidence -Destination $finalInstallerEvidence
    $createdOutputs.Add($finalInstallerEvidence)
}
catch {
    foreach ($createdOutput in $createdOutputs) {
        if (Test-Path -LiteralPath $createdOutput -PathType Leaf) {
            Remove-Item -LiteralPath $createdOutput -Force
        }
    }
    throw
}
finally {
    $resolvedWork = [IO.Path]::GetFullPath($work)
    $leaf = [IO.Path]::GetFileName($resolvedWork)
    if (
        -not $resolvedWork.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $leaf -notmatch '^virtual-companion-installer-build-[0-9a-f]{32}$'
    ) {
        throw "Refusing to remove an unexpected installer build directory"
    }
    if (Test-Path -LiteralPath $resolvedWork) {
        Remove-Item -LiteralPath $resolvedWork -Recurse -Force
    }
}

[pscustomobject]@{
    installer = $finalInstaller
    windows_stage_evidence = $finalStageEvidence
    windows_installer_evidence = $finalInstallerEvidence
    source_commit = $sourceCommit
    signer_certificate_sha256 = Get-CertificateSha256 $script:SigningCertificate
}
