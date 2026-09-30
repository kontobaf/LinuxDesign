#!/usr/bin/env python3
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gdk, GLib

from Xlib import X, display as xdisplay, Xatom
from Xlib import Xutil
import time
import Xlib.protocol.event


CSS = b"""
window.dock-window {
    background: transparent;
}

.dock-island {
    background-color: rgba(255, 249, 236, 0.92);
    border-radius: 22px;
    padding: 8px 14px;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
    border: 1px solid rgba(255, 255, 255, 0.6);
}

.dock-placeholder {
    color: #6b6355;
    font-size: 13px;
    font-weight: 500;
    padding: 6px 10px;
}
"""


# ---------- X11 EWMH-хуки ----------

def make_it_a_panel(xid: int):
    """
    Настраиваем окно как панель через EWMH:
      - не в списке задач
      - не в alt-tab
      - не в pager
      - всегда поверх (через client message)
      - не двигается и не ресайзится WM-ом
    """
    d = xdisplay.Display()
    root = d.screen().root
    win = d.create_resource_object("window", xid)

    # --- Тип окна: DOCK ---
    wm_type = d.intern_atom("_NET_WM_WINDOW_TYPE")
    wm_type_dock = d.intern_atom("_NET_WM_WINDOW_TYPE_DOCK")
    win.change_property(wm_type, Xatom.ATOM, 32, [wm_type_dock])

    # --- Состояние: ABOVE + STICKY + SKIP_TASKBAR + SKIP_PAGER ---
    # 1) Прямое выставление атома
    wm_state = d.intern_atom("_NET_WM_STATE")
    states = [
        d.intern_atom("_NET_WM_STATE_ABOVE"),
        d.intern_atom("_NET_WM_STATE_STICKY"),
        d.intern_atom("_NET_WM_STATE_SKIP_TASKBAR"),
        d.intern_atom("_NET_WM_STATE_SKIP_PAGER"),
    ]
    win.change_property(wm_state, Xatom.ATOM, 32, states)

    # 2) Client message к WM: попросить добавить ABOVE
    #    _NET_WM_STATE_ADD = 1
    data = [
        1,  # действие: ADD
        d.intern_atom("_NET_WM_STATE_ABOVE"),
        0, 0, 0
    ]
    ev = Xlib.protocol.event.ClientMessage(
        window=win,
        client_type=wm_state,
        data=(32, data),
    )
    mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
    root.send_event(ev, event_mask=mask)

    # --- Запрещаем WM трогать размер и позицию ---
    hints = win.get_wm_normal_hints()
    hints.flags |= (
        Xutil.PMinSize | Xutil.PMaxSize
        | Xutil.PPosition | Xutil.PWinGravity
    )
    geom = win.get_geometry()
    hints.min_width = hints.max_width = geom.width
    hints.min_height = hints.max_height = geom.height
    win.set_wm_normal_hints(hints)

    # --- Дополнительно: поднимаем окно сразу ---
    win.configure(stack_mode=X.Above)

    d.sync()
    d.close()

def position_bottom_center(xid: int):
    """Ставим окно внизу по центру экрана через X11."""
    d = xdisplay.Display()
    root = d.screen().root
    win = d.create_resource_object("window", xid)

    geom = win.get_geometry()
    w, h = geom.width, geom.height

    screen_w = d.screen().width_in_pixels
    screen_h = d.screen().height_in_pixels

    x = (screen_w - w) // 2
    y = screen_h - h - 20  # 20 px отступ от низа

    win.configure(x=x, y=y)
    d.sync()
    d.close()


# ---------- GTK ----------

class DockWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Mint Dock")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(520, 60)
        self.add_css_class("dock-window")
        self.set_can_focus(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        island = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        island.add_css_class("dock-island")

        label = Gtk.Label(label="Mint Dock — заготовка")
        label.add_css_class("dock-placeholder")
        island.append(label)

        box.append(island)
        self.set_child(box)

    def setup_x11(self):
        """Получаем XID окна и применяем X11-настройки."""
        surface = self.get_surface()
        if surface is None:
            return False

        # В GTK4 на X11 XID достаётся через GdkX11
        try:
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11
            x11_surface = surface
            xid = GdkX11.X11Surface.get_xid(x11_surface)
        except Exception as e:
            print("[dock] не удалось получить XID:", e)
            return False

        print(f"[dock] XID = {xid}")
        make_it_a_panel(xid)

        # Небольшая задержка, чтобы WM применил свойства, затем позиционируем
        GLib.timeout_add(200, lambda: (position_bottom_center(xid), False)[1])
        return False


class DockApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.mint.dock")

    def do_activate(self):
        win = DockWindow(self)
        win.present()
        # Даём GTK отрисовать окно и создать surface
        GLib.timeout_add(150, win.setup_x11)


def main():
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)

    app = DockApp()
    app.connect(
        "startup",
        lambda a: Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        ),
    )
    app.run(None)


if __name__ == "__main__":
    main()
