@echo off
REM ==========================================================================
REM  Customer 360 - double-click to run everything on Windows.
REM    1. creates a private Python environment (.venv) the first time
REM    2. installs the pinned libraries
REM    3. runs the pipeline (stops here if it fails)
REM    4. opens the dashboard in your browser
REM ==========================================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
)

echo Installing libraries...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :failed

echo Running the pipeline...
".venv\Scripts\python.exe" run_pipeline.py || goto :failed

echo Opening the dashboard (close this window to stop it)...
".venv\Scripts\python.exe" -m streamlit run streamlit_app.py
goto :eof

:failed
echo.
echo PIPELINE FAILED - read the message above, or logs\pipeline.log
pause
exit /b 1
