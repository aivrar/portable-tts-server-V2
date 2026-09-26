[CmdletBinding()]
param(
    [string]$Destination = $PSScriptRoot,
    [switch]$Download,
    [ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version = '2.0.1'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
$OutputEncoding = [Console]::OutputEncoding
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$base = "https://github.com/aivrar/portable-tts-server-V2/releases/download/v$Version"
$manifestPath = Join-Path $PSScriptRoot 'portable-manifest.json'
if ($Download -and -not (Test-Path -LiteralPath $manifestPath)) {
    Write-Host "Getting release $Version..."
    Invoke-WebRequest "$base/portable-manifest.json" -OutFile ($manifestPath + '.download') -UseBasicParsing
    Move-Item -LiteralPath ($manifestPath + '.download') -Destination $manifestPath -Force
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.version -ne $Version -or $manifest.folder -ne 'Portable-TTS-Server-V2' -or
    $manifest.archive -notmatch '^Portable-TTS-Server-V2-[0-9.]+-win-x64\.zip$') { throw 'Unexpected release manifest.' }
if (-not $manifest.parts -or $manifest.archive_sha256 -notmatch '^[a-f0-9]{64}$' -or $manifest.archive_bytes -le 0) { throw 'Invalid release checksums.' }
$Destination = [IO.Path]::GetFullPath($Destination)
if ($Destination -notmatch '^[A-Za-z]:\\' -or $Destination -match '[\r\n]') { throw 'Choose a local Windows drive.' }
$app = Join-Path $Destination $manifest.folder
if (Test-Path -LiteralPath $app) { throw "Destination already exists: $app. Extract into a new empty location." }
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$archivePath = Join-Path $PSScriptRoot $manifest.archive
$partial = $archivePath + '.partial'
$output = [IO.File]::Create($partial)
try {
    foreach ($part in $manifest.parts) {
        if ($part.name -notmatch '^Portable-TTS-Server-V2-[0-9.]+-win-x64\.zip\.part[0-9]{3}$' -or
            $part.sha256 -notmatch '^[a-f0-9]{64}$' -or $part.bytes -le 0) { throw 'Invalid release part.' }
        $path = Join-Path $PSScriptRoot $part.name
        if ($Download -and -not (Test-Path -LiteralPath $path)) {
            Write-Host "Downloading $($part.name)..."
            Invoke-WebRequest "$base/$($part.name)" -OutFile ($path + '.download') -UseBasicParsing
            Move-Item -LiteralPath ($path + '.download') -Destination $path
        }
        Write-Host "Checking $($part.name)..."
        if ((Get-Item -LiteralPath $path).Length -ne $part.bytes -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $part.sha256) { throw "Checksum failed: $($part.name). Delete this part from $PSScriptRoot and run the downloader again." }
        $inputStream = [IO.File]::OpenRead($path)
        try { $inputStream.CopyTo($output) } finally { $inputStream.Dispose() }
    }
} finally { $output.Dispose() }
if ((Get-Item -LiteralPath $partial).Length -ne $manifest.archive_bytes -or
    (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $manifest.archive_sha256) { throw 'Joined archive checksum failed.' }
Move-Item -LiteralPath $partial -Destination $archivePath -Force
Write-Host 'Extracting the complete portable app...'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
try {
    $prefix = $app.TrimEnd('\') + '\'
    foreach ($entry in $zip.Entries) {
        $target = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
        if ($target -ne $app -and -not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Archive entry escapes the app folder: $($entry.FullName)"
        }
    }
    if (-not $zip.GetEntry('Portable-TTS-Server-V2/TTSServer.exe')) { throw 'The archive is missing TTSServer.exe.' }
} finally { $zip.Dispose() }
# Publish only a completed extraction, so interrupted runs can be retried.
$extracting = Join-Path $Destination ('.tts-extract-' + [Guid]::NewGuid().ToString('N'))
try {
    [IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $extracting)
    [IO.Directory]::Move((Join-Path $extracting $manifest.folder), $app)
    [IO.Directory]::Delete($extracting)
} catch {
    throw "Extraction failed. Temporary files may remain at $extracting; the existing app was not overwritten. $($_.Exception.Message)"
}
Write-Host "Ready: $app\TTSServer.exe"
Write-Host "Download cache: $PSScriptRoot. You can remove this cache after verifying the app works."
Write-Host 'Windows must have WSL2 enabled. Kokoro and the app runtimes are included.'
