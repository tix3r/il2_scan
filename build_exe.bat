@echo off
setlocal
cd /d "%~dp0"
echo Building il2_scan v2.1 with assets\icon.ico
where py >nul 2>nul
if errorlevel 1 (echo ERROR: Python 3 not found & pause & exit /b 1)
tasklist /FI "IMAGENAME eq il2_scan.exe" | find /I "il2_scan.exe" >nul
if not errorlevel 1 (echo ERROR: Close il2_scan.exe before building & pause & exit /b 1)
if not exist "assets\icon.ico" (echo ERROR: assets\icon.ico missing & pause & exit /b 1)
py -m pip install --upgrade pyinstaller pillow pystray
if errorlevel 1 (echo ERROR: Dependency installation failed & pause & exit /b 1)
if exist "il2_scan.exe" del /f /q "il2_scan.exe"
if exist "il2_scan.exe" (echo ERROR: Old EXE could not be removed & pause & exit /b 1)
if exist "build" rmdir /s /q "build"
if exist "il2_scan.spec" del /f /q "il2_scan.spec"
py -m PyInstaller --noconfirm --clean --onefile --windowed --name "il2_scan" --distpath "." --icon "assets\icon.ico" --add-data "assets;assets" il2_source.py
if errorlevel 1 (echo ERROR: Build failed & pause & exit /b 1)
if not exist "il2_scan.exe" (echo ERROR: EXE not created & pause & exit /b 1)
echo.
echo NEW EXE READY:
for %%F in ("il2_scan.exe") do echo %%~fF - %%~zF bytes - %%~tF
pause
