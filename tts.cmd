@echo off
setlocal
set "TTS_SCRIPT=%~dp0tts.py"
if exist "%~dp0runtime\python\python.exe" goto run_bundled
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_py
python -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_python
python3 -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_python3
echo error: no Python 3 interpreter found on PATH 1>&2
exit /b 127
:run_bundled
"%~dp0runtime\python\python.exe" "%TTS_SCRIPT%" %*
exit /b %ERRORLEVEL%
:run_py
py -3 "%TTS_SCRIPT%" %*
exit /b %ERRORLEVEL%
:run_python
python "%TTS_SCRIPT%" %*
exit /b %ERRORLEVEL%
:run_python3
python3 "%TTS_SCRIPT%" %*
exit /b %ERRORLEVEL%
