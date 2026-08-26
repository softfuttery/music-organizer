$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE"
    }
}

Push-Location $projectRoot
try {
    # Keep personal scratch files in the workspace from breaking the project gate.
    Invoke-Checked python -m ruff check music_organizer tests app.py worker.py
    Invoke-Checked python -m coverage erase
    Invoke-Checked python -m coverage run -m pytest -q
    Invoke-Checked python -m coverage report
    Invoke-Checked python -m pip_audit -r requirements.lock
    Invoke-Checked docker compose config --quiet
    Push-Location (Join-Path $projectRoot 'frontend-vue')
    try {
        Invoke-Checked npm.cmd run lint
        Invoke-Checked npm.cmd test
        Invoke-Checked npm.cmd run build
        Invoke-Checked npm.cmd audit --omit=dev --audit-level=high
    }
    finally {
        Pop-Location
    }
}
finally {
    Pop-Location
}
