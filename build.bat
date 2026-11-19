@echo off
rem Builds dist\PPAP PDF Editor\PPAP PDF Editor.exe
cd /d "%~dp0"
if not exist .venv (
    py -3.13 -m venv .venv
    .venv\Scripts\python -m pip install --upgrade pip
)
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\python make_icon.py
.venv\Scripts\pyinstaller --noconfirm --windowed --name "PPAP PDF Editor" --icon icon.ico ^
    --exclude-module tkinter --exclude-module numpy --exclude-module pymupdf.layout ^
    --exclude-module pymupdf_layout --exclude-module pymupdf4llm --exclude-module onnxruntime ^
    --exclude-module networkx --exclude-module tabulate ^
    --collect-submodules winrt ppap_pdf_editor.py
