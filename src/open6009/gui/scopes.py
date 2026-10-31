"""Independent views into one shared, bounded analog display stream."""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from math import isfinite

import pyqtgraph as pg
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .workspace import Scope, Workspace

DIGITAL_COLORS = ("#5cc8ff", "#ffbc6b", "#a996ff", "#69d4af",
                  "#ff7d9d", "#e0d073", "#80aef2", "#d3a879",
                  "#6fc6c5", "#e19bc9", "#a9cf7b", "#d4a781")


def _eye_icon(visible: bool, color: QColor) -> QIcon:
    image = QPixmap(20, 20)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(color if visible else QColor("#778490"), 1.7))
    painter.drawEllipse(2, 6, 16, 8)
    if visible:
        painter.setBrush(color)
        painter.drawEllipse(8, 8, 4, 4)
    else:
        painter.drawLine(3, 16, 17, 3)
    painter.end()
    return QIcon(image)


class EyeKey(QListWidget):
    eye_clicked = Signal(str)

    def mousePressEvent(self, event: object) -> None:
        item = self.itemAt(event.position().toPoint())
        if (item is not None and event.position().x()
                <= self.visualItemRect(item).left() + 29):
            self.eye_clicked.emit(item.data(Qt.ItemDataRole.UserRole))
            event.accept()
            return
        super().mousePressEvent(event)


class TimeViewBox(pg.ViewBox):
    wheel_scrolled = Signal()

    def wheelEvent(self, event: object, axis: object = None) -> None:
        """The wheel pans elapsed time; it never zooms or pans voltage."""

        delta = event.delta() if hasattr(event, "delta") else event.angleDelta().y()
        low, high = self.viewRange()[0]
        width = high - low
        shift = -(delta / 120) * width * 0.15
        new_low = max(0.0, low + shift)
        self.setXRange(new_low, new_low + width, padding=0)
        self.wheel_scrolled.emit()
        event.accept()


class ScopeWidget(QWidget):
    edit_requested = Signal(str)
    delete_requested = Signal(str)
    duplicate_requested = Signal(str)
    reset_requested = Signal(str)
    transfer_requested = Signal(str, object, str)
    move_requested = Signal(str, int)
    collapsed_changed = Signal(str, bool)

    def __init__(self, scope: Scope, registry: Workspace, colors: list[str]) -> None:
        super().__init__()
        self.scope_id = scope.id
        self.kind = scope.kind
        self._registry = registry
        self._colors = colors
        self._curves: dict[str, object] = {}
        self._key_items: dict[str, QListWidgetItem] = {}
        self._visible: dict[str, bool] = {}
        self._digital_rendered_state: dict[
            str, tuple[int, float | None, bool, float | None]] = {}
        self._cleared_at: float | None = None
        self._collapsed = False
        self._hardware_range = 10.0
        self._time_window = 5.0
        self.setMinimumHeight(220)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        header = QHBoxLayout()
        self.collapse_button = QPushButton("▾")
        self.collapse_button.setToolTip("Collapse or expand this scope")
        self.collapse_button.setFixedWidth(34)
        self.collapse_button.clicked.connect(self._toggle_collapsed)
        header.addWidget(self.collapse_button)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: 700;")
        header.addWidget(self.title)
        header.addStretch()
        self.follow_live = QPushButton("Go live")
        self.follow_live.setCheckable(True)
        self.follow_live.setChecked(True)
        self.follow_live.setStyleSheet(
            "QPushButton:checked { background: #185b4d; border: 1px solid #39a486; }")
        header.addWidget(self.follow_live)
        actions = (
            ("−", "Show more time", lambda: self.zoom_time(1.5), True),
            ("+", "Show less time", lambda: self.zoom_time(1 / 1.5), True),
            ("Home", "Reset time and vertical range", self.home, False),
            ("Fit", "Fit visible analog data", self.fit_data, False),
            ("Edit", "Rename and choose scope signals", lambda: self.edit_requested.emit(
                self.scope_id), False),
            ("↑", "Move scope up", lambda: self.move_requested.emit(self.scope_id, -1), True),
            ("↓", "Move scope down", lambda: self.move_requested.emit(self.scope_id, 1), True),
            ("⧉", "Duplicate scope", lambda: self.duplicate_requested.emit(self.scope_id), True),
            ("Clear", "Clear this displayed history", lambda: self.reset_requested.emit(
                self.scope_id), False),
            ("×", "Delete scope view", lambda: self.delete_requested.emit(self.scope_id), True),
        )
        for text, tooltip, callback, compact in actions:
            button = QPushButton(text)
            button.setToolTip(tooltip)
            button.clicked.connect(callback)
            if compact:
                button.setFixedWidth(34)
            if text == "Fit":
                button.setVisible(self.kind == "analog")
                self.fit_button = button
            if text == "×":
                button.setStyleSheet(
                    "QPushButton { color: #ff9cae; }"
                    "QPushButton:hover { background: #713441; color: white; }")
            header.addWidget(button)
        layout.addLayout(header)
        self.body_widget = QWidget()
        body = QHBoxLayout(self.body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        self.key = EyeKey()
        self.key.setFixedWidth(170)
        self.key.setIconSize(QSize(20, 20))
        self.key.eye_clicked.connect(self._toggle_eye)
        self.key.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.key.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.key.customContextMenuRequested.connect(self._context_menu)
        body.addWidget(self.key)
        self.view_box = TimeViewBox(enableMenu=False)
        self.view_box.setMouseEnabled(x=True, y=False)
        self.view_box.setLimits(xMin=0, yMin=-10, yMax=10)
        self.view_box.wheel_scrolled.connect(lambda: self.follow_live.setChecked(False))
        self.plot = pg.PlotWidget(background="#111820", viewBox=self.view_box)
        self.plot.setLabel("left", "Digital state" if self.kind == "digital" else "Voltage",
                           units=None if self.kind == "digital" else "V")
        self.plot.setLabel("bottom", "Elapsed time", units="s")
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setClipToView(True)
        self.plot.setDownsampling(mode="peak")
        if self.kind != "digital":
            self.plot.setYRange(-self._hardware_range, self._hardware_range, padding=0)
        self.plot.setXRange(0, 5, padding=0)
        self.plot.getViewBox().sigRangeChangedManually.connect(self._manual_range)
        body.addWidget(self.plot, 1)
        layout.addWidget(self.body_widget, 1)
        self.note = QLabel()
        self.note.setStyleSheet("color: #93a2b0; font-size: 11px;")
        layout.addWidget(self.note)
        self.update_scope(scope)
        self.set_collapsed(scope.collapsed)
        self.set_hardware_range(10.0)

    def update_scope(self, scope: Scope) -> None:
        if scope.kind != self.kind:
            raise ValueError("scope kind cannot change")
        selected = {item.data(Qt.ItemDataRole.UserRole) for item in self.key.selectedItems()}
        self.title.setText(f"{scope.name}  ·  {len(scope.signals)} signal(s)")
        self.key.clear()
        self._key_items.clear()
        for signal_id in scope.signals:
            is_math = signal_id in self._registry.math
            label = self._registry._friendly_label(signal_id)
            if self.kind == "analog":
                handle = self._registry.math[signal_id].handle if is_math else signal_id
                identity = f"{handle}  ·  — V"
            else:
                identity = signal_id + " · Software-polled"
            item = QListWidgetItem(label + "\n" + identity)
            item.setData(Qt.ItemDataRole.UserRole, signal_id)
            channel = int(signal_id[2:]) if not is_math and self.kind == "analog" else None
            color = (DIGITAL_COLORS[int(signal_id[1]) * 8 + int(signal_id[3:])]
                     if self.kind == "digital" else
                     self._colors[channel] if channel is not None else "#c9a6e8")
            item.setForeground(QColor(color))
            item.setIcon(_eye_icon(self._visible.get(signal_id, True), QColor(color)))
            item.setToolTip("Click the eye to show or hide this trace in this scope only")
            self.key.addItem(item)
            self._key_items[signal_id] = item
            if signal_id in selected:
                item.setSelected(True)
            if signal_id not in self._curves:
                self._curves[signal_id] = self.plot.plot(
                    pen=pg.mkPen(color, width=1.5,
                                 style=Qt.PenStyle.DashLine if is_math
                                 else Qt.PenStyle.SolidLine))
            self._visible.setdefault(signal_id, True)
        for signal_id in tuple(self._curves):
            if signal_id not in scope.signals:
                self.plot.removeItem(self._curves.pop(signal_id))
                self._digital_rendered_state.pop(signal_id, None)
        self._signals = list(scope.signals)
        if self.kind == "digital":
            self._set_digital_axis(scope.signals)
        self.set_collapsed(scope.collapsed)

    def _set_digital_axis(self, signals: list[str]) -> None:
        count = max(1, len(signals))
        high = 2 * count + 0.5
        self.view_box.setLimits(yMin=-0.5, yMax=high)
        self.plot.setYRange(-0.5, high, padding=0)
        ticks = [(2 * (count - index - 1) + 0.5, signal_id)
                 for index, signal_id in enumerate(signals)]
        self.plot.getAxis("left").setTicks([ticks])
        self.note.setText("Software-polled states · timestamps are host observations, not edge times.")

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self.body_widget.setVisible(not collapsed)
        self.note.setVisible(not collapsed)
        self.collapse_button.setText("▸" if collapsed else "▾")
        self.setMinimumHeight(55 if collapsed else 220)

    def _toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)
        self.collapsed_changed.emit(self.scope_id, self._collapsed)

    def _manual_range(self, _ranges: object) -> None:
        self.follow_live.setChecked(False)

    def zoom_time(self, factor: float) -> None:
        low, high = self.plot.getViewBox().viewRange()[0]
        center = (low + high) / 2
        half = (high - low) * factor / 2
        self.follow_live.setChecked(False)
        self.plot.setXRange(max(0, center - half), max(0.001, center + half), padding=0)

    def home(self) -> None:
        if self.kind == "digital":
            self._set_digital_axis(self._signals)
        else:
            self.plot.setYRange(-self._hardware_range, self._hardware_range, padding=0)
        self.plot.setXRange(0, self._time_window, padding=0)
        self.follow_live.setChecked(False)

    def set_hardware_range(self, voltage_range: float) -> None:
        if self.kind == "digital":
            return
        self._hardware_range = voltage_range
        self.note.setText(
            f"Measured analog input · configured range ±{voltage_range:g} V · "
            "Fit data is an explicit zoom.")
        self.view_box.setLimits(yMin=-voltage_range, yMax=voltage_range)
        self.plot.setYRange(-voltage_range, voltage_range, padding=0)

    def fit_data(self) -> None:
        if self.kind == "digital":
            return
        samples = []
        for curve in self._curves.values():
            _, values = curve.getData()
            if values is not None:
                samples.extend(float(value) for value in values if isfinite(value))
        if samples:
            low, high = min(samples), max(samples)
            margin = max((high - low) * 0.05, self._hardware_range * 0.0001)
            low = max(-self._hardware_range, low - margin)
            high = min(self._hardware_range, high + margin)
            if high > low:
                self.plot.setYRange(low, high, padding=0)
        self.follow_live.setChecked(False)

    def render(self, times: deque[float], values: dict[int, deque[float]],
               math_values: dict[str, deque[float]], window_seconds: float,
               visible_channels: set[int]) -> None:
        if self.kind == "digital":
            return
        self._time_window = window_seconds
        if not times:
            return
        x = list(times)
        start = bisect_right(x, self._cleared_at) if self._cleared_at is not None else 0
        x = x[start:]
        if not x:
            return
        for signal_id, curve in self._curves.items():
            if signal_id in math_values:
                samples = list(math_values[signal_id])[start:]
                enabled = self._visible[signal_id]
            else:
                channel = int(signal_id[2:])
                samples = list(values[channel])[start:]
                enabled = self._visible[signal_id] and channel in visible_channels
            self._set_voltage_readout(signal_id, samples[-1] if samples else None)
            if enabled:
                curve.setData(x, samples)
            else:
                curve.setData([], [])
        if self.follow_live.isChecked():
            last = x[-1]
            self.plot.setXRange(max(0, last - window_seconds), max(window_seconds, last + 0.02),
                                padding=0)

    def _set_voltage_readout(self, signal_id: str, value: float | None) -> None:
        """Display the newest calibrated value without affecting trace visibility."""

        item = self._key_items.get(signal_id)
        if item is None or self.kind != "analog":
            return
        label = self._registry._friendly_label(signal_id)
        handle = (self._registry.math[signal_id].handle
                  if signal_id in self._registry.math else signal_id)
        reading = f"{value:+.5f} V" if value is not None and isfinite(value) else "— V"
        text = f"{label}\n{handle}  ·  {reading}"
        if item.text() != text:
            item.setText(text)

    def render_digital(self, history: dict[str, deque[tuple[float, int]]],
                       window_seconds: float) -> None:
        if self.kind != "digital":
            return
        self._time_window = window_seconds
        latest = 0.0
        for index, signal_id in enumerate(self._signals):
            source = history[signal_id]
            state = (len(source), source[-1][0] if source else None,
                     self._visible[signal_id], self._cleared_at)
            if source and self._visible[signal_id]:
                latest = max(latest, source[-1][0])
            if self._digital_rendered_state.get(signal_id) == state:
                continue
            self._digital_rendered_state[signal_id] = state
            curve = self._curves[signal_id]
            points = list(source)
            if self._cleared_at is not None:
                points = [(time, bit) for time, bit in points if time > self._cleared_at]
            if not points or not self._visible[signal_id]:
                curve.setData([], [])
                continue
            base = 2 * (len(self._signals) - index - 1)
            xs = [points[0][0]]
            ys = [base + points[0][1]]
            for time, bit in points[1:]:
                xs.extend((time, time))
                ys.extend((ys[-1], base + bit))
            curve.setData(xs, ys, symbol="o" if len(points) == 1 else None,
                          symbolSize=6)
        if latest and self.follow_live.isChecked():
            self.plot.setXRange(max(0, latest - window_seconds),
                                max(window_seconds, latest + 0.02), padding=0)

    def clear_view(self, latest_time: float | None) -> None:
        self._cleared_at = latest_time
        for curve in self._curves.values():
            curve.setData([], [])

    def _context_menu(self, point: object) -> None:
        item = self.key.itemAt(point)
        if item is None:
            return
        if not item.isSelected():
            self.key.clearSelection()
            item.setSelected(True)
        selected = tuple(row.data(Qt.ItemDataRole.UserRole) for row in self.key.selectedItems())
        if not selected:
            return
        menu = QMenu(self)
        visibility = "Hide" if all(self._visible.get(key, True) for key in selected) else "Show"
        menu.addAction(visibility, lambda: self._set_visible(selected, visibility == "Show"))
        menu.addSeparator()
        for label, operation in (
            ("Copy to new scope", "copy_new"),
            ("Move to new scope", "move_new"),
            ("Copy to existing scope", "copy_existing"),
            ("Move to existing scope", "move_existing"),
        ):
            menu.addAction(label, lambda _checked=False, op=operation:
                           self.transfer_requested.emit(self.scope_id, selected, op))
        menu.exec(self.key.mapToGlobal(point))

    def _set_visible(self, selected: tuple[str, ...], visible: bool) -> None:
        for signal_id in selected:
            self._visible[signal_id] = visible
            for index in range(self.key.count()):
                item = self.key.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == signal_id:
                    color = item.foreground().color()
                    item.setIcon(_eye_icon(visible, color))
                    break
            if not visible and signal_id in self._curves:
                self._curves[signal_id].setData([], [])

    def _toggle_eye(self, signal_id: str) -> None:
        self._set_visible((signal_id,), not self._visible[signal_id])
