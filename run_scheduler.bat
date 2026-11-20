@echo off
REM Keeps the scheduler running. Add to Task Scheduler with trigger "At log on":
REM   schtasks /Create /TN "AtomHeartStocker" /TR "C:\path\to\atom_heart_stocker\run_scheduler.bat" /SC ONLOGON
cd /d "%~dp0"
if not exist logs mkdir logs
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
:loop
python main.py scheduler >> logs\scheduler.log 2>&1
timeout /t 30 >nul
goto loop
