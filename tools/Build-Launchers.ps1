[CmdletBinding()]
param([string]$Destination = (Join-Path $PSScriptRoot '..\output\portable-build\stage\Portable-TTS-Server-V2'))
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Destination = [IO.Path]::GetFullPath($Destination)
$build = Join-Path $root 'output\portable-build\native'
New-Item -ItemType Directory -Path $build -Force | Out-Null
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$version = (Get-Content -LiteralPath (Join-Path $root 'app.json') -Raw | ConvertFrom-Json).version
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid application version.' }
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'Install Visual Studio Build Tools with Desktop development with C++ and a Windows SDK.' }
Import-Module (Join-Path $vs 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $vs -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64' | Out-Null
$manifest = @'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
 <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security></trustInfo>
 <dependency><dependentAssembly><assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"/></dependentAssembly></dependency>
 <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application><supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/></application></compatibility>
</assembly>
'@
[IO.File]::WriteAllText((Join-Path $build 'app.manifest'), $manifest)
$icon = (Join-Path $root 'assets\tts.ico').Replace('\','/')
$extractor = (Join-Path $root 'release\Extract-Portable-TTS.ps1').Replace('\','/')
$resource = "101 ICON `"$icon`"`r`n201 RCDATA `"$extractor`"`r`n"
[IO.File]::WriteAllText((Join-Path $build 'app.rc'), $resource)
Push-Location $build
try {
    & rc.exe /nologo /fo app.res app.rc
    if ($LASTEXITCODE -ne 0) { throw 'Resource compilation failed.' }
    foreach ($mode in @('TTSServer', 'Download-TTSServer')) {
        $defines = @('/D', ('RELEASE_VERSION=L\"' + $version + '\"'))
        if ($mode -eq 'Download-TTSServer') { $defines += '/DDOWNLOADER' }
        & cl.exe /nologo /W4 /O2 /MT /utf-8 @defines (Join-Path $root 'launcher\bootstrap.c') /Fo:bootstrap.obj app.res /link /SUBSYSTEM:WINDOWS /DYNAMICBASE /NXCOMPAT /MANIFEST:EMBED /MANIFESTINPUT:app.manifest "/OUT:$mode.exe" user32.lib gdi32.lib shell32.lib ole32.lib comctl32.lib
        if ($LASTEXITCODE -ne 0) { throw "Native build failed: $mode" }
    }
    Copy-Item -LiteralPath (Join-Path $build 'TTSServer.exe') -Destination (Join-Path $Destination 'TTSServer.exe') -Force
} finally { Pop-Location }
$dotnet = Join-Path $env:ProgramFiles 'dotnet\dotnet.exe'
& $dotnet publish (Join-Path $root 'launcher\TTSServer.csproj') -c Release -o (Join-Path $Destination 'runtime\launcher')
if ($LASTEXITCODE -ne 0) { throw 'Desktop host build failed.' }
Write-Host "Launchers ready: $Destination\TTSServer.exe and $build\Download-TTSServer.exe"
