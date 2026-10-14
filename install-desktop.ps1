param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'ColorNT'),
    [ValidatePattern('^[A-Za-z0-9 _.\-]+$')][string]$ShortcutName = 'ColorNT Toolbuild',
    [switch]$NoShortcut,
    [switch]$Portable,
    [switch]$Update
)
$ErrorActionPreference = 'Stop'
$sourceRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$installRoot = [IO.Path]::GetFullPath($Destination)
$exists = Test-Path -LiteralPath $installRoot
if ($exists) {
    $recordPath = Join-Path $installRoot 'installation.json'
    if (-not $Update -or -not (Test-Path -LiteralPath $recordPath) -or
        (Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json).application -ne 'ColorNT') {
        throw "Existing destination must be a ColorNT installation and requires -Update: $installRoot"
    }
}
if ($installRoot.StartsWith($sourceRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Install outside the source repository'
}
$pythonExe = (& python -c 'import sys, tkinter; assert sys.version_info >= (3,11); print(sys.executable)').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Python 3.11+ with Tk is required on this PC'
}
$pythonwExe = Join-Path (Split-Path -Parent $pythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonwExe)) { throw 'pythonw.exe is missing' }
foreach ($name in @('extract.erofs.exe', 'mkfs.erofs.exe', 'cygwin1.dll')) {
    $tool = Join-Path $sourceRoot "tools\local\erofs-tools\$name"
    if (-not (Test-Path -LiteralPath $tool)) { throw "Pinned local tool is missing: $tool" }
}
if (-not $exists) { New-Item -ItemType Directory -Path $installRoot | Out-Null }
# MSIX-hosted shells may redirect AppData to the calling app's LocalCache.
# Point launchers at the physical path so Explorer outside Codex can find it.
$resolvedInstallRoot = (& $pythonExe -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' $installRoot).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve physical install directory' }
$installRoot = $resolvedInstallRoot
foreach ($name in @('colornt', 'profiles', 'recipes')) {
    $sourceDirectory = Join-Path $sourceRoot $name
    foreach ($sourceFile in Get-ChildItem -LiteralPath $sourceDirectory -File -Recurse) {
        if ($sourceFile.FullName -match '[\\/]__pycache__[\\/]') { continue }
        $relativeFile = $sourceFile.FullName.Substring($sourceDirectory.Length + 1)
        $targetFile = Join-Path $installRoot (Join-Path $name $relativeFile)
        New-Item -ItemType Directory -Path (Split-Path -Parent $targetFile) -Force | Out-Null
        Copy-Item -LiteralPath $sourceFile.FullName -Destination $targetFile -Force
    }
}
foreach ($name in @('colornt_gui.py', 'toolbuild.py', 'prepare-workspace.ps1')) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot $name) -Destination $installRoot
}
New-Item -ItemType Directory -Path (Join-Path $installRoot 'tools\local') -Force | Out-Null
foreach ($name in @('lock.json', 'README.md')) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot "tools\$name") -Destination (Join-Path $installRoot "tools\$name")
}
New-Item -ItemType Directory -Path (Join-Path $installRoot 'scripts') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $sourceRoot 'scripts\desktop_build.py') -Destination (Join-Path $installRoot 'scripts\desktop_build.py')
Copy-Item -LiteralPath (Join-Path $sourceRoot 'scripts\flash_from_bootloader.py') -Destination (Join-Path $installRoot 'scripts\flash_from_bootloader.py')
Copy-Item -LiteralPath (Join-Path $sourceRoot 'scripts\prepare_windows_workspace.py') -Destination (Join-Path $installRoot 'scripts\prepare_windows_workspace.py')
foreach ($name in @('erofs-tools', 'payload', 'avb', 'policy-runtime')) {
    $toolDir = Join-Path $sourceRoot "tools\local\$name"
    if (Test-Path -LiteralPath $toolDir) {
        foreach ($sourceFile in Get-ChildItem -LiteralPath $toolDir -File -Recurse) {
            $relativeFile = $sourceFile.FullName.Substring($toolDir.Length + 1)
            $targetFile = Join-Path $installRoot "tools\local\$name\$relativeFile"
            New-Item -ItemType Directory -Path (Split-Path -Parent $targetFile) -Force | Out-Null
            Copy-Item -LiteralPath $sourceFile.FullName -Destination $targetFile -Force
        }
    }
}
New-Item -ItemType Directory -Path (Join-Path $installRoot 'config') -Force | Out-Null
$localConfig = Join-Path $sourceRoot 'config\project.local.json'
if (-not $Portable -and (Test-Path -LiteralPath $localConfig)) {
    $installedConfig = Join-Path $installRoot 'config\project.local.json'
    if ($Update -and (Test-Path -LiteralPath $installedConfig)) {
        $currentConfig = Get-Content -LiteralPath $installedConfig -Raw | ConvertFrom-Json
        $sourceConfig = Get-Content -LiteralPath $localConfig -Raw | ConvertFrom-Json
        foreach ($property in $sourceConfig.PSObject.Properties) {
            if (-not $currentConfig.PSObject.Properties[$property.Name]) {
                $currentConfig | Add-Member -NotePropertyName $property.Name -NotePropertyValue $property.Value
            }
        }
        $currentConfig | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $installedConfig -Encoding utf8
    } else {
        Copy-Item -LiteralPath $localConfig -Destination $installedConfig
    }
}
Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs\DESKTOP.md') -Destination (Join-Path $installRoot 'README.md')
New-Item -ItemType Directory -Path (Join-Path $installRoot 'docs') -Force | Out-Null
foreach ($name in @('PORT-BUILD-METHOD.md', 'PORT-TOOL-REQUIREMENTS.md')) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot "docs\$name") -Destination (Join-Path $installRoot "docs\$name")
}
$launcher = "@echo off`r`ncd /d `"%~dp0`"`r`nstart `"`" `"$pythonwExe`" `"%~dp0colornt_gui.py`"`r`n"
[IO.File]::WriteAllText((Join-Path $installRoot 'ColorNT.cmd'), $launcher, [Text.Encoding]::Default)
$installedVersion = (& $pythonExe -c 'import sys; sys.path.insert(0,sys.argv[1]); from colornt import __version__; print(__version__)' $installRoot).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot verify installed app version' }
@{ application='ColorNT'; version=$installedVersion; installed_at=(Get-Date -Format o); source=$sourceRoot;
   python=$pythonExe; destination=$installRoot; dependencies='Local Python/Tk; bundled pinned EROFS/payload tools';
   rom_inputs_copied=$false } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $installRoot 'installation.json') -Encoding utf8
if (-not $NoShortcut) {
    $desktopDir = [Environment]::GetFolderPath('Desktop')
    $shortcutPath = Join-Path $desktopDir ($ShortcutName + '.lnk')
    if ((Test-Path -LiteralPath $shortcutPath) -and -not $Update) { throw "Shortcut already exists: $shortcutPath; app installed at $installRoot" }
    $shellObject = New-Object -ComObject WScript.Shell
    $shortcut = $shellObject.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonwExe
    $shortcut.Arguments = '"' + (Join-Path $installRoot 'colornt_gui.py') + '"'
    $shortcut.WorkingDirectory = $installRoot
    $shortcut.Description = 'ColorNT local ROM builder (no flasher)'
    $shortcut.Save()
}
Write-Output "Installed ColorNT: $installRoot"
Write-Output 'ROMs/assets were not copied; run ColorNT.cmd or the desktop shortcut.'
