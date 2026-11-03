@echo off
echo Starting Zero RIR web app...
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python web_app.py
pause
