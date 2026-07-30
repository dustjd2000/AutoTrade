@echo off
title AutoTrade

REM 이 배치 파일이 있는 폴더로 이동한다. 경로를 하드코딩하지 않으므로
REM 폴더를 옮기거나 다른 PC에 복사해도 그대로 동작한다.
pushd "%~dp0"

REM 가상환경이 있으면 그 파이썬을, 없으면 시스템 파이썬을 쓴다.
REM pythonw.exe 는 콘솔 없이 도는 GUI용 파이썬이라 이 검은 창이 남지 않는다.
set "PY=python"
set "PYW=pythonw"
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    set "PYW=.venv\Scripts\pythonw.exe"
)
if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
    set "PYW=venv\Scripts\pythonw.exe"
)

"%PY%" --version > nul 2>&1
if errorlevel 1 (
    echo [오류] 파이썬을 찾을 수 없습니다. 설치 여부와 PATH 설정을 확인하세요.
    popd
    pause
    exit /b 1
)

REM pythonw.exe 가 없는 드문 설치에서는 콘솔용 파이썬으로 대신 실행한다
if not "%PYW%"=="pythonw" if not exist "%PYW%" set "PYW=%PY%"

if not exist ".env" (
    echo [경고] .env 파일이 없습니다. .env.example 을 참고해 값을 채워주세요.
    echo.
    pause
)

REM -X utf8 : 한글 로그가 깨지지 않도록 지정
REM 별도 프로세스로 띄우고 배치를 끝내므로 이 창은 바로 닫힌다.
REM 창이 뜨기 전에 죽는 경우는 대화상자와 logs/error/ 로 알린다 (scripts/run_ui.py).
REM --auto-start : 화면의 "시작" 버튼을 직접 누르지 않아도 창이 뜨는 즉시 엔진을 시작한다.
start "" /D "%~dp0" "%PYW%" -X utf8 scripts\run_ui.py --auto-start

popd
exit /b 0
