@echo off
rem Works from anywhere, including the Windows Startup folder
cd /d "%~dp0"
if not exist web_app.py cd /d "C:\Users\oabou\Desktop\Zero_RIR_Bot"
echo Starting Zero RIR web app...
set PYTHONIOENCODING=utf-8
python web_app.py
pause
