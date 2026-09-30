#!/usr/bin/env python3
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gdk, GLib

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


class DockWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Mint Dock")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(520, 60)

        # Прозрачный фон окна
        self.add_css_class("dock-window")

        # Всегда поверх и не забирает фокус
        # (в GTK4 это делается через свойства окна; полный "always on top"
        # через X11 EWMH сделаем позже, когда прикрутим позиционирование)
        self.set_can_focus(False)

        # Контейнер: пока просто placeholder внутри островка
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

    def move_to_bottom_center(self):
        """Позиционируем окно внизу по центру экрана."""
        display = Gdk.Display.get_default()
        monitors = display.get_monitors()
        if monitors.get_n_items() == 0:
            return
        monitor = monitors.get_item(0)
        geo = monitor.get_geometry()

        # Получаем реальный размер окна после отрисовки
        width, height = self.get_default_size()
        x = geo.x + (geo.width - width) // 2
        y = geo.y + geo.height - height - 20  # 20 px отступ от низа

        # В GTK4 прямого API для позиционирования топа нет,
        # поэтому используем X11-хак через GLib (см. ниже — сделаем позже).
        # Пока окно появится там, где его поставит WM.
        print(f"[dock] монитор: {geo.width}x{geo.height}, цель x={x}, y={y}")


class DockApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.mint.dock")

    def do_activate(self):
        win = DockWindow(self)
        win.present()
        GLib.timeout_add(100, lambda: (win.move_to_bottom_center(), False)[1])


def main():
    # Загружаем CSS
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)

    app = DockApp()
    app.connect("startup", lambda a: Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    ))
    app.run(None)


if __name__ == "__main__":
    main()
