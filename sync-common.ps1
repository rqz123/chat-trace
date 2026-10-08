Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Default Google Drive exchange folder; override with -SharedDir or CHATTRACE_SHARED_DIR.
$script:ChatTraceDefaultSharedDir = if ($env:CHATTRACE_SHARED_DIR) {
    $env:CHATTRACE_SHARED_DIR
} else {
    "G:\My Drive\Claude\Project\chat-trace\shared-state"
}

function Get-ChatTraceMachineId {
    return "$env:COMPUTERNAME\$env:USERNAME"
}

function Get-ChatTraceSyncContext {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectDir,
        [Parameter(Mandatory = $true)][string]$SharedDir
    )

    $resolvedProject = (Resolve-Path -LiteralPath $ProjectDir).Path
    New-Item -ItemType Directory -Path $SharedDir -Force | Out-Null
    $sharedRoot = (Resolve-Path -LiteralPath $SharedDir).Path
    $dataDir = Join-Path $resolvedProject "data"
    $context = [pscustomobject]@{
        ProjectDir = $resolvedProject
        DataDir = $dataDir
        Python = Join-Path $resolvedProject ".venv\Scripts\python.exe"
        StartScript = Join-Path $resolvedProject "start.ps1"
        StopScript = Join-Path $resolvedProject "stop.ps1"
        Helper = Join-Path $resolvedProject "db-sync.py"
        Database = Join-Path $dataDir "chattrace.db"
        SharedRoot = $sharedRoot
        SnapshotDir = Join-Path $sharedRoot "snapshots"
        Manifest = Join-Path $sharedRoot "current.json"
        Lease = Join-Path $sharedRoot "lease.json"
    }
    foreach ($required in @($context.Python, $context.StartScript, $context.StopScript, $context.Helper)) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Required file not found: $required"
        }
    }
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    New-Item -ItemType Directory -Path $context.SnapshotDir -Force | Out-Null
    return $context
}

function Read-ChatTraceJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Write-ChatTraceJsonAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $temporary = Join-Path $directory (".{0}.{1}.tmp" -f (Split-Path -Leaf $Path), [guid]::NewGuid().ToString("N"))
    try {
        $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-ChatTraceScript {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string[]]$Arguments = @()
    )
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Path @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$(Split-Path -Leaf $Path) failed with exit code $LASTEXITCODE."
    }
}

function Invoke-ChatTraceHelper {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    Push-Location -LiteralPath $Context.ProjectDir
    try {
        $output = & $Context.Python $Context.Helper @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "db-sync.py $($Arguments[0]) failed with exit code $LASTEXITCODE."
        }
        return $output
    }
    finally {
        Pop-Location
    }
}

function Test-ChatTraceDatabase {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Path
    )
    Invoke-ChatTraceHelper -Context $Context -Arguments @("verify", $Path) | Out-Null
}

function Get-ChatTraceSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ChatTraceActiveLease {
    param([Parameter(Mandatory = $true)]$Context)
    $lease = Read-ChatTraceJson -Path $Context.Lease
    if ($null -eq $lease) {
        return $null
    }
    try {
        $expiresAt = [DateTimeOffset]::Parse([string]$lease.expires_at)
    }
    catch {
        throw "The shared lease file is invalid: $($Context.Lease)"
    }
    if ($expiresAt -le [DateTimeOffset]::UtcNow) {
        return $null
    }
    return $lease
}

function Assert-ChatTraceLeaseAvailable {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [switch]$Force
    )
    $lease = Get-ChatTraceActiveLease -Context $Context
    if ($null -eq $lease -or [string]$lease.owner -eq (Get-ChatTraceMachineId)) {
        return
    }
    if (-not $Force) {
        throw "Database is checked out by '$($lease.owner)' until $($lease.expires_at). Run release.cmd there first. Use -Force only after confirming that computer is not running ChatTrace."
    }
}
