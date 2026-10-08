param(
    [string]$ProjectDir = $PSScriptRoot,
    [string]$SharedDir = "",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "sync-common.ps1")
if (-not $SharedDir) { $SharedDir = $ChatTraceDefaultSharedDir }

$context = Get-ChatTraceSyncContext -ProjectDir $ProjectDir -SharedDir $SharedDir
$machineId = Get-ChatTraceMachineId
$activeLease = Get-ChatTraceActiveLease -Context $context
if ($null -ne $activeLease -and [string]$activeLease.owner -ne $machineId -and -not $Force) {
    throw "Database is checked out by '$($activeLease.owner)'. Refusing to overwrite its work. Use -Force only after confirming that computer is offline."
}
if (-not (Test-Path -LiteralPath $context.Database)) {
    throw "Local database not found: $($context.Database)"
}

Write-Host "Stopping ChatTrace before publishing a consistent snapshot..."
# stop.ps1 refuses while a sync or summary is running; this release then stops too.
Invoke-ChatTraceScript -Path $context.StopScript

$temporaryLocal = Join-Path ([IO.Path]::GetTempPath()) ("chattrace-{0}.db" -f [guid]::NewGuid().ToString("N"))
$uploadingPath = $null
try {
    Invoke-ChatTraceHelper -Context $context -Arguments @("backup", $context.Database, $temporaryLocal) | Out-Null
    $schemaVersion = [int](Invoke-ChatTraceHelper -Context $context -Arguments @("schema", $temporaryLocal))

    $manifest = Read-ChatTraceJson -Path $context.Manifest
    $generation = 1
    if ($null -ne $manifest -and $null -ne $manifest.generation) {
        $generation = [int64]$manifest.generation + 1
    }
    $timestamp = [DateTimeOffset]::UtcNow
    $safeMachine = ($env:COMPUTERNAME -replace '[^A-Za-z0-9_.-]', '_')
    $snapshotName = "chattrace-{0:D8}-{1}-{2}.db" -f $generation, $timestamp.ToString("yyyyMMddTHHmmssZ"), $safeMachine
    $snapshotPath = Join-Path $context.SnapshotDir $snapshotName
    $uploadingPath = Join-Path $context.SnapshotDir (".{0}.uploading" -f $snapshotName)

    Copy-Item -LiteralPath $temporaryLocal -Destination $uploadingPath -Force
    Test-ChatTraceDatabase -Context $context -Path $uploadingPath
    $hash = Get-ChatTraceSha256 -Path $uploadingPath
    $size = (Get-Item -LiteralPath $uploadingPath).Length
    Move-Item -LiteralPath $uploadingPath -Destination $snapshotPath -Force
    $uploadingPath = $null

    Write-ChatTraceJsonAtomic -Path $context.Manifest -Value ([ordered]@{
        format_version = 1
        generation = $generation
        snapshot = "snapshots/$snapshotName"
        sha256 = $hash
        size_bytes = $size
        schema_version = $schemaVersion
        published_at = $timestamp.ToString("o")
        published_by = $machineId
    })

    if (Test-Path -LiteralPath $context.Lease) {
        $lease = Read-ChatTraceJson -Path $context.Lease
        if ($Force -or $null -eq $lease -or [string]$lease.owner -eq $machineId) {
            Remove-Item -LiteralPath $context.Lease -Force
        }
    }

    Write-Host "Database released to Google Drive."
    Write-Host "Generation: $generation (schema v$schemaVersion)"
    Write-Host "Snapshot: $snapshotPath"
    Write-Host "SHA-256: $hash"
    Write-Host "ChatTrace stays stopped. Wait until Google Drive reports 'Up to date' before running take.cmd on the other computer."
}
finally {
    Remove-Item -LiteralPath $temporaryLocal -Force -ErrorAction SilentlyContinue
    if ($null -ne $uploadingPath) {
        Remove-Item -LiteralPath $uploadingPath -Force -ErrorAction SilentlyContinue
    }
}
