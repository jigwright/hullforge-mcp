<#
.SYNOPSIS
  Start Claude Code for HullForge work with everything it does not need left out.

.DESCRIPTION
  Measured on a two-turn session (Claude Code 2.1, Nov 2026):
    default launch                                   72,762 input tokens
    --tools ""          (no shell, file or web tools) 41,724
    + --strict-mcp-config (only HullForge's server)   14,234
    + HullForge's own system prompt                   10,062
  Every turn re-reads all of it, so this is paid on every model round-trip.

  What is left out: Claude Code's built-in tools (Bash, Edit, Write, Read, web,
  subagents), every other MCP server and connector, and Claude Code's general
  coding system prompt, replaced by a short HullForge one. Slash commands stay,
  so /hullforge:design and the other stage prompts still work.

  Uses your Claude subscription login; no API key.

.PARAMETER Project
  The Unreal project folder. Defaults to the current folder.
.PARAMETER Model
  Optional model, e.g. sonnet or opus.
.PARAMETER Full
  Start Claude Code normally (all tools and connectors), for comparison.
#>
param(
    [string]$Project = (Get-Location).Path,
    [string]$Model = '',
    [switch]$Full,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$Rest
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$cfgDir = Join-Path $env:LOCALAPPDATA 'HullForge'
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null

$mcp = Join-Path $cfgDir 'claude-mcp.json'
$config = @{ mcpServers = @{ hullforge = @{
    type = 'stdio'; command = 'uv'
    args = @('run', '--directory', $repo.Replace('\', '/'), 'hullforge-mcp')
} } } | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText($mcp, $config, (New-Object Text.UTF8Encoding $false))

$prompt = Join-Path $here 'hf_system.md'
$cmd = @()
if (-not $Full) {
    $cmd += @('--tools', '""', '--strict-mcp-config', '--mcp-config', $mcp, '--system-prompt-file', $prompt)
}
if ($Model) { $cmd += @('--model', $Model) }
if ($Rest) { $cmd += $Rest }

Set-Location $Project
& claude @cmd
