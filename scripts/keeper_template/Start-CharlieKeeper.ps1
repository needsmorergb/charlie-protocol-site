<#
.SYNOPSIS
    Start the Charlie keeper from this folder, restarting it if it dies.

.DESCRIPTION
    Runs `python keeper.py run` against keeper.json in this folder. If the
    process exits for a reason that is not "done" (budget reached, stop file,
    crank count), it waits and starts it again -- the keeper's own state file
    carries the budget across restarts, so a restart never resets the spend.

    Stop it cleanly by creating the stop file named in keeper.json
    (default keeper.stop, next to this script). The keeper notices before its
    next crank and exits; this script then stops too.

.EXAMPLE
    .\Start-CharlieKeeper.ps1                # foreground, output to console and keeper.out.log
    .\Start-CharlieKeeper.ps1 -Preflight     # only the go/no-go table
#>
[CmdletBinding()]
param(
    [string]$Config = "keeper.json",
    [switch]$Preflight,
    [switch]$Once,
    [int]$RestartDelaySeconds = 120,
    [int]$MaxRestarts = 20
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw "python 3.11+ is required and was not found on PATH" }

$out = Join-Path $PSScriptRoot "keeper.out.log"

if ($Preflight) {
    & $python.Source keeper.py --config $Config preflight 2>&1 | Tee-Object -FilePath $out -Append
    exit $LASTEXITCODE
}
if ($Once) {
    & $python.Source keeper.py --config $Config once 2>&1 | Tee-Object -FilePath $out -Append
    exit $LASTEXITCODE
}

$restarts = 0
while ($true) {
    "$(Get-Date -Format o) starting keeper (restart $restarts)" | Tee-Object -FilePath $out -Append
    & $python.Source keeper.py --config $Config run 2>&1 | Tee-Object -FilePath $out -Append
    $code = $LASTEXITCODE
    # 0: stopped for a stated reason (budget, stop file). 2: config refused.
    # 3: not armed. 4: budget already spent. None of these should be retried.
    if ($code -in 0, 2, 3, 4) {
        "$(Get-Date -Format o) keeper exited with $code; not restarting" | Tee-Object -FilePath $out -Append
        exit $code
    }
    $restarts++
    if ($restarts -ge $MaxRestarts) {
        "$(Get-Date -Format o) $MaxRestarts restarts; giving up. Run -Preflight and read the log." | Tee-Object -FilePath $out -Append
        exit 1
    }
    "$(Get-Date -Format o) keeper exited with $code; restarting in $RestartDelaySeconds s" | Tee-Object -FilePath $out -Append
    Start-Sleep -Seconds $RestartDelaySeconds
}
