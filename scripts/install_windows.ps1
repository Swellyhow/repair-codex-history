param(
    [string]$CodexHome = (Join-Path $HOME '.codex'),
    [string]$CodexAppId
)

$ErrorActionPreference = 'Stop'
$scriptDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$repairScript = Join-Path $scriptDirectory 'repair_history.py'
$launcher = Join-Path $scriptDirectory 'launch_codex_with_guard.ps1'
if (-not (Test-Path -LiteralPath $repairScript) -or -not (Test-Path -LiteralPath $launcher)) {
    throw 'repair_history.py or launch_codex_with_guard.ps1 is missing.'
}

if (-not $CodexAppId) {
    $matches = @(Get-StartApps | Where-Object { $_.Name -match '(?i)codex' })
    if ($matches.Count -ne 1 -or -not $matches[0].AppID) {
        throw 'Could not identify exactly one Codex Start Menu app. Re-run with -CodexAppId after checking Get-StartApps.'
    }
    $CodexAppId = $matches[0].AppID
}

$codexHomePath = (New-Item -ItemType Directory -Force -Path $CodexHome).FullName
$pythonCommand = Get-Command python.exe -ErrorAction Stop
$pythonPath = $pythonCommand.Source
$powershellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Codex Continuity.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershellPath
# Windows shortcut arguments cannot be passed as an array. These values are
# filesystem paths (quotes are illegal in Windows paths), so explicit quoting
# safely preserves spaces in the project and Codex home directories.
$shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -CodexAppId "{1}" -CodexHome "{2}" -PythonExe "{3}" -RepairScript "{4}"' -f $launcher, $CodexAppId, $codexHomePath, $pythonPath, $repairScript
$shortcut.WorkingDirectory = $scriptDirectory
$shortcut.Description = 'Run the local history continuity guard, then launch Codex.'
$shortcut.Save()

$stateDirectory = Join-Path $codexHomePath 'history-repair-state'
New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
$marker = @{
    installed_at = [DateTime]::UtcNow.ToString('o')
    version = 6
    launcher = $launcher
    shortcut = $shortcutPath
} | ConvertTo-Json
Set-Content -LiteralPath (Join-Path $stateDirectory 'guard-installed.json') -Value $marker -Encoding utf8
Write-Output (@{ guard_installed = $true; shortcut = $shortcutPath; app_id = $CodexAppId } | ConvertTo-Json -Compress)
