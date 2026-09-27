[CmdletBinding()]
param(
    [string]$Repository = 'https://github.com/Swellyhow/repair-codex-history.git',
    [string]$Branch = 'feat/session-continuity-v6',
    [string]$CodexHome = (Join-Path $HOME '.codex'),
    [switch]$RunDoctor
)

$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $Command $($Arguments -join ' ')"
    }
}

function Copy-DirectoryContents {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        return
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Copy-Item -Path (Join-Path $Source '*') -Destination $Destination -Recurse -Force
}

$git = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $git) {
    throw 'git.exe was not found. Install Git for Windows and retry.'
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('repair-codex-history-' + [guid]::NewGuid().ToString('N'))
$target = Join-Path ([IO.Path]::GetFullPath($CodexHome)) 'skills\repair-codex-history'

try {
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    Invoke-Checked -Command $git.Source -Arguments @(
        'clone', '--depth', '1', '--single-branch', '--branch', $Branch, '--', $Repository, $tempRoot
    )

    $required = @(
        (Join-Path $tempRoot 'SKILL.md'),
        (Join-Path $tempRoot 'scripts\repair_history.py')
    )
    foreach ($path in $required) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "The repository is missing required file: $path"
        }
    }

    # Copy only the installable Skill payload. Existing files are updated in place;
    # unrelated files in the user's Skill directory are preserved.
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item -LiteralPath (Join-Path $tempRoot 'SKILL.md') -Destination $target -Force
    Copy-DirectoryContents -Source (Join-Path $tempRoot 'agents') -Destination (Join-Path $target 'agents')
    Copy-DirectoryContents -Source (Join-Path $tempRoot 'scripts') -Destination (Join-Path $target 'scripts')
    Copy-DirectoryContents -Source (Join-Path $tempRoot 'references') -Destination (Join-Path $target 'references')

    $result = [ordered]@{
        installed = $true
        repository = $Repository
        branch = $Branch
        skill_directory = $target
        doctor_run = $false
    }

    if ($RunDoctor) {
        if (-not $python) {
            throw 'python.exe was not found. Install Python 3.10+ or omit -RunDoctor.'
        }
        $repairScript = Join-Path $target 'scripts\repair_history.py'
        $doctorOutput = & $python.Source $repairScript doctor --codex-home $CodexHome --json
        if ($LASTEXITCODE -ne 0) {
            throw "doctor failed: $doctorOutput"
        }
        $result.doctor_run = $true
        $result.doctor = ($doctorOutput | Out-String).Trim()
    }

    $result | ConvertTo-Json -Depth 8 -Compress
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
