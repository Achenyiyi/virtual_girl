[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallationPath,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedExeSha256,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedAppAsarSha256,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedGodotSha256,

    [Parameter(Mandatory = $true)]
    [string]$ModelPath,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedModelSha256,

    [string]$ManagedAvatarManifestPath = "",

    [string]$EvidenceJson = "",

    [string]$AppVersion = "",

    [switch]$RequireAuthenticode
)

$ErrorActionPreference = "Stop"
$evidenceRequested = -not [string]::IsNullOrWhiteSpace($EvidenceJson)
if ($evidenceRequested -and -not $RequireAuthenticode) {
    throw "EvidenceJson requires RequireAuthenticode"
}
if ($evidenceRequested -and $AppVersion.Trim() -notmatch '^\d+\.\d+\.\d+$') {
    throw "EvidenceJson requires AppVersion as stable SemVer"
}
if (-not $evidenceRequested -and -not [string]::IsNullOrWhiteSpace($AppVersion)) {
    throw "AppVersion is valid only with EvidenceJson"
}

$installation = (Resolve-Path -LiteralPath $InstallationPath).Path
$model = (Resolve-Path -LiteralPath $ModelPath).Path
$manifestInput = $ManagedAvatarManifestPath.Trim()
if (-not $manifestInput) {
    $manifestInput = Join-Path (
        Split-Path -Parent $PSScriptRoot
    ) "integrations\airi-v0.11.3\managed-avatar.json"
}
$manifestPath = (Resolve-Path -LiteralPath $manifestInput).Path
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$airi = Join-Path $installation "airi.exe"
$appAsar = Join-Path $installation "resources\app.asar"
$godot = Join-Path $installation "resources\godot-stage\godot-stage.exe"

function Assert-Sha256([string]$Path, [string]$Expected, [string]$AssetName) {
    if ($Expected -notmatch '^[0-9a-fA-F]{64}$') {
        throw "Expected SHA-256 is invalid for $AssetName"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Expected.ToLowerInvariant()) {
        throw "SHA-256 mismatch for $AssetName"
    }
    return $actual
}

$airiSha256 = Assert-Sha256 $airi $ExpectedExeSha256 "airi.exe"
$appAsarSha256 = Assert-Sha256 $appAsar $ExpectedAppAsarSha256 "app.asar"
$godotSha256 = Assert-Sha256 $godot $ExpectedGodotSha256 "godot-stage.exe"
$modelSha256 = Assert-Sha256 $model $ExpectedModelSha256 "managed avatar"

if ($manifest.schema_version -ne 2) {
    throw "Managed avatar manifest schema is unsupported"
}
if ($manifest.sha256 -ne $ExpectedModelSha256.ToLowerInvariant()) {
    throw "Managed avatar manifest does not pin the requested model digest"
}
if ($manifest.size_bytes -ne (Get-Item -LiteralPath $model).Length) {
    throw "Managed avatar manifest does not pin the requested model size"
}
$permissions = $manifest.license.permissions
if (
    $manifest.license.source -ne "embedded-vrm-0.x-meta-and-owner-confirmed-vroid-hub-page" -or
    $permissions.corporate_commercial_use -ne $true -or
    $permissions.personal_commercial_use -ne $true -or
    $permissions.redistribution -ne $true -or
    $permissions.modification -ne $true -or
    $permissions.credit_required -ne $false
) {
    throw "Managed avatar manifest does not authorize the required release uses"
}

$modelStream = [IO.File]::OpenRead($model)
try {
    $header = New-Object byte[] 20
    $modelHeaderIsInvalid = (
        $modelStream.Read($header, 0, 20) -ne 20 -or
        [Text.Encoding]::ASCII.GetString($header, 0, 4) -ne "glTF" -or
        [BitConverter]::ToUInt32($header, 4) -ne 2 -or
        [BitConverter]::ToUInt32($header, 8) -ne $modelStream.Length -or
        [Text.Encoding]::ASCII.GetString($header, 16, 4) -ne "JSON"
    )
    if ($modelHeaderIsInvalid) {
        throw "Managed model is not a valid GLB 2.0 file"
    }
    $jsonLength = [BitConverter]::ToUInt32($header, 12)
    if ($jsonLength -lt 2 -or $jsonLength -gt $modelStream.Length - 20) {
        throw "Managed model JSON chunk is invalid"
    }
    $jsonBytes = New-Object byte[] $jsonLength
    if ($modelStream.Read($jsonBytes, 0, $jsonLength) -ne $jsonLength) {
        throw "Managed model JSON chunk is truncated"
    }
    $jsonText = [Text.Encoding]::UTF8.GetString($jsonBytes).TrimEnd(
        [char[]]@(0, 9, 10, 13, 32)
    )
    $document = $jsonText | ConvertFrom-Json
    $embeddedLicense = $document.extensions.VRM.meta
    $license = $manifest.license
    if (
        $null -eq $embeddedLicense -or
        $embeddedLicense.title -ne $license.title -or
        $embeddedLicense.author -ne $license.author -or
        $embeddedLicense.allowedUserName -ne $license.allowed_user_name -or
        $embeddedLicense.commercialUssageName -ne $license.commercial_usage_name -or
        $embeddedLicense.licenseName -ne $license.license_name -or
        $embeddedLicense.otherPermissionUrl -ne $license.license_url -or
        $embeddedLicense.otherLicenseUrl -ne $license.license_url
    ) {
        throw "Managed model embedded license does not match the approved manifest"
    }
    $licenseUri = [Uri]$license.license_url
    $licenseQuery = [System.Web.HttpUtility]::ParseQueryString($licenseUri.Query)
    if (
        $licenseUri.Scheme -ne "https" -or
        $licenseUri.Host -ne "hub.vroid.com" -or
        $licenseUri.AbsolutePath -ne "/license" -or
        $licenseQuery["corporate_commercial_use"] -ne "allow" -or
        $licenseQuery["personal_commercial_use"] -ne "profit" -or
        $licenseQuery["redistribution"] -ne "allow" -or
        $licenseQuery["modification"] -ne "allow" -or
        $licenseQuery["credit"] -ne "unnecessary"
    ) {
        throw "Managed model license URL does not authorize the required release uses"
    }
}
finally {
    $modelStream.Dispose()
}

$airiSignature = Get-AuthenticodeSignature -LiteralPath $airi
$godotSignature = Get-AuthenticodeSignature -LiteralPath $godot
$signatures = @($airiSignature, $godotSignature)

function Assert-ReleaseSignature([string]$AssetName, [object]$Signature) {
    if ($Signature.Status.ToString() -ne "Valid") {
        throw "$AssetName Authenticode signature is not valid"
    }
    if ($null -eq $Signature.SignerCertificate) {
        throw "$AssetName Authenticode signer certificate is missing"
    }
    if ($null -eq $Signature.TimeStamperCertificate) {
        throw "$AssetName Authenticode timestamp certificate is missing"
    }
}

function Get-CertificateSha256(
    [Security.Cryptography.X509Certificates.X509Certificate2]$Certificate
) {
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash($Certificate.RawData)
        return [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

if ($RequireAuthenticode) {
    Assert-ReleaseSignature "airi.exe" $airiSignature
    Assert-ReleaseSignature "godot-stage.exe" $godotSignature
}

if ($evidenceRequested) {
    $evidencePath = [IO.Path]::GetFullPath($EvidenceJson.Trim())
    $evidenceDirectory = [IO.Path]::GetDirectoryName($evidencePath)
    if (-not [IO.Directory]::Exists($evidenceDirectory)) {
        throw "EvidenceJson parent directory does not exist"
    }
    if ([IO.Directory]::Exists($evidencePath)) {
        throw "EvidenceJson points to a directory"
    }
    if ([IO.File]::Exists($evidencePath)) {
        throw "EvidenceJson already exists"
    }
    foreach ($protectedPath in @($airi, $appAsar, $godot, $model, $manifestPath)) {
        if ([StringComparer]::OrdinalIgnoreCase.Equals($evidencePath, $protectedPath)) {
            throw "EvidenceJson must not overwrite a verified input"
        }
    }

    $evidence = [ordered]@{
        schema_version = 1
        app_version = $AppVersion.Trim()
        generated_at = [DateTimeOffset]::UtcNow.ToString(
            "yyyy-MM-ddTHH:mm:ss.fffffff'Z'",
            [Globalization.CultureInfo]::InvariantCulture
        )
        passed = $true
        artifact_sha256 = [ordered]@{
            airi_exe = $airiSha256
            app_asar = $appAsarSha256
            godot_stage_exe = $godotSha256
            managed_avatar = $modelSha256
        }
        authenticode = [ordered]@{
            airi_exe = [ordered]@{
                status = $airiSignature.Status.ToString()
                signer_certificate_sha256 = Get-CertificateSha256 (
                    $airiSignature.SignerCertificate
                )
                timestamp_certificate_sha256 = Get-CertificateSha256 (
                    $airiSignature.TimeStamperCertificate
                )
            }
            godot_stage_exe = [ordered]@{
                status = $godotSignature.Status.ToString()
                signer_certificate_sha256 = Get-CertificateSha256 (
                    $godotSignature.SignerCertificate
                )
                timestamp_certificate_sha256 = Get-CertificateSha256 (
                    $godotSignature.TimeStamperCertificate
                )
            }
        }
        model_license = [ordered]@{
            model_id = $manifest.model_id
            title = $manifest.license.title
            author = $manifest.license.author
            source = $manifest.license.source
            license_url = $manifest.license.license_url
            corporate_commercial_use = $true
            personal_commercial_use = $true
            redistribution = $true
            modification = $true
            credit_required = $false
        }
    }
    $json = $evidence | ConvertTo-Json -Depth 5
    $encoding = [Text.UTF8Encoding]::new($false)
    $bytes = $encoding.GetBytes($json + [Environment]::NewLine)
    $stream = [IO.FileStream]::new(
        $evidencePath,
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
    $evidence
    return
}

[pscustomobject]@{
    airi_exe = $airi
    app_asar = $appAsar
    godot_stage_exe = $godot
    model = $model
    managed_avatar_manifest = $manifestPath
    model_license = [pscustomobject]@{
        author = $manifest.license.author
        commercial_use = $true
        redistribution = $true
        modification = $true
        credit_required = $false
        source_url = $manifest.license.license_url
    }
    signatures = $signatures | Select-Object Path, Status, StatusMessage
}
