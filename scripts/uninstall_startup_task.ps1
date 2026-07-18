# PostDoctor: remove the Startup-folder shortcut created by install_startup_task.ps1
#
# Usage:
#   .\scripts\uninstall_startup_task.ps1                  # removes the "rakuba_ai" shortcut
#   .\scripts\uninstall_startup_task.ps1 another_account   # removes a different account's shortcut

$ErrorActionPreference = "Stop"

$Account = if ($args.Count -ge 1) { $args[0] } else { "rakuba_ai" }
$StartupDir = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupDir ("PostDoctor Dashboard Server (" + $Account + ").lnk")

if (-not (Test-Path $ShortcutPath)) {
    Write-Host ("Not found: " + $ShortcutPath)
    exit 0
}

Remove-Item -Path $ShortcutPath -Force
Write-Host ("Removed: " + $ShortcutPath)
