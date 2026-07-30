[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CheckoutPath,

    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),

    [string]$GodotPath = "godot",

    [string]$GodotUserPath = "",

    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$AiriCommit = "dbf812488829a61cc2e95909e021b215704d066c"
$DotnetSdkVersion = "8.0.206"
$PnpmVersion = "10.33.0"
$GodotVersionPrefix = "4.6.2.stable.mono."
$checkout = (Resolve-Path -LiteralPath $CheckoutPath).Path
$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$patch = Join-Path $repository "integrations\airi-v0.11.3\airi-v0.11.3-avatar-bridge.patch"

if (-not (Test-Path -LiteralPath (Join-Path $checkout ".git"))) {
    throw "AIRI checkout is not a Git worktree: $checkout"
}
if ((git -C $checkout rev-parse HEAD).Trim() -ne $AiriCommit) {
    throw "AIRI checkout must be pinned to $AiriCommit"
}
if (git -C $checkout status --porcelain) {
    throw "AIRI checkout must be clean before applying the pinned patch"
}
if ((node --version).Trim() -ne "v24.9.0") {
    throw "Node.js v24.9.0 is required for the approved Windows build"
}
$installedDotnetSdks = @(dotnet --list-sdks)
$hasPinnedDotnetSdk = $installedDotnetSdks | Where-Object {
    ($_ -split '\s+', 2)[0] -eq $DotnetSdkVersion
}
if (-not $hasPinnedDotnetSdk) {
    throw ".NET SDK $DotnetSdkVersion is required for the approved Windows build"
}
$actualGodotVersion = ((& $GodotPath --version) | Select-Object -First 1).Trim()
if (-not $actualGodotVersion.StartsWith($GodotVersionPrefix)) {
    throw "Godot 4.6.2 stable Mono is required for the approved Windows build"
}
$godotAppData = $env:APPDATA
$godotLocalAppData = $env:LOCALAPPDATA
if ($GodotUserPath.Trim()) {
    $godotUserHome = (Resolve-Path -LiteralPath $GodotUserPath).Path
    $godotAppData = $godotUserHome
    $godotLocalAppData = $godotUserHome
}
if (-not $godotAppData -or -not $godotLocalAppData) {
    throw "APPDATA and LOCALAPPDATA are required for the Godot Windows export"
}

git -C $checkout apply --check $patch
git -C $checkout apply $patch

$globalJsonPath = Join-Path $checkout "global.json"
if (Test-Path -LiteralPath $globalJsonPath) {
    throw "Refusing to overwrite AIRI global.json: $globalJsonPath"
}
$globalJson = [ordered]@{
    sdk = [ordered]@{
        version = $DotnetSdkVersion
        rollForward = "disable"
    }
} | ConvertTo-Json -Depth 3
$globalJsonBytes = [Text.UTF8Encoding]::new($false).GetBytes(
    $globalJson + [Environment]::NewLine
)
$globalJsonStream = [IO.File]::Open(
    $globalJsonPath,
    [IO.FileMode]::CreateNew,
    [IO.FileAccess]::Write,
    [IO.FileShare]::None
)
try {
    $globalJsonStream.Write($globalJsonBytes, 0, $globalJsonBytes.Length)
}
finally {
    $globalJsonStream.Dispose()
}

Push-Location $checkout
try {
    $actualDotnetSdkVersion = (dotnet --version).Trim()
}
finally {
    Pop-Location
}
if ($actualDotnetSdkVersion -ne $DotnetSdkVersion) {
    throw (
        ".NET SDK selection mismatch: expected $DotnetSdkVersion, " +
        "got $actualDotnetSdkVersion"
    )
}

$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$corepackBin = Join-Path $tempRoot ("airi-corepack-" + [Guid]::NewGuid().ToString("N"))
$originalPath = $env:PATH
New-Item -ItemType Directory -Path $corepackBin | Out-Null
try {
    # electron-builder starts pnpm subprocesses, so the pinned version must lead PATH too.
    corepack enable pnpm --install-directory $corepackBin
    $env:PATH = "$corepackBin;$originalPath"

    Push-Location $checkout
    try {
        if ((pnpm --version).Trim() -ne $PnpmVersion) {
            throw "pnpm $PnpmVersion is required for the approved Windows build"
        }
        if (-not $SkipInstall) {
            # Limit lifecycle scripts and binary downloads to the desktop app's dependency graph.
            pnpm --filter '@proj-airi/stage-tamagotchi...' install --frozen-lockfile
        }

        $godotProject = Join-Path $checkout "engines\stage-tamagotchi-godot"
        $godotOutput = Join-Path $godotProject "build\win\godot-stage.exe"
        $godotOutputDirectory = Split-Path -Parent $godotOutput
        $godotLogId = [Guid]::NewGuid().ToString("N")
        $godotStdout = Join-Path $godotOutputDirectory "godot-export-$godotLogId.stdout.log"
        $godotStderr = Join-Path $godotOutputDirectory "godot-export-$godotLogId.stderr.log"
        $godotArguments = @(
            "--headless",
            "--path",
            ('"{0}"' -f $godotProject),
            "--export-release",
            '"Windows Desktop"',
            ('"{0}"' -f $godotOutput)
        )
        New-Item -ItemType Directory -Force -Path $godotOutputDirectory | Out-Null
        $godotProcessArguments = @{
            FilePath = $GodotPath
            ArgumentList = $godotArguments
            WorkingDirectory = $godotProject
            Environment = @{
                APPDATA = $godotAppData
                LOCALAPPDATA = $godotLocalAppData
            }
            RedirectStandardOutput = $godotStdout
            RedirectStandardError = $godotStderr
            WindowStyle = "Hidden"
            Wait = $true
            PassThru = $true
        }
        $godotProcess = Start-Process @godotProcessArguments
        if ($godotProcess.ExitCode -ne 0) {
            throw (
                "Godot Windows sidecar export failed with exit code " +
                "$($godotProcess.ExitCode). Logs: $godotStdout, $godotStderr"
            )
        }
        if (-not (Test-Path -LiteralPath $godotOutput)) {
            throw "Godot Windows sidecar export is unavailable: $godotOutput"
        }

        pnpm --filter '@proj-airi/stage-tamagotchi' build:unpack
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:PATH = $originalPath
    $resolvedCorepackBin = [IO.Path]::GetFullPath($corepackBin)
    $leaf = [IO.Path]::GetFileName($resolvedCorepackBin)
    $isExpectedCorepackPath = $resolvedCorepackBin.StartsWith(
        $tempRoot,
        [StringComparison]::OrdinalIgnoreCase
    ) -and $leaf -match '^airi-corepack-[0-9a-f]{32}$'
    if (-not $isExpectedCorepackPath) {
        throw "Refusing to remove unexpected Corepack directory: $resolvedCorepackBin"
    }
    Remove-Item -LiteralPath $resolvedCorepackBin -Recurse -Force
}

$output = Join-Path $checkout "apps\stage-tamagotchi\dist\win-unpacked"
$requiredOutputs = @(
    (Join-Path $output "airi.exe"),
    (Join-Path $output "resources\app.asar"),
    (Join-Path $output "resources\godot-stage\godot-stage.exe")
)
foreach ($requiredOutput in $requiredOutputs) {
    if (-not (Test-Path -LiteralPath $requiredOutput)) {
        throw "AIRI unpacked Windows build output is unavailable: $requiredOutput"
    }
}

Write-Output $output
