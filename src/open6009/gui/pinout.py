"""Vector USB-6009 screw-terminal reference, verified against NI 371303n.

Source: https://download.ni.com/support/manuals/371303n.pdf, Figure 6.
The sketch is conceptual and shows the numbered analog and digital strips in
the published top-to-bottom order; it is not a scale drawing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .workspace import Workspace

PIN_LABELS = (
    "GND", "AI0 / AI0+", "AI4 / AI0−", "GND",
    "AI1 / AI1+", "AI5 / AI1−", "GND",
    "AI2 / AI2+", "AI6 / AI2−", "GND",
    "AI3 / AI3+", "AI7 / AI3−", "GND",
    "AO0", "AO1", "GND",
    "P0.0", "P0.1", "P0.2", "P0.3", "P0.4", "P0.5", "P0.6", "P0.7",
    "P1.0", "P1.1", "P1.2", "P1.3",
    "PFI0 / CTR0", "+2.5 V", "+5 V", "GND",
)

PIN_IDS = (
    None, "AI0", "AI4", None, "AI1", "AI5", None, "AI2", "AI6", None,
    "AI3", "AI7", None, "AO0", "AO1", None,
    *(f"P0.{bit}" for bit in range(8)), *(f"P1.{bit}" for bit in range(4)),
    "CTR0", None, None, None,
)


class PinoutView(QGraphicsView):
    """A fixed, scrollbar-free device view that scales with its dialog."""

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)


class PinoutDialog(QDialog):
    def __init__(self, workspace: Workspace, *, selected_pin: int | None = None,
                 parent: object = None) -> None:
        super().__init__(parent)
        self.workspace = workspace
        self.setWindowTitle("NI USB-6009 · Device Pinout")
        self.resize(770, 850)
        self.setMinimumSize(690, 760)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel(
            "Top-down schematic · analog 1–16 at left, digital 17–32 at right. "
            "Check physical labels before wiring."), 1)
        self.close_button = QPushButton("×")
        self.close_button.setFixedSize(34, 30)
        self.close_button.setToolTip("Close pinout")
        self.close_button.setAccessibleName("Close pinout")
        self.close_button.setStyleSheet(
            "QPushButton { color: #ffafbc; font-size: 20px; font-weight: 700; }"
            "QPushButton:hover { background: #713441; color: white; }")
        self.close_button.clicked.connect(self.reject)
        header.addWidget(self.close_button)
        layout.addLayout(header)
        self.scene = QGraphicsScene(self)
        self.view = PinoutView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setInteractive(True)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        layout.addWidget(self.view, 1)
        self.details = QLabel("Select a terminal to see its function and software assignment.")
        self.details.setWordWrap(True)
        self.details.setMinimumHeight(55)
        layout.addWidget(self.details)
        self.pin_items: dict[int, QGraphicsRectItem] = {}
        self.pin_pens: dict[int, QPen] = {}
        self._highlight_pin: int | None = None
        self._highlight_on = False
        self._highlight_steps = 0
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setInterval(220)
        self._highlight_timer.timeout.connect(self._blink_highlight)
        self._build_scene()
        self.scene.selectionChanged.connect(self._selection_changed)
        if selected_pin is not None:
            self.select_pin(selected_pin)

    def _build_scene(self) -> None:
        self.scene.addRect(305, 22, 120, 658, QPen(QColor("#617184")),
                           QBrush(QColor("#25313d")))
        self.scene.addText("USB-6009").setPos(324, 335)
        for pin, label in enumerate(PIN_LABELS, start=1):
            left = pin <= 16
            x = 12 if left else 445
            y = 30 + ((pin - 1) % 16) * 40
            signal_id = PIN_IDS[pin - 1]
            if signal_id is None:
                color = "#68747f" if label == "GND" else "#a77849"
            elif signal_id.startswith("AI"):
                color = "#287794"
            elif signal_id.startswith("AO"):
                color = "#987040"
            else:
                color = "#496d5c"
            rect = self.scene.addRect(0, 0, 260, 35, QPen(QColor("#8c9aa6")),
                                      QBrush(QColor(color)))
            rect.setPos(x, y)
            rect.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
            rect.setData(0, pin)
            rect.setToolTip(f"Terminal {pin} · {label}")
            text = self.scene.addText(f"{pin:02d}   {label}")
            text.setDefaultTextColor(QColor("white"))
            text.setParentItem(rect)
            text.setPos(8, 3)
            text.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.pin_items[pin] = rect
            self.pin_pens[pin] = QPen(rect.pen())
        self.scene.setSceneRect(0, 0, 720, 700)

    def select_pin(self, pin: int) -> None:
        if pin not in self.pin_items:
            raise ValueError("terminal must be 1..32")
        for item in self.pin_items.values():
            item.setSelected(False)
        self.pin_items[pin].setSelected(True)
        self._start_highlight(pin)

    def _start_highlight(self, pin: int) -> None:
        if self._highlight_pin is not None:
            self.pin_items[self._highlight_pin].setPen(self.pin_pens[self._highlight_pin])
        self._highlight_pin = pin
        self._highlight_on = False
        self._highlight_steps = 10
        self._highlight_timer.start()
        self._blink_highlight()

    def _blink_highlight(self) -> None:
        if self._highlight_pin is None:
            self._highlight_timer.stop()
            return
        item = self.pin_items[self._highlight_pin]
        self._highlight_on = not self._highlight_on
        if self._highlight_on:
            item.setPen(QPen(QColor("#ffe16b"), 4))
        else:
            item.setPen(self.pin_pens[self._highlight_pin])
        self._highlight_steps -= 1
        if self._highlight_steps <= 0:
            self._highlight_timer.stop()
            item.setPen(QPen(QColor("#ffe16b"), 3))

    def _selection_changed(self) -> None:
        selected = self.scene.selectedItems()
        if not selected:
            return
        pin = selected[0].data(0)
        label = PIN_LABELS[pin - 1]
        signal_id = PIN_IDS[pin - 1]
        if signal_id is None:
            setting = "Ground/reference terminal · not configurable in Open6009."
        else:
            signal = self.workspace.signals[signal_id]
            setting = (
                f"{signal.label} · {'active' if signal.active else 'inactive'} in workspace. "
                + ("Native analog input supported."
                   if signal_id.startswith("AI") else
                   "Check backend-supported direction and physical validation before use.")
            )
        self.details.setText(f"Terminal {pin}: {label}. {setting}")
