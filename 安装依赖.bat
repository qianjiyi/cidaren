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
    echo Install Anaconda or Miniconda, then run this script again.
    pause
    exit /b 1
)

echo Conda: %CIDAREN_CONDA%
"%CIDAREN_CONDA%" run -n cidaren python -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo Creating Conda environment: cidaren ^(Python 3.12^)
    "%CIDAREN_CONDA%" create -y -n cidaren python=3.12 pip
    if errorlevel 1 (
        echo [ERROR] Failed to create the cidaren environment.
        pause
        exit /b 1
    )
)

"%CIDAREN_CONDA%" run -n cidaren python -c "import sys; assert sys.version_info[:2] == (3, 12)" >nul 2>&1
if errorlevel 1 (
    echo Updating Conda environment cidaren to Python 3.12
    "%CIDAREN_CONDA%" install -y -n cidaren python=3.12 pip
    if errorlevel 1 (
        echo [ERROR] Failed to install Python 3.12 in the cidaren environment.
        pause
        exit /b 1
    )
)

if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul

echo Installing dependencies into Conda environment: cidaren
"%CIDAREN_CONDA%" run --no-capture-output -n cidaren python -m pip install -e .
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

"%CIDAREN_CONDA%" run --no-capture-output -n cidaren python -m pip check
if errorlevel 1 (
    echo [ERROR] Dependency check failed.
    pause
    exit /b 1
)

echo Dependencies are ready.
pause
