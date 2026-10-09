@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "CIDAREN_CONDA="

if defined CONDA_EXE if exist "%CONDA_EXE%" set "CIDAREN_CONDA=%CONDA_EXE%"
if not defined CIDAREN_CONDA (
    for /f "delims=" %%I in ('where conda.exe 2^>nul') do if not defined CIDAREN_CONDA set "CIDAREN_CONDA=%%~fI"
)

for %%I in (
    "%USERPROFILE%\anaconda3\Scripts\conda.exe"
    "%USERPROFILE%\miniconda3\Scripts\conda.exe"
    "%LOCALAPPDATA%\anaconda3\Scripts\conda.exe"
    "%LOCALAPPDATA%\miniconda3\Scripts\conda.exe"
    "%ProgramData%\anaconda3\Scripts\conda.exe"
    "%ProgramData%\miniconda3\Scripts\conda.exe"
) do if not defined CIDAREN_CONDA if exist "%%~fI" set "CIDAREN_CONDA=%%~fI"

if not defined CIDAREN_CONDA (
    echo [ERROR] Conda was not found.
    echo Install Anaconda or Miniconda, then run 安装依赖.bat.
    pause
    exit /b 1
)

"%CIDAREN_CONDA%" run -n cidaren python -c "import sys; assert sys.version_info[:2] == (3, 12)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Conda environment cidaren with Python 3.12 was not found.
    echo Run 安装依赖.bat first.
    pause
    exit /b 1
)

echo Starting CiDaRen at http://127.0.0.1:5001
"%CIDAREN_CONDA%" run --no-capture-output -n cidaren python -m cidaren
set "CIDAREN_EXIT_CODE=%ERRORLEVEL%"

if not "%CIDAREN_EXIT_CODE%"=="0" (
    echo [ERROR] CiDaRen exited with code %CIDAREN_EXIT_CODE%.
    pause
)
exit /b %CIDAREN_EXIT_CODE%
