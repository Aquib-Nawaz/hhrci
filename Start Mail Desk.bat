@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if errorlevel 1 goto no_python
python start_mail_desk.py
goto finished

:use_py
py -3 start_mail_desk.py
goto finished

:no_python
echo Python 3.10 or newer is required. Install Python from https://www.python.org/downloads/
pause
exit /b 1

:finished
set "exit_code=%errorlevel%"
if not "%exit_code%"=="0" pause
exit /b %exit_code%
