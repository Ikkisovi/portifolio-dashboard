@echo off
set SCRIPT_DIR=%~dp0
python -m streamlit run "%SCRIPT_DIR%app.py"
