@echo off
call C:\Users\y134\venv\Scripts\activate.bat
cd /d D:\SynologyDrive\桌面\line-backend
uvicorn app.main:app --reload --port 8000