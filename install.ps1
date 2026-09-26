# ZergSwarm installer for Windows. Run this in PowerShell:
#   irm https://raw.githubusercontent.com/Lunarwerx/ZergSwarm/main/install.ps1 | iex
# It installs the newest release with uv (or pipx when uv is absent but Python 3.11+ is present; with
# neither, it installs uv, which brings its own Python), then runs `zswarm setup` to connect ZergSwarm
# to every AI assistant it finds (Claude Code, Claude Desktop, Codex) and open the web console.
# ZERGSWARM_SOURCE installs that wheel path or URL instead (used for testing); ZERGSWARM_NO_SETUP skips `zswarm setup`.
#
# One script block, left with `return`: under `irm | iex` an `exit` would close the window before the message could
# be read. Native commands are judged by $LASTEXITCODE, never by $ErrorActionPreference = "Stop", which in Windows
# PowerShell 5.1 throws on anything a native command writes to stderr.
& {
    $Repo = "Lunarwerx/ZergSwarm"

    # What to install: ZERGSWARM_SOURCE wins (testing); else the newest release's wheel, or the main branch with no release yet.
    if ($env:ZERGSWARM_SOURCE) {
        $source = $env:ZERGSWARM_SOURCE
        Write-Host "Installing from ZERGSWARM_SOURCE: $source"
    } else {
        $source = "https://github.com/$Repo/archive/refs/heads/main.zip"
        try {
            $rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "zergswarm-installer" } -ErrorAction Stop
            $whl = $rel.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1
            if ($whl) { $source = $whl.browser_download_url; Write-Host "Installing ZergSwarm $($rel.tag_name)" }
        } catch { Write-Host "No release found; installing from the main branch" }
    }

    # The installer: uv when it is on PATH, else pipx on a Python 3.11+, else a fresh uv install.
    $py = $null
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        foreach ($cmd in @(@("py", "-3"), @("python"), @("python3"))) {
            if (-not (Get-Command $cmd[0] -ErrorAction SilentlyContinue)) { continue }
            $probe = @($cmd | Select-Object -Skip 1) + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
            $ver = & $cmd[0] @probe 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -and [version]$ver -ge [version]"3.11") { $py = $cmd; break }
        }
        if (-not $py) {
            Write-Host "No Python 3.11 or newer here, so ZergSwarm installs with uv, which brings its own Python (https://docs.astral.sh/uv/)."
            powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" *> $null
            $env:Path = "$env:USERPROFILE\.local\bin;" + $env:Path
            if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
                Write-Host "uv could not be installed automatically. Install Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." -ForegroundColor Yellow
                return
            }
        }
    }

    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv tool install --force --python ">=3.11" $source
        if ($LASTEXITCODE -ne 0) { Write-Host "uv could not install ZergSwarm from $source" -ForegroundColor Yellow; return }
        uv tool update-shell *> $null
        $bin = (uv tool dir --bin).Trim()
    } else {
        $pyExe, $pyArgs = $py[0], @($py | Select-Object -Skip 1)
        & $pyExe @pyArgs -m pipx --version *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Installing pipx (it keeps ZergSwarm in its own environment)"
            & $pyExe @pyArgs -m pip install --user --upgrade pipx
            if ($LASTEXITCODE -ne 0) { Write-Host "pip could not install pipx; see https://pipx.pypa.io for other ways, then run this again." -ForegroundColor Yellow; return }
        }
        & $pyExe @pyArgs -m pipx ensurepath *> $null
        & $pyExe @pyArgs -m pipx install --force $source
        if ($LASTEXITCODE -ne 0) { Write-Host "pipx could not install ZergSwarm from $source" -ForegroundColor Yellow; return }
        $bin = (& $pyExe @pyArgs -m pipx environment --value PIPX_BIN_DIR).Trim()
    }

    $env:Path = "$bin;" + $env:Path

    Write-Host ""
    Write-Host "ZergSwarm is installed." -ForegroundColor Green
    if ($env:ZERGSWARM_NO_SETUP) {
        Write-Host "  zswarm setup     connect your assistants and open the console"
        Write-Host "  a new terminal will see the zswarm command too"
        return
    }
    # setup says what is left (a key, then a first ask), so nothing is repeated after it.
    $zswarm = Get-Command zswarm -ErrorAction SilentlyContinue
    if ($zswarm) { & $zswarm.Source setup } else { & "$bin\zswarm.exe" setup }
    if ($LASTEXITCODE -ne 0) { Write-Host "Setup did not finish. Run it again any time: zswarm setup" -ForegroundColor Yellow }
}