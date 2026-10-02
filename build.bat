@echo off
rem Build a single Flashbang.exe (no console) next to the script.
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller
pyinstaller --noconfirm --onefile --windowed --name Flashbang Home-Assistant-Flashbang.py
echo.
echo Done. The exe is in dist\Flashbang.exe
pause
