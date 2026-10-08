param(
    [string]$ProjectDir = $PSScriptRoot,
    [string]$SharedDir = "",
    [int]$LeaseHours = 168,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "sync-common.ps1")
if (-not $SharedDir) { $SharedDir = $ChatTraceDefaultSharedDir }

if ($LeaseHours -lt 1) {
    throw "LeaseHours must be at least 1."
}

$context = Get-ChatTraceSyncContext -ProjectDir $ProjectDir -SharedDir $SharedDir
$machineId = Get-ChatTraceMachineId
Assert-ChatTraceLeaseAvailable -Context $context -Force:$Force
$manifest = Read-ChatTraceJson -Path $context.Manifest
$restoredGeneration = 0
$leaseWritten = $false

try {
    if ($null -ne $manifest) {
        $snapshotRelative = [string]$manifest.snapshot
        if ([string]::IsNullOrWhiteSpace($snapshotRelative) -or $snapshotRelative.Contains("..") -or [IO.Path]::IsPathRooted($snapshotRelative)) {
            throw "The shared manifest contains an invalid snapshot path."
        }
        $snapshotPath = Join-Path $context.SharedRoot ($snapshotRelative.Replace('/', [IO.Path]::DirectorySeparatorChar))
        if (-not (Test-Path -LiteralPath $snapshotPath)) {
            throw "The manifest arrived before its snapshot. Wait for Google Drive to finish syncing: $snapshotPath"
        }
        if ((Get-ChatTraceSha256 -Path $snapshotPath) -ne ([string]$manifest.sha256).ToLowerInvariant()) {
            throw "Snapshot hash mismatch. Google Drive may still be syncing; wait and try again."
        }
        if ((Get-Item -LiteralPath $snapshotPath).Length -ne [int64]$manifest.size_bytes) {
            throw "Snapshot size mismatch. Google Drive may still be syncing; wait and try again."
        }
        Test-ChatTraceDatabase -Context $context -Path $snapshotPath

        # Older code cannot run a database migrated by newer code.
        $codeSchema = [int](Invoke-ChatTraceHelper -Context $context -Arguments @("code-schema"))
        $snapshotSchema = [int](Invoke-ChatTraceHelper -Context $context -Arguments @("schema", $snapshotPath))
        if ($snapshotSchema -gt $codeSchema) {
            throw "Snapshot uses schema v$snapshotSchema but this checkout only knows v$codeSchema. Update the code (git pull, then pip install -e .) and run take.cmd again."
        }

        Write-Host "Stopping ChatTrace before restoring generation $($manifest.generation)..."
        Invoke-ChatTraceScript -Path $context.StopScript

        $incomingPath = Join-Path $context.DataDir ("chattrace.{0}.incoming" -f [guid]::NewGuid().ToString("N"))
        $localBackupDir = Join-Path $context.DataDir "sync-local-backups"
        New-Item -ItemType Directory -Path $localBackupDir -Force | Out-Null
        try {
            Copy-Item -LiteralPath $snapshotPath -Destination $incomingPath -Force
            Test-ChatTraceDatabase -Context $context -Path $incomingPath
            if (Test-Path -LiteralPath $context.Database) {
                # A consistent copy that includes anything still in the local WAL.
                $backupName = "chattrace-before-generation-{0}-{1}.db" -f $manifest.generation, [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
                Invoke-ChatTraceHelper -Context $context -Arguments @("backup", $context.Database, (Join-Path $localBackupDir $backupName)) | Out-Null
                Remove-Item -LiteralPath $context.Database -Force
            }
            # The service has stopped, so any remaining SQLite sidecar files are stale.
            # They must not be paired with the database restored from another computer.
            Remove-Item -LiteralPath "$($context.Database)-wal" -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath "$($context.Database)-shm" -Force -ErrorAction SilentlyContinue
            Move-Item -LiteralPath $incomingPath -Destination $context.Database
            Test-ChatTraceDatabase -Context $context -Path $context.Database
        }
        finally {
            Remove-Item -LiteralPath $incomingPath -Force -ErrorAction SilentlyContinue
        }
        $restoredGeneration = [int64]$manifest.generation
    }
    elseif (-not (Test-Path -LiteralPath $context.Database)) {
        throw "No shared snapshot or local database exists. Run release.cmd on the computer that has the archive first."
    }

    $now = [DateTimeOffset]::UtcNow
    $lease = [ordered]@{
        format_version = 1
        owner = $machineId
        acquired_at = $now.ToString("o")
        expires_at = $now.AddHours($LeaseHours).ToString("o")
        generation = $restoredGeneration
        project_dir = $context.ProjectDir
    }
    Write-ChatTraceJsonAtomic -Path $context.Lease -Value $lease
    $leaseWritten = $true

    Invoke-ChatTraceScript -Path $context.StartScript
    Write-Host "Control acquired by $machineId."
    Write-Host "Restored generation: $restoredGeneration"
    Write-Host "Lease expires: $($lease.expires_at)"
}
catch {
    if ($leaseWritten -and (Test-Path -LiteralPath $context.Lease)) {
        $writtenLease = Read-ChatTraceJson -Path $context.Lease
        if ($null -ne $writtenLease -and [string]$writtenLease.owner -eq $machineId) {
            Remove-Item -LiteralPath $context.Lease -Force -ErrorAction SilentlyContinue
        }
    }
    throw
}
