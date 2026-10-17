@echo off
rem ============================================================
rem  L4D2 Mod Studio - launcher
rem  This file is intentionally pure ASCII: the project folder
rem  path contains non-ASCII characters, and a .bat carrying
rem  non-ASCII literals gets mangled by the console codepage.
rem  All paths are resolved at runtime via %~dp0 instead.
rem ============================================================
chcp 65001 >nul 2>nul
title L4D2 Mod Studio
cd /d "%~dp0"

set "PYEXE="
if exist "%~dp0runtime\python\python.exe" set "PYEXE=%~dp0runtime\python\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "C:\Python312\python.exe" set "PYEXE=C:\Python312\python.exe"
if defined PYEXE goto run

where py >nul 2>nul
if %errorlevel%==0 set "PYEXE=py -3"
if defined PYEXE goto run

where python >nul 2>nul
if %errorlevel%==0 set "PYEXE=python"
if defined PYEXE goto run

echo.
echo  [ERROR] Python 3.10+ not found.
echo          Install it from https://www.python.org/downloads/
echo          or drop a portable copy into:  %~dp0runtime\python\
echo.
pause
exit /b 1

:run
echo.
echo  Starting L4D2 Mod Studio ...
echo  (keep this window open; closing it stops the app)
echo.
rem  %* forwards args, e.g.  START.bat --port 17247   (when the default port is taken)
%PYEXE% "%~dp0app\server.py" %*
echo.
echo  Server stopped.
pause
