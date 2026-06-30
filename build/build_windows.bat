@echo off
REM Build ProctorClient.exe on Windows.
REM Run this from the repo root in a Command Prompt:  build\build_windows.bat
REM Output: dist\ProctorClient.exe  (single self-contained file)

setlocal
cd /d "%~dp0.."

echo [build] creating virtual environment...
python -m venv .venv-build || goto :error
call .venv-build\Scripts\activate.bat || goto :error

echo [build] installing dependencies...
python -m pip install --upgrade pip || goto :error
pip install -r requirements-build.txt || goto :error

echo [build] running PyInstaller...
pyinstaller --clean --noconfirm proctor-client.spec || goto :error

echo.
echo [build] DONE -> dist\ProctorClient.exe
echo Copy proctor.ini.example to proctor.ini next to the exe to preset settings.
goto :eof

:error
echo [build] FAILED
exit /b 1
