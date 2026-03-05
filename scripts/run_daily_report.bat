@echo off
REM ============================================================
REM AlphaSAGE Daily Report: Generate + Publish
REM Runs after strategy finalizes at 15:55
REM ============================================================

set PYTHON=E:\factor\.venv\Scripts\python.exe
set PROJECT=E:\factor\lean_project\Pensive Tan Bull Local
set CONFIG=%PROJECT%\scripts\daily_report_config.json
set LOGDIR=%PROJECT%\reports\scheduler_logs

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

set DATESTAMP=%date:~0,4%%date:~5,2%%date:~8,2%

echo [%date% %time%] Starting daily report pipeline >> "%LOGDIR%\daily_report_%DATESTAMP%.log"

REM Step 1: Generate report via LLM
echo [%date% %time%] Step 1: Generating report... >> "%LOGDIR%\daily_report_%DATESTAMP%.log"
"%PYTHON%" "%PROJECT%\scripts\generate_daily_report.py" --date today --config "%CONFIG%" --strict-date false --allow-stale-cache true --max-date-lag-days 1 >> "%LOGDIR%\daily_report_%DATESTAMP%.log" 2>&1
if %ERRORLEVEL% neq 0 (
    echo [%date% %time%] FAILED: generate_daily_report.py exited with %ERRORLEVEL% >> "%LOGDIR%\daily_report_%DATESTAMP%.log"
    exit /b %ERRORLEVEL%
)

REM Step 2: Publish to GitHub Pages + Bark notification
echo [%date% %time%] Step 2: Publishing to GitHub Pages... >> "%LOGDIR%\daily_report_%DATESTAMP%.log"
"%PYTHON%" "%PROJECT%\scripts\publish_daily_report.py" --date today >> "%LOGDIR%\daily_report_%DATESTAMP%.log" 2>&1
if %ERRORLEVEL% neq 0 (
    echo [%date% %time%] FAILED: publish_daily_report.py exited with %ERRORLEVEL% >> "%LOGDIR%\daily_report_%DATESTAMP%.log"
    exit /b %ERRORLEVEL%
)

echo [%date% %time%] Pipeline complete >> "%LOGDIR%\daily_report_%DATESTAMP%.log"
