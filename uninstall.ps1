#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA "Programs\AgentsChat"),
    [switch]$RemoveData,
    [switch]$KeepSkill
)

$ErrorActionPreference = "Stop"
$AppName = "Agents Chat"
$SkillName = "agents-chat-desktop"
$Destination = [System.IO.Path]::GetFullPath($Destination)

function Step([string]$Message) { Write-Host ("==> " + $Message) -ForegroundColor Cyan }

Step "Stopping the tray host and hub"
try {
    $quit = [System.Threading.EventWaitHandle]::OpenExisting("Local\AgentsChatQuit05")
    [void]$quit.Set()
    $quit.Dispose()
    Start-Sleep -Seconds 2
} catch { }
$cli = Join-Path $Destination "AgentsChat.Cli.exe"
$state = if (Test-Path (Join-Path $Destination "data")) { Join-Path $Destination "data" } else { Join-Path $Destination ".bridge" }
$config = if (Test-Path (Join-Path $Destination "config.local.json")) { Join-Path $Destination "config.local.json" } else { Join-Path $Destination "config.json" }
if ((Test-Path $cli) -and (Test-Path $config)) {
    try { & $cli --state $state --config $config stop 2>$null | Out-Null } catch { }
    Start-Sleep -Seconds 2
}

Step "Removing shortcuts and registration"
$menu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Agents Chat"
Remove-Item $menu -Recurse -Force -ErrorAction SilentlyContinue
$desktop = [Environment]::GetFolderPath("Desktop")
Remove-Item (Join-Path $desktop "Agents Chat.lnk") -Force -ErrorAction SilentlyContinue
$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Agents Chat.lnk"
Remove-Item $startup -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Agents Chat.vbs") -Force -ErrorAction SilentlyContinue
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AgentsChat" -Recurse -Force -ErrorAction SilentlyContinue

if (-not $KeepSkill) {
    Step "Removing the user-level skill"
    foreach ($skills in @(
        (Join-Path $env:USERPROFILE ".codex\skills"),
        (Join-Path $env:USERPROFILE ".qoder\skills"),
        (Join-Path $env:USERPROFILE ".agents\skills"))) {
        Remove-Item (Join-Path $skills $SkillName) -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Step "Removing application files"
if (Test-Path $Destination) {
    if ($RemoveData) {
        Remove-Item $Destination -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Get-ChildItem $Destination -Force | Where-Object {
            $_.Name -notin @("config.json", "config.local.json", "data", ".bridge")
        } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "    configuration and data kept in $Destination" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Agents Chat was removed." -ForegroundColor Green
if (-not $RemoveData) { Write-Host "Data was kept. Run with -RemoveData to delete it as well." }
