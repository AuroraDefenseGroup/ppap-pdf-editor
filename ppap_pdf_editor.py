#!/usr/bin/env python3
"""PPAP PDF Editor

A Windows desktop PDF viewer/editor for building PPAP / FAI ballooned drawings.

  * Read PDFs (continuous scroll, zoom, find text, print)
  * Edit existing PDF text, remove text, white-out areas
  * Add text boxes, rectangles, lines and arrows
  * Add numbered inspection balloons (circle, square, triangle, diamond) with
    optional leader lines, auto-incrementing numbers and a balloon list that
    exports to CSV for the dimensional results sheet.

Markups are written into the PDF as standard annotations (visible in Acrobat
and every other viewer) and a copy is embedded in the file so they stay
editable when the PDF is reopened here.  "Export Flattened PDF" burns them
into the page content permanently.
"""

import copy
import csv
import json
import math
import os
import re
import sys

import pymupdf
from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import (QAction, QActionGroup, QBrush, QColor, QFont,
                           QFontMetricsF, QIcon, QImage, QKeySequence,
                           QPainter, QPainterPath, QPainterPathStroker, QPen,
                           QPixmap, QPolygonF,
                           QTransform)
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6 import QtWidgets as W

APP_NAME = "PPAP PDF Editor"
APP_VERSION = "1.0"
TAG = "PPAP-Editor"              # annotation title used to recognise our markups
EMB_NAME = "ppap_markups.json"   # embedded file holding the editable markups
PAGE_GAP = 16
MAX_PIXMAP = 9000                # max rendered page dimension in pixels
UNDO_LIMIT = 60

REDACT_IMAGE_NONE = getattr(pymupdf, "PDF_REDACT_IMAGE_NONE", 0)
REDACT_IMAGE_PIXELS = getattr(pymupdf, "PDF_REDACT_IMAGE_PIXELS", 2)
REDACT_LINE_NONE = getattr(pymupdf, "PDF_REDACT_LINE_ART_NONE", 0)
REDACT_LINE_COVERED = getattr(pymupdf, "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", 1)
REDACT_TEXT_REMOVE = getattr(pymupdf, "PDF_REDACT_TEXT_REMOVE", 0)

BASE14 = {
    "Helvetica": ("helv", "hebo", "heit", "hebi"),
    "Times": ("tiro", "tibo", "tiit", "tibi"),
    "Courier": ("cour", "cobo", "coit", "cobi"),
}
QT_FAMILY = {"Helvetica": "Arial", "Times": "Times New Roman", "Courier": "Courier New"}

SHAPES = ["circle", "square", "triangle", "diamond"]
LEADER_ENDS = ["dot", "arrow", "none"]

DEFAULT_BALLOON = {
    "shape": "circle", "r": 9.0, "fs": 10.0, "stroke": "#d00000",
    "fill": "#ffffff", "text": "#d00000", "width": 1.2, "end": "dot",
}
DEFAULT_MARKUP = {"stroke": "#d00000", "fill": None, "width": 1.5, "fs": 10.0,
                  "opacity": 1.0}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def rgb(hexcolor):
    c = QColor(hexcolor)
    return (c.redF(), c.greenF(), c.blueF())


def int_to_hex(value):
    return "#%06x" % (int(value) & 0xFFFFFF)


def natural_key(s):
    parts = re.split(r"(\d+)", str(s))
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def font_px(family="Helvetica", bold=False, italic=False):
    """Qt font with a 100 px em; painters scale it to the wanted PDF size."""
    f = QFont(QT_FAMILY.get(family, "Arial"))
    f.setPixelSize(100)
    f.setBold(bold)
    f.setItalic(italic)
    return f


def balloon_extent(d):
    return d["r"] * {"triangle": 1.5, "diamond": 1.35}.get(d["shape"], 1.0)


def balloon_poly(d):
    """Polygon (local coords around 0,0) or None for a circle."""
    r = d["r"]
    s = d["shape"]
    if s == "square":
        return [(-r, -r), (r, -r), (r, r), (-r, r)]
    if s == "triangle":
        k = r * 1.5
        return [(0, -k), (k * 0.866, k * 0.5), (-k * 0.866, k * 0.5)]
    if s == "diamond":
        k = r * 1.35
        return [(0, -k), (k, 0), (0, k), (-k, 0)]
    return None


def balloon_text_center(d):
    """Where the number sits inside the shape (local coords)."""
    if d["shape"] == "triangle":
        return 0.0, d["r"] * 0.18
    return 0.0, 0.0


def balloon_fs(d):
    n = max(1, len(str(d["num"])))
    room = 2 * d["r"] * {"triangle": 0.62, "diamond": 0.75}.get(d["shape"], 0.82)
    return max(3.0, min(d["fs"], room / (0.58 * n)))


def leader_edge(d, lx, ly):
    """Point on the balloon outline in the direction of (lx, ly)."""
    dist = math.hypot(lx, ly)
    if dist < 1e-6:
        return None
    ux, uy = lx / dist, ly / dist
    poly = balloon_poly(d)
    if poly is None:
        t = d["r"]
    else:
        t = None
        n = len(poly)
        for i in range(n):
            ax, ay = poly[i]
            bx, by = poly[(i + 1) % n]
            ex, ey = bx - ax, by - ay
            den = ux * ey - uy * ex
            if abs(den) < 1e-9:
                continue
            tt = (ax * ey - ay * ex) / den
            ss = (ax * uy - ay * ux) / den
            if tt > 0 and -1e-6 <= ss <= 1 + 1e-6:
                t = tt if t is None else min(t, tt)
        if t is None:
            t = d["r"]
    if dist <= t:
        return None
    return ux * t, uy * t


def text_box_size(d):
    fm = QFontMetricsF(font_px())
    k = d["fs"] / 100.0
    lines = d["text"].split("\n") or [""]
    w = max(fm.horizontalAdvance(line) for line in lines) * k
    h = len(lines) * fm.lineSpacing() * k
    pad = 2.0
    return w + 2 * pad, h + 2 * pad


def make_app_icon(size=256):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("white"))
    p.setPen(QPen(QColor("#9aa3ad"), size * 0.03))
    p.drawRoundedRect(QRectF(size * .12, size * .04, size * .66, size * .86), size * .05, size * .05)
    p.setPen(QPen(QColor("#9aa3ad"), size * 0.025))
    for i in range(5):
        y = size * (0.22 + i * 0.1)
        p.drawLine(QPointF(size * .2, y), QPointF(size * .6, y))
    p.setPen(QPen(QColor("#d00000"), size * 0.05))
    p.setBrush(QColor("white"))
    c = QPointF(size * .64, size * .66)
    p.drawEllipse(c, size * .28, size * .28)
    f = QFont("Arial")
    f.setPixelSize(int(size * .34))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#d00000"))
    p.drawText(QRectF(c.x() - size * .28, c.y() - size * .28, size * .56, size * .56), Qt.AlignCenter, "1")
    p.end()
    return pm


# --------------------------------------------------------------------------
# small widgets
# --------------------------------------------------------------------------

class ColorButton(W.QToolButton):
    """Colour swatch button. allow_none adds a 'No colour' option."""

    def __init__(self, color, allow_none=False, tooltip="", parent=None):
        super().__init__(parent)
        self.allow_none = allow_none
        self._color = color
        self.setToolTip(tooltip)
        self.setFixedSize(QSize(30, 24))
        self.callbacks = []
        if allow_none:
            menu = W.QMenu(self)
            menu.addAction("Choose colour…", self.choose)
            menu.addAction("No colour", lambda: self.set_color(None, notify=True))
            self.setMenu(menu)
            self.setPopupMode(W.QToolButton.InstantPopup)
        else:
            self.clicked.connect(self.choose)
        self.refresh()

    def color(self):
        return self._color

    def set_color(self, color, notify=False):
        self._color = color
        self.refresh()
        if notify:
            for cb in self.callbacks:
                cb(color)

    def choose(self):
        c = W.QColorDialog.getColor(QColor(self._color or "#ffffff"), self, "Choose colour")
        if c.isValid():
            self.set_color(c.name(), notify=True)

    def refresh(self):
        pm = QPixmap(20, 14)
        if self._color:
            pm.fill(QColor(self._color))
        else:
            pm.fill(QColor("white"))
            p = QPainter(pm)
            p.setPen(QPen(QColor("#d00000"), 2))
            p.drawLine(0, 14, 20, 0)
            p.end()
        p = QPainter(pm)
        p.setPen(QColor("#555"))
        p.drawRect(0, 0, 19, 13)
        p.end()
        self.setIcon(QIcon(pm))
        self.setIconSize(QSize(20, 14))


def dspin(value, lo, hi, step=0.5, decimals=1, suffix=""):
    s = W.QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(decimals)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    return s


# --------------------------------------------------------------------------
# dialogs
# --------------------------------------------------------------------------

class MarkupDialog(W.QDialog):
    """Edit the properties of one markup."""

    def __init__(self, parent, d):
        super().__init__(parent)
        self.d = copy.deepcopy(d)
        t = d["type"]
        self.setWindowTitle({"balloon": "Balloon", "text": "Text Box", "rect": "Rectangle",
                             "line": "Line / Arrow"}[t])
        form = W.QFormLayout(self)
        self.f = {}
        if t == "balloon":
            self.f["num"] = W.QLineEdit(str(d["num"]))
            self.f["char"] = W.QLineEdit(d.get("char", ""))
            self.f["note"] = W.QLineEdit(d.get("note", ""))
            self.f["shape"] = W.QComboBox()
            self.f["shape"].addItems([s.title() for s in SHAPES])
            self.f["shape"].setCurrentIndex(SHAPES.index(d["shape"]))
            self.f["dia"] = dspin(d["r"] * 2, 6, 200, 1, 1, " pt")
            self.f["fs"] = dspin(d["fs"], 3, 100, 0.5, 1, " pt")
            self.f["stroke"] = ColorButton(d["stroke"])
            self.f["fill"] = ColorButton(d["fill"], allow_none=True)
            self.f["text"] = ColorButton(d["text"])
            self.f["width"] = dspin(d["width"], 0.2, 10, 0.2, 1, " pt")
            self.f["end"] = W.QComboBox()
            self.f["end"].addItems([e.title() for e in LEADER_ENDS])
            self.f["end"].setCurrentIndex(LEADER_ENDS.index(d.get("end", "dot")))
            self.f["leader"] = W.QCheckBox("Has leader line")
            self.f["leader"].setChecked(bool(d.get("leader")))
            self.f["leader"].setEnabled(bool(d.get("leader")))
            for label, key in (("Number", "num"), ("Characteristic", "char"), ("Notes", "note"),
                               ("Shape", "shape"), ("Diameter", "dia"), ("Font size", "fs"),
                               ("Outline colour", "stroke"), ("Fill colour", "fill"),
                               ("Number colour", "text"), ("Line width", "width"),
                               ("Leader end", "end"), ("", "leader")):
                form.addRow(label, self.f[key])
        elif t == "text":
            self.f["text"] = W.QPlainTextEdit(d["text"])
            self.f["text"].setMinimumSize(320, 110)
            self.f["fs"] = dspin(d["fs"], 3, 200, 0.5, 1, " pt")
            self.f["color"] = ColorButton(d["color"])
            self.f["fill"] = ColorButton(d.get("fill"), allow_none=True)
            self.f["border"] = ColorButton(d.get("border"), allow_none=True)
            for label, key in (("Text", "text"), ("Font size", "fs"), ("Text colour", "color"),
                               ("Background", "fill"), ("Border", "border")):
                form.addRow(label, self.f[key])
        elif t == "rect":
            self.f["stroke"] = ColorButton(d.get("stroke"), allow_none=True)
            self.f["fill"] = ColorButton(d.get("fill"), allow_none=True)
            self.f["width"] = dspin(d["width"], 0.2, 20, 0.2, 1, " pt")
            self.f["opacity"] = W.QSpinBox()
            self.f["opacity"].setRange(5, 100)
            self.f["opacity"].setSuffix(" %")
            self.f["opacity"].setValue(int(round(d.get("opacity", 1) * 100)))
            for label, key in (("Outline colour", "stroke"), ("Fill colour", "fill"),
                               ("Line width", "width"), ("Opacity", "opacity")):
                form.addRow(label, self.f[key])
        elif t == "line":
            self.f["stroke"] = ColorButton(d["stroke"])
            self.f["width"] = dspin(d["width"], 0.2, 20, 0.2, 1, " pt")
            self.f["arrow"] = W.QCheckBox("Arrow head")
            self.f["arrow"].setChecked(d.get("arrow", False))
            for label, key in (("Colour", "stroke"), ("Line width", "width"), ("", "arrow")):
                form.addRow(label, self.f[key])
        bb = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def result_data(self):
        d, f = self.d, self.f
        t = d["type"]
        if t == "balloon":
            d["num"] = f["num"].text().strip() or d["num"]
            d["char"] = f["char"].text()
            d["note"] = f["note"].text()
            d["shape"] = SHAPES[f["shape"].currentIndex()]
            d["r"] = f["dia"].value() / 2
            d["fs"] = f["fs"].value()
            d["stroke"] = f["stroke"].color()
            d["fill"] = f["fill"].color()
            d["text"] = f["text"].color()
            d["width"] = f["width"].value()
            d["end"] = LEADER_ENDS[f["end"].currentIndex()]
            if not f["leader"].isChecked():
                d["leader"] = None
        elif t == "text":
            d["text"] = f["text"].toPlainText().rstrip("\n")
            d["fs"] = f["fs"].value()
            d["color"] = f["color"].color()
            d["fill"] = f["fill"].color()
            d["border"] = f["border"].color()
            d["w"], d["h"] = text_box_size(d)
        elif t == "rect":
            d["stroke"] = f["stroke"].color()
            d["fill"] = f["fill"].color()
            d["width"] = f["width"].value()
            d["opacity"] = f["opacity"].value() / 100.0
        elif t == "line":
            d["stroke"] = f["stroke"].color()
            d["width"] = f["width"].value()
            d["arrow"] = f["arrow"].isChecked()
        return d


class EditTextDialog(W.QDialog):
    def __init__(self, parent, text, family, bold, italic, size, color):
        super().__init__(parent)
        self.setWindowTitle("Edit PDF Text")
        form = W.QFormLayout(self)
        self.text = W.QLineEdit(text)
        self.text.setMinimumWidth(360)
        self.family = W.QComboBox()
        self.family.addItems(list(BASE14))
        self.family.setCurrentText(family)
        self.bold = W.QCheckBox("Bold")
        self.bold.setChecked(bold)
        self.italic = W.QCheckBox("Italic")
        self.italic.setChecked(italic)
        style = W.QHBoxLayout()
        style.addWidget(self.family)
        style.addWidget(self.bold)
        style.addWidget(self.italic)
        self.size = dspin(size, 2, 300, 0.5, 1, " pt")
        self.color = ColorButton(color)
        form.addRow("Text", self.text)
        form.addRow("Font", style)
        form.addRow("Size", self.size)
        form.addRow("Colour", self.color)
        note = W.QLabel("The original text is removed and replaced with a standard PDF font.\n"
                        "Leave the text empty to delete it.")
        note.setStyleSheet("color: #666;")
        form.addRow(note)
        bb = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.text.selectAll()

    def values(self):
        return (self.text.text(), self.family.currentText(), self.bold.isChecked(),
                self.italic.isChecked(), self.size.value(), self.color.color())


# --------------------------------------------------------------------------
# graphics items
# --------------------------------------------------------------------------

class PageItem(W.QGraphicsItem):
    def __init__(self, win, index, w, h):
        super().__init__()
        self.win = win
        self.index = index
        self.w = w
        self.h = h
        self.pix = None
        self.pix_scale = 0.0

    def boundingRect(self):
        return QRectF(-4, -4, self.w + 10, self.h + 10)

    def page_rect(self):
        return QRectF(0, 0, self.w, self.h)

    def paint(self, p, opt, widget=None):
        r = self.page_rect()
        p.fillRect(r.translated(3, 3), QColor(0, 0, 0, 70))
        p.fillRect(r, QColor("white"))
        if self.pix is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawPixmap(r, self.pix, QRectF(self.pix.rect()))
        if abs(self.pix_scale - self.win.render_scale()) > 1e-3:
            self.win.request_render(self.index)


class LeaderHandle(W.QGraphicsItem):
    """Draggable end point of a balloon leader line."""

    def __init__(self, balloon):
        super().__init__(balloon)
        self.balloon = balloon
        self._syncing = False
        self.setFlags(W.QGraphicsItem.ItemIsMovable | W.QGraphicsItem.ItemSendsGeometryChanges |
                      W.QGraphicsItem.ItemIgnoresTransformations)
        self.setCursor(Qt.SizeAllCursor)
        self.setZValue(20)
        self.setToolTip("Drag to move the leader end")

    def boundingRect(self):
        return QRectF(-6, -6, 12, 12)

    def paint(self, p, opt, widget=None):
        if self.balloon.isSelected() or self.isUnderMouse():
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor("#0060ff"), 1.5))
            p.setBrush(QColor(255, 255, 255, 200))
            p.drawRect(QRectF(-4, -4, 8, 8))

    def sync(self):
        d = self.balloon.d
        self._syncing = True
        self.setPos(d["leader"][0] - self.balloon.pos().x(), d["leader"][1] - self.balloon.pos().y())
        self._syncing = False

    def itemChange(self, change, value):
        if change == W.QGraphicsItem.ItemPositionHasChanged and not self._syncing:
            b = self.balloon
            b.prepareGeometryChange()
            b.d["leader"] = [b.pos().x() + self.pos().x(), b.pos().y() + self.pos().y()]
            b.update()
        return super().itemChange(change, value)


class MarkupItem(W.QGraphicsItem):
    def __init__(self, win, d):
        super().__init__()
        self.win = win
        self.d = d
        self._syncing = False
        self.setFlags(W.QGraphicsItem.ItemIsSelectable | W.QGraphicsItem.ItemIsMovable |
                      W.QGraphicsItem.ItemSendsGeometryChanges)
        self.setZValue(10)
        self.setAcceptHoverEvents(True)

    def anchor(self):
        return QPointF(self.d["x"], self.d["y"])

    def sync_pos(self):
        self._syncing = True
        self.setPos(self.anchor())
        self._syncing = False

    def moved(self, dx, dy):
        self.d["x"] += dx
        self.d["y"] += dy

    def refresh(self):
        self.prepareGeometryChange()
        self.sync_pos()
        self.update()

    def itemChange(self, change, value):
        if change == W.QGraphicsItem.ItemPositionHasChanged and not self._syncing:
            a = self.anchor()
            self.prepareGeometryChange()
            self.moved(self.pos().x() - a.x(), self.pos().y() - a.y())
            self.after_move()
        return super().itemChange(change, value)

    def after_move(self):
        pass

    def mouseDoubleClickEvent(self, e):
        self.win.edit_markup(self)

    def contextMenuEvent(self, e):
        if not self.isSelected():
            self.scene().clearSelection()
            self.setSelected(True)
        m = W.QMenu()
        m.addAction("Edit…", lambda: self.win.edit_markup(self))
        m.addAction("Delete", self.win.delete_selected)
        m.exec(e.screenPos())

    def draw_selection(self, p, rect):
        if self.isSelected():
            pen = QPen(QColor("#0060ff"), 0, Qt.DashLine)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(rect)


class BalloonItem(MarkupItem):
    def __init__(self, win, d):
        super().__init__(win, d)
        self.setZValue(12)
        self.handle = None
        self.sync_pos()
        self.update_handle()

    def update_handle(self):
        if self.d.get("leader"):
            if self.handle is None:
                self.handle = LeaderHandle(self)
            self.handle.sync()
        elif self.handle is not None:
            self.scene() and self.scene().removeItem(self.handle)
            self.handle.setParentItem(None)
            self.handle = None

    def refresh(self):
        super().refresh()
        self.update_handle()

    def moved(self, dx, dy):
        # the balloon moves, the leader end stays on the feature
        self.d["x"] += dx
        self.d["y"] += dy

    def after_move(self):
        if self.handle is not None:
            self.handle.sync()

    def itemChange(self, change, value):
        if change == W.QGraphicsItem.ItemSelectedHasChanged and self.handle is not None:
            self.handle.update()
        return super().itemChange(change, value)

    def boundingRect(self):
        e = balloon_extent(self.d) + self.d["width"] + 2
        r = QRectF(-e, -e, 2 * e, 2 * e)
        if self.d.get("leader"):
            lx = self.d["leader"][0] - self.d["x"]
            ly = self.d["leader"][1] - self.d["y"]
            m = self.d["width"] * 4 + 4
            r = r.united(QRectF(lx - m, ly - m, 2 * m, 2 * m))
        return r

    def shape(self):
        path = QPainterPath()
        e = balloon_extent(self.d) + 1
        path.addEllipse(QPointF(0, 0), e, e)
        return path

    def paint(self, p, opt, widget=None):
        d = self.d
        p.setRenderHint(QPainter.Antialiasing)
        stroke = QColor(d["stroke"])
        pen = QPen(stroke, d["width"])
        pen.setJoinStyle(Qt.MiterJoin)
        if d.get("leader"):
            lx = d["leader"][0] - d["x"]
            ly = d["leader"][1] - d["y"]
            edge = leader_edge(d, lx, ly)
            if edge is not None:
                p.setPen(pen)
                p.drawLine(QPointF(*edge), QPointF(lx, ly))
                end = d.get("end", "dot")
                p.setBrush(stroke)
                if end == "dot":
                    rr = max(1.5, d["width"] * 2.4)
                    p.setPen(Qt.NoPen)
                    p.drawEllipse(QPointF(lx, ly), rr, rr)
                elif end == "arrow":
                    ang = math.atan2(ly - edge[1], lx - edge[0])
                    L = max(5.0, d["width"] * 5)
                    pts = [QPointF(lx, ly),
                           QPointF(lx - L * math.cos(ang - 0.4), ly - L * math.sin(ang - 0.4)),
                           QPointF(lx - L * math.cos(ang + 0.4), ly - L * math.sin(ang + 0.4))]
                    p.setPen(Qt.NoPen)
                    p.drawPolygon(QPolygonF(pts))
        p.setPen(pen)
        p.setBrush(QColor(d["fill"]) if d.get("fill") else Qt.NoBrush)
        poly = balloon_poly(d)
        if poly is None:
            p.drawEllipse(QPointF(0, 0), d["r"], d["r"])
        else:
            p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in poly]))
        fs = balloon_fs(d)
        cx, cy = balloon_text_center(d)
        p.save()
        p.translate(cx, cy)
        p.scale(fs / 100.0, fs / 100.0)
        p.setFont(font_px())
        p.setPen(QColor(d["text"]))
        R = balloon_extent(d) * 100.0 / fs
        p.drawText(QRectF(-R, -R, 2 * R, 2 * R), Qt.AlignCenter, str(d["num"]))
        p.restore()
        e = balloon_extent(d) + d["width"]
        self.draw_selection(p, QRectF(-e, -e, 2 * e, 2 * e))


class TextItem(MarkupItem):
    def __init__(self, win, d):
        super().__init__(win, d)
        self.setZValue(11)
        self.sync_pos()

    def boundingRect(self):
        return QRectF(-2, -2, self.d["w"] + 4, self.d["h"] + 4)

    def paint(self, p, opt, widget=None):
        d = self.d
        r = QRectF(0, 0, d["w"], d["h"])
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(d["border"]), 1) if d.get("border") else Qt.NoPen)
        p.setBrush(QColor(d["fill"]) if d.get("fill") else Qt.NoBrush)
        p.drawRect(r)
        k = d["fs"] / 100.0
        p.save()
        p.translate(2, 2)
        p.scale(k, k)
        p.setFont(font_px())
        p.setPen(QColor(d["color"]))
        p.drawText(QRectF(0, 0, (d["w"] - 4) / k + 50, (d["h"] - 4) / k + 50),
                   Qt.AlignLeft | Qt.AlignTop, d["text"])
        p.restore()
        self.draw_selection(p, r)


class RectItem(MarkupItem):
    def __init__(self, win, d):
        super().__init__(win, d)
        self.sync_pos()

    def boundingRect(self):
        m = self.d["width"] + 2
        return QRectF(-m, -m, self.d["w"] + 2 * m, self.d["h"] + 2 * m)

    def paint(self, p, opt, widget=None):
        d = self.d
        r = QRectF(0, 0, d["w"], d["h"])
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(d.get("opacity", 1.0))
        p.setPen(QPen(QColor(d["stroke"]), d["width"]) if d.get("stroke") else Qt.NoPen)
        p.setBrush(QColor(d["fill"]) if d.get("fill") else Qt.NoBrush)
        p.drawRect(r)
        p.setOpacity(1.0)
        self.draw_selection(p, r)


class LineItem(MarkupItem):
    def __init__(self, win, d):
        super().__init__(win, d)
        self.sync_pos()

    def anchor(self):
        return QPointF(self.d["x1"], self.d["y1"])

    def moved(self, dx, dy):
        for k in ("x1", "x2"):
            self.d[k] += dx
        for k in ("y1", "y2"):
            self.d[k] += dy

    def local_end(self):
        return QPointF(self.d["x2"] - self.d["x1"], self.d["y2"] - self.d["y1"])

    def boundingRect(self):
        e = self.local_end()
        m = self.d["width"] * 5 + 6
        return QRectF(QPointF(0, 0), e).normalized().adjusted(-m, -m, m, m)

    def shape(self):
        path = QPainterPath()
        e = self.local_end()
        path.moveTo(0, 0)
        path.lineTo(e)
        s = QPainterPathStroker()
        s.setWidth(max(6.0, self.d["width"] + 4))
        return s.createStroke(path)

    def paint(self, p, opt, widget=None):
        d = self.d
        e = self.local_end()
        c = QColor(d["stroke"])
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(c, d["width"], Qt.SolidLine, Qt.FlatCap))
        p.drawLine(QPointF(0, 0), e)
        if d.get("arrow"):
            ang = math.atan2(e.y(), e.x())
            L = max(7.0, d["width"] * 6 + 2)
            pts = [e, QPointF(e.x() - L * math.cos(ang - 0.4), e.y() - L * math.sin(ang - 0.4)),
                   QPointF(e.x() - L * math.cos(ang + 0.4), e.y() - L * math.sin(ang + 0.4))]
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawPolygon(QPolygonF(pts))
        if self.isSelected():
            self.draw_selection(p, QRectF(QPointF(0, 0), e).normalized())


ITEM_CLASSES = {"balloon": BalloonItem, "text": TextItem, "rect": RectItem, "line": LineItem}


# --------------------------------------------------------------------------
# the view
# --------------------------------------------------------------------------

class PdfView(W.QGraphicsView):
    DRAG_TOOLS = ("balloon", "rect", "remove", "whiteout", "line", "arrow")

    def __init__(self, win):
        super().__init__(win.scene)
        self.win = win
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        self.setTransformationAnchor(W.QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(W.QGraphicsView.AnchorViewCenter)
        self.setViewportUpdateMode(W.QGraphicsView.FullViewportUpdate)
        self.setBackgroundBrush(QColor("#585d64"))
        self.setDragMode(W.QGraphicsView.RubberBandDrag)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self._pan = None
        self._drag = None
        self._preview = None
        self._snapshot = None
        self._hover = None

    # drag & drop a PDF onto the window
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            self.win.open_file(urls[0].toLocalFile())

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.win.zoom_by(1.0015 ** e.angleDelta().y())
            e.accept()
        else:
            super().wheelEvent(e)

    def _scene_pt(self, e):
        return self.mapToScene(e.position().toPoint())

    def mousePressEvent(self, e):
        tool = self.win.tool
        if e.button() == Qt.MiddleButton or (e.button() == Qt.LeftButton and tool == "hand"):
            self._pan = e.position()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            return
        if e.button() != Qt.LeftButton:
            super().mousePressEvent(e)
            return
        if tool == "select":
            self._snapshot = self.win.markup_snapshot()
            super().mousePressEvent(e)
            return
        sp = self._scene_pt(e)
        pi, lp = self.win.page_at(sp)
        if pi is None:
            return
        if tool in self.DRAG_TOOLS:
            self._drag = (pi, lp)
            self._make_preview(tool, pi, lp)
        elif tool == "text":
            self.win.place_text(pi, lp)
        elif tool == "edittext":
            self._clear_hover()
            self.win.edit_text_at(pi, lp)

    def mouseDoubleClickEvent(self, e):
        if self.win.tool == "select":
            super().mouseDoubleClickEvent(e)
        else:
            self.mousePressEvent(e)

    def _make_preview(self, tool, pi, lp):
        pen = QPen(QColor("#0060ff"), 1.5, Qt.DashLine)
        pen.setCosmetic(True)
        if tool in ("line", "arrow", "balloon"):
            it = W.QGraphicsLineItem(lp.x(), lp.y(), lp.x(), lp.y(), pi)
        else:
            it = W.QGraphicsRectItem(QRectF(lp, lp), pi)
            if tool == "whiteout":
                it.setBrush(QColor(255, 255, 255, 200))
            elif tool == "remove":
                it.setBrush(QColor(255, 0, 0, 40))
        it.setPen(pen)
        it.setZValue(100)
        self._preview = it

    def mouseMoveEvent(self, e):
        if self._pan is not None:
            delta = e.position() - self._pan
            self._pan = e.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            return
        sp = self._scene_pt(e)
        pi, lp = self.win.page_at(sp)
        self.win.show_cursor_pos(pi, lp)
        if self._drag is not None:
            dpi, p0 = self._drag
            p1 = self.win.clamp_to_page(dpi, dpi.mapFromScene(sp))
            if isinstance(self._preview, W.QGraphicsLineItem):
                self._preview.setLine(p0.x(), p0.y(), p1.x(), p1.y())
            elif self._preview is not None:
                self._preview.setRect(QRectF(p0, p1).normalized())
            return
        if self.win.tool == "edittext":
            self._update_hover(pi, lp)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._pan is not None:
            self._pan = None
            self.win.update_cursor()
            return
        if self._drag is not None:
            dpi, p0 = self._drag
            p1 = self.win.clamp_to_page(dpi, dpi.mapFromScene(self._scene_pt(e)))
            if self._preview is not None:
                self.scene().removeItem(self._preview)
                self._preview = None
            self._drag = None
            self.win.finish_drag(self.win.tool, dpi, p0, p1)
            return
        super().mouseReleaseEvent(e)
        if self._snapshot is not None:
            self.win.commit_if_changed(self._snapshot)
            self._snapshot = None

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            if self._drag is not None:
                if self._preview is not None:
                    self.scene().removeItem(self._preview)
                    self._preview = None
                self._drag = None
            else:
                self.win.set_tool("select")
            return
        arrows = {Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0), Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1)}
        sel = self.win.selected_items()
        if k in arrows and sel:
            step = 10 if e.modifiers() & Qt.ShiftModifier else 1
            snap = self.win.markup_snapshot()
            dx, dy = arrows[k]
            for it in sel:
                it.setPos(it.pos() + QPointF(dx * step, dy * step))
            self.win.commit_if_changed(snap)
            return
        super().keyPressEvent(e)

    def _clear_hover(self):
        if self._hover is not None:
            if self._hover.scene():
                self.scene().removeItem(self._hover)
            self._hover = None

    def _update_hover(self, pi, lp):
        self._clear_hover()
        if pi is None:
            return
        hit = self.win.span_at(pi.index, lp)
        if hit is None:
            return
        r = self.win.span_display_rect(pi.index, hit[0])
        it = W.QGraphicsRectItem(r, pi)
        pen = QPen(QColor("#0060ff"), 1)
        pen.setCosmetic(True)
        it.setPen(pen)
        it.setBrush(QColor(0, 96, 255, 35))
        it.setZValue(90)
        self._hover = it


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class MainWindow(W.QMainWindow):
    TOOLS = [
        ("select", "Select", "V", "Select, move and double-click markups to edit them. Delete removes them."),
        ("hand", "Pan", "H", "Drag to scroll the page (middle mouse button also pans)."),
        ("balloon", "Balloon", "B", "Click to drop a numbered balloon, or press on the feature and drag "
                                    "out to place a balloon with a leader line."),
        ("text", "Add Text", "T", "Click where the new text box should go."),
        ("edittext", "Edit Text", "E", "Click existing PDF text to change it."),
        ("remove", "Remove Text", "X", "Drag a box around PDF text to delete it (graphics are kept)."),
        ("whiteout", "Whiteout", "W", "Drag a box to erase everything inside it (text, lines, images)."),
        ("rect", "Rectangle", "R", "Drag to draw a rectangle."),
        ("arrow", "Arrow", "A", "Drag to draw an arrow."),
        ("line", "Line", "L", "Drag to draw a line."),
    ]

    def __init__(self):
        super().__init__()
        self.settings = QSettings("ADG Engineering", "PPAP PDF Editor")
        self.doc = None
        self.path = None
        self.pages = []
        self.items = []
        self.undo_stack = []
        self.redo_stack = []
        self.dirty = False
        self.zoom = 1.0
        self.tool = "select"
        self.text_cache = {}
        self.pending = set()
        self.find_hits = []
        self.find_index = -1
        self.find_items = []
        self._table_guard = False
        self._table_snapshot = None

        self.balloon_style = dict(DEFAULT_BALLOON)
        self.balloon_style.update(self._load_json_setting("balloon_style"))
        self.markup_style = dict(DEFAULT_MARKUP)
        self.markup_style.update(self._load_json_setting("markup_style"))

        self.setWindowIcon(QIcon(make_app_icon()))
        self.scene = W.QGraphicsScene(self)
        self.scene.selectionChanged.connect(self.on_scene_selection)
        self.view = PdfView(self)
        self.setCentralWidget(self.view)
        self.render_timer = QTimer(self, singleShot=True, interval=30, timeout=self.render_pending)
        self.view.verticalScrollBar().valueChanged.connect(self.on_scrolled)
        self.view.horizontalScrollBar().valueChanged.connect(self.on_scrolled)

        self._build_actions()
        self._build_toolbars()
        self._build_dock()
        self._build_status()
        self.set_tool("select")
        self.update_title()
        self.update_enabled()

        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        else:
            self.resize(1400, 900)

    # ---------------------------------------------------------------- setup
    def _load_json_setting(self, key):
        try:
            return json.loads(self.settings.value(key, "{}"))
        except Exception:
            return {}

    def _act(self, text, slot, shortcut=None, tip=None, icon=None):
        a = QAction(text, self)
        if icon is not None:
            a.setIcon(self.style().standardIcon(icon))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setStatusTip(tip)
            a.setToolTip(tip)
        a.triggered.connect(slot)
        return a

    def _build_actions(self):
        S = W.QStyle
        self.a_open = self._act("&Open…", lambda: self.open_file(), QKeySequence.Open,
                                "Open a PDF (or an image of a drawing)", S.SP_DialogOpenButton)
        self.a_save = self._act("&Save", self.save, QKeySequence.Save, "Save the PDF", S.SP_DialogSaveButton)
        self.a_save_as = self._act("Save &As…", self.save_as, QKeySequence.SaveAs)
        self.a_flat = self._act("Export &Flattened PDF…", self.export_flattened, None,
                                "Save a copy with all markups burned into the page")
        self.a_print = self._act("&Print…", self.print_doc, QKeySequence.Print, "Print with markups")
        self.a_close = self._act("&Close", self.close_doc, "Ctrl+W")
        self.a_exit = self._act("E&xit", self.close, "Alt+F4")

        self.a_undo = self._act("&Undo", self.undo, QKeySequence.Undo, "Undo", S.SP_ArrowBack)
        self.a_redo = self._act("&Redo", self.redo, QKeySequence.Redo, "Redo", S.SP_ArrowForward)
        self.a_redo.setShortcuts([QKeySequence.Redo, QKeySequence("Ctrl+Y")])
        self.a_delete = self._act("&Delete Markup", self.delete_selected, QKeySequence.Delete)
        self.a_selall = self._act("Select &All Markups", self.select_all, QKeySequence.SelectAll)
        self.a_renumber = self._act("&Renumber Balloons 1…n", self.renumber_balloons)
        self.a_restyle = self._act("Apply Balloon Style to &All Balloons", self.restyle_balloons)
        self.a_csv = self._act("&Export Balloon List (CSV)…", self.export_csv)

        self.a_zin = self._act("Zoom &In", lambda: self.zoom_by(1.25), QKeySequence.ZoomIn)
        self.a_zin.setShortcuts([QKeySequence("Ctrl+="), QKeySequence("Ctrl++")])
        self.a_zout = self._act("Zoom &Out", lambda: self.zoom_by(0.8), QKeySequence.ZoomOut)
        self.a_fitw = self._act("Fit &Width", self.fit_width, "Ctrl+2")
        self.a_fitp = self._act("Fit &Page", self.fit_page, "Ctrl+1")
        self.a_actual = self._act("&Actual Size", lambda: self.set_zoom(96 / 72), "Ctrl+0")
        self.a_find = self._act("&Find…", self.focus_find, QKeySequence.Find)

        self.tool_group = QActionGroup(self)
        self.tool_actions = {}
        for key, label, sc, tip in self.TOOLS:
            a = QAction(label, self, checkable=True)
            a.setShortcut(QKeySequence(sc))
            a.setToolTip(f"{label} ({sc})\n{tip}")
            a.setStatusTip(tip)
            a.triggered.connect(lambda checked, k=key: self.set_tool(k))
            self.tool_group.addAction(a)
            self.tool_actions[key] = a

        mb = self.menuBar()
        m = mb.addMenu("&File")
        for a in (self.a_open, self.a_save, self.a_save_as, self.a_flat, None, self.a_csv, None,
                  self.a_print, None, self.a_close, self.a_exit):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Edit")
        for a in (self.a_undo, self.a_redo, None, self.a_delete, self.a_selall, None,
                  self.a_renumber, self.a_restyle, None, self.a_find):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&View")
        for a in (self.a_zin, self.a_zout, self.a_actual, self.a_fitw, self.a_fitp):
            m.addAction(a)
        self.view_menu = m
        m = mb.addMenu("&Tools")
        for a in self.tool_actions.values():
            m.addAction(a)
        m = mb.addMenu("&Help")
        m.addAction("&Quick Guide", self.show_help, "F1")
        m.addAction("&About", self.show_about)

    def _build_toolbars(self):
        tb = self.addToolBar("File")
        tb.setObjectName("file_tb")
        tb.setIconSize(QSize(20, 20))
        for a in (self.a_open, self.a_save, self.a_undo, self.a_redo):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.a_zout)
        self.zoom_label = W.QLabel(" 100% ")
        self.zoom_label.setMinimumWidth(48)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        tb.addWidget(self.zoom_label)
        tb.addAction(self.a_zin)
        tb.addAction(self.a_fitw)
        tb.addAction(self.a_fitp)
        tb.addSeparator()
        tb.addWidget(W.QLabel(" Page "))
        self.page_spin = W.QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.valueChanged.connect(lambda v: self.goto_page(v - 1))
        tb.addWidget(self.page_spin)
        self.page_total = W.QLabel(" of 0 ")
        tb.addWidget(self.page_total)
        tb.addSeparator()
        self.find_edit = W.QLineEdit()
        self.find_edit.setPlaceholderText("Find text…  (Enter = next)")
        self.find_edit.setClearButtonEnabled(True)
        self.find_edit.setMaximumWidth(220)
        self.find_edit.returnPressed.connect(self.find_next)
        self.find_edit.textChanged.connect(self.find_changed)
        tb.addWidget(self.find_edit)
        self.find_label = W.QLabel("")
        tb.addWidget(self.find_label)

        self.addToolBarBreak()
        tt = self.addToolBar("Tools")
        tt.setObjectName("tools_tb")
        tt.setToolButtonStyle(Qt.ToolButtonTextOnly)
        for key, *_ in self.TOOLS:
            tt.addAction(self.tool_actions[key])
            if key in ("hand", "balloon", "whiteout"):
                tt.addSeparator()

        self.addToolBarBreak()
        bt = self.addToolBar("Balloon")
        bt.setObjectName("balloon_tb")
        bs = self.balloon_style
        bt.addWidget(W.QLabel(" Balloon  Next # "))
        self.next_num = W.QSpinBox()
        self.next_num.setRange(1, 99999)
        self.next_num.setValue(1)
        bt.addWidget(self.next_num)
        bt.addWidget(W.QLabel("  Shape "))
        self.b_shape = W.QComboBox()
        self.b_shape.addItems([s.title() for s in SHAPES])
        self.b_shape.setCurrentIndex(SHAPES.index(bs["shape"]))
        self.b_shape.currentIndexChanged.connect(lambda i: self._set_bstyle("shape", SHAPES[i]))
        bt.addWidget(self.b_shape)
        bt.addWidget(W.QLabel("  Size "))
        self.b_dia = dspin(bs["r"] * 2, 6, 200, 1, 0, " pt")
        self.b_dia.valueChanged.connect(lambda v: self._set_bstyle("r", v / 2))
        bt.addWidget(self.b_dia)
        bt.addWidget(W.QLabel("  Font "))
        self.b_fs = dspin(bs["fs"], 3, 100, 0.5, 1, " pt")
        self.b_fs.valueChanged.connect(lambda v: self._set_bstyle("fs", v))
        bt.addWidget(self.b_fs)
        bt.addWidget(W.QLabel("  Colour "))
        self.b_color = ColorButton(bs["stroke"], tooltip="Balloon outline and number colour")
        self.b_color.callbacks.append(lambda c: (self._set_bstyle("stroke", c), self._set_bstyle("text", c)))
        bt.addWidget(self.b_color)
        bt.addWidget(W.QLabel(" Fill "))
        self.b_fill = ColorButton(bs["fill"], allow_none=True, tooltip="Balloon fill colour")
        self.b_fill.callbacks.append(lambda c: self._set_bstyle("fill", c))
        bt.addWidget(self.b_fill)
        bt.addWidget(W.QLabel("  Line "))
        self.b_width = dspin(bs["width"], 0.2, 10, 0.2, 1, " pt")
        self.b_width.valueChanged.connect(lambda v: self._set_bstyle("width", v))
        bt.addWidget(self.b_width)
        bt.addWidget(W.QLabel("  Leader end "))
        self.b_end = W.QComboBox()
        self.b_end.addItems([e.title() for e in LEADER_ENDS])
        self.b_end.setCurrentIndex(LEADER_ENDS.index(bs.get("end", "dot")))
        self.b_end.currentIndexChanged.connect(lambda i: self._set_bstyle("end", LEADER_ENDS[i]))
        bt.addWidget(self.b_end)
        self.b_capture = W.QCheckBox("Capture nearby text")
        self.b_capture.setToolTip("Fill the balloon's Characteristic with the PDF text at the\n"
                                  "leader end (or under the balloon).")
        self.b_capture.setChecked(self.settings.value("capture", "true") == "true")
        self.b_capture.toggled.connect(lambda v: self.settings.setValue("capture", "true" if v else "false"))
        bt.addWidget(self.b_capture)

        mt = self.addToolBar("Markup")
        mt.setObjectName("markup_tb")
        ms = self.markup_style
        mt.addWidget(W.QLabel(" Text / Shapes  Colour "))
        self.m_color = ColorButton(ms["stroke"], tooltip="Text, line and outline colour")
        self.m_color.callbacks.append(lambda c: self._set_mstyle("stroke", c))
        mt.addWidget(self.m_color)
        mt.addWidget(W.QLabel(" Fill "))
        self.m_fill = ColorButton(ms["fill"], allow_none=True, tooltip="Text box / rectangle fill")
        self.m_fill.callbacks.append(lambda c: self._set_mstyle("fill", c))
        mt.addWidget(self.m_fill)
        mt.addWidget(W.QLabel("  Line "))
        self.m_width = dspin(ms["width"], 0.2, 20, 0.2, 1, " pt")
        self.m_width.valueChanged.connect(lambda v: self._set_mstyle("width", v))
        mt.addWidget(self.m_width)
        mt.addWidget(W.QLabel("  Text "))
        self.m_fs = dspin(ms["fs"], 3, 200, 0.5, 1, " pt")
        self.m_fs.valueChanged.connect(lambda v: self._set_mstyle("fs", v))
        mt.addWidget(self.m_fs)

        self.view_menu.addSeparator()
        for t in (tb, tt, bt, mt):
            self.view_menu.addAction(t.toggleViewAction())

    def _set_bstyle(self, key, value):
        self.balloon_style[key] = value
        self.settings.setValue("balloon_style", json.dumps(self.balloon_style))

    def _set_mstyle(self, key, value):
        self.markup_style[key] = value
        self.settings.setValue("markup_style", json.dumps(self.markup_style))

    def _build_dock(self):
        dock = W.QDockWidget("Balloon List", self)
        dock.setObjectName("balloon_dock")
        w = W.QWidget()
        lay = W.QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        self.table = W.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Page", "Characteristic", "Notes"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(W.QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, W.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, W.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, W.QHeaderView.Stretch)
        hh.setSectionResizeMode(3, W.QHeaderView.Stretch)
        self.table.itemChanged.connect(self.on_table_edit)
        self.table.itemSelectionChanged.connect(self.on_table_select)
        lay.addWidget(self.table)
        row = W.QHBoxLayout()
        for text, slot in (("Renumber 1…n", self.renumber_balloons), ("Export CSV…", self.export_csv),
                           ("Delete", self.delete_selected)):
            b = W.QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)
        self.table_count = W.QLabel("0 balloons")
        lay.addWidget(self.table_count)
        dock.setWidget(w)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        dock.resize(380, 600)
        self.resizeDocks([dock], [380], Qt.Horizontal)
        self.view_menu.addSeparator()
        self.view_menu.addAction(dock.toggleViewAction())

    def _build_status(self):
        sb = self.statusBar()
        self.pos_label = W.QLabel("")
        sb.addPermanentWidget(self.pos_label)
        self.tool_label = W.QLabel("")
        sb.addWidget(self.tool_label, 1)

    # ---------------------------------------------------------------- state
    def update_title(self):
        name = os.path.basename(self.path) if self.path else ("Untitled" if self.doc else "")
        star = " *" if self.dirty else ""
        self.setWindowTitle(f"{name}{star} - {APP_NAME}" if name else APP_NAME)

    def update_enabled(self):
        has = self.doc is not None
        for a in (self.a_save, self.a_save_as, self.a_flat, self.a_print, self.a_close, self.a_csv,
                  self.a_zin, self.a_zout, self.a_fitw, self.a_fitp, self.a_actual, self.a_selall,
                  self.a_renumber, self.a_restyle):
            a.setEnabled(has)
        self.a_undo.setEnabled(bool(self.undo_stack))
        self.a_redo.setEnabled(bool(self.redo_stack))

    def set_tool(self, key):
        self.tool = key
        self.tool_actions[key].setChecked(True)
        if key != "edittext":
            self.view._clear_hover()
        tip = next(t[3] for t in self.TOOLS if t[0] == key)
        self.tool_label.setText(tip)
        self.update_cursor()
        if key != "select":
            self.scene.clearSelection()

    def update_cursor(self):
        cur = {"select": Qt.ArrowCursor, "hand": Qt.OpenHandCursor, "edittext": Qt.IBeamCursor}
        self.view.viewport().setCursor(cur.get(self.tool, Qt.CrossCursor))
        self.view.setDragMode(W.QGraphicsView.RubberBandDrag if self.tool == "select"
                              else W.QGraphicsView.NoDrag)

    # ---------------------------------------------------------------- file
    def open_file(self, path=None):
        if not self.maybe_save():
            return
        if not path:
            start = self.settings.value("last_dir", os.path.expanduser("~"))
            path, _ = W.QFileDialog.getOpenFileName(
                self, "Open PDF", start,
                "PDF files (*.pdf);;Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp);;All files (*.*)")
            if not path:
                return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            is_pdf = data[:1024].lstrip().startswith(b"%PDF") or path.lower().endswith(".pdf")
            if is_pdf:
                doc = pymupdf.open(stream=data, filetype="pdf")
            else:
                img = pymupdf.open(path)
                doc = pymupdf.open("pdf", img.convert_to_pdf())
                img.close()
            if doc.needs_pass:
                while True:
                    pw, ok = W.QInputDialog.getText(self, "Password", "This PDF is password protected:",
                                                    W.QLineEdit.Password)
                    if not ok:
                        return
                    if doc.authenticate(pw):
                        break
            if doc.page_count == 0:
                raise RuntimeError("The document has no pages.")
        except Exception as ex:
            W.QMessageBox.critical(self, APP_NAME, f"Could not open the file:\n{path}\n\n{ex}")
            return
        markups = self.extract_saved_markups(doc)
        self.settings.setValue("last_dir", os.path.dirname(path))
        self.load_document(doc, path if is_pdf else None, markups)
        self.statusBar().showMessage(f"Opened {path}", 4000)

    def extract_saved_markups(self, doc):
        """Pull our markups out of the PDF so they can be edited again."""
        markups = []
        try:
            if EMB_NAME in doc.embfile_names():
                payload = json.loads(doc.embfile_get(EMB_NAME).decode("utf-8"))
                markups = [m for m in payload.get("markups", []) if m.get("type") in ITEM_CLASSES
                           and 0 <= m.get("page", -1) < doc.page_count]
                doc.embfile_del(EMB_NAME)
                for page in doc:
                    xrefs = [a.xref for a in page.annots() if a.info.get("title") == TAG]
                    for x in xrefs:
                        page.delete_annot(page.load_annot(x))
        except Exception as ex:
            print("could not read saved markups:", ex, file=sys.stderr)
        return markups

    def load_document(self, doc, path, markups):
        self.clear_find()
        if self.doc is not None:
            self.doc.close()
        self.doc = doc
        self.path = path
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.text_cache.clear()
        self.build_pages()
        self.restore_markups(markups)
        nums = [int(m["num"]) for m in markups if m["type"] == "balloon" and str(m["num"]).isdigit()]
        self.next_num.setValue(max(nums) + 1 if nums else 1)
        self.dirty = False
        self.update_title()
        self.update_enabled()
        QTimer.singleShot(0, self.fit_width)

    def close_doc(self):
        if not self.maybe_save():
            return
        self.clear_find()
        self.scene.clear()
        self.pages = []
        self.items = []
        if self.doc is not None:
            self.doc.close()
        self.doc = None
        self.path = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.dirty = False
        self.page_spin.setRange(1, 1)
        self.page_total.setText(" of 0 ")
        self.refresh_table()
        self.update_title()
        self.update_enabled()

    def build_output(self, flatten):
        out = pymupdf.open("pdf", self.doc.tobytes())
        for it in self.items:
            self.write_markup(out[it.d["page"]], it.d)
        if flatten:
            out.bake(annots=True, widgets=False)
        else:
            payload = json.dumps({"app": APP_NAME, "version": 1,
                                  "markups": self.markup_snapshot()}).encode("utf-8")
            out.embfile_add(EMB_NAME, payload, filename=EMB_NAME,
                            desc="PPAP PDF Editor markups (keeps balloons editable)")
        return out

    def _write(self, path, flatten):
        try:
            out = self.build_output(flatten)
            out.save(path, garbage=3, deflate=True)
            out.close()
            return True
        except Exception as ex:
            W.QMessageBox.critical(self, APP_NAME,
                                   f"Could not save:\n{path}\n\n{ex}\n\n"
                                   "If the file is open in another program, close it and try again.")
            return False

    def save(self):
        if self.doc is None:
            return False
        if not self.path:
            return self.save_as()
        if self._write(self.path, False):
            self.dirty = False
            self.update_title()
            self.statusBar().showMessage(f"Saved {self.path}", 4000)
            return True
        return False

    def _ask_path(self, title, suffix=""):
        base = self.path or os.path.join(self.settings.value("last_dir", os.path.expanduser("~")), "drawing.pdf")
        stem, _ = os.path.splitext(base)
        path, _ = W.QFileDialog.getSaveFileName(self, title, stem + suffix + ".pdf", "PDF files (*.pdf)")
        if path and not path.lower().endswith(".pdf"):
            path += ".pdf"
        return path

    def save_as(self):
        if self.doc is None:
            return False
        path = self._ask_path("Save PDF As")
        if not path:
            return False
        if self._write(path, False):
            self.path = path
            self.dirty = False
            self.settings.setValue("last_dir", os.path.dirname(path))
            self.update_title()
            self.statusBar().showMessage(f"Saved {path}", 4000)
            return True
        return False

    def export_flattened(self):
        if self.doc is None:
            return
        path = self._ask_path("Export Flattened PDF", "_ballooned")
        if path and self._write(path, True):
            self.statusBar().showMessage(f"Exported {path}", 5000)

    def maybe_save(self):
        if self.doc is None or not self.dirty:
            return True
        r = W.QMessageBox.question(self, APP_NAME, "Save changes to this PDF?",
                                   W.QMessageBox.Save | W.QMessageBox.Discard | W.QMessageBox.Cancel)
        if r == W.QMessageBox.Save:
            return self.save()
        return r == W.QMessageBox.Discard

    def closeEvent(self, e):
        if self.maybe_save():
            self.settings.setValue("geometry", self.saveGeometry())
            e.accept()
        else:
            e.ignore()

    # --------------------------------------------------------- writing PDF
    def write_markup(self, page, d):
        """Write one markup as standard PDF annotations (display coords -> PDF)."""
        M = page.derotation_matrix
        rot = page.rotation

        def P(x, y):
            return pymupdf.Point(x, y) * M

        def R(x0, y0, x1, y1):
            return (pymupdf.Rect(x0, y0, x1, y1) * M).normalize()

        def finish(annot, subject, content=None):
            # a FreeText annotation's Contents *is* its visible text - leave it alone
            info = {"title": TAG, "subject": subject}
            if content is not None:
                info["content"] = content
            annot.set_info(**info)
            annot.update()

        t = d["type"]
        if t == "balloon":
            x, y, r = d["x"], d["y"], d["r"]
            stroke, fill = rgb(d["stroke"]), (rgb(d["fill"]) if d.get("fill") else None)
            content = f"Balloon {d['num']}" + (f": {d['char']}" if d.get("char") else "") + \
                      (f" ({d['note']})" if d.get("note") else "")
            if d.get("leader"):
                lx, ly = d["leader"][0] - x, d["leader"][1] - y
                edge = leader_edge(d, lx, ly)
                if edge is not None:
                    a = page.add_line_annot(P(*d["leader"]), P(x + edge[0], y + edge[1]))
                    a.set_colors(stroke=stroke, fill=stroke)
                    a.set_border(width=d["width"])
                    end = {"dot": pymupdf.PDF_ANNOT_LE_CIRCLE, "arrow": pymupdf.PDF_ANNOT_LE_CLOSED_ARROW,
                           "none": pymupdf.PDF_ANNOT_LE_NONE}[d.get("end", "dot")]
                    a.set_line_ends(end, pymupdf.PDF_ANNOT_LE_NONE)
                    finish(a, "balloon leader", content)
            poly = balloon_poly(d)
            if poly is None:
                a = page.add_circle_annot(R(x - r, y - r, x + r, y + r))
            elif d["shape"] == "square":
                a = page.add_rect_annot(R(x - r, y - r, x + r, y + r))
            else:
                a = page.add_polygon_annot([P(x + px, y + py) for px, py in poly])
            a.set_colors(stroke=stroke, fill=fill)
            a.set_border(width=d["width"])
            finish(a, "balloon", content)
            fs = balloon_fs(d)
            cx, cy = balloon_text_center(d)
            hw = balloon_extent(d)
            box = R(x + cx - hw, y + cy - fs * 0.62, x + cx + hw, y + cy + fs * 0.62 + 2)
            a = page.add_freetext_annot(box, str(d["num"]), fontsize=fs, fontname="helv",
                                        text_color=rgb(d["text"]), align=pymupdf.TEXT_ALIGN_CENTER,
                                        rotate=rot)
            finish(a, "balloon number")
        elif t == "text":
            x, y = d["x"], d["y"]
            box = R(x, y, x + d["w"] + 4, y + d["h"] + 2)
            a = page.add_freetext_annot(
                box, d["text"], fontsize=d["fs"], fontname="helv", text_color=rgb(d["color"]),
                fill_color=rgb(d["fill"]) if d.get("fill") else None,
                border_color=rgb(d["border"]) if d.get("border") else None,
                border_width=1 if d.get("border") else 0, rotate=rot)
            finish(a, "text")
        elif t == "rect":
            a = page.add_rect_annot(R(d["x"], d["y"], d["x"] + d["w"], d["y"] + d["h"]))
            a.set_colors(stroke=rgb(d["stroke"]) if d.get("stroke") else None,
                         fill=rgb(d["fill"]) if d.get("fill") else None)
            a.set_border(width=d["width"] if d.get("stroke") else 0)
            a.set_opacity(d.get("opacity", 1.0))
            finish(a, "rectangle")
        elif t == "line":
            a = page.add_line_annot(P(d["x1"], d["y1"]), P(d["x2"], d["y2"]))
            c = rgb(d["stroke"])
            a.set_colors(stroke=c, fill=c)
            a.set_border(width=d["width"])
            if d.get("arrow"):
                a.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_CLOSED_ARROW)
            finish(a, "arrow" if d.get("arrow") else "line")

    # ---------------------------------------------------------------- print
    def print_doc(self):
        if self.doc is None:
            return
        printer = QPrinter(QPrinter.HighResolution)
        dlg = QPrintDialog(printer, self)
        dlg.setMinMax(1, self.doc.page_count)
        if dlg.exec() != W.QDialog.Accepted:
            return
        W.QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            out = self.build_output(flatten=True)
            first = printer.fromPage() or 1
            last = printer.toPage() or out.page_count
            painter = QPainter(printer)
            dpi = min(printer.resolution(), 300)
            for n, i in enumerate(range(first - 1, last)):
                if n:
                    printer.newPage()
                pm = out[i].get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), alpha=False)
                img = QImage(pm.samples, pm.width, pm.height, pm.stride, QImage.Format_RGB888).copy()
                target = painter.viewport()
                if (img.width() > img.height()) != (target.width() > target.height()):
                    img = img.transformed(QTransform().rotate(90))
                s = min(target.width() / img.width(), target.height() / img.height())
                w, h = img.width() * s, img.height() * s
                painter.drawImage(QRectF((target.width() - w) / 2, (target.height() - h) / 2, w, h), img)
            painter.end()
            out.close()
        except Exception as ex:
            W.QMessageBox.critical(self, APP_NAME, f"Printing failed:\n{ex}")
        finally:
            W.QApplication.restoreOverrideCursor()

    # ---------------------------------------------------------------- pages
    def build_pages(self):
        self.scene.clear()
        self.pages = []
        self.items = []
        rects = [p.rect for p in self.doc]
        maxw = max(r.width for r in rects)
        y = 0.0
        for i, r in enumerate(rects):
            pi = PageItem(self, i, r.width, r.height)
            pi.setPos((maxw - r.width) / 2, y)
            self.scene.addItem(pi)
            self.pages.append(pi)
            y += r.height + PAGE_GAP
        self.scene.setSceneRect(QRectF(-40, -40, maxw + 80, y + 40))
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, len(self.pages))
        self.page_spin.setValue(1)
        self.page_spin.blockSignals(False)
        self.page_total.setText(f" of {len(self.pages)} ")

    def render_scale(self):
        return self.zoom * self.view.devicePixelRatioF()

    def request_render(self, i):
        self.pending.add(i)
        if not self.render_timer.isActive():
            self.render_timer.start()

    def visible_pages(self):
        vr = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        return [pi.index for pi in self.pages if pi.sceneBoundingRect().intersects(vr)]

    def render_pending(self):
        if self.doc is None:
            self.pending.clear()
            return
        vis = self.visible_pages()
        want = self.render_scale()
        for i in sorted(self.pending):
            if i in vis and i < len(self.pages):
                pi = self.pages[i]
                if abs(pi.pix_scale - want) < 1e-3:
                    continue
                s = min(want, MAX_PIXMAP / max(pi.w, pi.h))
                try:
                    pm = self.doc[i].get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False)
                    img = QImage(pm.samples, pm.width, pm.height, pm.stride, QImage.Format_RGB888).copy()
                    pi.pix = QPixmap.fromImage(img)
                except Exception as ex:
                    print("render failed", i, ex, file=sys.stderr)
                pi.pix_scale = want
                pi.update()
        self.pending.clear()
        if vis:
            keep = set(range(min(vis) - 2, max(vis) + 3))
            for pi in self.pages:
                if pi.index not in keep and pi.pix is not None:
                    pi.pix = None
                    pi.pix_scale = 0.0

    def invalidate_page(self, i):
        self.text_cache.pop(i, None)
        pi = self.pages[i]
        pi.pix_scale = 0.0
        pi.update()

    def page_at(self, sp):
        for pi in self.pages:
            if pi.sceneBoundingRect().contains(sp):
                lp = pi.mapFromScene(sp)
                if pi.page_rect().contains(lp):
                    return pi, lp
        return None, None

    def clamp_to_page(self, pi, lp):
        return QPointF(min(max(lp.x(), 0), pi.w), min(max(lp.y(), 0), pi.h))

    def current_page(self):
        if not self.pages:
            return 0
        vr = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        best, area = 0, -1
        for pi in self.pages:
            r = pi.sceneBoundingRect().intersected(vr)
            a = r.width() * r.height() if r.isValid() else 0
            if a > area:
                best, area = pi.index, a
        return best

    def on_scrolled(self, *_):
        if not self.pages:
            return
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(self.current_page() + 1)
        self.page_spin.blockSignals(False)

    def goto_page(self, i):
        if not self.pages:
            return
        i = max(0, min(i, len(self.pages) - 1))
        r = self.pages[i].sceneBoundingRect()
        vh = self.view.viewport().height() / self.zoom
        self.view.centerOn(r.center().x(), r.top() - 10 + vh / 2)

    # ---------------------------------------------------------------- zoom
    def set_zoom(self, z, anchor_center=False):
        z = max(0.05, min(z, 16.0))
        if anchor_center:
            self.view.setTransformationAnchor(W.QGraphicsView.AnchorViewCenter)
        self.view.setTransform(QTransform.fromScale(z, z))
        self.view.setTransformationAnchor(W.QGraphicsView.AnchorUnderMouse)
        self.zoom = z
        self.zoom_label.setText(f" {z * 72 / 96 * 100:.0f}% ")
        self.view.viewport().update()

    def zoom_by(self, f):
        if self.doc is not None:
            self.set_zoom(self.zoom * f, anchor_center=not self.view.underMouse())

    def fit_width(self):
        if not self.pages:
            return
        i = self.current_page()
        w = max(pi.w for pi in self.pages)
        self.set_zoom((self.view.viewport().width() - 40) / w, anchor_center=True)
        self.goto_page(i)

    def fit_page(self):
        if not self.pages:
            return
        i = self.current_page()
        pi = self.pages[i]
        vp = self.view.viewport()
        self.set_zoom(min((vp.width() - 40) / pi.w, (vp.height() - 30) / pi.h), anchor_center=True)
        self.view.centerOn(pi.sceneBoundingRect().center())

    def show_cursor_pos(self, pi, lp):
        if pi is None:
            self.pos_label.setText("")
        else:
            self.pos_label.setText(f"Page {pi.index + 1}   X {lp.x() / 72:.2f} in   Y {lp.y() / 72:.2f} in")

    # ---------------------------------------------------------------- markups
    def markup_snapshot(self):
        return [copy.deepcopy(it.d) for it in self.items]

    def add_item(self, d):
        cls = ITEM_CLASSES[d["type"]]
        it = cls(self, d)
        it.setParentItem(self.pages[d["page"]])
        self.items.append(it)
        return it

    def restore_markups(self, markups):
        for it in self.items:
            if it.scene():
                self.scene.removeItem(it)
        self.items = []
        for d in markups:
            self.add_item(copy.deepcopy(d))
        self.refresh_table()

    def push_undo(self, markups, doc_bytes=None):
        self.undo_stack.append((markups, doc_bytes))
        del self.undo_stack[:-UNDO_LIMIT]
        self.redo_stack.clear()
        self.mark_dirty()

    def mark_dirty(self):
        self.dirty = True
        self.update_title()
        self.update_enabled()

    def begin_markup_change(self):
        self.push_undo(self.markup_snapshot())

    def begin_content_change(self):
        self.push_undo(self.markup_snapshot(), self.doc.tobytes())

    def commit_if_changed(self, snapshot):
        if snapshot != self.markup_snapshot():
            self.push_undo(snapshot)
            self.refresh_table()

    def _swap_state(self, src, dst):
        if not src:
            return
        markups, doc_bytes = src.pop()
        dst.append((self.markup_snapshot(), self.doc.tobytes() if doc_bytes is not None else None))
        if doc_bytes is not None:
            self.doc.close()
            self.doc = pymupdf.open("pdf", doc_bytes)
            self.text_cache.clear()
            for pi in self.pages:
                pi.pix_scale = 0.0
                pi.update()
            self.clear_find()
        self.restore_markups(markups)
        self.mark_dirty()

    def undo(self):
        self._swap_state(self.undo_stack, self.redo_stack)

    def redo(self):
        self._swap_state(self.redo_stack, self.undo_stack)

    def selected_items(self):
        return [it for it in self.items if it.isSelected()]

    def delete_selected(self):
        sel = self.selected_items()
        if not sel:
            return
        self.begin_markup_change()
        for it in sel:
            self.items.remove(it)
            self.scene.removeItem(it)
        self.refresh_table()

    def select_all(self):
        self.set_tool("select")
        for it in self.items:
            it.setSelected(True)

    def edit_markup(self, it):
        dlg = MarkupDialog(self, it.d)
        if dlg.exec() != W.QDialog.Accepted:
            return
        new = dlg.result_data()
        if new == it.d:
            return
        self.begin_markup_change()
        it.prepareGeometryChange()
        it.d.clear()
        it.d.update(new)
        it.refresh()
        self.refresh_table()

    def new_balloon(self, pi, center, leader):
        bs = self.balloon_style
        d = {"type": "balloon", "page": pi.index, "x": center.x(), "y": center.y(),
             "num": str(self.next_num.value()), "char": "", "note": "",
             "leader": [leader.x(), leader.y()] if leader is not None else None}
        for k in ("shape", "r", "fs", "stroke", "fill", "text", "width", "end"):
            d[k] = bs[k]
        if self.b_capture.isChecked():
            d["char"] = self.text_near(pi.index, leader if leader is not None else center,
                                       10 if leader is not None else d["r"] + 18)
        self.begin_markup_change()
        it = self.add_item(d)
        self.next_num.setValue(self.next_num.value() + 1)
        self.refresh_table()
        self.select_in_table(it)
        msg = f"Balloon {d['num']} placed"
        if d["char"]:
            msg += f"  —  characteristic: {d['char']}"
        self.statusBar().showMessage(msg, 5000)

    def place_text(self, pi, lp):
        text, ok = W.QInputDialog.getMultiLineText(self, "Add Text", "Text:")
        if not ok or not text.strip():
            return
        ms = self.markup_style
        d = {"type": "text", "page": pi.index, "x": lp.x(), "y": lp.y(), "text": text.rstrip("\n"),
             "fs": ms["fs"], "color": ms["stroke"], "fill": ms.get("fill"), "border": None}
        d["w"], d["h"] = text_box_size(d)
        self.begin_markup_change()
        self.add_item(d)

    def finish_drag(self, tool, pi, p0, p1):
        dist_px = math.hypot(p1.x() - p0.x(), p1.y() - p0.y()) * self.zoom
        ms = self.markup_style
        if tool == "balloon":
            if dist_px < 8:
                self.new_balloon(pi, p0, None)
            else:
                self.new_balloon(pi, p1, p0)
            return
        if dist_px < 4:
            return
        r = QRectF(p0, p1).normalized()
        if tool == "rect":
            self.begin_markup_change()
            self.add_item({"type": "rect", "page": pi.index, "x": r.x(), "y": r.y(), "w": r.width(),
                           "h": r.height(), "stroke": ms["stroke"], "fill": ms.get("fill"),
                           "width": ms["width"], "opacity": 1.0 if not ms.get("fill") else 0.35})
        elif tool in ("line", "arrow"):
            self.begin_markup_change()
            self.add_item({"type": "line", "page": pi.index, "x1": p0.x(), "y1": p0.y(), "x2": p1.x(),
                           "y2": p1.y(), "stroke": ms["stroke"], "width": ms["width"],
                           "arrow": tool == "arrow"})
        elif tool in ("remove", "whiteout"):
            self.redact_area(pi.index, r, whiteout=(tool == "whiteout"))

    # --------------------------------------------------------- balloon list
    def balloons(self):
        b = [it for it in self.items if it.d["type"] == "balloon"]
        b.sort(key=lambda it: (natural_key(it.d["num"]), it.d["page"]))
        return b

    def refresh_table(self):
        self._table_guard = True
        bl = self.balloons()
        self.table.setRowCount(len(bl))
        for row, it in enumerate(bl):
            d = it.d
            vals = [str(d["num"]), str(d["page"] + 1), d.get("char", ""), d.get("note", "")]
            for col, v in enumerate(vals):
                cell = W.QTableWidgetItem(v)
                if col == 1:
                    cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                cell.setData(Qt.UserRole, id(it))
                self.table.setItem(row, col, cell)
        self.table_count.setText(f"{len(bl)} balloon{'s' if len(bl) != 1 else ''}")
        self._table_snapshot = self.markup_snapshot()
        self._table_guard = False

    def _item_by_id(self, key):
        for it in self.items:
            if id(it) == key:
                return it
        return None

    def on_table_edit(self, cell):
        if self._table_guard:
            return
        it = self._item_by_id(cell.data(Qt.UserRole))
        if it is None:
            return
        key = {0: "num", 2: "char", 3: "note"}.get(cell.column())
        if key is None:
            return
        val = cell.text().strip() if key == "num" else cell.text()
        if key == "num" and not val:
            QTimer.singleShot(0, self.refresh_table)
            return
        if it.d.get(key, "") == val:
            return
        self.push_undo(self._table_snapshot)
        it.prepareGeometryChange()
        it.d[key] = val
        it.update()
        QTimer.singleShot(0, self.refresh_table)

    def on_table_select(self):
        if self._table_guard:
            return
        rows = {c.row() for c in self.table.selectedItems()}
        if not rows:
            return
        self._table_guard = True
        self.scene.clearSelection()
        target = None
        for row in rows:
            it = self._item_by_id(self.table.item(row, 0).data(Qt.UserRole))
            if it is not None:
                it.setSelected(True)
                target = it
        self._table_guard = False
        if target is not None:
            self.view.ensureVisible(target.sceneBoundingRect(), 80, 80)

    def on_scene_selection(self):
        if self._table_guard:
            return
        self._table_guard = True
        self.table.clearSelection()
        sel = {id(it) for it in self.selected_items()}
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.UserRole) in sel:
                self.table.selectRow(row) if len(sel) == 1 else None
        self._table_guard = False

    def select_in_table(self, it):
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.UserRole) == id(it):
                self._table_guard = True
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                self._table_guard = False
                break

    def renumber_balloons(self):
        bl = self.balloons()
        if not bl:
            return
        self.begin_markup_change()
        for n, it in enumerate(bl, 1):
            it.d["num"] = str(n)
            it.update()
        self.next_num.setValue(len(bl) + 1)
        self.refresh_table()

    def restyle_balloons(self):
        bl = self.balloons()
        if not bl:
            return
        self.begin_markup_change()
        for it in bl:
            it.prepareGeometryChange()
            for k in ("shape", "r", "fs", "stroke", "fill", "text", "width", "end"):
                it.d[k] = self.balloon_style[k]
            it.refresh()

    def export_csv(self):
        bl = self.balloons()
        if not bl:
            W.QMessageBox.information(self, APP_NAME, "There are no balloons to export.")
            return
        stem = os.path.splitext(self.path or "drawing.pdf")[0]
        path, _ = W.QFileDialog.getSaveFileName(self, "Export Balloon List", stem + "_balloons.csv",
                                                "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["Balloon #", "Page", "Characteristic / Specification", "Notes",
                            "Measured Result", "OK / NOT OK"])
                for it in bl:
                    d = it.d
                    w.writerow([d["num"], d["page"] + 1, d.get("char", ""), d.get("note", ""), "", ""])
            self.statusBar().showMessage(f"Exported {len(bl)} balloons to {path}", 5000)
        except Exception as ex:
            W.QMessageBox.critical(self, APP_NAME, f"Could not write CSV:\n{ex}")

    # ---------------------------------------------------------------- PDF text
    def page_text(self, i):
        if i not in self.text_cache:
            self.text_cache[i] = self.doc[i].get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
        return self.text_cache[i]

    def _to_unrot(self, i, lp):
        return pymupdf.Point(lp.x(), lp.y()) * self.doc[i].derotation_matrix

    def span_display_rect(self, i, span):
        r = (pymupdf.Rect(span["bbox"]) * self.doc[i].rotation_matrix).normalize()
        return QRectF(r.x0, r.y0, r.width, r.height)

    def span_at(self, i, lp):
        up = self._to_unrot(i, lp)
        best, best_d = None, 3.0
        for b in self.page_text(i)["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b["lines"]:
                for s in line["spans"]:
                    if not s["text"].strip():
                        continue
                    r = pymupdf.Rect(s["bbox"])
                    dx = max(r.x0 - up.x, 0, up.x - r.x1)
                    dy = max(r.y0 - up.y, 0, up.y - r.y1)
                    dist = math.hypot(dx, dy)
                    if dist < best_d or (dist == 0 and best_d == 0):
                        best, best_d = (s, line), dist
                        if dist == 0:
                            return best
        return best

    def text_near(self, i, lp, radius):
        """Text of the PDF line nearest to a point (for balloon characteristics)."""
        up = self._to_unrot(i, lp)
        best, best_d = "", radius
        for b in self.page_text(i)["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b["lines"]:
                text = "".join(s["text"] for s in line["spans"]).strip()
                if not text:
                    continue
                r = pymupdf.Rect(line["bbox"])
                dx = max(r.x0 - up.x, 0, up.x - r.x1)
                dy = max(r.y0 - up.y, 0, up.y - r.y1)
                dist = math.hypot(dx, dy)
                if dist < best_d:
                    best, best_d = text, dist
        return best

    def edit_text_at(self, pi, lp):
        i = pi.index
        hit = self.span_at(i, lp)
        if hit is None:
            self.statusBar().showMessage(
                "No editable text here. Scanned drawings are images - use Whiteout and Add Text instead.", 6000)
            return
        span, line = hit
        name = span["font"].lower()
        flags = span["flags"]
        bold = "bold" in name or "black" in name or bool(flags & 16)
        italic = "italic" in name or "oblique" in name or bool(flags & 2)
        if "courier" in name or "mono" in name or flags & 8:
            family = "Courier"
        elif any(k in name for k in ("times", "roman", "serif", "georgia", "garamond", "cambria")) \
                and "sans" not in name:
            family = "Times"
        else:
            family = "Helvetica"
        dlg = EditTextDialog(self, span["text"], family, bold, italic, round(span["size"], 1),
                             int_to_hex(span["color"]))
        if dlg.exec() != W.QDialog.Accepted:
            return
        text, family, bold, italic, size, color = dlg.values()
        if (text == span["text"] and abs(size - span["size"]) < 0.05
                and color == int_to_hex(span["color"])):
            return
        self.begin_content_change()
        page = self.doc[i]
        r = pymupdf.Rect(span["bbox"])
        dx, dy = line["dir"]
        if abs(dx) >= abs(dy):
            h = r.height
            r.y0 += h * 0.2
            r.y1 -= h * 0.2
        else:
            w = r.width
            r.x0 += w * 0.2
            r.x1 -= w * 0.2
        try:
            page.add_redact_annot(r)
            page.apply_redactions(images=REDACT_IMAGE_NONE, graphics=REDACT_LINE_NONE,
                                  text=REDACT_TEXT_REMOVE)
            if text:
                angle = int(round(math.degrees(math.atan2(-dy, dx)) / 90.0)) * 90 % 360
                page.insert_text(span["origin"], text, fontsize=size,
                                 fontname=BASE14[family][int(bold) + 2 * int(italic)],
                                 color=rgb(color), rotate=angle)
        except Exception as ex:
            W.QMessageBox.critical(self, APP_NAME, f"Could not edit the text:\n{ex}")
        self.invalidate_page(i)
        self.clear_find()

    def redact_area(self, i, rect, whiteout):
        page = self.doc[i]
        r = (pymupdf.Rect(rect.left(), rect.top(), rect.right(), rect.bottom())
             * page.derotation_matrix).normalize()
        self.begin_content_change()
        try:
            if whiteout:
                page.add_redact_annot(r, fill=(1, 1, 1))
                page.apply_redactions(images=REDACT_IMAGE_PIXELS, graphics=REDACT_LINE_COVERED,
                                      text=REDACT_TEXT_REMOVE)
            else:
                page.add_redact_annot(r, fill=False)
                page.apply_redactions(images=REDACT_IMAGE_NONE, graphics=REDACT_LINE_NONE,
                                      text=REDACT_TEXT_REMOVE)
        except Exception as ex:
            W.QMessageBox.critical(self, APP_NAME, f"Could not remove content:\n{ex}")
        self.invalidate_page(i)
        self.clear_find()

    # ---------------------------------------------------------------- find
    def focus_find(self):
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def clear_find(self):
        for it in self.find_items:
            if it.scene():
                self.scene.removeItem(it)
        self.find_items = []
        self.find_hits = []
        self.find_index = -1
        if hasattr(self, "find_label"):
            self.find_label.setText("")

    def find_changed(self, _text):
        self.clear_find()

    def find_next(self):
        q = self.find_edit.text().strip()
        if not q or self.doc is None:
            return
        if not self.find_hits:
            self.clear_find()
            for i, page in enumerate(self.doc):
                for r in page.search_for(q):
                    d = (r * page.rotation_matrix).normalize()
                    self.find_hits.append((i, QRectF(d.x0, d.y0, d.width, d.height)))
            for i, r in self.find_hits:
                it = W.QGraphicsRectItem(r, self.pages[i])
                it.setPen(Qt.NoPen)
                it.setBrush(QColor(255, 220, 0, 90))
                it.setZValue(5)
                self.find_items.append(it)
            if not self.find_hits:
                self.find_label.setText("  not found ")
                return
        self.find_index = (self.find_index + 1) % len(self.find_hits)
        for n, it in enumerate(self.find_items):
            it.setBrush(QColor(255, 140, 0, 140) if n == self.find_index else QColor(255, 220, 0, 90))
        it = self.find_items[self.find_index]
        self.view.centerOn(it.sceneBoundingRect().center())
        self.find_label.setText(f"  {self.find_index + 1} of {len(self.find_hits)} ")

    # ---------------------------------------------------------------- help
    def show_help(self):
        W.QMessageBox.information(self, "Quick Guide", """\
<b>Reading</b><br>
Ctrl + mouse wheel zooms. Middle mouse button or the Pan tool scrolls.
Ctrl+F finds text.<br><br>
<b>Balloons (PPAP / FAI)</b><br>
Choose the Balloon tool (B). Click to drop a balloon, or press on the
dimension and drag out to place the balloon with a leader line. Numbers
increase automatically; set the next number in the Balloon toolbar.
With <i>Capture nearby text</i> on, the dimension text is copied into the
balloon's Characteristic column.<br>
Double-click a balloon to edit it. Drag the small square at the end of a
selected balloon's leader to move the leader end. Edit Characteristic and
Notes directly in the Balloon List, then File &gt; Export Balloon List
(CSV) for the dimensional results sheet.<br><br>
<b>Text</b><br>
Edit Text (E): click existing PDF text to change it.<br>
Remove Text (X): drag a box to delete the PDF text inside it.<br>
Whiteout (W): drag a box to erase everything inside (also works on scans).<br>
Add Text (T): click to add a text box.<br><br>
<b>Saving</b><br>
Save keeps markups editable (they are standard PDF annotations, so
Acrobat and other viewers show them). Export Flattened PDF burns them
into the drawing for release.<br><br>
Arrow keys nudge selected markups (Shift = 10 pt). Delete removes them.
Esc returns to the Select tool.""")

    def show_about(self):
        W.QMessageBox.about(self, f"About {APP_NAME}",
                            f"<b>{APP_NAME}</b> {APP_VERSION}<br><br>"
                            "PDF viewer and editor for PPAP / FAI ballooned drawings.<br><br>"
                            f"PDF engine: PyMuPDF {pymupdf.__version__}<br>"
                            "UI: Qt for Python (PySide6)")


def main():
    W.QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = W.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("ADG Engineering")
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args and os.path.isfile(args[0]):
        QTimer.singleShot(0, lambda: win.open_file(args[0]))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
