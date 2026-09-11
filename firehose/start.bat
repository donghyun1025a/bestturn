@echo off
REM 윈도우용 실행 파일. 더블클릭하면 가상환경 준비 후 대시보드를 엽니다.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [오류] python 을 찾을 수 없습니다.
  echo        https://www.python.org/downloads/ 에서 설치할 때
  echo        "Add python.exe to PATH" 를 반드시 체크하세요.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo 가상환경을 만드는 중입니다...
  python -m venv .venv || (echo [오류] 가상환경 생성 실패 & pause & exit /b 1)
)

echo 의존성을 확인하는 중입니다...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo [오류] 설치 실패 & pause & exit /b 1)

echo 대시보드를 엽니다. 이 창을 닫으면 종료됩니다.
".venv\Scripts\python.exe" run.py
pause
