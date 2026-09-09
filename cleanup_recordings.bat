@echo off
cd /d "D:\brainex ai\FINAL _MVP"
call venv\Scripts\activate.bat
python manage.py delete_expired_recordings >> recording_cleanup.log 2>&1