[CmdletBinding()]
param([switch]$AllowLockedDisk)
$ErrorActionPreference = 'Stop'
$state = & (Join-Path $PSScriptRoot 'Start-TTSServer.ps1') -VerifyOnly
$registryPath = Join-Path $PSScriptRoot 'output\run\registry\tts_server.json'
if (Test-Path -LiteralPath $registryPath) {
    try {
        $registry = Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json
        $url = [Uri]$registry.endpoints.api
        if ($url.Host -ne '127.0.0.1' -or $url.Scheme -ne 'http' -or $registry.extra.wsl_distro -ne $state.Distro) {
            throw 'Discovery does not identify this portable copy.'
        }
        $linuxRoot = ((& wsl.exe -d $state.Distro --exec wslpath -u $PSScriptRoot) -join '').Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the app folder.' }
        $config = Invoke-RestMethod -Uri ($url.AbsoluteUri.TrimEnd('/') + '/api/config') -Headers @{'X-TTS-API-Token'=$registry.auth.token} -TimeoutSec 5
        if ($config.output_dir -cne ($linuxRoot + '/output')) { throw 'The API belongs to another folder.' }
        Invoke-RestMethod -Uri ($url.AbsoluteUri.TrimEnd('/') + '/api/shutdown') -Method Post -Headers @{'X-TTS-API-Token'=$registry.auth.token} -TimeoutSec 40 | Out-Null
        Start-Sleep -Seconds 4
    } catch { Write-Warning 'No verified live API for this folder; stopping only its own WSL distro.' }
}
& wsl.exe --terminate $state.Distro
if ($LASTEXITCODE -ne 0) { throw 'Could not stop the portable WSL disk.' }
try {
    $disk = [IO.File]::Open($state.VhdPath, 'Open', 'Read', 'None')
    $disk.Dispose()
    Write-Host 'TTS Server is stopped and its disk is released. You can move or copy the complete folder.'
} catch {
    Write-Warning 'TTS Server is stopped, but Windows still holds its disk. Do not move or copy the VHD yet. Use Copy-TTSServer.ps1 -Destination <new-folder> to export this copy without stopping other WSL apps. See PORTABILITY.md.'
    if (-not $AllowLockedDisk) { throw 'The app is stopped, but its disk is not released for a manual move.' }
}
