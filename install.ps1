# Kimi Agent installer for Windows.
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/Trexfly3/kimi-agent/main/install.ps1 | iex"
#
# It installs uv (a small tool that downloads Python and Python apps) if you don't have it, then Kimi Agent with
# its own copy of Python, adds "Kimi Agent" to the Start menu and the desktop and opens it. Nothing needs
# administrator rights. Run it again to update; uninstall from Settings > Apps like any other program.
#
# Environment: KIMI_AGENT_REPO (owner/name), KIMI_AGENT_REF (branch or tag), KIMI_AGENT_SOURCE (a URL to install
# from instead, such as https://... or file:///C:/path/to/kimi-agent.zip), KIMI_AGENT_NO_LAUNCH=1.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'

    $repo = if ($env:KIMI_AGENT_REPO) { $env:KIMI_AGENT_REPO } else { 'Trexfly3/kimi-agent' }
    $ref = if ($env:KIMI_AGENT_REF) { $env:KIMI_AGENT_REF } else { 'main' }
    $source = if ($env:KIMI_AGENT_SOURCE) { $env:KIMI_AGENT_SOURCE } else { "https://github.com/$repo/archive/$ref.zip" }
    $appDir = Join-Path $env:LOCALAPPDATA 'KimiAgent'
    # Trust the Windows certificate store, so networks with an HTTPS-inspecting proxy work too.
    # (UV_NATIVE_TLS is the older name of UV_SYSTEM_CERTS.)
    if (-not $env:UV_SYSTEM_CERTS) { $env:UV_SYSTEM_CERTS = '1' }
    if (-not $env:UV_NATIVE_TLS) { $env:UV_NATIVE_TLS = '1' }
    $uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\KimiAgent'

    function Say([string]$text) {
        Write-Host '==> ' -ForegroundColor DarkYellow -NoNewline
        Write-Host $text
    }

    function Find-Uv {
        $command = Get-Command uv.exe -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
        foreach ($dir in @($env:XDG_BIN_HOME, (Join-Path $HOME '.local\bin'), (Join-Path $HOME '.cargo\bin'))) {
            if ($dir -and (Test-Path -LiteralPath (Join-Path $dir 'uv.exe'))) { return (Join-Path $dir 'uv.exe') }
        }
        return $null
    }

    # Windows PowerShell turns a native program's stderr into errors when it is redirected; ignore those here.
    function Invoke-Quietly([scriptblock]$block) {
        $ErrorActionPreference = 'Continue'
        try { & $block *> $null } catch { Write-Verbose "ignored: $_" }
    }

    $uv = Find-Uv
    if (-not $uv) {
        Say 'Installing uv (it downloads Python and Kimi Agent for you)...'
        # The official installer, from astral.sh or (where that is blocked) from uv's GitHub releases.
        powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; try { irm https://astral.sh/uv/install.ps1 | iex } catch { irm https://github.com/astral-sh/uv/releases/latest/download/uv-installer.ps1 | iex }"
        $uv = Find-Uv
        if (-not $uv) {
            throw 'uv was installed but cannot be found. Close this window, open a new PowerShell window and run the installer again.'
        }
    }
    $bin = (& $uv tool dir --bin | Out-String).Trim()
    $cli = Join-Path $bin 'kimi-agent.exe'
    $gui = Join-Path $bin 'kimi-agent-gui.exe'

    if (Test-Path -LiteralPath $cli) {
        # Updating: close the running app first, because Windows cannot replace files that are in use.
        Invoke-Quietly { & $cli --quit-gui }
    }
    Say 'Installing Kimi Agent (this can take a minute)...'
    & $uv tool install --force --python 3.12 --reinstall-package kimi-agent "kimi-agent[computer] @ $source"
    if ($LASTEXITCODE -ne 0) {
        Say 'Trying again without the screen-control add-ons...'
        & $uv tool install --force --python 3.12 --reinstall-package kimi-agent "kimi-agent @ $source"
        if ($LASTEXITCODE -ne 0) { throw 'Installing Kimi Agent failed (see the messages above).' }
    }
    Invoke-Quietly { & $uv tool update-shell }  # so "kimi-agent" works in new terminals

    & $cli --install-shortcuts
    if ($LASTEXITCODE -ne 0) { Write-Warning "Could not create the shortcuts; start Kimi Agent with: $gui" }

    # An entry in Settings > Apps, so it can be removed like any other program.
    New-Item -ItemType Directory -Force -Path $appDir | Out-Null
    $uninstaller = Join-Path $appDir 'uninstall.ps1'
    Set-Content -LiteralPath $uninstaller -Encoding UTF8 -Value @'
# Removes Kimi Agent. Written by its installer; run by Settings > Apps > Kimi Agent > Uninstall.
$ErrorActionPreference = 'Continue'
$uv = Get-Command uv.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if (-not $uv) {
    foreach ($candidate in @((Join-Path $HOME '.local\bin\uv.exe'), (Join-Path $HOME '.cargo\bin\uv.exe'))) {
        if (Test-Path -LiteralPath $candidate) { $uv = $candidate; break }
    }
}
if ($uv) {
    $cli = Join-Path (& $uv tool dir --bin | Out-String).Trim() 'kimi-agent.exe'
    if (Test-Path -LiteralPath $cli) {
        & $cli --quit-gui | Out-Null
        & $cli --remove-shortcuts | Out-Null
    }
    & $uv tool uninstall kimi-agent | Out-Null
}
Remove-Item -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\KimiAgent' -Recurse -Force -ErrorAction SilentlyContinue
Write-Host 'Kimi Agent was removed. Your settings and conversations are still in' (Join-Path $HOME '.kimi-agent')
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
'@
    $version = ((& $cli --version | Out-String).Trim() -replace '^kimi-agent\s+', '')
    $toolDir = (& $uv tool dir | Out-String).Trim()
    $icon = Join-Path $toolDir 'kimi-agent\Lib\site-packages\kimi_agent\gui\static\icon.ico'
    New-Item -Path $uninstallKey -Force | Out-Null
    $strings = @{
        DisplayName     = 'Kimi Agent'
        DisplayVersion  = $version
        Publisher       = 'Kimi Agent'
        DisplayIcon     = $icon
        InstallLocation = $appDir
        URLInfoAbout    = "https://github.com/$repo"
        UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstaller`""
    }
    foreach ($name in $strings.Keys) {
        New-ItemProperty -Path $uninstallKey -Name $name -Value $strings[$name] -PropertyType String -Force | Out-Null
    }
    foreach ($name in @('NoModify', 'NoRepair')) {
        New-ItemProperty -Path $uninstallKey -Name $name -Value 1 -PropertyType DWord -Force | Out-Null
    }

    Say "Kimi Agent $version is installed. Open it any time from the Start menu or the shortcut on your desktop."
    Say 'To update, run this installer again. To uninstall, use Settings > Apps.'
    if ($env:KIMI_AGENT_NO_LAUNCH -ne '1') {
        Say 'Opening Kimi Agent...'
        Start-Process -FilePath $gui
    }
}
