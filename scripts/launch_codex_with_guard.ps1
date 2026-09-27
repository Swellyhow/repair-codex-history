param(
    [Parameter(Mandatory = $true)][string]$CodexAppId,
    [Parameter(Mandatory = $true)][string]$CodexHome,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$RepairScript
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework
$pythonPath = (Resolve-Path -LiteralPath $PythonExe -ErrorAction Stop).Path
$repairPath = (Resolve-Path -LiteralPath $RepairScript -ErrorAction Stop).Path
$homePath = (Resolve-Path -LiteralPath $CodexHome -ErrorAction Stop).Path
function Quote-ProcessArgument([string]$value) {
    # Start-Process joins ArgumentList entries into one command line; quote
    # path-like values so spaces cannot split the Python arguments.
    return '"' + ($value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}
try {
    $tempRoot = [System.IO.Path]::GetTempPath()
    $stdoutPath = Join-Path $tempRoot ("codex-guard-{0}.out" -f [guid]::NewGuid())
    $stderrPath = Join-Path $tempRoot ("codex-guard-{0}.err" -f [guid]::NewGuid())
    try {
        $process = Start-Process -FilePath $pythonPath -ArgumentList @(
            (Quote-ProcessArgument $repairPath), 'guard', '--codex-home', (Quote-ProcessArgument $homePath), '--yes', '--json'
        ) -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $exitCode = $process.ExitCode
        $stdout = if (Test-Path -LiteralPath $stdoutPath) { Get-Content -Raw -LiteralPath $stdoutPath } else { '' }
        $stderr = if (Test-Path -LiteralPath $stderrPath) { Get-Content -Raw -LiteralPath $stderrPath } else { '' }
    } finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
    $result = $null
    if ($stdout.Trim()) {
        try { $result = $stdout | ConvertFrom-Json } catch { $result = $null }
    }
    if ($exitCode -ne 0 -or -not $result -or -not $result.guard_complete -or $result.next_action -ne 'launch') {
        $detail = if ($result -and $result.next_action) { $result.next_action } elseif ($stderr.Trim()) { $stderr.Trim() } elseif ($stdout.Trim()) { $stdout.Trim() } else { "exit code $exitCode" }
        $choice = [System.Windows.MessageBox]::Show(
            "Continuity guard did not complete. Codex was not started.`n`n$detail`n`nQuit Codex if it is open, then try again. You may choose Yes to launch Codex without the guard.",
            'Codex Continuity', 'YesNo', 'Warning'
        )
        if ($choice -eq 'Yes') {
            Start-Process explorer.exe -ArgumentList "shell:AppsFolder\$CodexAppId"
        }
        exit 1
    }
} catch {
    [System.Windows.MessageBox]::Show(
        "Continuity guard failed and Codex was not started:`n$_",
        'Codex Continuity', 'OK', 'Error'
    ) | Out-Null
    exit 1
}

Start-Process -FilePath explorer.exe -ArgumentList @("shell:AppsFolder\$CodexAppId")
