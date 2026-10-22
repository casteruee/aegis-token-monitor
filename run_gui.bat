@echo off
setlocal
rem Aegis Token Monitor v0.4 GUI launcher.
pushd "%~dp0"
if errorlevel 1 (
    echo [aegis] Cannot open the application directory.
    pause
    exit /b 1
)
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo [aegis] The local Python environment is missing.
    pause
    popd
    exit /b 2
)
if not exist "%~dp0run_gui.py" (
    echo [aegis] run_gui.py is missing.
    pause
    popd
    exit /b 3
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run_gui.py"
set "launch_status=%errorlevel%"
popd
if not "%launch_status%"=="0" (
    echo [aegis] Failed to start the GUI.
    pause
)
exit /b %launch_status%
