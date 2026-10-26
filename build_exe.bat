@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
rem Do not inherit interpreter overrides from conda or other shells.
set "PYTHONHOME="
set "PYTHONPATH="

rem Always build with the project venv, never whatever "python" is on PATH.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Project venv not found: %PY%
  echo Create it first:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)

rem Minimal PATH while building: PyInstaller resolves DLL dependencies through PATH,
rem and DLLs from conda or other apps (e.g. an ICU build) must not leak into the bundle.
set "PATH=%~dp0.venv\Scripts;%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0"
set "CONDA_PREFIX="
set "CONDA_DEFAULT_ENV="

set "MODE=gui"
set "NAME=CDriveRescue"
if /i "%~1"=="--cli" (
  set "MODE=cli"
  set "NAME=CDriveRescueCLI"
)

"%PY%" verify_build.py preflight %MODE%
if errorlevel 1 exit /b 1

if exist "dist\%NAME%" rmdir /s /q "dist\%NAME%"
if exist "build\%NAME%" rmdir /s /q "build\%NAME%"
if "%MODE%"=="cli" goto cli

"%PY%" -m PyInstaller --noconfirm --clean --windowed --name CDriveRescue --icon "src\ui\assets\app.ico" --hidden-import src.core.cache_cleaner --hidden-import src.core.app_mover --hidden-import src.ui.main_window --hidden-import psutil --collect-all PyQt6 --collect-all send2trash --add-data "src/core/cache_rules.json;src/core" --add-data "src/ui/theme.qss;src/ui" --add-data "src/ui/assets;src/ui/assets" main.py
if errorlevel 1 exit /b 1
goto verify

:cli
"%PY%" -m PyInstaller --noconfirm --clean --console --name CDriveRescueCLI --icon "src\ui\assets\app.ico" --hidden-import psutil --collect-all send2trash --add-data "src/core/cache_rules.json;src/core" backend_cli.py
if errorlevel 1 exit /b 1

:verify
"%PY%" verify_build.py verify %MODE%
if errorlevel 1 exit /b 1
echo Build succeeded: dist\%NAME%\%NAME%.exe
exit /b 0
