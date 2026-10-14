$ErrorActionPreference = 'Stop'
$record = Join-Path $PSScriptRoot 'installation.json'
$pythonCommand = if (Test-Path -LiteralPath $record) {
    (Get-Content -LiteralPath $record -Raw | ConvertFrom-Json).python
} else { (Get-Command python).Source }
& $pythonCommand (Join-Path $PSScriptRoot 'scripts\prepare_windows_workspace.py')
if ($LASTEXITCODE -ne 0) { throw 'Workspace preparation failed; see config/workspace-prepared.json for API stage and Windows error.' }
