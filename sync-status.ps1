param(
    [string]$ProjectDir = $PSScriptRoot,
    [string]$SharedDir = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "sync-common.ps1")
. (Join-Path $PSScriptRoot "service-common.ps1")
if (-not $SharedDir) { $SharedDir = $ChatTraceDefaultSharedDir }

$context = Get-ChatTraceSyncContext -ProjectDir $ProjectDir -SharedDir $SharedDir
$manifest = Read-ChatTraceJson -Path $context.Manifest
$lease = Read-ChatTraceJson -Path $context.Lease

Write-Host "This computer: $(Get-ChatTraceMachineId)"
Write-Host "Shared folder: $($context.SharedRoot)"
if ($null -eq $manifest) {
    Write-Host "Shared snapshot: none"
} else {
    Write-Host "Shared generation: $($manifest.generation) (schema v$($manifest.schema_version))"
    Write-Host "Published by: $($manifest.published_by)"
    Write-Host "Published at: $($manifest.published_at)"
    Write-Host "Snapshot: $($manifest.snapshot)"
}

if ($null -eq $lease) {
    Write-Host "Lease: available"
} else {
    $state = "active"
    if ([DateTimeOffset]::Parse([string]$lease.expires_at) -le [DateTimeOffset]::UtcNow) {
        $state = "expired"
    }
    Write-Host "Lease: $state"
    Write-Host "Owner: $($lease.owner)"
    Write-Host "Expires: $($lease.expires_at)"
}

$running = ($null -ne (Get-ChatTraceRecordedProcess)) -or (@(Get-ChatTraceUnmanagedProcesses).Count -gt 0)
Write-Host "Local service running: $running"
