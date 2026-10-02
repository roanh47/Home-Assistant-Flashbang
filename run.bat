@echo off
rem Install the deps and start Roan's Flashbang without a console window.
cd /d "%~dp0"
python -m pip install -r requirements.txt
start "" pythonw Home-Assistant-Flashbang.py