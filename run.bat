@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title wxMoments
chcp 65001 >NUL
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "CONFIG_FILE=config\config.json"
set "CONFIG_EXAMPLE=config\config.example.json"
set "RUNTIME_DIR=runtime"
set "VENV_DIR=%RUNTIME_DIR%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "WXMOMENTS_RUNTIME_DIR=%RUNTIME_DIR%"
set "INSTALL_LOG=%RUNTIME_DIR%\install.log"
set "REQ_FILE=config/requirements.txt"

if not exist "%CONFIG_FILE%" copy /Y "%CONFIG_EXAMPLE%" "%CONFIG_FILE%" >NUL
if not exist "%RUNTIME_DIR%" mkdir "%RUNTIME_DIR%" >NUL 2>NUL
if not exist "%PYTHON_EXE%" (
    echo [5%%] Preparing runtime...
    where py >NUL 2>NUL
    if errorlevel 1 (
        where python >NUL 2>NUL
        if errorlevel 1 (
            echo Python 3 was not found. Please install Python 3.10 or newer.
            pause
            exit /b 1
        )
        python -m venv "%VENV_DIR%" >NUL 2>&1
    ) else (
        py -3 -m venv "%VENV_DIR%" >NUL 2>&1
    )
    if not exist "%PYTHON_EXE%" (
        echo Unable to create the virtual environment.
        pause
        exit /b 1
    )
)

call :install_deps
if errorlevel 1 exit /b 1

"%PYTHON_EXE%" "src\wxmoments.py" %*
if errorlevel 1 goto fail
if "%~1"=="" pause
exit /b 0

:install_deps
echo [15%%] Installing or repairing dependencies...
"%PYTHON_EXE%" -m ensurepip --upgrade >"%INSTALL_LOG%" 2>&1
if exist "wheels" (
    "%PYTHON_EXE%" -m pip install --no-index --find-links="wheels" --disable-pip-version-check -r "%REQ_FILE%" >>"%INSTALL_LOG%" 2>&1
    if errorlevel 1 (
        echo Packaged wheels did not match this Python. Trying online install...
        "%PYTHON_EXE%" -m pip install --find-links="wheels" --disable-pip-version-check -r "%REQ_FILE%" >>"%INSTALL_LOG%" 2>&1
    )
) else (
    "%PYTHON_EXE%" -m pip install --disable-pip-version-check -r "%REQ_FILE%" >>"%INSTALL_LOG%" 2>&1
)
if errorlevel 1 (
    echo Dependency installation failed. See: %INSTALL_LOG%
    pause
    exit /b 1
)
"%PYTHON_EXE%" -m pip check >>"%INSTALL_LOG%" 2>&1
if errorlevel 1 (
    echo Dependency check failed after installation. See: %INSTALL_LOG%
    pause
    exit /b 1
)
exit /b 0

:fail
echo.
echo wxMoments failed. See the error above.
pause
exit /b 1
