"""Writes icon.ico for the PyInstaller build."""
import sys
from PySide6.QtWidgets import QApplication
import ppap_pdf_editor as A

app = QApplication(sys.argv)
A.make_app_icon(256).save("icon.ico", "ICO")
