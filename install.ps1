# ZergSwarm installer for Windows. Run this in PowerShell:
#   irm https://raw.githubusercontent.com/Lunarwerx/ZergSwarm/main/install.ps1 | iex
# It installs the latest release with pipx (which puts the `zswarm` command on your PATH), then tells you the two
# commands that connect it to Claude Code and open its console. It changes nothing else.
#
# One script block, left with `return`: under `irm | iex` an `exit` would close the window before the message could
# be read. Native commands are judged by $LASTEXITCODE, never by $ErrorActionPreference = "Stop", which in Windows
# PowerShell 5.1 throws on anything a native command writes to stderr.
& {
    $Repo = "Lunarwerx/ZergSwarm"

    $py = $null
    foreach ($cmd in @(@("py", "-3"), @("python"), @("python3"))) {
        if (-not (Get-Command $cmd[0] -ErrorAction SilentlyContinue)) { continue }
        $probe = @($cmd | Select-Object -Skip 1) + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
        $ver = & $cmd[0] @probe 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver -and [version]$ver -ge [version]"3.11") { $py = $cmd; break }
    }
    if (-not $py) {
        Write-Host "ZergSwarm needs Python 3.11 or newer. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." -ForegroundColor Yellow
        return
    }
    $pyExe, $pyArgs = $py[0], @($py | Select-Object -Skip 1)

    # The newest release's wheel; with no release yet, the main branch.
    $source = "https://github.com/$Repo/archive/refs/heads/main.zip"
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "zergswarm-installer" } -ErrorAction Stop
        $whl = $rel.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1
        if ($whl) { $source = $whl.browser_download_url; Write-Host "Installing ZergSwarm $($rel.tag_name)" }
    } catch { Write-Host "No release found; installing from the main branch" }

    & $pyExe @pyArgs -m pipx --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing pipx (it keeps ZergSwarm in its own environment)"
        & $pyExe @pyArgs -m pip install --user --upgrade pipx
        if ($LASTEXITCODE -ne 0) { Write-Host "pip could not install pipx; see https://pipx.pypa.io for other ways, then run this again." -ForegroundColor Yellow; return }
    }
    & $pyExe @pyArgs -m pipx ensurepath *> $null
    & $pyExe @pyArgs -m pipx install --force $source
    if ($LASTEXITCODE -ne 0) { Write-Host "pipx could not install ZergSwarm from $source" -ForegroundColor Yellow; return }

    Write-Host ""
    Write-Host "ZergSwarm is installed. Open a NEW terminal (so it sees the zswarm command), then:" -ForegroundColor Green
    Write-Host "  zswarm install     connect it to Claude Code (add --client all for Claude Desktop and Codex too)"
    Write-Host "  zswarm ui          open the console in your browser and add an API key"
}
