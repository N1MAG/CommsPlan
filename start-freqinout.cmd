@echo off
setlocal

set "SCRIPT_WORKTREE=%~dp0"
if defined FREQINOUT_INSTALL_DIR (
  set "WORKTREE=%FREQINOUT_INSTALL_DIR%"
) else (
  set "WORKTREE=%SCRIPT_WORKTREE%"
)

if exist "%WORKTREE%\.venv\" (
  if not exist "%WORKTREE%\.venv\Scripts\python.exe" (
    >&2 echo CommsPlan installation is incomplete: .venv has no usable Python.
    >&2 echo Run: py -3.11 install_freqinout.py
    exit /b 1
  )
  set "PYTHON=%WORKTREE%\.venv\Scripts\python.exe"
) else if exist "%WORKTREE%\venv\Scripts\python.exe" (
  set "PYTHON=%WORKTREE%\venv\Scripts\python.exe"
) else (
  >&2 echo CommsPlan is not installed in "%WORKTREE%".
  >&2 echo Run: py -3.11 install_freqinout.py
  exit /b 1
)

rem FIO owns the Windows default profile under LOCALAPPDATA/APPDATA.
rem Preserve explicitly selected profiles without creating a parallel default.
if defined FREQINOUT_RUNTIME_ROOT if not defined FREQINOUT_CONFIG_DIR (
  set "FREQINOUT_CONFIG_DIR=%FREQINOUT_RUNTIME_ROOT%"
  >&2 echo Warning: FREQINOUT_RUNTIME_ROOT is deprecated; use FREQINOUT_CONFIG_DIR.
)

pushd "%WORKTREE%" >nul || exit /b 1
set "FIO_LAUNCH_WORKTREE=%WORKTREE%"
"%PYTHON%" -c "import hashlib, json, os, pathlib, sys; from freqinout.version import __version__; import freqinout.main, PySide6; root=pathlib.Path(os.environ['FIO_LAUNCH_WORKTREE']); r=json.loads((root/'.freqinout-install-verified.json').read_text()); assert r.get('version') == __version__; assert pathlib.Path(r.get('python','')).resolve() == pathlib.Path(sys.executable).resolve(); assert r.get('requirements_sha256') == hashlib.sha256((root/'requirements.txt').read_bytes()).hexdigest()" >nul 2>&1
if errorlevel 1 (
  >&2 echo CommsPlan installation validation failed.
  >&2 echo Run: py -3.11 install_freqinout.py
  popd
  exit /b 1
)
"%PYTHON%" -m freqinout.main %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
