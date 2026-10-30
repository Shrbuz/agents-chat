#requires -Version 5.1
<#
.SYNOPSIS
  One-command installer for Agents Chat.

.DESCRIPTION
  Downloads the latest self-contained Windows portable release, installs it
  under %LOCALAPPDATA%\Programs\AgentsChat, installs the user-level
  agents-chat-desktop skill for Codex/Qoder/DSH, creates shortcuts and starts
  the tray host. No Python, Node, pip or administrator rights are required.

.EXAMPLE
  irm https://github.com/OWNER/agents-chat/releases/latest/download/install.ps1 | iex

.EXAMPLE
  .\install.ps1 -LocalZip .\dist\AgentsChat-Portable-0.5.0.zip -Destination $env:TEMP\AgentsChatTest -NoStart -NoShortcuts -NoRegistry -NoSkill
#>
[CmdletBinding()]
param(
    [string]$Repo = "__REPO__",
    [string]$Version = "latest",
    [string]$Destination = (Join-Path $env:LOCALAPPDATA "Programs\AgentsChat"),
    [string]$LocalZip,
    [switch]$NoStart,
    [switch]$NoShortcuts,
    [switch]$NoRegistry,
    [switch]$NoSkill,
    [switch]$Autostart
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
$AppName = "Agents Chat"
$SkillName = "agents-chat-desktop"
if ($Repo -notmatch "/" -and $env:AGENTS_CHAT_REPO) { $Repo = $env:AGENTS_CHAT_REPO }

function Step([string]$Message) { Write-Host ("==> " + $Message) -ForegroundColor Cyan }
function Fail([string]$Message) { throw ("Agents Chat install failed: " + $Message) }

function Stop-AgentsChat([string]$Root) {
    try {
        $quit = [System.Threading.EventWaitHandle]::OpenExisting("Local\AgentsChatQuit05")
        [void]$quit.Set()
        $quit.Dispose()
        Start-Sleep -Seconds 2
    } catch { }
    $cli = Join-Path $Root "AgentsChat.Cli.exe"
    $state = if (Test-Path (Join-Path $Root "data")) { Join-Path $Root "data" } else { Join-Path $Root ".bridge" }
    $config = if (Test-Path (Join-Path $Root "config.local.json")) { Join-Path $Root "config.local.json" } else { Join-Path $Root "config.json" }
    if ((Test-Path $cli) -and (Test-Path $config)) {
        try { & $cli --state $state --config $config stop 2>$null | Out-Null } catch { }
        Start-Sleep -Seconds 2
    }
    for ($i = 0; $i -lt 60; $i++) {
        $running = @(Get-Process AgentsChat -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -and $_.Path.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
        })
        if ($running.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    }
    Fail "the existing tray did not exit; close it from the notification area and run again"
}

function Get-Release([string]$Repository, [string]$Tag) {
    if ([string]::IsNullOrWhiteSpace($Repository) -or $Repository -notmatch "/") {
        Fail "repository is not set; pass -Repo owner/repo or use the published release installer"
    }
    $headers = @{ "User-Agent" = "AgentsChatInstaller" }
    $api = if ($Tag -eq "latest") {
        "https://api.github.com/repos/$Repository/releases/latest"
    } else {
        "https://api.github.com/repos/$Repository/releases/tags/$Tag"
    }
    $release = Invoke-RestMethod -Uri $api -Headers $headers -UseBasicParsing
    $zip = $release.assets | Where-Object { $_.name -like "AgentsChat-Portable-*.zip" } | Select-Object -First 1
    if (-not $zip) { Fail "no portable zip asset found in release $($release.tag_name)" }
    $checksum = $release.assets | Where-Object { $_.name -like "AgentsChat-Portable-*.zip.sha256" } | Select-Object -First 1
    return [pscustomobject]@{
        Tag = $release.tag_name
        ZipUrl = $zip.browser_download_url
        ZipName = $zip.name
        ChecksumUrl = if ($checksum) { $checksum.browser_download_url } else { $null }
    }
}

function Install-Skill([string]$Root) {
    $source = Join-Path $Root "tool-skill\SKILL.md"
    if (-not (Test-Path $source)) { Write-Warning "skill source not found; skipped"; return }
    $roots = @(
        (Join-Path $env:USERPROFILE ".codex\skills"),
        (Join-Path $env:USERPROFILE ".qoder\skills"),
        (Join-Path $env:USERPROFILE ".agents\skills")
    )
    foreach ($skills in $roots) {
        $target = Join-Path $skills $SkillName
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        Copy-Item -Path $source -Destination (Join-Path $target "SKILL.md") -Force
    }
    Write-Host ("    skill installed for Codex/Qoder/DSH")
}

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkingDirectory, [string]$Description) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $Target
    $link.Arguments = $Arguments
    $link.WorkingDirectory = $WorkingDirectory
    $link.Description = $Description
    $link.Save()
}

function Install-Shortcuts([string]$Root) {
    $exe = Join-Path $Root "AgentsChat.exe"
    $menu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Agents Chat"
    New-Item -ItemType Directory -Force -Path $menu | Out-Null
    New-Shortcut (Join-Path $menu "Agents Chat.lnk") $exe "" $Root "Agents Chat tray and collaboration hub"
    $uninstall = Join-Path $Root "uninstall.ps1"
    New-Shortcut (Join-Path $menu "Uninstall Agents Chat.lnk") "powershell.exe" `
        ('-NoProfile -ExecutionPolicy Bypass -File "' + $uninstall + '"') $Root "Uninstall Agents Chat"
    $desktop = [Environment]::GetFolderPath("Desktop")
    New-Shortcut (Join-Path $desktop "Agents Chat.lnk") $exe "" $Root "Agents Chat tray and collaboration hub"
    Write-Host "    Start Menu and Desktop shortcuts created"
}

function Install-Registry([string]$Root, [string]$InstalledVersion) {
    $key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AgentsChat"
    New-Item -Path $key -Force | Out-Null
    $exe = Join-Path $Root "AgentsChat.exe"
    $uninstall = Join-Path $Root "uninstall.ps1"
    Set-ItemProperty -Path $key -Name DisplayName -Value $AppName
    Set-ItemProperty -Path $key -Name DisplayVersion -Value $InstalledVersion
    Set-ItemProperty -Path $key -Name InstallLocation -Value $Root
    Set-ItemProperty -Path $key -Name DisplayIcon -Value $exe
    Set-ItemProperty -Path $key -Name UninstallString -Value ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $uninstall + '"')
    Write-Host "    uninstall entry registered"
}

function Enable-Autostart([string]$Root) {
    $startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Agents Chat.lnk"
    New-Shortcut $startup (Join-Path $Root "AgentsChat.exe") "" $Root "Start Agents Chat at sign-in"
    Write-Host "    autostart enabled"
}

$Destination = [System.IO.Path]::GetFullPath($Destination)

$tempRoot = Join-Path $env:TEMP ("agents-chat-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
$backup = Join-Path $tempRoot "backup"
New-Item -ItemType Directory -Force -Path $backup | Out-Null

try {
    if ($LocalZip) {
        Step "Using local package $LocalZip"
        $zipPath = (Resolve-Path $LocalZip).Path
        $installedVersion = "local"
        $stage = Join-Path $tempRoot "stage"
        Expand-Archive -Path $zipPath -DestinationPath $stage -Force
        $packageRoot = Get-ChildItem $stage -Directory | Select-Object -First 1
        if (-not $packageRoot) { Fail "package has no root folder" }
        $versionFile = Join-Path $packageRoot.FullName "VERSION"
        if (Test-Path $versionFile) { $installedVersion = (Get-Content $versionFile -Raw).Trim() }
    } else {
        Step "Resolving the latest release from $Repo"
        $release = Get-Release $Repo $Version
        $installedVersion = $release.Tag.TrimStart("v")
        $zipPath = Join-Path $tempRoot $release.ZipName
        Step "Downloading $($release.ZipName)"
        Invoke-WebRequest -Uri $release.ZipUrl -OutFile $zipPath -UseBasicParsing
        if ($release.ChecksumUrl) {
            $checksumPath = $zipPath + ".sha256"
            Invoke-WebRequest -Uri $release.ChecksumUrl -OutFile $checksumPath -UseBasicParsing
            $expected = ((Get-Content $checksumPath -Raw).Trim() -split "\s+")[0].ToLowerInvariant()
            $actual = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($expected -ne $actual) { Fail "SHA-256 mismatch; refusing to install" }
            Write-Host "    SHA-256 verified"
        }
        $stage = Join-Path $tempRoot "stage"
        Expand-Archive -Path $zipPath -DestinationPath $stage -Force
        $packageRoot = Get-ChildItem $stage -Directory | Select-Object -First 1
        if (-not $packageRoot) { Fail "package has no root folder" }
    }

    if (-not (Test-Path (Join-Path $packageRoot.FullName "AgentsChat.exe"))) {
        Fail "package does not contain AgentsChat.exe"
    }

    if (Test-Path $Destination) { Stop-AgentsChat $Destination }

    Step "Preserving existing configuration and data"
    foreach ($name in @("config.json", "config.local.json", "data", ".bridge")) {
        $source = Join-Path $Destination $name
        if (Test-Path $source) {
            Copy-Item -Path $source -Destination (Join-Path $backup $name) -Recurse -Force
        }
    }

    Step "Installing to $Destination"
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Copy-Item -Path (Join-Path $packageRoot.FullName "*") -Destination $Destination -Recurse -Force
    New-Item -ItemType Directory -Force -Path (Join-Path $Destination "data") | Out-Null
    foreach ($name in @("config.json", "config.local.json", "data", ".bridge")) {
        $saved = Join-Path $backup $name
        if (Test-Path $saved) {
            $target = Join-Path $Destination $name
            if (Test-Path $target) { Remove-Item $target -Recurse -Force }
            Copy-Item -Path $saved -Destination $target -Recurse -Force
        }
    }

    $skillInstalled = $false
    if (-not $NoSkill) { Install-Skill $Destination; $skillInstalled = $true }
    if (-not $NoShortcuts) { Install-Shortcuts $Destination }
    if (-not $NoRegistry) { Install-Registry $Destination $installedVersion }
    if ($Autostart) { Enable-Autostart $Destination }

    $state = if (Test-Path (Join-Path $Destination "data")) { Join-Path $Destination "data" } else { Join-Path $Destination ".bridge" }
    if (-not $NoStart) {
        Step "Starting the tray host"
        Start-Process -FilePath (Join-Path $Destination "AgentsChat.exe")
        $endpoint = Join-Path $state "endpoint.json"
        $ready = $false
        for ($i = 0; $i -lt 60; $i++) {
            if (Test-Path $endpoint) { $ready = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if ($ready) {
            Write-Host "    hub is running" -ForegroundColor Green
        } else {
            Write-Warning "hub did not report ready yet; open Agents Chat from the notification area"
        }
    }

    Write-Host ""
    Write-Host ("Agents Chat " + $installedVersion + " installed at " + $Destination) -ForegroundColor Green
    Write-Host ""
    Write-Host "Open a NEW chat in your project and say:" -ForegroundColor Yellow
    Write-Host '  Use the agents-chat-desktop skill to connect this project to Agents Chat for multi-role desktop-chat collaboration. Pick the roles yourself, bind each chat with join, and tell me what to say in every chat.' -ForegroundColor White
    Write-Host ""
    if ($skillInstalled) {
        Write-Host "The skill was installed globally for Codex, Qoder and DSH. Existing chats need a new session to see it."
    } else {
        Write-Host "Skill installation was skipped (-NoSkill)."
    }
}
finally {
    try { Remove-Item $tempRoot -Recurse -Force -ErrorAction SilentlyContinue } catch { }
}
