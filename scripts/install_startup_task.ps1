# PostDoctor: make the dashboard server auto-start at Windows logon,
# via a shortcut in the current user's Startup folder (no admin rights needed).
#
# (Task Scheduler registration was tried first but this machine denies
# Register-ScheduledTask / schtasks.exe to non-elevated processes, so the
# Startup-folder shortcut is used instead -- it only needs normal file write
# access and Windows runs it automatically at every logon.)
#
# The server listens on 0.0.0.0 so the dashboard can be opened from a phone
# over Tailscale (http://<tailscale-ip>:8765/). LAN access is blocked by the
# inbound firewall rule "PostDoctor: block 8765 except Tailscale" -- create
# that rule (admin PowerShell) BEFORE running this script; without it the
# venv's base pythonw.exe has an allow-all rule and the LAN can reach 8765.
#
# Usage:
#   .\scripts\install_startup_task.ps1                  # targets account "rakuba_ai"
#   .\scripts\install_startup_task.ps1 another_account   # targets a different account

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RootDir "venv\Scripts\pythonw.exe"
$MainPy = Join-Path $RootDir "main.py"
$Account = if ($args.Count -ge 1) { $args[0] } else { "rakuba_ai" }

if (-not (Test-Path $PythonExe)) {
    throw ("venv not found: " + $PythonExe + " -- run 'python -m venv venv' and 'pip install -r requirements.txt' first.")
}

$StartupDir = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupDir ("PostDoctor Dashboard Server (" + $Account + ").lnk")

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $PythonExe
$shortcut.Arguments = '"' + $MainPy + '" serve --account ' + $Account + ' --host 0.0.0.0 --no-browser'
$shortcut.WorkingDirectory = $RootDir
$shortcut.WindowStyle = 7  # minimized
$shortcut.Description = "PostDoctor dashboard server (" + $Account + ")"
$shortcut.Save()

Write-Host ("Created: " + $ShortcutPath)
Write-Host "It will auto-start at http://127.0.0.1:8765/ (and the Tailscale IP) on next Windows logon."
Write-Host ""
Write-Host ("Try it now:      Start-Process -FilePath '" + $PythonExe + "' -ArgumentList '" + $shortcut.Arguments + "'")
Write-Host ("Remove it:       .\scripts\uninstall_startup_task.ps1 " + $Account)
