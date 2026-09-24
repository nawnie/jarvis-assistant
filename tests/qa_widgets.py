"""Widget audit for Jarvis Assistant - every page, every clickable control, two window sizes.

Uses Windows' own input path WITHOUT moving your real mouse: native mouse messages are
posted straight to Jarvis's window, so hit-testing, Qt's native event handling and the
widgets all run exactly as they do for a real click.

Checks:
  hit-test   - the widget a click at a control's centre (or at a spin-box arrow / combo arrow)
               would actually reach is that control, not something covering it
  clicks     - native clicks on every spin arrow, checkbox, combo and sidebar item do their job
  wheel      - the mouse wheel over a Settings spin box scrolls the page instead of changing the value
  contrast   - rendered text contrast of every label, the combo list, message box, tray menu,
               tooltip, info card and ask bar
  clipping   - button/checkbox text not cut off; info card stays on screen when its answer grows

Run: .venv\\Scripts\\python.exe tests\\qa_widgets.py [evidence-dir]      exit code = number of failures
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "qa" / "tmp_widgets"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
# isolated copy: no Ctrl+click capture, no hotkey, no server start - the audit must not touch the real Jarvis
(TMP / "config.json").write_text(json.dumps({"away_model_enabled": False, "away_free_comfyui": False, "projects_enabled": False, "explain_on_click": False, "quick_ask_hotkey": False,
                                              "llm_autostart_server": False}), encoding="utf-8")
os.environ["JARVIS_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(ROOT))
EVIDENCE = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "qa" / "evidence" / "widgets"
EVIDENCE.mkdir(parents=True, exist_ok=True)

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSpinBox, QApplication,  # noqa: E402
                               QCheckBox, QComboBox, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QStyle, QStyleOptionButton, QStyleOptionComboBox, QStyleOptionSpinBox,
                               QTextEdit, QToolTip, QWidget)

app = QApplication(sys.argv)
from wk.brain import Engine  # noqa: E402
from wk.ui import MainWindow, Tray  # noqa: E402

user32 = ctypes.windll.user32
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
failures, passes = [], 0


def check(name, ok, detail=""):
    global passes
    if ok:
        passes += 1
    else:
        failures.append(f"{name}  [{detail}]")
        print(f"FAIL {name}  [{detail}]", flush=True)


def pump(secs=0.15):
    t0 = time.time()
    while time.time() - t0 < secs:
        app.processEvents()
        time.sleep(0.01)


def physical(widget, local):
    g = widget.mapToGlobal(local)
    dpr = widget.screen().devicePixelRatio()
    return round(g.x() * dpr), round(g.y() * dpr)


def post_click(widget, local):
    hwnd = int(widget.window().winId())
    x, y = physical(widget, local)
    pt = wt.POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(pt))
    lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
    user32.PostMessageW(hwnd, 0x0200, 0, lp)
    pump(0.03)
    user32.PostMessageW(hwnd, 0x0201, 0x0001, lp)
    pump(0.03)
    user32.PostMessageW(hwnd, 0x0202, 0, lp)
    pump(0.12)


def post_wheel(widget, local, notches=-1):
    hwnd = int(widget.window().winId())
    x, y = physical(widget, local)
    wparam = ((notches * 120) & 0xFFFF) << 16
    user32.PostMessageW(hwnd, 0x020A, wparam, ((y & 0xFFFF) << 16) | (x & 0xFFFF))
    pump(0.2)


# --- rendered contrast (WCAG): background = most common colour, text = the most distinct one ---
def _lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def rendered_contrast(image):
    colours = Counter()
    for y in range(0, image.height(), 1):
        for x in range(0, image.width(), 1):
            c = image.pixelColor(x, y)
            colours[(c.red(), c.green(), c.blue())] += 1
    if not colours:
        return 0.0
    bg = colours.most_common(1)[0][0]
    solid = [c for c, n in colours.items() if n >= 3 and c != bg] or [bg]
    return max(_ratio(bg, c) for c in solid)


def crop_of(top, widget):
    img = top.grab().toImage()
    dpr = img.devicePixelRatio()
    r = QRect(widget.mapTo(top, QPoint(0, 0)), widget.size())
    return img.copy(QRect(int(r.x() * dpr), int(r.y() * dpr), int(r.width() * dpr), int(r.height() * dpr)))


# ---------------------------------------------------------------------------
# build an isolated Jarvis with some data in every list
# ---------------------------------------------------------------------------
e = Engine()
w = MainWindow(e)
tray = Tray(e, w, app)
now = time.time()
for a, b, p, t in ((now - 3000, now - 1800, "chrome.exe", "prism-ml - Hugging Face - Google Chrome"),
                   (now - 1800, now - 600, "code.exe", "ui.py - Watchkeeper - Visual Studio Code")):
    e.store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)", (a, b, p, t))
e.store.add_clip("code.exe", "some copied text")
e.store.add_reminder(now + 600, "stretch")
e.store.add_fact("likes amber")
e.store.add_journal(now - 3600, now, "- worked on Jarvis QA")
e.store.add_chat("user", "hi")
e.store.add_chat("assistant", "hello")
e.store.add_event("file", "New in Downloads: x.zip")
w.setWindowFlag(Qt.WindowStaysOnTopHint, True)   # posted wheel events are routed by what's on top
w.move(40, 30)
w.show()
w._refresh_today()
pump(0.8)
page_names = [w.nav.item(i).text() for i in range(w.nav.count())]

INTERACTIVE = (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit, QTextEdit, QAbstractItemView)


def interactive_widgets(page):
    out = []
    for wd in page.findChildren(QWidget):
        if not isinstance(wd, INTERACTIVE):
            continue
        if not wd.isVisibleTo(w) or not wd.isEnabled() and not isinstance(wd, QAbstractButton):
            continue
        parent = wd.parentWidget()
        if isinstance(parent, (QAbstractSpinBox, QComboBox, QAbstractItemView)):
            continue  # internal parts (spin box's text field, table corner button, ...)
        out.append(wd)
    return out


def probe_points(wd):
    """(label, local point, strict) - strict = the click must reach this exact widget, not a child."""
    if isinstance(wd, QAbstractSpinBox):
        opt = QStyleOptionSpinBox()
        wd.initStyleOption(opt)
        pts = []
        for sc, name in ((QStyle.SC_SpinBoxUp, "up-arrow"), (QStyle.SC_SpinBoxDown, "down-arrow")):
            r = wd.style().subControlRect(QStyle.CC_SpinBox, opt, sc, wd)
            if r.isValid() and r.width() > 0:
                pts.append((name, r.center(), True))
        pts.append(("text", QPoint(12, wd.height() // 2), False))
        return pts
    if isinstance(wd, QComboBox):
        opt = QStyleOptionComboBox()
        wd.initStyleOption(opt)
        r = wd.style().subControlRect(QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxArrow, wd)
        return [("arrow", r.center(), False), ("centre", wd.rect().center(), False)]
    if isinstance(wd, QCheckBox):
        opt = QStyleOptionButton()
        wd.initStyleOption(opt)
        return [("indicator", wd.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, wd).center(), False)]
    if isinstance(wd, QAbstractItemView) and wd.model() and wd.model().rowCount() > 0:
        r = wd.visualRect(wd.model().index(0, 0))
        return [("first row", wd.viewport().mapTo(wd, r.center()), False)]
    return [("centre", wd.rect().center(), False)]


def ensure_visible(wd):
    p = wd.parentWidget()
    while p is not None:
        if isinstance(p, QScrollArea):
            p.ensureWidgetVisible(wd, 10, 10)
            pump(0.05)
            return
        p = p.parentWidget()


# ---------------------------------------------------------------------------
# 1. hit-test + clipping audit on every page at two window sizes
# ---------------------------------------------------------------------------
for size in ((1120, 740), (900, 600)):
    w.resize(*size)
    pump(0.4)
    for i, name in enumerate(page_names):
        w.nav.setCurrentRow(i)
        pump(0.25)
        page = w.stack.currentWidget()
        w.grab().save(str(EVIDENCE / f"{size[0]}x{size[1]}-{i}-{name}.png"))
        for wd in interactive_widgets(page):
            ensure_visible(wd)
            label = f"{size[0]}x{size[1]} {name}: {type(wd).__name__} '{getattr(wd, 'text', lambda: '')() or wd.objectName()}'"
            for part, pt, strict in probe_points(wd):
                # Qt's own widget-tree hit test: independent of whatever other app windows are on top
                top = wd.window()
                hit = top.childAt(top.mapFromGlobal(wd.mapToGlobal(pt)))
                reached = hit is wd or (not strict and hit is not None and wd.isAncestorOf(hit))
                check(f"hit-test {label} {part}", reached,
                      f"click lands on {type(hit).__name__ if hit else 'nothing'}"
                      + (f" '{hit.objectName()}'" if hit is not None and hit.objectName() else ""))
            if isinstance(wd, (QPushButton, QCheckBox)) and wd.text():
                check(f"text fits {label}", wd.width() >= wd.sizeHint().width() - 1,
                      f"width {wd.width()} < needed {wd.sizeHint().width()}")
        # every text label on the page must be readable against what's behind it
        for lab in page.findChildren(QLabel):
            if lab.isVisibleTo(w) and lab.text().strip() and lab.width() > 4 and lab.height() > 4:
                ensure_visible(lab)
                ratio = rendered_contrast(crop_of(w, lab))
                check(f"contrast {size[0]}x{size[1]} {name}: label '{lab.text()[:30]}'", ratio >= 4.5, f"{ratio:.2f}:1")

# ---------------------------------------------------------------------------
# 2. native clicks actually work
# ---------------------------------------------------------------------------
w.resize(1120, 740)
w.show_page("Settings")
pump(0.4)
for key, spin in w.s_spins.items():
    ensure_visible(spin)
    opt = QStyleOptionSpinBox()
    spin.initStyleOption(opt)
    spin.setValue(spin.minimum() + 1)
    for sc, name, delta in ((QStyle.SC_SpinBoxUp, "up", 1), (QStyle.SC_SpinBoxDown, "down", -1)):
        before = spin.value()
        post_click(spin, spin.style().subControlRect(QStyle.CC_SpinBox, opt, sc, spin).center())
        check(f"click {key} {name} arrow", spin.value() == before + delta * spin.singleStep(), f"{before} -> {spin.value()}")
for key, box in w.s_checks.items():
    ensure_visible(box)
    opt = QStyleOptionButton()
    box.initStyleOption(opt)
    before = box.isChecked()
    post_click(box, box.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, box).center())
    check(f"click checkbox {key}", box.isChecked() != before)
    box.setChecked(before)
ensure_visible(w.s_trigger)
opt = QStyleOptionComboBox()
w.s_trigger.initStyleOption(opt)
post_click(w.s_trigger, w.s_trigger.style().subControlRect(QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxArrow, w.s_trigger).center())
pump(0.4)
view = w.s_trigger.view()
check("click combo opens its list", view.isVisible())
if view.isVisible():
    popup_img = view.window().grab().toImage()
    popup_img.save(str(EVIDENCE / "combo-popup.png"))
    ratio = rendered_contrast(popup_img)
    check("contrast combo list", ratio >= 4.5, f"{ratio:.2f}:1")
    w.s_trigger.hidePopup()
    pump(0.2)
for i, name in enumerate(page_names):
    post_click(w.nav, w.nav.visualItemRect(w.nav.item(i)).center())
    check(f"click sidebar '{name}'", w.stack.currentIndex() == i, f"on page {w.stack.currentIndex()}")

# ---------------------------------------------------------------------------
# 3. mouse wheel over a spin box scrolls the Settings page, not the value
# ---------------------------------------------------------------------------
w.show_page("Settings")
pump(0.3)
scroll = [a for a in w.stack.currentWidget().findChildren(QScrollArea)][0]
scroll.verticalScrollBar().setValue(0)
spin = w.s_spins["digest_minutes"]
ensure_visible(spin)
scroll.verticalScrollBar().setValue(0)
pump(0.2)
before_value, before_scroll = spin.value(), scroll.verticalScrollBar().value()
if spin.visibleRegion().contains(spin.rect().center()):
    post_wheel(spin, spin.rect().center(), -1)
    check("wheel over spin box leaves its value alone", spin.value() == before_value, f"{before_value} -> {spin.value()}")
    check("wheel over spin box scrolls the page", scroll.verticalScrollBar().value() > before_scroll,
          f"scroll {before_scroll} -> {scroll.verticalScrollBar().value()}")
ensure_visible(w.s_trigger)
pump(0.2)
combo_before = w.s_trigger.currentIndex()
post_wheel(w.s_trigger, w.s_trigger.rect().center(), -1)
check("wheel over combo leaves its choice alone", w.s_trigger.currentIndex() == combo_before)

# ---------------------------------------------------------------------------
# 4. pop-up surfaces are readable
# ---------------------------------------------------------------------------
box = QMessageBox(QMessageBox.Information, "Jarvis Assistant", "Settings saved.", parent=w)
box.show()
pump(0.4)
img = box.grab().toImage()
img.save(str(EVIDENCE / "messagebox.png"))
ratio = rendered_contrast(crop_of(box, [lab for lab in box.findChildren(QLabel) if lab.text() == "Settings saved."][0]))
check("contrast 'Settings saved' message box", ratio >= 4.5, f"{ratio:.2f}:1")
box.close()
tray._menu.popup(QPoint(300, 300))
pump(0.4)
img = tray._menu.grab().toImage()
img.save(str(EVIDENCE / "tray-menu.png"))
check("contrast tray menu", rendered_contrast(img) >= 4.5, f"{rendered_contrast(img):.2f}:1")
tray._menu.close()
w.show_page("Now")
pump(0.3)
QToolTip.showText(w.today_table.mapToGlobal(QPoint(20, 20)), w.today_table.toolTip(), w.today_table)
pump(0.6)
tips = [t for t in QApplication.topLevelWidgets() if type(t).__name__ == "QTipLabel" and t.isVisible()]
if tips:
    img = tips[0].grab().toImage()
    img.save(str(EVIDENCE / "tooltip.png"))
    check("contrast tooltip", rendered_contrast(img) >= 4.5, f"{rendered_contrast(img):.2f}:1")
QToolTip.hideText()

# ---------------------------------------------------------------------------
# 5. info card: readable, and stays on screen when a long answer arrives
# ---------------------------------------------------------------------------
area = w.screen().availableGeometry()
rid = w.card.open("Card title", "subtitle", "**facts** line", near=QPoint(area.right() - 60, area.bottom() - 60))
pump(0.2)
w.card.set_answer(rid, "\n\n".join(f"Line {i} of a long answer that wraps across the card." for i in range(30)))
pump(0.4)
g = w.card.frameGeometry()
check("info card stays on screen after a long answer", area.contains(g), f"card {g.getRect()} vs screen {area.getRect()}")
img = w.card.grab().toImage()
img.save(str(EVIDENCE / "card.png"))
check("contrast info card", rendered_contrast(img) >= 4.5, f"{rendered_contrast(img):.2f}:1")
w.card.hide()

e.shutdown()
w.card.close()
print(f"\nwidget audit: {passes} passed, {len(failures)} failed", flush=True)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(min(len(failures), 250))
