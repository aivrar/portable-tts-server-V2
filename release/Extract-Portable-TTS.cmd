@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Extract-Portable-TTS.ps1" %*
if errorlevel 1 pause
