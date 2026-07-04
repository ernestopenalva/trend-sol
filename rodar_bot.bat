@echo off
REM trend-sol - Monitor principal local

if exist venv\Scripts\activate.bat (
  call venv\Scripts\activate.bat
)

python main.py
