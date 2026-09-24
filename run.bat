@echo off
setlocal
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Create the .venv and install requirements first. See README.md.
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0jarvis_assistant.pyw" %*
