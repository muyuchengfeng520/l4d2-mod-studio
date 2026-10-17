@echo off
rem ============================================================
rem  L4D2 Mod Studio - build the single-file desktop exe
rem
rem  Intentionally pure ASCII (same reason as START.bat):
rem  this folder path contains non-ASCII characters and a .bat
rem  carrying non-ASCII literals gets mangled by the codepage.
rem
rem  What it does:
rem    1. create an isolated build venv under  .build\venv
rem    2. install pywebview (native window) + pyinstaller
rem    3. regenerate assets\app.ico from tools\make_icon.py
rem    4. freeze desktop.py into a one-file windowed exe
rem    5. copy the exe to the project root as L4D2ModStudio.exe
rem
rem  Everything (venv, caches, work dir) stays inside this folder,
rem  so the user's own Python and the C: drive are left alone.
rem ============================================================
setlocal
chcp 65001 >nul 2>nul
cd /d "%~dp0.."
set "ROOT=%CD%"
set "VENV=%ROOT%\.build\venv"
set "PY=%VENV%\Scripts\python.exe"

echo.
echo  [1/5] locating a Python to bootstrap the build venv ...
set "BOOTPY="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "BOOTPY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined BOOTPY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "BOOTPY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined BOOTPY (
  where py >nul 2>nul && set "BOOTPY=py -3"
)
if not defined BOOTPY (
  where python >nul 2>nul && set "BOOTPY=python"
)
if not defined BOOTPY (
  echo  [ERROR] Python 3.10+ not found. Install it from python.org first.
  pause & exit /b 1
)
echo        using %BOOTPY%

echo.
echo  [2/5] creating build venv at .build\venv ...
if not exist "%PY%" (
  %BOOTPY% -m venv "%VENV%" || (echo  [ERROR] venv creation failed. & pause & exit /b 1)
) else (
  echo        already exists, reusing
)

echo.
echo  [3/5] installing pywebview + pyinstaller ...
set "PYINSTALLER_CONFIG_DIR=%ROOT%\.build\pyi-cache"
"%PY%" -m pip install -q --upgrade pip
"%PY%" -m pip install -q pywebview pyinstaller || (echo  [ERROR] pip install failed. & pause & exit /b 1)

echo.
echo  [4/5] generating icon + freezing ...
"%PY%" "%ROOT%\tools\make_icon.py" "%ROOT%\assets\app.ico" || (echo  [ERROR] icon build failed. & pause & exit /b 1)

rem The full tag library is now built into core/taglib.py (BUILTIN_TAGS), so
rem data\taglib is OPTIONAL: it only exists to let the user override the built-in
rem library with a newer NekoVpk file. When present it gets bundled too.
set "TAGLIB_ARG="
if exist "%ROOT%\data\taglib\TaggedAssets.jsonc" (
  set "TAGLIB_ARG=--add-data "%ROOT%\data\taglib;data\taglib""
)

rem Run PyInstaller via "python -m" instead of the pyinstaller.exe shim:
rem the shim pip writes hard-codes the venv's absolute path at creation time,
rem so moving/renaming the project folder silently breaks it (pyinstaller.exe
rem exits with no output). "python -m PyInstaller" has no such binding.
"%PY%" -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name L4D2ModStudio ^
  --icon "%ROOT%\assets\app.ico" ^
  --paths "%ROOT%" --paths "%ROOT%\app" ^
  --hidden-import server ^
  --add-data "%ROOT%\web;web" ^
  %TAGLIB_ARG% ^
  --collect-all webview --collect-all pythonnet --collect-all clr_loader ^
  --distpath "%ROOT%\dist" --workpath "%ROOT%\.build\pyi" --specpath "%ROOT%\.build" ^
  "%ROOT%\desktop.py"
if errorlevel 1 ( echo  [ERROR] pyinstaller failed. & pause & exit /b 1 )

echo.
echo  [5/5] copying exe to the project root ...
copy /y "%ROOT%\dist\L4D2ModStudio.exe" "%ROOT%\L4D2ModStudio.exe" >nul || (echo  [ERROR] copy failed. & pause & exit /b 1)

echo.
echo  ============================================================
echo   Done:  %ROOT%\L4D2ModStudio.exe
echo  ============================================================
echo.
pause
