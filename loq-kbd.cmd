@echo off
rem Launcher for loq_kbd.py from cmd.exe.
setlocal
set "HERE=%~dp0"
python "%HERE%loq_kbd.py" %*
exit /b %ERRORLEVEL%
