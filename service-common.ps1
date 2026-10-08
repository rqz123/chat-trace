Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Shared by start.ps1, stop.ps1 and the database handoff scripts.
# The service always uses <project>\data so both computers keep the same layout.

$script:ChatTraceProjectDir = $PSScriptRoot
$script:ChatTraceDataDir = Join-Path $PSScriptRoot "data"
$script:ChatTracePython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$script:ChatTraceStatePath = Join-Path $script:ChatTraceDataDir "chattrace-service.json"
$script:ChatTracePort = if ($env:CHATTRACE_PORT) { [int]$env:CHATTRACE_PORT } else { 18765 }
$script:ChatTraceBaseUrl = "http://127.0.0.1:$($script:ChatTracePort)"

function Get-ChatTraceRecordedProcess {
    # Returns the recorded service process, or $null (removing stale state).
    if (-not (Test-Path -LiteralPath $script:ChatTraceStatePath)) {
        return $null
    }
    try {
        $state = Get-Content -LiteralPath $script:ChatTraceStatePath -Raw | ConvertFrom-Json
        $process = Get-Process -Id ([int]$state.pid) -ErrorAction Stop
        $recordedStart = [DateTimeOffset]::Parse([string]$state.started_at).UtcDateTime
        if ([Math]::Abs(($process.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -le 2) {
            return $process
        }
    }
    catch {
        # A missing process or invalid state file is treated as stale state.
    }
    Remove-Item -LiteralPath $script:ChatTraceStatePath -Force -ErrorAction SilentlyContinue
    return $null
}

function Get-ChatTraceUnmanagedProcesses {
    # ChatTrace servers started by hand (not through start.ps1) from this project.
    $pattern = "*-m chattrace.app*"
    return (Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like $pattern -and $_.ExecutablePath -like "$script:ChatTraceProjectDir*" })
}

function Get-ChatTraceBusyReason {
    # Returns why the service must not be stopped right now, or $null.
    try {
        $jobs = Invoke-RestMethod -Uri "$script:ChatTraceBaseUrl/api/jobs?limit=5" -TimeoutSec 3
        foreach ($job in @($jobs)) {
            if ($job.status -in @("queued", "running")) {
                return "sync job #$($job.id) is $($job.status)"
            }
        }
        $orchestration = Invoke-RestMethod -Uri "$script:ChatTraceBaseUrl/api/orchestration" -TimeoutSec 3
        if ($orchestration.active) {
            return "'launch WeChat and archive' is in progress"
        }
        $summaries = Invoke-RestMethod -Uri "$script:ChatTraceBaseUrl/api/summaries" -TimeoutSec 3
        foreach ($summary in @($summaries)) {
            if ($summary.status -in @("queued", "running")) {
                return "topic summary #$($summary.id) is $($summary.status)"
            }
        }
    }
    catch {
        # Service not answering: nothing in progress that it could report.
    }
    return $null
}

function Stop-ChatTraceProcessTree {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    # The venv python.exe is a launcher; the server runs in its child process.
    & taskkill.exe /PID $ProcessId /T /F | Out-Null
    Wait-Process -Id $ProcessId -Timeout 15 -ErrorAction SilentlyContinue
}
