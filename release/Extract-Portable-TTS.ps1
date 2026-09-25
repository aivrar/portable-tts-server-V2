[CmdletBinding()]
param(
    [string]$Destination = $PSScriptRoot,
    [switch]$Download,
    [string]$Version = '2.0.0'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$base = "https://github.com/aivrar/portable-tts-server-V2/releases/download/v$Version"
$manifestPath = Join-Path $PSScriptRoot 'portable-manifest.json'
if ($Download -and -not (Test-Path -LiteralPath $manifestPath)) {
    Invoke-WebRequest "$base/portable-manifest.json" -OutFile $manifestPath -UseBasicParsing
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.version -ne $Version -or $manifest.folder -ne 'Portable-TTS-Server-V2' -or
    $manifest.archive -notmatch '^Portable-TTS-Server-V2-[0-9.]+-win-x64\.zip$') { throw 'Unexpected release manifest.' }
$Destination = [IO.Path]::GetFullPath($Destination)
$app = Join-Path $Destination $manifest.folder
if (Test-Path -LiteralPath $app) { throw "Destination already exists: $app. Extract into a new empty location." }
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$archivePath = Join-Path $PSScriptRoot $manifest.archive
$partial = $archivePath + '.partial'
$output = [IO.File]::Create($partial)
try {
    foreach ($part in $manifest.parts) {
        if ($part.name -notmatch '^Portable-TTS-Server-V2-[0-9.]+-win-x64\.zip\.part[0-9]{3}$') { throw 'Invalid part name.' }
        $path = Join-Path $PSScriptRoot $part.name
        if ($Download -and -not (Test-Path -LiteralPath $path)) {
            Write-Host "Downloading $($part.name)..."
            Invoke-WebRequest "$base/$($part.name)" -OutFile ($path + '.download') -UseBasicParsing
            Move-Item -LiteralPath ($path + '.download') -Destination $path
        }
        Write-Host "Checking $($part.name)..."
        if ((Get-Item -LiteralPath $path).Length -ne $part.bytes -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $part.sha256) { throw "Checksum failed: $($part.name)" }
        $inputStream = [IO.File]::OpenRead($path)
        try { $inputStream.CopyTo($output) } finally { $inputStream.Dispose() }
    }
} finally { $output.Dispose() }
if ((Get-Item -LiteralPath $partial).Length -ne $manifest.archive_bytes -or
    (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $manifest.archive_sha256) { throw 'Joined archive checksum failed.' }
Move-Item -LiteralPath $partial -Destination $archivePath -Force
Write-Host 'Extracting the complete portable app...'
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $Destination)
Write-Host "Ready: $app\Start-TTSServer.cmd"
Write-Host 'Windows must have WSL2 enabled. Kokoro and the app runtimes are included.'
