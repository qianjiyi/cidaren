@echo off
setlocal EnableExtensions
chcp 65001 >nul
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
    echo [ERROR] Conda was not found. Install Anaconda or Miniconda first.
    pause
    exit /b 1
)

"%CIDAREN_CONDA%" run -n cidaren python -c "import sys; assert sys.version_info[:2] == (3, 12)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Install project dependencies first.
    pause
    exit /b 1
)

"%CIDAREN_CONDA%" run --no-capture-output -n cidaren python -m cidaren.bank_tools %*
set "CIDAREN_EXIT_CODE=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %CIDAREN_EXIT_CODE%
