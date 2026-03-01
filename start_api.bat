@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
REM On Linux/macOS use gunicorn. On Windows use uvicorn directly:
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 4 --timeout-keep-alive 5
