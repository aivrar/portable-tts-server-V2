[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$source = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$target = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
if ($target -notmatch '^[A-Za-z]:\\' -or $target -match '[\r\n]') {
    throw 'Choose a new folder on a local Windows drive.'
}
if ($target -eq $source -or $target.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The destination must be outside this app folder.'
}
if (Test-Path -LiteralPath $target) { throw 'The destination already exists. Choose a new folder.' }
if (Test-Path -LiteralPath (Join-Path $source 'runtime\transfer-rootfs.tar')) {
    throw 'After verifying this copy, delete its runtime\transfer-rootfs.tar snapshot before creating another transfer.'
}
$state = & (Join-Path $source 'Start-TTSServer.ps1') -VerifyOnly
Write-Host 'Stopping this copy, then copying files and exporting its Linux disk. Do not start it until this finishes.'
& (Join-Path $source 'Stop-TTSServer.ps1') -AllowLockedDisk
New-Item -ItemType Directory -Path $target | Out-Null
New-Item -ItemType Directory -Path (Join-Path $target 'runtime') | Out-Null
$incomplete = Join-Path $target 'runtime\transfer-incomplete'
Set-Content -LiteralPath $incomplete -Value 'Copy/export must complete before this folder can start.'
# Export, rather than copy a locked VHD. Other WSL applications keep running.
# Robocopy returns 0-7 for successful copies; never mirror/delete the source.
& robocopy.exe $source $target /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /NFL /NDL /NJH /NJS /NP /XD (Join-Path $source 'wsl') (Join-Path $source '.git')
if ($LASTEXITCODE -ge 8) { throw "File copy failed. The incomplete destination is preserved at $target" }
$transfer = Join-Path $target 'runtime\transfer-rootfs.tar'
if (Test-Path -LiteralPath $transfer) { throw 'A previous transfer snapshot is present. Remove it after verifying its imported copy before creating another transfer.' }
New-Item -ItemType Directory -Path (Split-Path $transfer) -Force | Out-Null
Write-Host 'Exporting installed Linux dependencies and models; this can take several minutes...'
& wsl.exe --export $state.Distro ($transfer + '.partial')
if ($LASTEXITCODE -ne 0) { throw "Linux export failed. The source is intact; the incomplete destination is at $target" }
Move-Item -LiteralPath ($transfer + '.partial') -Destination $transfer
Remove-Item -LiteralPath $incomplete
Write-Host "Portable copy ready: $target"
Write-Host 'Run Start-TTSServer.cmd there to import the transferred disk. After verifying the new copy, you may delete runtime\transfer-rootfs.tar there to reclaim snapshot space.'
