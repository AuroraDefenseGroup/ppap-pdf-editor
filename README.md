# PPAP PDF Editor

Windows 10/11 desktop app for reading and editing PDFs and building PPAP / FAI ballooned drawings.

**Run:** `dist\PPAP PDF Editor\PPAP PDF Editor.exe`. Copy the whole `PPAP PDF Editor` folder to install it on another PC; no Python is needed.
You can also drag a PDF onto the window, or right-click a PDF, choose "Open with", and pick the exe.

## Features
- **Reading:** continuous scroll, Ctrl+wheel zoom, Fit Width / Fit Page, page jump, Find (Ctrl+F), Print.
- **Balloons (B):** click to drop a numbered balloon, or press on a dimension and drag out to get a leader line. Shapes are circle, square, triangle and diamond (e.g. for key/critical characteristics). Numbers count up automatically and you can set colour, fill, size, font and leader end (dot/arrow/none).
  - *Capture nearby text* copies the dimension under the leader into the balloon's Characteristic.
  - The Balloon List panel lets you edit Characteristic and Notes, renumber 1…n, and export a CSV for the dimensional results sheet.
- **Edit Text (E):** click existing PDF text to retype it or change its size and colour.
- **Remove Text (X):** drag a box to delete PDF text; lines and graphics stay.
- **Whiteout (W):** drag a box to erase everything inside it. This also works on scanned drawings.
- **Add Text (T), Rectangle (R), Arrow (A), Line (L).**
- Undo/redo (Ctrl+Z / Ctrl+Y) covers everything, including text edits.

## Saving
- **Save** writes markups as standard PDF annotations, so Acrobat, Edge and other viewers show them. It also embeds a copy so the markups stay editable when you reopen the file in this app.
- **Export Flattened PDF** burns the markups into the page for a released drawing.

Edited PDF text is replaced using the standard Helvetica, Times or Courier fonts, not the drawing's original embedded font.

## Rebuilding
Run `build.bat`. Source: `ppap_pdf_editor.py` (Python 3.13, PyMuPDF, PySide6, PyInstaller).
