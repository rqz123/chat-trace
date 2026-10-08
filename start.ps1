$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "service-common.ps1")

$logsDir = Join-Path $ChatTraceDataDir "logs"
$stdoutPath = Join-Path $logsDir "server.out"
$stderrPath = Join-Path $logsDir "server.err"

if (-not (Test-Path -LiteralPath $ChatTracePython)) {
    throw "Virtual environment not found. Run the setup commands in README.md first."
}
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

$existing = Get-ChatTraceRecordedProcess
if ($null -ne $existing) {
    Write-Host "ChatTrace is already running (PID $($existing.Id))."
    Write-Host "Open $ChatTraceBaseUrl"
    exit 0
}

$unmanaged = @(Get-ChatTraceUnmanagedProcesses)
if ($unmanaged.Count -gt 0) {
    throw "ChatTrace was started outside start.ps1 (PID $(@($unmanaged.ProcessId) -join ', ')). Run stop.ps1 first."
}

$listener = Get-NetTCPConnection -LocalPort $ChatTracePort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -ne $listener) {
    throw "Port $ChatTracePort is already in use by PID $($listener.OwningProcess). Stop that process or set CHATTRACE_PORT."
}

# Child processes inherit the environment of this script.
$env:CHATTRACE_DATA_DIR = $ChatTraceDataDir
$process = Start-Process `
    -FilePath $ChatTracePython `
    -ArgumentList @("-m", "chattrace.app") `
    -WorkingDirectory $ChatTraceProjectDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

[ordered]@{
    pid = $process.Id
    started_at = $process.StartTime.ToUniversalTime().ToString("o")
    project_dir = $ChatTraceProjectDir
    port = $ChatTracePort
} | ConvertTo-Json | Set-Content -LiteralPath $ChatTraceStatePath -Encoding UTF8

$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    if ($process.HasExited) {
        break
    }
    try {
        $response = Invoke-WebRequest -Uri "$ChatTraceBaseUrl/api/health" -UseBasicParsing -TimeoutSec 1
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $ready) {
    if (-not $process.HasExited) {
        Stop-ChatTraceProcessTree -ProcessId $process.Id
    }
    Remove-Item -LiteralPath $ChatTraceStatePath -Force -ErrorAction SilentlyContinue
    $details = if (Test-Path -LiteralPath $stderrPath) {
        (Get-Content -LiteralPath $stderrPath -Tail 20 -Encoding UTF8) -join [Environment]::NewLine
    } else {
        "No error log was created."
    }
    throw "ChatTrace did not become ready.`n$details"
}

Write-Host "ChatTrace started (PID $($process.Id))."
Write-Host "Open $ChatTraceBaseUrl"
Write-Host "Data: $ChatTraceDataDir"
Write-Host "Logs: $stdoutPath and $stderrPath"
