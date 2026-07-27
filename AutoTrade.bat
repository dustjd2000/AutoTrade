@echo off
title AutoTrade

REM 이 배치 파일이 있는 폴더로 이동한다. 경로를 하드코딩하지 않으므로
REM 폴더를 옮기거나 다른 PC로 복사해도 그대로 동작한다.
pushd "%~dp0"

REM 가상환경이 있으면 그쪽 파이썬을, 없으면 시스템 파이썬을 쓴다
set "PY=python"
if exist ".venv/Scripts/python.exe" set "PY=.venv/Scripts/python.exe"
if exist "venv/Scripts/python.exe" set "PY=venv/Scripts/python.exe"

"%PY%" --version > nul 2>&1
if errorlevel 1 (
    echo [오류] 파이썬을 찾을 수 없습니다. 설치 여부와 PATH 설정을 확인하세요.
    popd
    pause
    exit /b 1
)

if not exist ".env" (
    echo [경고] .env 파일이 없습니다. .env.example 을 복사해 값을 채워주세요.
    echo.
)

echo AutoTrade 를 시작합니다...
echo.

REM -X utf8 : 한글 로그가 깨지지 않도록 강제
"%PY%" -X utf8 scripts/run_ui.py
set "EXITCODE=%ERRORLEVEL%"

popd

REM 비정상 종료 시 원인을 읽을 수 있도록 창을 닫지 않는다
if not "%EXITCODE%"=="0" (
    echo.
    echo [오류] 프로그램이 비정상 종료되었습니다. 종료 코드: %EXITCODE%
    pause
)
exit /b %EXITCODE%
