#requires -Version 7.0

[CmdletBinding()]
param(
    [string]$PythonPath = "",

    [string]$Config = "",

    [string]$RuntimeWorkingDirectory = "",

    [switch]$IsolatedRuntime,

    [string]$OutputPath = "",

    [switch]$SkipReadyPrompt,

    [switch]$Overwrite
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$explicitPython = -not [string]::IsNullOrWhiteSpace($PythonPath)
$explicitConfig = -not [string]::IsNullOrWhiteSpace($Config)
$explicitWorkingDirectory = -not [string]::IsNullOrWhiteSpace($RuntimeWorkingDirectory)

if ($IsolatedRuntime -and -not ($explicitPython -and $explicitConfig -and $explicitWorkingDirectory)) {
    throw "-IsolatedRuntime 必须同时指定 -PythonPath、-Config 和 -RuntimeWorkingDirectory。"
}

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $python = (Get-Command python -ErrorAction Stop).Source
} else {
    $python = (Resolve-Path -LiteralPath $PythonPath).Path
}

$configArguments = @()
if ($explicitConfig) {
    $resolvedConfig = (Resolve-Path -LiteralPath $Config).Path
    $configArguments = @("--config", $resolvedConfig)
}

$runtimeDirectory = $repository
if ($explicitWorkingDirectory) {
    $runtimeDirectory = (Resolve-Path -LiteralPath $RuntimeWorkingDirectory).Path
}
$pythonArguments = @("-X", "utf8")
if ($IsolatedRuntime) {
    $pythonArguments = @("-I", "-s", "-B", "-X", "utf8")
}

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputPath = Join-Path ".tmp\voice-acceptance" "voice-acceptance-$timestamp.json"
}
if ([IO.Path]::IsPathRooted($OutputPath)) {
    $resultPath = [IO.Path]::GetFullPath($OutputPath)
} else {
    $resultPath = [IO.Path]::GetFullPath((Join-Path $repository $OutputPath))
}

if ((Test-Path -LiteralPath $resultPath) -and -not $Overwrite) {
    throw "结果文件已存在；请更换 OutputPath，或明确使用 -Overwrite。"
}

Write-Host "正在校验当前运行配置..." -ForegroundColor Cyan
Push-Location $runtimeDirectory
try {
    & $python @pythonArguments -m companion @configArguments --log-level WARNING --validate-config
    $validationExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($validationExitCode -ne 0) {
    throw "配置校验失败，尚未占用麦克风。"
}

Write-Host ""
Write-Host "真实语音验收将直接调用正式生产入口：python -m companion --accept-voice-json" -ForegroundColor Cyan
if ($IsolatedRuntime) {
    Write-Host "当前模式：已安装隔离运行时（不会从仓库导入 companion 源码）。" -ForegroundColor Cyan
} else {
    Write-Host "当前模式：工作树预构建验收；通过后仍需在最终签名安装包上重新采集发布证据。" -ForegroundColor Yellow
}
Write-Host "第一轮：看到【第一轮】后说出屏幕上的完整句子，然后保持安静，等待 AD学姐完整说完。"
Write-Host "第二轮：看到【第二轮】后说出屏幕上的完整句子；看到【现在打断】后，立刻持续说话 2 至 3 秒。"
Write-Host "启动和 Whisper 预热可能需要十几秒；在【第一轮】出现前不要提前说话。"
Write-Host "本次只保存隐私安全的检查项和延迟，不保存录音、转录或回答正文。"
Write-Host ""
if (-not $SkipReadyPrompt) {
    [void](Read-Host "准备好后按 Enter 开始")
}

$resultDirectory = Split-Path -Parent $resultPath
[void](New-Item -ItemType Directory -Path $resultDirectory -Force)
$partialPath = "$resultPath.partial-$PID"

Write-Host ""
Write-Host "正在启动，请等到【第一轮】提示出现..." -ForegroundColor Yellow
Push-Location $runtimeDirectory
try {
    & $python @pythonArguments -m companion @configArguments --log-level WARNING --accept-voice-json `
        1> $partialPath
    $runtimeExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $partialPath)) {
    throw "验收进程没有生成 JSON 结果。"
}

try {
    $report = Get-Content -LiteralPath $partialPath -Raw -Encoding utf8 | ConvertFrom-Json
} catch {
    Remove-Item -LiteralPath $partialPath -Force
    throw "验收输出不是有效 JSON；为避免保存未批准内容，原始输出已丢弃。"
}

$expectedCodes = @(
    "voice.complete_turn",
    "voice.first_audio_latency",
    "voice.incremental_playback",
    "voice.pcm_continuity",
    "voice.completed_history",
    "voice.interrupt_terminal",
    "voice.interrupt_latency",
    "voice.interrupted_history"
)

function Test-ExactFields([object]$Value, [string[]]$Expected) {
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $sortedExpected = @($Expected | Sort-Object)
    return (
        $actual.Count -eq $sortedExpected.Count -and
        ($actual -join "`n") -eq ($sortedExpected -join "`n")
    )
}

$checks = @($report.checks)
$actualCodes = @($checks | ForEach-Object { [string]$_.code })
$codesMatch = (
    $checks.Count -eq $expectedCodes.Count -and
    ($actualCodes -join "`n") -eq ($expectedCodes -join "`n")
)
$rootFieldsMatch = Test-ExactFields $report @(
    "schema_version",
    "app_version",
    "generated_at",
    "exit_code",
    "passed",
    "checks"
)
$checkFieldsMatch = @(
    $checks | Where-Object {
        -not (Test-ExactFields $_ @("code", "passed", "message", "actual_ms", "target_ms"))
    }
).Count -eq 0
$privacyFieldsMatch = $rootFieldsMatch -and $checkFieldsMatch

$resultSaved = $false
if ($privacyFieldsMatch) {
    if ((Test-Path -LiteralPath $resultPath) -and $Overwrite) {
        Remove-Item -LiteralPath $resultPath -Force
    }
    Move-Item -LiteralPath $partialPath -Destination $resultPath
    $resultSaved = $true
} else {
    Remove-Item -LiteralPath $partialPath -Force
}

$allChecksPassed = $checks.Count -gt 0 -and @($checks | Where-Object { $_.passed -ne $true }).Count -eq 0
$reportPassed = (
    $report.schema_version -eq 1 -and
    $report.passed -eq $true -and
    $report.exit_code -eq 0 -and
    $runtimeExitCode -eq 0 -and
    $codesMatch -and
    $privacyFieldsMatch -and
    $allChecksPassed
)

Write-Host ""
Write-Host "验收结果："
foreach ($check in $checks) {
    $status = if ($check.passed -eq $true) { "PASS" } else { "FAIL" }
    $color = if ($check.passed -eq $true) { "Green" } else { "Red" }
    $latency = ""
    if ($null -ne $check.actual_ms) {
        $latency = " ($($check.actual_ms) ms"
        if ($null -ne $check.target_ms) {
            $latency += " / 目标 $($check.target_ms) ms"
        }
        $latency += ")"
    }
    Write-Host "[$status] $($check.code)$latency - $($check.message)" -ForegroundColor $color
}

if (-not $codesMatch) {
    Write-Host "[FAIL] 报告没有包含规定顺序的全部 8 个检查项。" -ForegroundColor Red
}
if (-not ($rootFieldsMatch -and $checkFieldsMatch)) {
    Write-Host "[FAIL] 报告包含缺失或未批准字段，不能作为隐私安全证据。" -ForegroundColor Red
}

Write-Host ""
if ($resultSaved) {
    Write-Host "JSON 结果：$resultPath"
} else {
    Write-Host "JSON 结果未保存：报告包含未批准字段。" -ForegroundColor Red
}
if ($reportPassed) {
    Write-Host "PASS：真实语音验收 8/8 全部通过。" -ForegroundColor Green
    exit 0
}

Write-Host "FAIL：本次结果不能作为通过证据；请保留 JSON 并反馈失败检查项。" -ForegroundColor Red
exit 1
