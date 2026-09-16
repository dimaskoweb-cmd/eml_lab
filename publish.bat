@echo off
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
"C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -X utf8 publish_to_github.py
pause
