@echo off
REM Aegis Token Monitor v0.2 - CLI launcher (independent; does not touch v0.1 run.bat)
REM Uses v0.2's own venv interpreter; falls back to system python (stdlib-only CLI).

chcp 65001 >nul
cd /d %~dp0

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 run.py %*
    exit /b %ERRORLEVEL%
)

python run.py %*
exit /b %ERRORLEVEL%
