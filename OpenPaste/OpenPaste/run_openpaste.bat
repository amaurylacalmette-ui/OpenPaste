@echo off
rem Starts OpenPaste in the background (no console window).
start "OpenPaste" /min pythonw "%~dp0openpaste.py" %*
