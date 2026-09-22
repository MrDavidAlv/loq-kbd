#!/usr/bin/env pwsh
# Launcher for loq_kbd.py on Windows PowerShell / PowerShell 7.
#   loq-kbd blue            loq-kbd mode3 40            loq-kbd ff8800
$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$script = Join-Path $here 'loq_kbd.py'

$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $python = $found.Source; break }
}
if (-not $python) {
    Write-Error "No Python found on PATH. Install it from python.org or the Microsoft Store."
    exit 1
}

& $python $script @args
exit $LASTEXITCODE
