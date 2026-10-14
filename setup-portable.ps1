param([switch]$Policy)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = (& python -c 'import sys,tkinter; assert sys.version_info >= (3,11); print(sys.executable)').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+ with Tk and Add Python to PATH, then run Setup.cmd again.' }
& $taskPython scripts/bootstrap_portable.py
if ($LASTEXITCODE -ne 0) { throw 'Dependency setup failed; read the error above.' }
if ($Policy) {
    & $taskPython toolbuild.py setup-policy
    if ($LASTEXITCODE -ne 0) { throw 'Policy setup failed; WSL2 must already be enabled.' }
}
Write-Output 'Ready. Open ColorNT.cmd, choose ROM A, ROM B and output. Mods default OFF.'
Write-Output 'For full v1.4 import the matching mod data ZIP and prepare SELinux once in the GUI.'
