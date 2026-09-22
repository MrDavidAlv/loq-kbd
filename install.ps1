#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Windows installer for loq-kbd.

.DESCRIPTION
    Puts this folder on your user PATH so 'loq-kbd' works from any prompt, and
    registers a scheduled task that reapplies your lighting at logon and after
    the machine wakes up. No driver and no reboot: Windows already has a HID
    stack, and the keyboard speaks standard HID LampArray.

    Run it as your normal user.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -NoTask       # PATH only, skip the scheduled task
    .\install.ps1 -Uninstall    # undo both
#>
[CmdletBinding()]
param(
    [switch]$NoTask,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$taskName = 'loq-kbd lighting'

function Find-Python {
    <# Returns the console interpreter and, when present, the windowless one.
       pythonw.exe is what the scheduled task should use: an animated mode runs
       until stopped, and with python.exe that means a console window sitting
       open for as long as the lighting is on. #>
    foreach ($candidate in @('python', 'python3', 'py')) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        $exe = $found.Source
        $windowless = Join-Path (Split-Path -Parent $exe) 'pythonw.exe'
        return [pscustomobject]@{
            Console    = $exe
            Windowless = if (Test-Path $windowless) { $windowless } else { $exe }
        }
    }
    return $null
}

if ($Uninstall) {
    Write-Host '== removing the scheduled task'
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host '== removing this folder from PATH'
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $kept = ($current -split ';' | Where-Object { $_ -and $_ -ne $here }) -join ';'
    [Environment]::SetEnvironmentVariable('Path', $kept, 'User')
    Write-Host ''
    Write-Host 'Done. Your files were left alone, and your keyboard keeps'
    Write-Host 'whatever colour it has until the next restart.'
    exit 0
}

Write-Host '== 1/4  Python'
$python = Find-Python
if (-not $python) {
    Write-Error @'
No Python on PATH. Install it, then run this again:
    winget install Python.Python.3.12
or get it from https://python.org (tick "Add python.exe to PATH").
'@
    exit 1
}
Write-Host "   $($python.Console)"

Write-Host '== 2/4  PATH'
$current = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($current -split ';') -contains $here) {
    Write-Host '   already there'
} else {
    $updated = if ($current) { "$current;$here" } else { $here }
    [Environment]::SetEnvironmentVariable('Path', $updated, 'User')
    Write-Host "   added $here"
    Write-Host '   (open a new terminal for this to take effect)'
}

$config = Join-Path $here 'color.conf'
if (-not (Test-Path $config)) {
    Copy-Item (Join-Path $here 'color.conf.example') $config
    Write-Host '   created color.conf (default: blue)'
}

Write-Host '== 3/4  scheduled task'
if ($NoTask) {
    Write-Host '   skipped (-NoTask)'
} else {
    $action = New-ScheduledTaskAction -Execute $python.Windowless `
        -Argument "`"$(Join-Path $here 'loq_kbd.py')`" apply" -WorkingDirectory $here

    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    # Logon is the reliable trigger, so it goes in on its own first. The resume
    # trigger is added separately below, because subscribing to the System event
    # log can be refused for a standard user - and if that happens, a working
    # logon-only task is a much better outcome than a half-finished install.
    $triggers = @(New-ScheduledTaskTrigger -AtLogOn)
    $resumeAdded = $false

    try {
        $class = Get-CimClass -ClassName MSFT_TaskEventTrigger `
            -Namespace Root/Microsoft/Windows/TaskScheduler
        $resume = New-CimInstance -CimClass $class -ClientOnly
        $resume.Enabled = $true
        # Kernel-Power 107 is what the system logs when it resumes from sleep.
        $resume.Subscription = @'
<QueryList><Query Id="0" Path="System"><Select Path="System">
*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=107]]
</Select></Query></QueryList>
'@
        $triggers += $resume
        $resumeAdded = $true
    } catch {
        Write-Warning "could not build the resume trigger: $($_.Exception.Message)"
    }

    try {
        Register-ScheduledTask -TaskName $taskName -Action $action `
            -Trigger $triggers -Settings $settings -Force `
            -Description 'Reapply Lenovo LOQ keyboard lighting' | Out-Null
    } catch {
        if (-not $resumeAdded) { throw }
        Write-Warning "the resume trigger was rejected, registering logon only: $($_.Exception.Message)"
        $resumeAdded = $false
        Register-ScheduledTask -TaskName $taskName -Action $action `
            -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Settings $settings -Force `
            -Description 'Reapply Lenovo LOQ keyboard lighting' | Out-Null
    }

    if ($resumeAdded) {
        Write-Host '   registered: runs at logon and after resume'
    } else {
        Write-Host '   registered: runs at logon'
        Write-Host '   after waking from sleep, run  loq-kbd apply  yourself'
    }
}

Write-Host '== 4/4  check'
& $python.Console (Join-Path $here 'loq_kbd.py') info
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Warning @'
Could not talk to the keyboard. The usual cause on Windows 11 is Dynamic
Lighting holding the device: open Settings > Personalization > Dynamic Lighting
and turn off "Use Dynamic Lighting on my devices".

Worth knowing: if Dynamic Lighting works for you, you may not need this tool at
all - it can set a fixed colour by itself.
'@
    exit 1
}

Write-Host ''
Write-Host 'Done. Try:'
Write-Host '   loq-kbd red          loq-kbd morado 60       loq-kbd ff8800'
Write-Host '   loq-kbd mode3        loq-kbd modes           loq-kbd colours'
Write-Host '   loq-kbd save mode3 50     # remember it for logon and resume'
