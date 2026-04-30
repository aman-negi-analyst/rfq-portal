@echo off
title RFQ Portal
:start
echo Starting RFQ Portal...
python app.py
echo.
echo App stopped or crashed. Restarting in 5 seconds...
echo Press CTRL+C to stop.
timeout /t 5
goto start
