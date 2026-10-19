@echo off
rem Builds dist\PPAP PDF Editor\PPAP PDF Editor.exe
cd /d "%~dp0"
if not exist .venv (
    py -3.13 -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install pymupdf PySide6 pyinstaller
)
.venv\Scripts\python make_icon.py
.venv\Scripts\pyinstaller --noconfirm --windowed --name "PPAP PDF Editor" --icon icon.ico ^
    --exclude-module tkinter --exclude-module pymupdf.layout --exclude-module numpy ppap_pdf_editor.py
