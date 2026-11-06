@echo off
REM Build NP Launcher into a single Windows executable.
echo Installing build dependencies...
python -m pip install --upgrade pyinstaller flask requests || goto :error
echo Building...
python -m PyInstaller np-launcher.spec --clean --noconfirm || goto :error
echo.
echo Done. Executable is in: dist\"NP Launcher\"
pause
exit /b 0
:error
echo.
echo Build failed. See output above.
pause
exit /b 1
