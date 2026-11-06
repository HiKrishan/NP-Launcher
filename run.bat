@echo off
REM Run NP Launcher from source.
python -m pip install -r requirements.txt || goto :error
python run.py %*
exit /b 0
:error
echo Failed to start. Is Python installed and on PATH?
pause
exit /b 1
