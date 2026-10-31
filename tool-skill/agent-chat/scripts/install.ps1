param(
    [string]$CodexSkillsRoot = (Join-Path $env:USERPROFILE '.agents\skills'),
    [string]$QoderSkillsRoot = (Join-Path $env:USERPROFILE '.qoder\skills')
)
$ErrorActionPreference = 'Stop'
$agentSkillSource = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath (Join-Path $agentSkillSource 'SKILL.md') -PathType Leaf)) {
    throw 'Skill source is missing SKILL.md.'
}
foreach ($agentSkillRoot in @($CodexSkillsRoot, $QoderSkillsRoot)) {
    $agentSkillDestination = Join-Path $agentSkillRoot 'agent-chat'
    if (Test-Path -LiteralPath $agentSkillDestination) {
        $agentExisting = Get-Item -LiteralPath $agentSkillDestination -Force
        if ($agentExisting.LinkType -eq 'Junction' -and
            [System.IO.Path]::GetFullPath([string]$agentExisting.Target).TrimEnd('\') -eq $agentSkillSource.TrimEnd('\')) {
            Write-Output "Already installed: $agentSkillDestination"
            continue
        }
        throw "Existing skill entry will not be overwritten: $agentSkillDestination"
    }
    New-Item -ItemType Directory -Path $agentSkillRoot -Force | Out-Null
    New-Item -ItemType Junction -Path $agentSkillDestination -Target $agentSkillSource | Out-Null
    Write-Output "Installed: $agentSkillDestination -> $agentSkillSource"
}
