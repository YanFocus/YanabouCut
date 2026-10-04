@echo off
cd /d D:\projet_1
python manage.py process_queue --loop
pause
