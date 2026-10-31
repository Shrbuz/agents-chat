#requires -Version 5.1
<#
.SYNOPSIS
  Uninstalls Agents Chat.

.DESCRIPTION
  Stops the tray host, removes shortcuts and the uninstall registration, and
  deletes the application files. Configuration and data are kept unless
  -RemoveData is passed.

  Every deletion is guarded: the destination must be an explicit, non-root
  folder, it must contain this product's files, and a copy registered elsewhere
  is left alone unless it is the one being uninstalled.

.EXAMPLE
  .\uninstall.ps1 -DryRun
  Shows exactly what would be deleted without touching anything.

.EXAMPLE
  .\uninstall.ps1 -RemoveData
  Removes the application and its data.
#>
[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA "Programs\AgentsChat"),
    [switch]$RemoveData,
    [switch]$KeepSkill,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$AppName = "Agents Chat"
$SkillName = "agents-chat-desktop"
$KeepNames = @("config.json", "config.local.json", "data", ".bridge")

function Step([string]$Message) { Write-Host ("==> " + $Message) -ForegroundColor Cyan }
function Invoke-Guard([string]$Message, [scriptblock]$Action) {
    if ($DryRun) { Write-Host ("DRY RUN: " + $Message); return }
    & $Action
}

# Refuse anything that could resolve to the user profile, a system folder, a
# drive root, or the current directory. Path::GetFullPath("") returns the CWD in
# Windows PowerShell, so an empty -Destination used to mean "delete where I am".
function Assert-SafeDestination([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "uninstall failed: destination is empty. Pass the folder Agents Chat was installed into."
    }
    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not [System.IO.Path]::IsPathRooted($full)) {
        throw ("uninstall failed: destination must be an absolute path: " + $Path)
    }
    $full = $full.TrimEnd('\', '/')
    if ($full -match '^[A-Za-z]:$' -or $full -match '^\\\\[^\\]+\\[^\\]+$') {
        throw ("uninstall failed: refusing to use a drive or share root as the destination: " + $full)
    }
    $segments = @($full -split '[\\/]+' | Where-Object { $_ -ne '' })
    if ($segments.Count -lt 3) {
        throw ("uninstall failed: destination is too close to a root to be safe: " + $full)
    }
    $guarded = @()
    foreach ($name in @("USERPROFILE", "LOCALAPPDATA", "APPDATA", "ProgramData", "ProgramFiles", "WINDIR", "SystemRoot", "TEMP", "TMP", "PUBLIC")) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            $guarded += ([System.IO.Path]::GetFullPath($value).TrimEnd('\', '/'))
        }
    }
    foreach ($folder in @("Desktop", "MyDocuments", "MyPictures", "MyMusic", "MyVideos", "Favorites", "UserProfile")) {
        try {
            $value = [Environment]::GetFolderPath($folder)
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                $guarded += ([System.IO.Path]::GetFullPath($value).TrimEnd('\', '/'))
            }
        } catch { }
    }
    foreach ($protected in @($guarded | Sort-Object -Unique)) {
        if ($full -ieq $protected) {
            throw ("uninstall failed: refusing to use a protected folder as the destination: " + $full)
        }
        if ($protected.StartsWith($full + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw ("uninstall failed: refusing to use " + $full + " as the destination: it contains the protected folder " + $protected)
        }
    }
    return $full
}

$Destination = Assert-SafeDestination $Destination

# Read the registration before it may be removed. A mismatch is only a warning:
# portable builds and -NoRegistry installs are legitimately unregistered, and
# another copy on the same machine may own the shortcuts and the HKCU entry.
$owner = $null
try {
    $owner = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AgentsChat" -Name InstallLocation -ErrorAction Stop).InstallLocation
} catch { }
$ownsRegistration = $true
if (-not [string]::IsNullOrWhiteSpace($owner)) {
    $owner = ([System.IO.Path]::GetFullPath($owner)).TrimEnd('\', '/')
    if ($owner -ine $Destination) {
        $ownsRegistration = $false
    }
}

$installExists = $false
if ($null -ne $Destination) { $installExists = Test-Path $Destination }

# The folder itself must prove it is this product before anything is cleared,
# because a mistyped destination must never wipe an unrelated directory. A
# leftover folder holding only configuration/data is accepted when -RemoveData
# is passed, which covers re-running the uninstall.
if ($installExists) {
    $hasApp = (Test-Path (Join-Path $Destination "AgentsChat.exe")) -and (Test-Path (Join-Path $Destination "bridge.py"))
    $leftoverOnly = $true
    foreach ($entry in @(Get-ChildItem $Destination -Force)) {
        if ($entry.Name -notin $KeepNames) { $leftoverOnly = $false; break }
    }
    if (-not $hasApp -and -not ($RemoveData -and $leftoverOnly)) {
        Write-Warning ("$Destination does not look like an Agents Chat install (no AgentsChat.exe/bridge.py).")
        Write-Warning "Refusing to delete files there."
        $Destination = $null
        $installExists = $false
    }
}

Step "Stopping the tray host and hub"
try {
    $quit = [System.Threading.EventWaitHandle]::OpenExisting("Local\AgentsChatQuit05")
    [void]$quit.Set()
    $quit.Dispose()
    Start-Sleep -Seconds 2
} catch { }
if ($installExists) {
    $cli = Join-Path $Destination "AgentsChat.Cli.exe"
    $state = if (Test-Path (Join-Path $Destination "data")) { Join-Path $Destination "data" } else { Join-Path $Destination ".bridge" }
    $config = if (Test-Path (Join-Path $Destination "config.local.json")) { Join-Path $Destination "config.local.json" } else { Join-Path $Destination "config.json" }
    if ((Test-Path $cli) -and (Test-Path $config)) {
        try { & $cli --state $state --config $config stop 2>$null | Out-Null } catch { }
        Start-Sleep -Seconds 2
    }
}

if (-not $ownsRegistration) {
    Step "Removing shortcuts and registration"
    Write-Host ("    skipped: another copy is registered at " + $owner) -ForegroundColor Yellow
    Write-Host "    run its own uninstall.ps1 to remove the shortcuts and registration" -ForegroundColor Yellow
} else {
    Step "Removing shortcuts and registration"
    $menu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Agents Chat"
    $desktop = [Environment]::GetFolderPath("Desktop")
    $startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Agents Chat.lnk"
    $startupVbs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Agents Chat.vbs"
    Invoke-Guard ("remove " + $menu) { Remove-Item $menu -Recurse -Force -ErrorAction SilentlyContinue }
    Invoke-Guard ("remove " + (Join-Path $desktop "Agents Chat.lnk")) { Remove-Item (Join-Path $desktop "Agents Chat.lnk") -Force -ErrorAction SilentlyContinue }
    if (-not [string]::IsNullOrWhiteSpace($env:APPDATA)) {
        Invoke-Guard ("remove " + $startup) { Remove-Item $startup -Force -ErrorAction SilentlyContinue }
        Invoke-Guard ("remove " + $startupVbs) { Remove-Item $startupVbs -Force -ErrorAction SilentlyContinue }
    }
    Invoke-Guard "remove the HKCU uninstall registration" {
        Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AgentsChat" -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if (-not $KeepSkill) {
    Step "Removing the user-level skill"
    if (-not $ownsRegistration) {
        Write-Host "    skipped: the registered copy still needs the shared skill" -ForegroundColor Yellow
    } elseif ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        Write-Warning "USERPROFILE is not set; skipped skill removal"
    } else {
        foreach ($skills in @(
            (Join-Path $env:USERPROFILE ".codex\skills"),
            (Join-Path $env:USERPROFILE ".qoder\skills"),
            (Join-Path $env:USERPROFILE ".agents\skills"))) {
            # Only the one skill folder this product created, never a parent.
            $target = Join-Path $skills $SkillName
            Invoke-Guard ("remove " + $target) { Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue }
        }
    }
}

Step "Removing application files"
if (-not $installExists) {
    Write-Host "    no installation found at the destination; nothing to delete" -ForegroundColor Yellow
} elseif ($RemoveData) {
    Invoke-Guard ("delete " + $Destination) { Remove-Item $Destination -Recurse -Force -ErrorAction SilentlyContinue }
} else {
    $items = @(Get-ChildItem $Destination -Force | Where-Object { $_.Name -notin $KeepNames })
    foreach ($item in $items) {
        Invoke-Guard ("delete " + $item.FullName) { Remove-Item $item.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Write-Host "    configuration and data kept in $Destination" -ForegroundColor Yellow
}

Write-Host ""
if ($DryRun) {
    Write-Host "Dry run only. Nothing was deleted." -ForegroundColor Yellow
} else {
    Write-Host "Agents Chat was removed." -ForegroundColor Green
    if (-not $RemoveData) { Write-Host "Data was kept. Run with -RemoveData to delete it as well." }
}
