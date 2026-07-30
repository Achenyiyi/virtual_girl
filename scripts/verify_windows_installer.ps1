[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,

    [Parameter(Mandatory = $true)]
    [string]$AppVersion,

    [Parameter(Mandatory = $true)]
    [string]$SourceCommit,

    [Parameter(Mandatory = $true)]
    [string]$StageEvidenceJson,

    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),

    [string]$PythonPath = "python",

    [string]$EvidenceJson = "",

    [string]$ExpectedEvidenceJson = ""
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

if ($AppVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "AppVersion must be stable SemVer"
}
if ($SourceCommit -cnotmatch '^[0-9a-f]{40}$') {
    throw "SourceCommit must be a lowercase 40-character Git commit"
}
if ($EvidenceJson.Trim() -and $ExpectedEvidenceJson.Trim()) {
    throw "EvidenceJson and ExpectedEvidenceJson are mutually exclusive"
}

$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$installerItem = Get-Item -LiteralPath $installer
$stageEvidencePath = (Resolve-Path -LiteralPath $StageEvidenceJson).Path
$stageEvidenceSha256 = (
    Get-FileHash -LiteralPath $stageEvidencePath -Algorithm SHA256
).Hash.ToLowerInvariant()
$expectedInstallerName = "VirtualCompanion-$AppVersion-windows-x64.exe"
if ($installerItem.Name -cne $expectedInstallerName) {
    throw "Installer filename must be $expectedInstallerName"
}
$bundleVerifier = Join-Path $repository "scripts\verify_windows_bundle.py"
if (-not (Test-Path -LiteralPath $bundleVerifier -PathType Leaf)) {
    throw "Windows bundle verifier is unavailable"
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
    if ($signature.Status.ToString() -ne "Valid") {
        throw "$AssetName Authenticode signature is not valid"
    }
    if ($null -eq $signature.SignerCertificate) {
        throw "$AssetName Authenticode signer certificate is missing"
    }
    if ($null -eq $signature.TimeStamperCertificate) {
        throw "$AssetName Authenticode timestamp certificate is missing"
    }
    return $signature
}

function Assert-ExactFields([object]$Value, [string[]]$Expected, [string]$Context) {
    if ($null -eq $Value) {
        throw "$Context is missing"
    }
    $actual = @($Value.PSObject.Properties.Name) | Sort-Object
    $expectedSorted = @($Expected) | Sort-Object
    if (Compare-Object $expectedSorted $actual) {
        throw "$Context fields are incomplete or unexpected"
    }
}

function Assert-Sha256([object]$Value, [string]$Context) {
    if ($Value -isnot [string] -or $Value -cnotmatch '^[0-9a-f]{64}$') {
        throw "$Context must be a lowercase SHA-256 digest"
    }
}

function Invoke-HiddenProcess(
    [string]$FilePath,
    [string[]]$ArgumentList,
    [string]$WorkingDirectory,
    [string]$Name
) {
    $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "$Name failed with exit code $($process.ExitCode)"
    }
}

function Write-NewUtf8Json([string]$Path, [object]$Value) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $parent = [IO.Path]::GetDirectoryName($fullPath)
    if (-not [IO.Directory]::Exists($parent)) {
        throw "EvidenceJson parent directory does not exist"
    }
    if ([IO.File]::Exists($fullPath) -or [IO.Directory]::Exists($fullPath)) {
        throw "EvidenceJson already exists"
    }
    $json = $Value | ConvertTo-Json -Depth 6
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(
        $json + [Environment]::NewLine
    )
    $stream = [IO.FileStream]::new(
        $fullPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

function Assert-ExpectedEvidence([string]$Path, [object]$Actual) {
    $expectedPath = (Resolve-Path -LiteralPath $Path).Path
    $expected = Get-Content -LiteralPath $expectedPath -Raw | ConvertFrom-Json
    $expectedFields = @(
        "schema_version",
        "app_version",
        "source_commit",
        "generated_at",
        "passed",
        "installer",
        "authenticode",
        "bundle_manifest_sha256",
        "windows_stage_evidence_sha256",
        "smoke"
    ) | Sort-Object
    $actualFields = @($expected.PSObject.Properties.Name) | Sort-Object
    if (Compare-Object $expectedFields $actualFields) {
        throw "Expected installer evidence fields are incomplete or unexpected"
    }
    if (
        $expected.schema_version -ne 1 -or
        $expected.app_version -cne $Actual.app_version -or
        $expected.source_commit -cne $Actual.source_commit -or
        $expected.passed -ne $true -or
        $expected.installer.filename -cne $Actual.installer.filename -or
        $expected.installer.size_bytes -ne $Actual.installer.size_bytes -or
        $expected.installer.sha256 -cne $Actual.installer.sha256 -or
        $expected.authenticode.status -cne $Actual.authenticode.status -or
        $expected.authenticode.signer_certificate_sha256 -cne (
            $Actual.authenticode.signer_certificate_sha256
        ) -or
        $expected.authenticode.timestamp_certificate_sha256 -cne (
            $Actual.authenticode.timestamp_certificate_sha256
        ) -or
        $expected.bundle_manifest_sha256 -cne $Actual.bundle_manifest_sha256 -or
        $expected.windows_stage_evidence_sha256 -cne (
            $Actual.windows_stage_evidence_sha256
        )
    ) {
        throw "Installer does not match ExpectedEvidenceJson"
    }
    $expectedSmokeFields = @(
        "silent_install",
        "bundle_integrity",
        "config_validation",
        "runtime_import",
        "cli_help",
        "uninstaller_authenticode",
        "silent_uninstall",
        "install_directory_removed"
    ) | Sort-Object
    $smokeFields = @($expected.smoke.PSObject.Properties.Name) | Sort-Object
    if (Compare-Object $expectedSmokeFields $smokeFields) {
        throw "Expected installer smoke evidence fields are incomplete or unexpected"
    }
    foreach ($field in $expectedSmokeFields) {
        if ($expected.smoke.$field -ne $true) {
            throw "Expected installer smoke evidence did not pass: $field"
        }
    }
}

$stageEvidence = Get-Content -LiteralPath $stageEvidencePath -Raw | ConvertFrom-Json
Assert-ExactFields $stageEvidence @(
    "schema_version",
    "app_version",
    "generated_at",
    "passed",
    "artifact_sha256",
    "authenticode",
    "model_license"
) "Windows stage evidence"
if (
    $stageEvidence.schema_version -ne 1 -or
    $stageEvidence.app_version -cne $AppVersion -or
    $stageEvidence.passed -ne $true
) {
    throw "Windows stage evidence does not match this installer verification"
}
Assert-ExactFields $stageEvidence.artifact_sha256 @(
    "airi_exe",
    "app_asar",
    "godot_stage_exe",
    "managed_avatar"
) "Windows stage artifact evidence"
foreach ($artifact in @("airi_exe", "app_asar", "godot_stage_exe", "managed_avatar")) {
    Assert-Sha256 $stageEvidence.artifact_sha256.$artifact `
        "Windows stage artifact evidence for $artifact"
}
Assert-ExactFields $stageEvidence.authenticode @(
    "airi_exe",
    "godot_stage_exe"
) "Windows stage Authenticode evidence"
foreach ($artifact in @("airi_exe", "godot_stage_exe")) {
    $signatureEvidence = $stageEvidence.authenticode.$artifact
    Assert-ExactFields $signatureEvidence @(
        "status",
        "signer_certificate_sha256",
        "timestamp_certificate_sha256"
    ) "Windows stage Authenticode evidence for $artifact"
    if ($signatureEvidence.status -cne "Valid") {
        throw "Windows stage Authenticode evidence for $artifact is not valid"
    }
    Assert-Sha256 $signatureEvidence.signer_certificate_sha256 `
        "Windows stage signer certificate for $artifact"
    Assert-Sha256 $signatureEvidence.timestamp_certificate_sha256 `
        "Windows stage timestamp certificate for $artifact"
}

$installerSignature = Assert-ValidSignature $installer "Windows installer"
$installerSignerSha256 = Get-CertificateSha256 $installerSignature.SignerCertificate
$installerSha256 = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$smokeRoot = Join-Path $tempRoot ("virtual-companion-installer-smoke-" + [Guid]::NewGuid().ToString("N"))
$installation = Join-Path $smokeRoot "installation"
$installLog = Join-Path $smokeRoot "install.log"
$uninstallLog = Join-Path $smokeRoot "uninstall.log"
New-Item -ItemType Directory -Path $smokeRoot | Out-Null
$bundleManifestSha256 = ""
$uninstallSucceeded = $false
$directoryRemoved = $false
$validatedUninstaller = ""
$uninstallerAuthenticode = $false

try {
    $installArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/SkipAvatarToken=1",
        ('/DIR="{0}"' -f $installation),
        ('/LOG="{0}"' -f $installLog)
    )
    Invoke-HiddenProcess $installer $installArguments $smokeRoot "Silent installer smoke test"
    $uninstallers = @(Get-ChildItem -LiteralPath $installation -File -Filter "unins*.exe")
    if ($uninstallers.Count -ne 1) {
        throw "Expected exactly one Inno Setup uninstaller"
    }
    $uninstallerSignature = Assert-ValidSignature $uninstallers[0].FullName `
        "Windows uninstaller"
    $uninstallerSignerSha256 = Get-CertificateSha256 (
        $uninstallerSignature.SignerCertificate
    )
    if ($uninstallerSignerSha256 -cne $installerSignerSha256) {
        throw "Windows uninstaller does not use the installer signing identity"
    }
    $validatedUninstaller = $uninstallers[0].FullName
    $uninstallerAuthenticode = $true
    $bundleManifest = Join-Path $installation "bundle-manifest.json"
    if (-not (Test-Path -LiteralPath $bundleManifest -PathType Leaf)) {
        throw "Installed bundle manifest is missing"
    }
    $bundleManifestSha256 = (
        Get-FileHash -LiteralPath $bundleManifest -Algorithm SHA256
    ).Hash.ToLowerInvariant()

    & $PythonPath $bundleVerifier $installation `
        --expected-version $AppVersion `
        --expected-commit $SourceCommit `
        --allow-inno-uninstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Installed bundle integrity verification failed"
    }

    $installedStageEvidence = Join-Path $installation "provenance\windows-stage.json"
    $installedStageEvidenceSha256 = (
        Get-FileHash -LiteralPath $installedStageEvidence -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($installedStageEvidenceSha256 -cne $stageEvidenceSha256) {
        throw "Installed Windows stage evidence does not match the approved evidence"
    }

    $installedStageArtifacts = [ordered]@{
        airi_exe = Join-Path $installation "airi\airi.exe"
        app_asar = Join-Path $installation "airi\resources\app.asar"
        godot_stage_exe = Join-Path $installation (
            "airi\resources\godot-stage\godot-stage.exe"
        )
        managed_avatar = Join-Path $installation "model\managed-avatar.vrm"
    }
    foreach ($artifact in $installedStageArtifacts.Keys) {
        $actualDigest = (
            Get-FileHash -LiteralPath $installedStageArtifacts[$artifact] -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ($actualDigest -cne $stageEvidence.artifact_sha256.$artifact) {
            throw "Installed Windows stage artifact does not match evidence: $artifact"
        }
    }

    $installedStageSignatures = [ordered]@{
        airi_exe = Assert-ValidSignature $installedStageArtifacts.airi_exe "airi.exe"
        godot_stage_exe = Assert-ValidSignature (
            $installedStageArtifacts.godot_stage_exe
        ) "godot-stage.exe"
    }
    foreach ($artifact in $installedStageSignatures.Keys) {
        $actualSignature = $installedStageSignatures[$artifact]
        $expectedSignature = $stageEvidence.authenticode.$artifact
        $actualSignerSha256 = Get-CertificateSha256 $actualSignature.SignerCertificate
        $actualTimestampSha256 = Get-CertificateSha256 (
            $actualSignature.TimeStamperCertificate
        )
        if (
            $actualSignerSha256 -cne $installerSignerSha256 -or
            $actualSignerSha256 -cne $expectedSignature.signer_certificate_sha256 -or
            $actualTimestampSha256 -cne $expectedSignature.timestamp_certificate_sha256
        ) {
            throw "Installed Windows stage signature does not match evidence: $artifact"
        }
    }

    $bundledPython = Join-Path $installation "runtime\python.exe"
    $bundledConfig = Join-Path $installation "config\production.yaml"
    & $bundledPython -I -s -B -m companion --config $bundledConfig --validate-config
    if ($LASTEXITCODE -ne 0) {
        throw "Installed production configuration validation failed"
    }
    & $bundledPython -I -s -B -c (
        "import companion, ctranslate2, faster_whisper, numpy, sounddevice; " +
        "print(companion.__version__)"
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Installed companion import failed"
    }
    & $bundledPython -I -s -B -m companion --help
    if ($LASTEXITCODE -ne 0) {
        throw "Installed companion CLI help failed"
    }
    & $PythonPath $bundleVerifier $installation `
        --expected-version $AppVersion `
        --expected-commit $SourceCommit `
        --allow-inno-uninstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Installed bundle changed during runtime smoke checks"
    }

    $uninstallArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        ('/LOG="{0}"' -f $uninstallLog)
    )
    Invoke-HiddenProcess $validatedUninstaller $uninstallArguments $smokeRoot `
        "Silent uninstaller smoke test"
    $uninstallSucceeded = $true
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        if (-not (Test-Path -LiteralPath $installation)) {
            $directoryRemoved = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $directoryRemoved) {
        throw "Silent uninstall left the installation directory behind"
    }
}
finally {
    if ((Test-Path -LiteralPath $installation) -and -not $uninstallSucceeded) {
        if ($validatedUninstaller -and (Test-Path -LiteralPath $validatedUninstaller)) {
            Start-Process -FilePath $validatedUninstaller `
                -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") `
                -WorkingDirectory $smokeRoot -WindowStyle Hidden -Wait | Out-Null
        }
    }
    $resolvedSmokeRoot = [IO.Path]::GetFullPath($smokeRoot)
    $leaf = [IO.Path]::GetFileName($resolvedSmokeRoot)
    if (
        -not $resolvedSmokeRoot.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $leaf -notmatch '^virtual-companion-installer-smoke-[0-9a-f]{32}$'
    ) {
        throw "Refusing to remove an unexpected installer smoke directory"
    }
    if (Test-Path -LiteralPath $resolvedSmokeRoot) {
        Remove-Item -LiteralPath $resolvedSmokeRoot -Recurse -Force
    }
}

$evidence = [ordered]@{
    schema_version = 1
    app_version = $AppVersion
    source_commit = $SourceCommit
    generated_at = [DateTimeOffset]::UtcNow.ToString(
        "yyyy-MM-ddTHH:mm:ss.fffffff'Z'",
        [Globalization.CultureInfo]::InvariantCulture
    )
    passed = $true
    installer = [ordered]@{
        filename = $installerItem.Name
        size_bytes = $installerItem.Length
        sha256 = $installerSha256
    }
    authenticode = [ordered]@{
        status = $installerSignature.Status.ToString()
        signer_certificate_sha256 = Get-CertificateSha256 (
            $installerSignature.SignerCertificate
        )
        timestamp_certificate_sha256 = Get-CertificateSha256 (
            $installerSignature.TimeStamperCertificate
        )
    }
    bundle_manifest_sha256 = $bundleManifestSha256
    windows_stage_evidence_sha256 = $stageEvidenceSha256
    smoke = [ordered]@{
        silent_install = $true
        bundle_integrity = $true
        config_validation = $true
        runtime_import = $true
        cli_help = $true
        uninstaller_authenticode = $uninstallerAuthenticode
        silent_uninstall = $uninstallSucceeded
        install_directory_removed = $directoryRemoved
    }
}

if ($ExpectedEvidenceJson.Trim()) {
    Assert-ExpectedEvidence $ExpectedEvidenceJson $evidence
}
if ($EvidenceJson.Trim()) {
    Write-NewUtf8Json $EvidenceJson $evidence
}
$evidence
