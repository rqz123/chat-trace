param(
    # Stop even while a sync, launch-and-archive or topic summary is running.
    # An interrupted sync is marked as such on the next start; archived rows are kept.
    [switch]$Force
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "service-common.ps1")

$recorded = Get-ChatTraceRecordedProcess
$targets = @()
if ($null -ne $recorded) {
    $targets += $recorded.Id
}
foreach ($process in Get-ChatTraceUnmanagedProcesses) {
    if ($targets -notcontains $process.ProcessId) {
        $targets += $process.ProcessId
    }
}

if ($targets.Count -eq 0) {
    Write-Host "ChatTrace is not running."
    exit 0
}

$busy = Get-ChatTraceBusyReason
if ($null -ne $busy -and -not $Force) {
    throw "Not stopping ChatTrace: $busy. Wait for it to finish, or run stop.ps1 -Force."
}

foreach ($id in $targets) {
    Stop-ChatTraceProcessTree -ProcessId $id
}
Remove-Item -LiteralPath $ChatTraceStatePath -Force -ErrorAction SilentlyContinue

Write-Host "ChatTrace stopped (PID $($targets -join ', '))."
