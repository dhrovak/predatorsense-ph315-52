"""PredatorSense window."""

import os
import sys
import tempfile
import time
from collections import deque

from PyQt6.QtCore import QObject, QPointF, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from . import rgb
from .client import ServiceError, request

POLL_MS = 1000
APPLY_DELAY_MS = 350
ACTION_ERROR_SECONDS = 8
HISTORY = 60
ICON_PATH = "/opt/predator-sense/predator-sense.svg"
ACCENT = "#e3263b"
BIG_TEXT = "font-size: 24pt; font-weight: bold;"

STYLE = f"""
QWidget {{ background: #15171c; color: #e6e6e6; font-size: 10pt; }}
QGroupBox {{ border: 1px solid #2c2f36; border-radius: 8px; margin-top: 14px; padding: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #9aa0aa; }}
QPushButton {{ background: #23262d; border: 1px solid #343842; border-radius: 6px; padding: 10px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: bold; }}
QPushButton:disabled {{ color: #666; }}
QComboBox {{ background: #23262d; border: 1px solid #343842; border-radius: 6px; padding: 6px; }}
QSlider::groove:horizontal {{ height: 6px; background: #343842; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: white; width: 16px; margin: -6px 0; border-radius: 8px; }}
QLabel#banner {{ background: #5a1d24; border-radius: 6px; padding: 8px; }}
QFrame#card {{ background: #1d2026; border-radius: 8px; }}
"""


def _temp_color(t):
    if t is None:
        return "#9aa0aa"
    if t >= 90:
        return "#ff4d4d"
    if t >= 75:
        return "#ffb020"
    return "#4cd964"


class TempChart(QWidget):
    LOW, HIGH = 30, 100

    def __init__(self):
        super().__init__()
        self.cpu = deque(maxlen=HISTORY)
        self.gpu = deque(maxlen=HISTORY)
        self.setMinimumHeight(90)

    def add(self, cpu, gpu):
        self.cpu.append(cpu)
        self.gpu.append(gpu)
        self.update()

    def _line(self, painter, values, color):
        w, h = self.width(), self.height()
        step = w / (HISTORY - 1)
        offset = HISTORY - len(values)
        painter.setPen(QPen(QColor(color), 2))
        segment = []
        # Missing readings break the line instead of being bridged.
        for i, v in enumerate(list(values) + [None]):
            if v is None:
                if len(segment) > 1:
                    painter.drawPolyline(segment)
                segment = []
                continue
            v = min(max(v, self.LOW), self.HIGH)
            segment.append(QPointF((offset + i) * step, h - (v - self.LOW) / (self.HIGH - self.LOW) * h))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#1d2026"))
        p.setPen(QPen(QColor("#2c2f36"), 1))
        for t in (50, 70, 90):
            y = self.height() - (t - self.LOW) / (self.HIGH - self.LOW) * self.height()
            p.drawLine(0, int(y), self.width(), int(y))
            p.drawText(4, int(y) - 2, f"{t}°")
        series = [("CPU", self.cpu, ACCENT), ("GPU", self.gpu, "#3fa7ff")]
        shown = [(name, color) for name, values, color in series if any(v is not None for v in values)]
        for _name, values, color in series:
            self._line(p, values, color)
        for i, (name, color) in enumerate(reversed(shown)):
            p.setPen(QColor(color))
            p.drawText(self.width() - 50 - 40 * i, 14, name)


class Card(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet("color: #9aa0aa; background: transparent;")
        self.temp = QLabel("--°C")
        self.temp.setStyleSheet(f"background: transparent; {BIG_TEXT}")
        self.rpm = QLabel("-- rpm")
        self.rpm.setStyleSheet("color: #9aa0aa; background: transparent;")
        for w in (heading, self.temp, self.rpm):
            layout.addWidget(w, alignment=Qt.AlignmentFlag.AlignHCenter)

    def show_values(self, temp, rpm):
        self.temp.setText("—" if temp is None else f"{temp}°C")
        self.temp.setStyleSheet(f"background: transparent; {BIG_TEXT} color: {_temp_color(temp)};")
        self.rpm.setText("— rpm" if rpm is None else f"{rpm} rpm")


class ColorButton(QPushButton):
    def __init__(self, on_change, label=""):
        super().__init__(label)
        self._on_change = on_change
        self.color = "#ff0000"
        self.setMinimumHeight(34)
        self.clicked.connect(self._pick)

    def set_color(self, color):
        self.color = color
        text = "#000" if QColor(color).lightness() > 140 else "#fff"
        self.setStyleSheet(f"background: {color}; color: {text}; border-radius: 6px;")

    def _pick(self):
        chosen = QColorDialog.getColor(QColor(self.color), self, "Choose a color")
        if chosen.isValid():
            self.set_color(chosen.name())
            self._on_change()


class ServiceWorker(QObject):
    """Talks to the service off the UI thread; requests run one at a time, in order."""

    done = pyqtSignal(str, object, str)  # cmd, data, error ("" on success)

    @pyqtSlot(str, object)
    def call(self, cmd, params):
        try:
            data = request(cmd, **params)
        except ServiceError as e:
            self.done.emit(cmd, None, str(e))
        else:
            self.done.emit(cmd, data, "")


class Window(QWidget):
    send = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PredatorSense")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.setMinimumWidth(460)
        self._fan_busy_until = 0.0
        self._rgb_loaded = False
        self._status_pending = False
        self._status_messages = []
        self._action_error = None
        self._action_error_until = 0.0

        root = QVBoxLayout(self)
        title = QLabel("PREDATOR SENSE")
        title.setStyleSheet(f"color: {ACCENT}; font-size: 16pt; font-weight: bold; letter-spacing: 3px;")
        root.addWidget(title, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.hide()
        root.addWidget(self.banner)

        root.addWidget(self._build_monitor())
        root.addWidget(self._build_fans())
        root.addWidget(self._build_keyboard())
        root.addStretch()

        self._fan_timer = self._debounce(self._send_custom_fans)
        self._rgb_timer = self._debounce(self._send_rgb)

        self._worker_thread = QThread(self)
        self._worker = ServiceWorker()
        self._worker.moveToThread(self._worker_thread)
        self.send.connect(self._worker.call)
        self._worker.done.connect(self._on_reply)
        self._worker_thread.start()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self.refresh)
        self._poll_timer.start(POLL_MS)
        self.refresh()

    def closeEvent(self, event):
        self._poll_timer.stop()
        self._worker_thread.quit()
        self._worker_thread.wait()
        super().closeEvent(event)

    def _debounce(self, fn):
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(APPLY_DELAY_MS)
        timer.timeout.connect(fn)
        return timer

    # --- layout ------------------------------------------------------------

    def _build_monitor(self):
        box = QGroupBox("Monitor")
        layout = QVBoxLayout(box)
        cards = QHBoxLayout()
        self.cpu_card, self.gpu_card = Card("CPU"), Card("GPU")
        cards.addWidget(self.cpu_card)
        cards.addWidget(self.gpu_card)
        layout.addLayout(cards)
        self.chart = TempChart()
        layout.addWidget(self.chart)
        return box

    def _build_fans(self):
        self.fan_box = QGroupBox("Fans")
        layout = QVBoxLayout(self.fan_box)
        buttons = QHBoxLayout()
        self.fan_group = QButtonGroup(self)
        self.fan_buttons = {}
        for key, text in (("auto", "Auto"), ("turbo", "Turbo"), ("custom", "Custom")):
            b = QPushButton(text)
            b.setCheckable(True)
            self.fan_group.addButton(b)
            self.fan_buttons[key] = b
            buttons.addWidget(b)
            b.clicked.connect(lambda _checked, k=key: self._fan_mode_clicked(k))
        layout.addLayout(buttons)

        self.custom_panel = QWidget()
        grid = QGridLayout(self.custom_panel)
        grid.setContentsMargins(0, 6, 0, 0)
        self.fan_sliders, self.fan_labels = {}, {}
        for row, fan in enumerate(("cpu", "gpu")):
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setSingleStep(5)
            slider.setPageStep(10)
            slider.setValue(50)
            label = QLabel("50%")
            label.setMinimumWidth(40)
            slider.valueChanged.connect(lambda v, l=label: (l.setText(f"{v}%"), self._fan_timer.start()))
            grid.addWidget(QLabel(f"{fan.upper()} fan"), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(label, row, 2)
            self.fan_sliders[fan], self.fan_labels[fan] = slider, label
        self.custom_panel.hide()
        layout.addWidget(self.custom_panel)
        return self.fan_box

    def _build_keyboard(self):
        self.kb_box = QGroupBox("Keyboard lighting")
        grid = QGridLayout(self.kb_box)

        self.effect = QComboBox()
        self.effect.addItems([e.capitalize() for e in rgb.EFFECTS])
        self.effect.currentIndexChanged.connect(self._effect_changed)
        grid.addWidget(QLabel("Effect"), 0, 0)
        grid.addWidget(self.effect, 0, 1)

        self.color_label = QLabel("Color")
        self.color_button = ColorButton(self._rgb_changed)
        grid.addWidget(self.color_label, 1, 0)
        grid.addWidget(self.color_button, 1, 1)

        self.zones_label = QLabel("Zones")
        zones = QWidget()
        zl = QHBoxLayout(zones)
        zl.setContentsMargins(0, 0, 0, 0)
        self.zone_buttons = [ColorButton(self._rgb_changed, str(i + 1)) for i in range(rgb.ZONES)]
        for b in self.zone_buttons:
            zl.addWidget(b)
        self.zones_widget = zones
        grid.addWidget(self.zones_label, 2, 0)
        grid.addWidget(zones, 2, 1)

        self.brightness = self._slider(0, 100, 100)
        grid.addWidget(QLabel("Brightness"), 3, 0)
        grid.addWidget(self.brightness, 3, 1)

        self.speed_label = QLabel("Speed")
        self.speed = self._slider(1, 9, 4)
        grid.addWidget(self.speed_label, 4, 0)
        grid.addWidget(self.speed, 4, 1)

        self.direction_label = QLabel("Direction")
        self.direction = QComboBox()
        self.direction.addItems(["Right to left", "Left to right"])
        self.direction.currentIndexChanged.connect(self._rgb_changed)
        grid.addWidget(self.direction_label, 5, 0)
        grid.addWidget(self.direction, 5, 1)

        self.kb_timeout = QCheckBox("Turn off after 30 seconds idle")
        self.kb_timeout.setChecked(True)
        self.kb_timeout.toggled.connect(lambda on: self._send("set_kb_timeout", on=on))
        grid.addWidget(self.kb_timeout, 6, 0, 1, 2)
        self._update_rgb_visibility()
        return self.kb_box

    def _slider(self, low, high, value):
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(low, high)
        s.setValue(value)
        s.valueChanged.connect(self._rgb_changed)
        return s

    # --- service communication ---------------------------------------------

    def _send(self, cmd, **params):
        self.send.emit(cmd, params)

    def _on_reply(self, cmd, data, error):
        if cmd == "status":
            self._status_pending = False
            if error:
                self._status_messages = [error]
                self.fan_box.setEnabled(False)
                self.kb_box.setEnabled(False)
                self._update_banner()
            else:
                self._apply_status(data)
            return
        if error:
            self._action_error = error
            self._action_error_until = time.monotonic() + ACTION_ERROR_SECONDS
        else:
            self._action_error = None
        self._update_banner()

    def _update_banner(self):
        messages = list(self._status_messages)
        if self._action_error and time.monotonic() < self._action_error_until:
            messages.insert(0, self._action_error)
        messages = list(dict.fromkeys(messages))
        self.banner.setText("\n".join(messages))
        self.banner.setVisible(bool(messages))

    def refresh(self):
        if self._status_pending:
            return
        self._status_pending = True
        self._send("status")

    def _apply_status(self, data):
        messages = [m for m in (data.get("error"), data.get("notice"), data.get("fan_control_problem")) if m]
        if not data["keyboard"]:
            messages.append("Keyboard RGB device not found (facer module not loaded).")
        self._status_messages = messages
        self._update_banner()

        self.fan_box.setEnabled(bool(data["fan_control"] and data["sensors"]))
        self.kb_box.setEnabled(bool(data["keyboard"]))
        self.kb_timeout.setEnabled(bool(data["kb_timeout_control"]))
        if not self._rgb_loaded:
            self.kb_timeout.blockSignals(True)
            self.kb_timeout.setChecked(bool(data["kb_timeout"]))
            self.kb_timeout.blockSignals(False)
            self._load_rgb(data["rgb"])

        s = data.get("sensors")
        if s:
            self.cpu_card.show_values(s["cpu_temp"], s["cpu_rpm"])
            self.gpu_card.show_values(s["gpu_temp"], s["gpu_rpm"])
            self.chart.add(s["cpu_temp"], s["gpu_temp"])
            if self.fan_box.isEnabled() and time.monotonic() > self._fan_busy_until:
                self._sync_fans(s)

    def _sync_fans(self, s):
        modes = (s["cpu_fan"]["mode"], s["gpu_fan"]["mode"])
        key = "auto" if modes == ("auto", "auto") else "turbo" if modes == ("max", "max") else "custom"
        self.fan_buttons[key].setChecked(True)
        self.custom_panel.setVisible(key == "custom")
        if key == "custom" and not self._fan_timer.isActive():
            for fan in ("cpu", "gpu"):
                f = s[fan + "_fan"]
                if f["mode"] == "manual" and not self.fan_sliders[fan].isSliderDown():
                    self.fan_sliders[fan].blockSignals(True)
                    self.fan_sliders[fan].setValue(f["speed"])
                    self.fan_sliders[fan].blockSignals(False)
                    self.fan_labels[fan].setText(f"{f['speed']}%")

    def _hold_fan_sync(self):
        self._fan_busy_until = time.monotonic() + 2.0

    def _fan_mode_clicked(self, key):
        self._hold_fan_sync()
        self.custom_panel.setVisible(key == "custom")
        if key == "auto":
            self._send("set_fans", cpu="auto", gpu="auto")
        elif key == "turbo":
            self._send("set_turbo", on=True)
        else:
            self._send_custom_fans()

    def _send_custom_fans(self):
        if not self.fan_buttons["custom"].isChecked():
            return
        self._hold_fan_sync()
        self._send("set_fans", **{fan: slider.value() for fan, slider in self.fan_sliders.items()})

    # --- keyboard ----------------------------------------------------------

    def _load_rgb(self, settings):
        widgets = (self.effect, self.brightness, self.speed, self.direction)
        for w in widgets:
            w.blockSignals(True)
        self.effect.setCurrentIndex(list(rgb.EFFECTS).index(settings["effect"]))
        self.brightness.setValue(settings["brightness"])
        self.speed.setValue(max(1, settings["speed"]))
        self.direction.setCurrentIndex(list(rgb.DIRECTIONS).index(settings["direction"]))
        self.color_button.set_color(settings["color"])
        for b, c in zip(self.zone_buttons, settings["zones"]):
            b.set_color(c)
        for w in widgets:
            w.blockSignals(False)
        self._update_rgb_visibility()
        self._rgb_loaded = True

    def _current_effect(self):
        return list(rgb.EFFECTS)[self.effect.currentIndex()]

    def _update_rgb_visibility(self):
        effect = self._current_effect()
        for w in (self.color_label, self.color_button):
            w.setVisible(effect in rgb.EFFECTS_WITH_COLOR)
        for w in (self.zones_label, self.zones_widget):
            w.setVisible(effect == "static")
        for w in (self.speed_label, self.speed):
            w.setVisible(effect != "static")
        for w in (self.direction_label, self.direction):
            w.setVisible(effect in rgb.EFFECTS_WITH_DIRECTION)

    def _effect_changed(self):
        self._update_rgb_visibility()
        self._rgb_changed()

    def _rgb_changed(self):
        if self._rgb_loaded:
            self._rgb_timer.start()

    def _send_rgb(self):
        self._send("set_rgb", settings={
            "effect": self._current_effect(),
            "brightness": self.brightness.value(),
            "speed": self.speed.value(),
            "direction": list(rgb.DIRECTIONS)[self.direction.currentIndex()],
            "color": self.color_button.color,
            "zones": [b.color for b in self.zone_buttons],
        })


def _instance_path():
    # The per-user runtime dir is private, so no other user can squat on the name.
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "predator-sense-gui")
    return os.path.join(tempfile.gettempdir(), f"predator-sense-gui-{os.getuid()}")


def _activation_token():
    return os.environ.get("XDG_ACTIVATION_TOKEN") or os.environ.get("DESKTOP_STARTUP_ID") or ""


def _signal_running_instance(path):
    """Ask an already open window to come to the front; False if none is running."""
    sock = QLocalSocket()
    sock.connectToServer(path)
    if not sock.waitForConnected(500):
        return False
    sock.write(f"show {_activation_token()}\n".encode())
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def _listen_for_instances(path, window):
    server = QLocalServer(window)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    QLocalServer.removeServer(path)
    if not server.listen(path):
        return None

    def on_connection():
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def on_ready():
            parts = bytes(conn.readLine(256)).decode(errors="replace").split()
            if parts[:1] == ["show"]:
                if len(parts) > 1:
                    # Qt hands this token to the compositor so it lets the window take focus.
                    os.environ["XDG_ACTIVATION_TOKEN"] = parts[1]
                window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
                window.show()
                window.raise_()
                window.activateWindow()
            conn.disconnectFromServer()

        conn.readyRead.connect(on_ready)
        conn.disconnected.connect(conn.deleteLater)

    server.newConnection.connect(on_connection)
    return server


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PredatorSense")
    app.setDesktopFileName("predator-sense")
    path = _instance_path()
    if _signal_running_instance(path):
        return 0
    app.setStyleSheet(STYLE)
    window = Window()
    window._instance_server = _listen_for_instances(path, window)
    window.show()
    return app.exec()
