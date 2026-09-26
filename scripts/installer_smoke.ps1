# Run public/install.ps1 the way `irm | iex` does, inside a throwaway home, and report what it did.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/installer_smoke.ps1 -Wheel <zergswarm-*.whl> [-Uv <folder with uv.exe>]
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/installer_smoke.ps1 -Live [-Uv <folder with uv.exe>]
#
# -Wheel installs that file through this tree's installer; -Live runs the published one-liner exactly as a user does
# (the script from GitHub's main branch, the newest release's wheel). Without -Uv it takes the pipx path (pip installs pipx into the throwaway profile); with -Uv, the uv path. Home,
# AppData, pipx and uv folders all live under a temp folder, a stand-in browser records the URL it is asked to open,
# and the real ~/.claude.json is never written. One thing a sandbox cannot redirect: `pipx ensurepath` and
# `uv tool update-shell` write the USER PATH in the registry (HKCU\Environment), so this script restores that value
# exactly (same text, same ExpandString kind) when it ends (2026-09-26: a first run left three temp folders there).
param([string]$Wheel = "", [switch]$Live, [string]$Uv = "")
if (-not $Wheel -and -not $Live) { "Give -Wheel <file> or -Live"; return }
$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$T = Join-Path ([IO.Path]::GetTempPath()) ("zergswarm-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$H = Join-Path $T "home"
foreach ($d in "AppData\Roaming", "AppData\Local") { New-Item -ItemType Directory -Force (Join-Path $H $d) | Out-Null }
$reg = Get-Item "HKCU:\Environment"
$userPath = $reg.GetValue("Path", $null, "DoNotExpandEnvironmentNames")  # $null: this user has no PATH of their own
$userPathKind = if ($null -ne $userPath) { $reg.GetValueKind("Path") } else { $null }
# The real client entry, compared by content: Claude Code rewrites ~/.claude.json all day, so its time says nothing.
$realClaude = Join-Path $env:USERPROFILE ".claude.json"
# Read with Python: Windows PowerShell's ConvertFrom-Json refuses a file whose keys differ only by case, which a real
# ~/.claude.json has (project folders spelled D:/ and d:/).
# A missing or unreadable file says so, so the before/after comparison never matches two empty reads.
function Entry {
    if (-not (Test-Path $realClaude)) { return "absent" }
    $out = python -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding='utf-8')).get('mcpServers', {}).get('zswarm'), sort_keys=True))" $realClaude 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out) { return "unreadable " + [guid]::NewGuid() } else { return $out }
}
$realEntry = Entry
# Every process variable as it was: the finally puts them all back, so running this inside an open PowerShell window
# (`& scripts/installer_smoke.ps1`) leaves that window's environment as it found it.
$envBefore = @{}
Get-ChildItem Env: | ForEach-Object { $envBefore[$_.Name] = $_.Value }
$installer = @("public\install.ps1", "install.ps1") | ForEach-Object { Join-Path $Repo $_ } | Where-Object { Test-Path $_ } | Select-Object -First 1
try {
    $env:USERPROFILE = $H; $env:HOME = $H
    $env:APPDATA = Join-Path $H "AppData\Roaming"; $env:LOCALAPPDATA = Join-Path $H "AppData\Local"
    Remove-Item Env:ZSWARM_HOME, Env:CLAUDE_CONFIG_DIR, Env:CODEX_HOME -ErrorAction SilentlyContinue
    Get-ChildItem Env: | Where-Object { $_.Name -match "_API_KEYS?$|^HF_TOKENS?$" } | ForEach-Object { Remove-Item "Env:$($_.Name)" }
    if ($Wheel) { $env:ZERGSWARM_SOURCE = (Resolve-Path $Wheel).Path } else { Remove-Item Env:ZERGSWARM_SOURCE -ErrorAction SilentlyContinue }
    $opened = Join-Path $T "opened.txt"
    $fake = Join-Path $T "browser.py"
    Set-Content $fake "import sys`nopen(sys.argv[1], 'w').write(sys.argv[2].split('?')[0])"
    $env:BROWSER = "python " + ($fake -replace "\\", "/") + " " + ($opened -replace "\\", "/") + " %s"
    if ($Uv) {
        $env:Path = "$Uv;" + $env:Path
        $env:UV_CACHE_DIR = Join-Path $T "uv-cache"; $env:UV_TOOL_DIR = Join-Path $T "uv-tools"
        $env:UV_TOOL_BIN_DIR = Join-Path $T "uv-bin"; $env:UV_PYTHON_INSTALL_DIR = Join-Path $T "uv-python"
    }
    if ($Live) { Invoke-RestMethod "https://raw.githubusercontent.com/Lunarwerx/ZergSwarm/main/install.ps1" | Invoke-Expression }
    else { Get-Content $installer -Raw | Invoke-Expression }
    "---- results"
    "zswarm: " + (Get-Command zswarm -ErrorAction SilentlyContinue).Source
    $cj = Join-Path $H ".claude.json"
    "claude.json: " + $(if (Test-Path $cj) { (Get-Content $cj -Raw | ConvertFrom-Json).mcpServers.zswarm | ConvertTo-Json -Compress } else { "not written" })
    "codex: " + $(if (Test-Path (Join-Path $H ".codex\config.toml")) { "registered" } else { "not registered" })
    "browser opened: " + $(if (Test-Path $opened) { Get-Content $opened -Raw } else { "nothing" })
} finally {
    # Put back exactly what was there: the same value and kind, or no user PATH at all (an empty one would hide the machine's).
    if ($null -ne $userPath) { Set-ItemProperty -Path "HKCU:\Environment" -Name Path -Value $userPath -Type $userPathKind }
    else { Remove-ItemProperty -Path "HKCU:\Environment" -Name Path -ErrorAction SilentlyContinue }
    Get-ChildItem Env: | Where-Object { -not $envBefore.ContainsKey($_.Name) } | ForEach-Object { [Environment]::SetEnvironmentVariable($_.Name, $null, "Process") }
    foreach ($k in $envBefore.Keys) { [Environment]::SetEnvironmentVariable($k, $envBefore[$k], "Process") }
    "real ~/.claude.json zswarm entry unchanged: " + ((Entry) -eq $realEntry) + "; user PATH restored; sandbox left at $T"
}
