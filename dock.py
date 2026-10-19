#!/usr/bin/env python3
"""
Mint Dock — "dynamic island" док для KDE (X11).

  ( кнопка меню )  ( панель задач )

* Кнопка слева открывает меню приложений с анимацией (поиск + сетка иконок).
* Панель задач показывает открытые окна. Активное окно "выпирает" из панели
  и имеет ту же обводку. Клик по окну: активировать / свернуть. Средняя кнопка: закрыть.

Зависимости:  pip install PyQt6 python-xlib
Запуск:       python3 mint_dock.py     (сессия Plasma должна быть X11!)
"""
import os
import re
import shlex
import signal
import subprocess
import sys
import time

# Окна через Xlib видны только в X11-сессии, поэтому принудительно xcb
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from PyQt6.QtCore import (QEasingCurve, QParallelAnimationGroup, QPoint,
                          QPropertyAnimation, QRectF, QSize, Qt, QTimer,
                          QVariantAnimation)
from PyQt6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath, QPen)
from PyQt6.QtWidgets import (QApplication, QFrame, QGraphicsDropShadowEffect,
                             QLineEdit, QListWidget, QListWidgetItem,
                             QVBoxLayout, QWidget)

try:
    from Xlib import X, Xatom, display as xdisplay
    import Xlib.protocol.event
    HAVE_XLIB = True
except ImportError:  # pragma: no cover
    HAVE_XLIB = False

# ------------------------------------------------------------------ стиль ---
FILL = QColor(255, 249, 236, 242)
BORDER = QColor(176, 160, 128, 190)
BORDER_W = 1.3
RADIUS = 22

ICON = 38            # размер иконки в панели
SLOT = 54            # ширина одной ячейки
ISLAND_H = 62        # высота островков
BUMP = 14            # насколько активное окно выпирает вверх
PAD = 10             # внутренний отступ панели задач
GAP = 12             # зазор между кнопкой и панелью
SH = 22              # поле под тень вокруг док-окна
BOTTOM_MARGIN = 8    # отступ дока от низа экрана
MIN_TASK_W = 180


# ------------------------------------------------------ приложения (.desktop) --
def parse_desktop(path):
    data, inside = {}, False
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    if inside:
                        break
                    inside = line == "[Desktop Entry]"
                    continue
                if inside and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
    except OSError:
        return None
    return data


def app_dirs():
    home = os.path.expanduser("~")
    dirs = [os.path.join(home, ".local/share")]
    dirs += os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    dirs += ["/var/lib/flatpak/exports/share", os.path.join(home, ".local/share/flatpak/exports/share"),
             "/var/lib/snapd/desktop"]
    return [os.path.join(d, "applications") for d in dirs if d]


def load_apps():
    lang = (os.environ.get("LANG") or "en")[:2]
    apps, seen = [], set()
    for d in app_dirs():
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for fn in files:
                if not fn.endswith(".desktop") or fn in seen:
                    continue
                e = parse_desktop(os.path.join(root, fn))
                if not e or e.get("Type") != "Application":
                    continue
                if e.get("NoDisplay", "").lower() == "true" or e.get("Hidden", "").lower() == "true":
                    continue
                if not e.get("Exec"):
                    continue
                seen.add(fn)
                apps.append({
                    "id": fn[:-8],
                    "name": e.get(f"Name[{lang}]") or e.get("Name") or fn,
                    "icon": e.get("Icon", ""),
                    "exec": e["Exec"],
                    "terminal": e.get("Terminal", "").lower() == "true",
                    "wmclass": e.get("StartupWMClass", ""),
                })
    apps.sort(key=lambda a: a["name"].lower())
    return apps


def make_icon(name):
    if not name:
        return QIcon()
    if os.path.isabs(name):
        return QIcon(name)
    return QIcon.fromTheme(name)


def launch(app):
    cmd = re.sub(r"%[a-zA-Z%]", "", app["exec"]).strip()
    try:
        args = shlex.split(cmd)
        if app["terminal"]:
            args = ["konsole", "-e"] + args
        subprocess.Popen(args, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:
        print("launch failed:", ex, file=sys.stderr)


class IconIndex:
    """Сопоставляет WM_CLASS окна с иконкой из .desktop файлов."""

    def __init__(self, apps):
        self.map = {}
        for a in apps:
            keys = {a["id"].lower(), a["name"].lower()}
            if a["wmclass"]:
                keys.add(a["wmclass"].lower())
            try:
                first = [t for t in shlex.split(a["exec"]) if "=" not in t][0]
                keys.add(os.path.basename(first).lower())
            except Exception:
                pass
            for k in keys:
                self.map.setdefault(k, a["icon"])

    def get(self, *names):
        for n in names:
            if not n:
                continue
            icon = self.map.get(n.lower())
            if icon:
                ic = make_icon(icon)
                if not ic.isNull():
                    return ic
            ic = QIcon.fromTheme(n.lower())
            if not ic.isNull():
                return ic
        return QIcon()


# ------------------------------------------------------------------- X11 ---
class XWM:
    SKIP_TYPES = ("DOCK", "DESKTOP", "SPLASH", "TOOLTIP", "NOTIFICATION", "MENU",
                  "DROPDOWN_MENU", "POPUP_MENU", "COMBO", "DND", "UTILITY")

    def __init__(self):
        self.d = xdisplay.Display()
        self.root = self.d.screen().root
        self._atoms = {}
        self.skip_atoms = {self.atom(f"_NET_WM_WINDOW_TYPE_{t}") for t in self.SKIP_TYPES}

    def atom(self, name):
        a = self._atoms.get(name)
        if a is None:
            a = self._atoms[name] = self.d.intern_atom(name)
        return a

    def prop(self, win, name):
        try:
            r = win.get_full_property(self.atom(name), X.AnyPropertyType)
        except Exception:
            return None
        return r.value if r is not None else None

    def windows(self, ignore=()):
        lst = self.prop(self.root, "_NET_CLIENT_LIST")
        lst = [] if lst is None else lst
        act = self.prop(self.root, "_NET_ACTIVE_WINDOW")
        active = int(act[0]) if act is not None and len(act) else 0
        out = []
        for wid in lst:
            wid = int(wid)
            if wid in ignore:
                continue
            win = self.d.create_resource_object("window", wid)
            types = self.prop(win, "_NET_WM_WINDOW_TYPE")
            types = [] if types is None else types
            if any(int(t) in self.skip_atoms for t in types):
                continue
            state = self.prop(win, "_NET_WM_STATE")
            state = [int(s) for s in ([] if state is None else state)]
            if self.atom("_NET_WM_STATE_SKIP_TASKBAR") in state:
                continue
            title = self.prop(win, "_NET_WM_NAME")
            if isinstance(title, (bytes, bytearray)):
                title = title.decode("utf-8", "ignore")
            elif not title:
                try:
                    title = win.get_wm_name()
                except Exception:
                    title = ""
            try:
                cls = win.get_wm_class() or ("", "")
            except Exception:
                cls = ("", "")
            out.append({"id": wid, "title": title or "", "inst": cls[0], "cls": cls[1],
                        "hidden": self.atom("_NET_WM_STATE_HIDDEN") in state})
        return out, active

    def _send(self, win, ctype, data):
        ev = Xlib.protocol.event.ClientMessage(window=win, client_type=ctype, data=(32, data))
        self.root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self.d.flush()

    def activate(self, wid):
        win = self.d.create_resource_object("window", wid)
        self._send(win, self.atom("_NET_ACTIVE_WINDOW"), [2, X.CurrentTime, 0, 0, 0])

    def minimize(self, wid):
        win = self.d.create_resource_object("window", wid)
        self._send(win, self.atom("WM_CHANGE_STATE"), [3, 0, 0, 0, 0])  # IconicState

    def close(self, wid):
        win = self.d.create_resource_object("window", wid)
        self._send(win, self.atom("_NET_CLOSE_WINDOW"), [X.CurrentTime, 2, 0, 0, 0])

    def make_dock(self, wid, strut_bottom, screen_w):
        """Тип DOCK + не показывать в списке задач + резерв места снизу."""
        win = self.d.create_resource_object("window", wid)
        win.change_property(self.atom("_NET_WM_WINDOW_TYPE"), Xatom.ATOM, 32,
                            [self.atom("_NET_WM_WINDOW_TYPE_DOCK")])
        win.change_property(self.atom("_NET_WM_STATE"), Xatom.ATOM, 32, [
            self.atom("_NET_WM_STATE_ABOVE"), self.atom("_NET_WM_STATE_STICKY"),
            self.atom("_NET_WM_STATE_SKIP_TASKBAR"), self.atom("_NET_WM_STATE_SKIP_PAGER")])
        if strut_bottom:
            win.change_property(self.atom("_NET_WM_STRUT_PARTIAL"), Xatom.CARDINAL, 32,
                                [0, 0, 0, strut_bottom, 0, 0, 0, 0, 0, 0, 0, screen_w - 1])
        self.d.sync()


# ------------------------------------------------------------ меню приложений ---
class Launcher(QWidget):
    W, H, M = 560, 440, 24

    def __init__(self, apps):
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.apps = apps
        self.hidden_at = 0.0
        self.setFixedSize(self.W + 2 * self.M, self.H + 2 * self.M)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(self.M, self.M, self.M, self.M)
        card = QFrame()
        card.setObjectName("card")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 70))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(12)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск приложений…")
        self.search.textChanged.connect(self.filter)
        self.search.returnPressed.connect(self.launch_first)
        lay.addWidget(self.search)

        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setUniformItemSizes(True)
        self.list.setWordWrap(True)
        self.list.setIconSize(QSize(44, 44))
        self.list.setGridSize(QSize(100, 92))
        self.list.setSpacing(2)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.itemActivated.connect(self.on_item)
        self.list.itemClicked.connect(self.on_item)
        lay.addWidget(self.list)

        for a in apps:
            it = QListWidgetItem(make_icon(a["icon"]) if a["icon"] else QIcon(), a["name"])
            it.setData(Qt.ItemDataRole.UserRole, a)
            it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            it.setToolTip(a["name"])
            self.list.addItem(it)

        self.setStyleSheet(f"""
            #card {{ background: rgb(255,249,236); border: 1.3px solid rgba(176,160,128,190);
                     border-radius: {RADIUS}px; }}
            QLineEdit {{ background: rgba(0,0,0,0.05); border: none; border-radius: 12px;
                         padding: 9px 14px; font-size: 14px; color: #3d372c; }}
            QListWidget {{ background: transparent; outline: none; color: #3d372c; font-size: 12px; }}
            QListWidget::item {{ border-radius: 12px; padding: 6px 2px; }}
            QListWidget::item:hover {{ background: rgba(0,0,0,0.06); }}
            QListWidget::item:selected {{ background: rgba(0,0,0,0.10); color: #3d372c; }}
            QScrollBar:vertical {{ width: 6px; background: transparent; }}
            QScrollBar::handle:vertical {{ background: rgba(0,0,0,0.2); border-radius: 3px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        """)
        self.anim = None

    # --- фильтр / запуск
    def filter(self, text):
        t = text.strip().lower()
        first = None
        for i in range(self.list.count()):
            it = self.list.item(i)
            vis = t in it.text().lower()
            it.setHidden(not vis)
            if vis and first is None:
                first = it
        if first:
            self.list.setCurrentItem(first)

    def launch_first(self):
        it = self.list.currentItem()
        if it and not it.isHidden():
            self.on_item(it)

    def on_item(self, it):
        launch(it.data(Qt.ItemDataRole.UserRole))
        self.hide()

    def hideEvent(self, e):
        self.hidden_at = time.monotonic()
        super().hideEvent(e)

    # --- открытие с анимацией
    def toggle(self, anchor):
        """anchor — глобальная точка: левый край и верх островка-кнопки."""
        if self.isVisible():
            self.hide()
            return
        if time.monotonic() - self.hidden_at < 0.25:   # закрылось этим же кликом
            return
        screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        g = screen.geometry()
        x = max(g.left() + 4, min(anchor.x() - self.M, g.right() - self.width() - 4))
        y = anchor.y() - self.H - self.M - 12
        end = QPoint(x, y)

        self.search.clear()
        self.filter("")
        self.list.scrollToTop()
        self.setWindowOpacity(0.0)
        self.move(end + QPoint(0, 22))
        self.show()
        self.search.setFocus()

        a1 = QPropertyAnimation(self, b"windowOpacity", self)
        a1.setDuration(200)
        a1.setStartValue(0.0)
        a1.setEndValue(1.0)
        a2 = QPropertyAnimation(self, b"pos", self)
        a2.setDuration(240)
        a2.setStartValue(end + QPoint(0, 22))
        a2.setEndValue(end)
        a2.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim = QParallelAnimationGroup(self)
        self.anim.addAnimation(a1)
        self.anim.addAnimation(a2)
        self.anim.start()


# ------------------------------------------------------------------- док ---
class Item:
    def __init__(self, dock, wid):
        self.wid = wid
        self.title = ""
        self.icon = QIcon()
        self.letter = "?"
        self.hidden = False
        self.active = False
        self.t = 0.0                      # 0..1 — "выпирание"
        self.anim = QVariantAnimation(dock)
        self.anim.setDuration(220)
        self.anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self.anim.valueChanged.connect(self._tick)
        self.dock = dock

    def _tick(self, v):
        self.t = float(v)
        self.dock.update()

    def set_active(self, on):
        if on == self.active:
            return
        self.active = on
        self.anim.stop()
        self.anim.setStartValue(self.t)
        self.anim.setEndValue(1.0 if on else 0.0)
        self.anim.start()


class Dock(QWidget):
    def __init__(self, apps):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setWindowTitle("Mint Dock")

        self.icons = IconIndex(apps)
        self.launcher = Launcher(apps)
        self.items = {}
        self.order = []
        self.hover = None
        self.wm = None
        if HAVE_XLIB:
            try:
                self.wm = XWM()
            except Exception as ex:
                print("X11 недоступен:", ex, file=sys.stderr)

        self.relayout()
        self.apply_dock_hints()            # до show()
        # Qt перезаписывает тип окна при показе — ставим DOCK ещё раз после
        for ms in (150, 600, 2000):
            QTimer.singleShot(ms, self.apply_dock_hints)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(350)

    def apply_dock_hints(self):
        if not self.wm:
            return
        scr = QApplication.primaryScreen()
        dpr = scr.devicePixelRatio()
        try:
            self.wm.make_dock(int(self.winId()), int((ISLAND_H + BOTTOM_MARGIN + 6) * dpr),
                              int(scr.size().width() * dpr))
        except Exception as ex:
            print("make_dock:", ex, file=sys.stderr)

    # --- геометрия
    @property
    def island_y(self):
        return SH + BUMP

    def task_w(self):
        return max(MIN_TASK_W, 2 * PAD + len(self.order) * SLOT)

    @property
    def task_x(self):
        return SH + ISLAND_H + GAP

    def relayout(self):
        w = self.task_x + self.task_w() + SH
        h = SH + BUMP + ISLAND_H + SH
        self.setFixedSize(w, h)
        g = QApplication.primaryScreen().geometry()
        self.move(g.x() + (g.width() - w) // 2,
                  g.bottom() + 1 - h + SH - BOTTOM_MARGIN)
        self.update()

    def launcher_rect(self):
        return QRectF(SH, self.island_y, ISLAND_H, ISLAND_H)

    def task_rect(self):
        return QRectF(self.task_x, self.island_y, self.task_w(), ISLAND_H)

    def slot_x(self, idx):
        return self.task_x + PAD + idx * SLOT

    # --- обновление списка окон
    def refresh(self):
        if not self.wm:
            return
        try:
            wins, active = self.wm.windows(ignore={int(self.winId())})
        except Exception:
            return
        changed, seen = False, set()
        for w in wins:
            wid = w["id"]
            seen.add(wid)
            it = self.items.get(wid)
            if it is None:
                it = self.items[wid] = Item(self, wid)
                it.icon = self.icons.get(w["cls"], w["inst"])
                it.letter = (w["cls"] or w["title"] or "?")[:1].upper()
                self.order.append(wid)
                changed = True
            it.title = w["title"]
            it.hidden = w["hidden"]
            it.set_active(wid == active and not w["hidden"])
        for wid in [i for i in self.order if i not in seen]:
            self.items.pop(wid).anim.stop()
            self.order.remove(wid)
            changed = True
        if changed:
            self.relayout()
        else:
            self.update()

    # --- рисование
    def _shadow(self, p, rect, r):
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(12, 0, -1):
            p.setBrush(QColor(0, 0, 0, 4))
            p.drawRoundedRect(rect.adjusted(-i, -i + 5, i, i + 5), r + i, r + i)

    def _draw_icon(self, p, item, cx, cy, size, alpha=1.0):
        rect = QRectF(cx - size / 2, cy - size / 2, size, size)
        p.setOpacity(alpha)
        if not item.icon.isNull():
            pm = item.icon.pixmap(QSize(64, 64))
            p.drawPixmap(rect, pm, QRectF(pm.rect()))
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(120, 105, 80))
            p.drawEllipse(rect)
            p.setPen(QColor(255, 249, 236))
            f = QFont(self.font())
            f.setPixelSize(int(size * 0.5))
            f.setBold(True)
            p.setFont(f)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, item.letter)
        p.setOpacity(1.0)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        lr, tr = self.launcher_rect(), self.task_rect()
        self._shadow(p, lr, RADIUS)
        self._shadow(p, tr, RADIUS)

        # форма панели задач = островок ∪ "выпирающие" плитки активных окон
        path = QPainterPath()
        path.addRoundedRect(tr, RADIUS, RADIUS)
        for idx, wid in enumerate(self.order):
            it = self.items[wid]
            if it.t > 0.01:
                t = it.t
                tile = QPainterPath()
                tile.addRoundedRect(QRectF(self.slot_x(idx) - 3 * t, self.island_y - BUMP * t,
                                           SLOT + 6 * t, ISLAND_H - 10 + BUMP * t), 16, 16)
                path = path.united(tile)

        pen = QPen(BORDER, BORDER_W)
        p.setPen(pen)
        p.setBrush(FILL)
        p.drawPath(path)
        lp = QPainterPath()
        lp.addRoundedRect(lr, RADIUS, RADIUS)
        p.drawPath(lp)

        # кнопка меню: сетка 3x3
        if self.hover == "launcher":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 16))
            p.drawRoundedRect(lr.adjusted(7, 7, -7, -7), 14, 14)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(107, 99, 85))
        c, d, step = lr.center(), 7.0, 11.0
        for gy in (-1, 0, 1):
            for gx in (-1, 0, 1):
                p.drawRoundedRect(QRectF(c.x() + gx * step - d / 2, c.y() + gy * step - d / 2, d, d), 2, 2)

        # иконки окон
        for idx, wid in enumerate(self.order):
            it = self.items[wid]
            t = max(0.0, it.t)
            cx = self.slot_x(idx) + SLOT / 2
            if self.hover == wid and it.t < 0.5:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, 16))
                p.drawRoundedRect(QRectF(self.slot_x(idx) + 2, self.island_y + 6, SLOT - 4, ISLAND_H - 12), 14, 14)
            cy = self.island_y + ISLAND_H / 2 - BUMP * t / 2 - 4 * t
            self._draw_icon(p, it, cx, cy, ICON * (1 + 0.18 * t), 0.5 if it.hidden else 1.0)
            if not it.active and it.t < 0.5:       # точка "приложение запущено"
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(120, 105, 80, 200))
                p.drawEllipse(QRectF(cx - 2, self.island_y + ISLAND_H - 8, 4, 4))
        p.end()

    # --- мышь
    def hit(self, pos):
        pt = pos.toPoint()
        if self.launcher_rect().contains(pt.x(), pt.y()):
            return "launcher"
        if self.task_rect().adjusted(0, -BUMP, 0, 0).contains(pt.x(), pt.y()):
            idx = int((pt.x() - self.task_x - PAD) // SLOT)
            if 0 <= idx < len(self.order):
                return self.order[idx]
        return None

    def mouseMoveEvent(self, e):
        h = self.hit(e.position())
        if h != self.hover:
            self.hover = h
            it = self.items.get(h) if isinstance(h, int) else None
            self.setToolTip(it.title if it else ("Все приложения" if h == "launcher" else ""))
            self.update()

    def leaveEvent(self, _):
        self.hover = None
        self.update()

    def mousePressEvent(self, e):
        h = self.hit(e.position())
        if h == "launcher" and e.button() == Qt.MouseButton.LeftButton:
            anchor = self.mapToGlobal(QPoint(SH, self.island_y))
            self.launcher.toggle(anchor)
        elif isinstance(h, int) and self.wm:
            it = self.items[h]
            try:
                if e.button() == Qt.MouseButton.MiddleButton:
                    self.wm.close(h)
                elif e.button() == Qt.MouseButton.LeftButton:
                    if it.active:
                        self.wm.minimize(h)
                    else:
                        self.wm.activate(h)
            except Exception as ex:
                print("wm:", ex, file=sys.stderr)
            QTimer.singleShot(80, self.refresh)


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    dock = Dock(load_apps())
    dock.show()
    dock.refresh()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
