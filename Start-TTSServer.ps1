[CmdletBinding()]
param(
    [switch]$VerifyOnly,
    [switch]$PrepareOnly,
    [switch]$Headless,
    [ValidateRange(1024,65535)][int]$BridgePort = 9300,
    [ValidateRange(1024,65535)][int]$GatewayPort = 8300
)
$ErrorActionPreference = 'Stop'
if ($BridgePort -eq $GatewayPort) { throw 'BridgePort and GatewayPort must be different.' }
$AppRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
if ($AppRoot -notmatch '^[A-Za-z]:\\' -or $AppRoot -match '[\r\n]') {
    throw 'Extract the portable app onto a local Windows drive.'
}
$WslDir = Join-Path $AppRoot 'wsl'
$VhdPath = Join-Path $WslDir 'ext4.vhdx'
$RootfsPath = Join-Path $AppRoot 'runtime\linux-rootfs.tar.gz'
$TransferPath = Join-Path $AppRoot 'runtime\transfer-rootfs.tar'
if (-not $VerifyOnly -and (Test-Path -LiteralPath (Join-Path $AppRoot 'runtime\transfer-incomplete'))) {
    throw 'This portable transfer did not finish. Use the original copy and repeat Copy-TTSServer.ps1 into a new folder.'
}
if (Test-Path -LiteralPath $TransferPath -PathType Leaf) { $RootfsPath = $TransferPath }
$LauncherPath = Join-Path $AppRoot 'runtime\launcher\TTSServer.exe'
$LxssRoot = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'

function Normalize-LocalPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    if ($Path.StartsWith('\\?\')) { $Path = $Path.Substring(4) }
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}
function Get-TTSRegistrations {
    if (Test-Path -LiteralPath $LxssRoot) {
        Get-ChildItem -LiteralPath $LxssRoot | ForEach-Object { Get-ItemProperty -LiteralPath $_.PSPath }
    }
}
$all = @(Get-TTSRegistrations)
$registration = $all | Where-Object { (Normalize-LocalPath ([string]$_.BasePath)) -eq $WslDir } | Select-Object -First 1
if ($registration) {
    if ($registration.DistributionName -notmatch '^(linbox-TTS_Server|TTS-Server-V2-[a-f0-9]{12})$') {
        throw 'This disk is registered as an unrelated distribution. Refusing to use it.'
    }
    $DistroName = [string]$registration.DistributionName
    if (-not (Test-Path -LiteralPath $VhdPath -PathType Leaf)) { throw "Missing backing disk: $VhdPath" }
} else {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $bytes = $hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($AppRoot.ToLowerInvariant())) }
    finally { $hash.Dispose() }
    $suffix = ([BitConverter]::ToString($bytes)).Replace('-','').Substring(0,12).ToLowerInvariant()
    $DistroName = 'TTS-Server-V2-' + $suffix
    if ($all | Where-Object { $_.DistributionName -eq $DistroName }) {
        throw "$DistroName is registered to another location. No existing distribution was changed."
    }
    if ($VerifyOnly) { throw 'This portable copy is not registered. Run Start-TTSServer.cmd once.' }
    if (Test-Path -LiteralPath $VhdPath -PathType Leaf) {
        Write-Host 'Registering this portable disk with WSL2...'
        & wsl.exe --import-in-place $DistroName $VhdPath
    } else {
        if (-not (Test-Path -LiteralPath $RootfsPath -PathType Leaf)) {
            throw 'The bundled Linux runtime is missing. Download the complete portable release; a source checkout alone has no runtime.'
        }
        if ((Test-Path -LiteralPath $WslDir) -and @(Get-ChildItem -LiteralPath $WslDir -Force).Count) {
            throw "Refusing to import over files in $WslDir"
        }
        New-Item -ItemType Directory -Path $WslDir -Force | Out-Null
        Write-Host 'Preparing the bundled Linux runtime (first launch only)...'
        & wsl.exe --import $DistroName $WslDir $RootfsPath --version 2
    }
    if ($LASTEXITCODE -ne 0) { throw 'WSL registration failed. Enable WSL2, restart Windows if requested, and try again.' }
    $registration = Get-TTSRegistrations | Where-Object { $_.DistributionName -eq $DistroName } | Select-Object -First 1
    if (-not $registration -or (Normalize-LocalPath ([string]$registration.BasePath)) -ne $WslDir) {
        throw 'WSL backing-disk location verification failed.'
    }
}
$result = [pscustomobject]@{ AppRoot=$AppRoot; Distro=$DistroName; BasePath=$WslDir; VhdPath=$VhdPath; Ready=$true }
if ($VerifyOnly -or $PrepareOnly) { $result; return }

$LinuxRoot = ((& wsl.exe -d $DistroName --exec wslpath -u $AppRoot) -join '').Trim()
if ($LASTEXITCODE -ne 0 -or -not $LinuxRoot.StartsWith('/')) { throw 'Could not translate the portable folder for WSL.' }
& wsl.exe -d $DistroName --exec /usr/bin/python3 "$LinuxRoot/tools/configure_runtime.py"
if ($LASTEXITCODE -ne 0) { throw 'Portable runtime path configuration failed.' }

if ($Headless) {
    foreach ($port in @($BridgePort, $GatewayPort)) {
        $listener = New-Object Net.Sockets.TcpListener ([Net.IPAddress]::Loopback),$port
        try { $listener.Start() } catch { throw "Port $port is in use. Stop the other session or choose different ports." }
        finally { $listener.Stop() }
    }
    $argsList = @('-d', $DistroName, '--exec', 'env', "BRIDGE_PORT=$BridgePort", "TTS_PORT=$GatewayPort", '/opt/tts_server/venv/bin/python3', ('"' + $LinuxRoot + '/bridge.py"'))
    $process = Start-Process -FilePath wsl.exe -ArgumentList $argsList -WindowStyle Hidden -PassThru
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $ready = $false
    while ($timer.Elapsed.TotalSeconds -lt 120) {
        if ($process.HasExited) { throw 'The backend exited. Check output/run/tts_server_debug.log.' }
        try {
            $health = Invoke-WebRequest -Uri "http://127.0.0.1:$BridgePort/health" -UseBasicParsing -TimeoutSec 2
            if ($health.StatusCode -eq 200 -and (Test-Path -LiteralPath (Join-Path $AppRoot 'output/run/registry/tts_server.json'))) {
                $ready = $true
                break
            }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw 'The backend is still starting or failed. Check output/run logs or use Stop-TTSServer.cmd.' }
    Write-Host "TTS backend ready at http://127.0.0.1:$BridgePort (WSL host process $($process.Id))."
    return
}
if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
    throw 'The bundled desktop launcher is missing. Use the complete portable release or build the launcher first.'
}
# Fixed Version WebView2 requires these app-container read permissions on Windows 10.
if ([Environment]::OSVersion.Version.Build -lt 22000) {
    & icacls.exe (Join-Path $AppRoot 'runtime\webview2') /grant '*S-1-15-2-1:(OI)(CI)(RX)' '*S-1-15-2-2:(OI)(CI)(RX)' /T /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not grant the bundled browser its required read permissions.' }
}
$argsList = @('--root', ('"' + $AppRoot + '"'), '--linux-root', ('"' + $LinuxRoot + '"'), '--distro', $DistroName, '--bridge-port', $BridgePort, '--gateway-port', $GatewayPort)
Start-Process -FilePath $LauncherPath -ArgumentList $argsList -WorkingDirectory $AppRoot
