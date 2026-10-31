"""Open6009 instrument window."""

from __future__ import annotations

from collections import deque
from math import nan
from pathlib import Path
from time import monotonic

import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from open6009.acquisition.config import DIFFERENTIAL_RANGES, TerminalMode

from .model import AcquisitionSettings, PlotBatch
from .pinout import PinoutDialog
from .projects import (
    export_recording_csv,
    load_project,
    load_workspace,
    save_project,
    save_workspace,
)
from .scopes import DIGITAL_COLORS, ScopeWidget
from .worker import DeviceWorker
from .workspace import AI_PINS, Scope, Workspace

CHANNEL_COLORS = ("#5cc8ff", "#ffbc6b", "#a996ff", "#69d4af",
                  "#ff7d9d", "#e0d073", "#80aef2", "#d3a879")

DARK = """
QWidget { background: #151a21; color: #e6edf4; font-size: 13px; }
QMainWindow, QScrollArea { background: #11161c; }
QGroupBox { border: 1px solid #303a45; border-radius: 9px; margin-top: 13px;
            padding: 12px 8px 8px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
QPushButton { background: #26313d; border: 1px solid #3c4956; border-radius: 6px;
              padding: 7px 11px; min-height: 18px; }
QPushButton:hover { background: #344353; }
QPushButton:disabled { color: #73808d; background: #1d252d; }
QPushButton#startButton { background: #18765c; border-color: #229678; color: white; }
QPushButton#stopButton { background: #804045; border-color: #9b5158; color: white; }
QPushButton#startButton:disabled, QPushButton#stopButton:disabled {
    background: #1d252d; border-color: #303a45; color: #73808d; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #202a34;
    border: 1px solid #3a4652; border-radius: 5px; padding: 5px; }
QStatusBar { border-top: 1px solid #303a45; color: #a7b5c4; }
QSplitter::handle { background: #303a45; }
"""

LIGHT = """
QWidget { background: #f1f4f7; color: #1c2732; font-size: 13px; }
QGroupBox { border: 1px solid #c3cbd3; border-radius: 9px; margin-top: 13px;
            padding: 12px 8px 8px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
QPushButton { background: #e3eaf0; border: 1px solid #b8c4cf; border-radius: 6px;
              padding: 7px 11px; }
QPushButton#startButton { background: #208263; color: white; }
QPushButton#stopButton { background: #a65459; color: white; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: white;
    border: 1px solid #b8c4cf; border-radius: 5px; padding: 5px; }
QSplitter::handle { background: #c3cbd3; }
"""


class MainWindow(QMainWindow):
    def __init__(self, worker: DeviceWorker | None = None) -> None:
        super().__init__()
        self.settings = QSettings("Open6009", "Open6009")
        self.worker = worker or DeviceWorker()
        self._state = "Disconnected"
        self._connected = False
        self._closing = False
        self._worker_done = False
        self._samples = 0
        self._last_error = ""
        self._digital_output_ports: set[int] = set()
        self._digital_poll_pending: set[int] = set()
        self._digital_poll_paused = False
        self._digital_acquiring = False
        self._combined_digital = False
        self._digital_generation = 0
        self._digital_counts = {0: 0, 1: 0}
        self._digital_target: int | None = None
        self._digital_epoch: float | None = None
        self._digital_history: dict[str, deque[tuple[float, int]]] = {
            **{f"P0.{bit}": deque(maxlen=20000) for bit in range(8)},
            **{f"P1.{bit}": deque(maxlen=20000) for bit in range(4)},
        }
        self._project_path: Path | None = None
        self._last_recording_path: Path | None = None
        self._last_recording_meta: dict[str, object] = {}
        self._last_started_settings: AcquisitionSettings | None = None
        self._loaded_project_has_data = False
        self._project_include_data = False
        self._time: deque[float] = deque(maxlen=20000)
        self._values = {channel: deque(maxlen=20000) for channel in range(8)}
        self._math_values: dict[str, deque[float]] = {}
        self.workspace = Workspace()
        self.scope_widgets: list[ScopeWidget] = []
        self._undo_stack: list[dict[str, object]] = []
        self._redo_stack: list[dict[str, object]] = []
        self._build()
        self._wire_worker()
        self._apply_theme(self.settings.value("theme", "dark"))
        geometry = self.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter_state = self.settings.value("splitter")
        if splitter_state is not None:
            self.splitter.restoreState(splitter_state)
        scope_state = self.settings.value("scope_splitter")
        if scope_state is not None:
            self.scope_splitter.restoreState(scope_state)
        self._set_state("Disconnected", "Connect the USB-6009 to begin")
        self.worker.start()
        self.worker.submit("probe")
        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(50)
        self.plot_timer.timeout.connect(self._refresh_plot)
        self.plot_timer.start()
        self.digital_poll_timer = QTimer(self)
        self.digital_poll_timer.timeout.connect(self._poll_digital_inputs)
        self._set_digital_poll_interval()

    def _build(self) -> None:
        self.setWindowTitle("Open6009 · USB DAQ")
        self.resize(1440, 880)
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(9)
        toolbar = QHBoxLayout()
        brand = QLabel("OPEN6009")
        brand.setStyleSheet("font-size: 19px; font-weight: 750; letter-spacing: 1px;")
        toolbar.addWidget(brand)
        self.device_name = QLabel("No device connected")
        self.device_name.setStyleSheet("color: #9caeba; padding-left: 14px;")
        toolbar.addWidget(self.device_name)
        toolbar.addStretch()
        self.state_label = QLabel("● Disconnected")
        toolbar.addWidget(self.state_label)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._toggle_connection)
        toolbar.addWidget(self.connect_button)
        self.start_button = QPushButton("▶  Start")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self._start)
        toolbar.addWidget(self.start_button)
        self.stop_button = QPushButton("■  Stop")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.clicked.connect(self._stop)
        toolbar.addWidget(self.stop_button)
        self.toolbar_record = QCheckBox("Record")
        toolbar.addWidget(self.toolbar_record)
        self.pinout_button = QPushButton("▦  Pinout")
        self.pinout_button.setToolTip("Open the 32-terminal device pin assignment")
        self.pinout_button.clicked.connect(lambda: self._show_pinout())
        toolbar.addWidget(self.pinout_button)
        self.theme_button = QPushButton("Theme")
        self.theme_button.setToolTip("Toggle dark and light themes")
        self.theme_button.clicked.connect(self._toggle_theme)
        toolbar.addWidget(self.theme_button)
        root.addLayout(toolbar)
        filebar = QHBoxLayout()
        for label, callback in (
            ("New", self._new_workspace),
            ("Open", self._open_workspace),
            ("Save", self._save),
            ("Save As", self._save_as),
            ("Export CSV", self._export_csv),
            ("+ Analog Scope", lambda: self._add_scope_dialog("analog")),
            ("+ Digital Scope", lambda: self._add_scope_dialog("digital")),
            ("+ Math", self._manage_math),
            ("Record Folder…", self._choose_record_directory),
            ("Reset Workspace", self._reset_workspace),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            button.setToolTip(label)
            filebar.addWidget(button)
        filebar.addStretch()
        root.addLayout(filebar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self._left_panel())
        self.splitter.addWidget(self._waveform_panel())
        self.splitter.addWidget(self._right_panel())
        self.splitter.setSizes([315, 800, 315])
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)
        self.toolbar_record.toggled.connect(self.record_enabled.setChecked)
        self.record_enabled.toggled.connect(self.toolbar_record.setChecked)
        self.setCentralWidget(central)
        self.status_connection = QLabel("USB: disconnected")
        self.status_acquisition = QLabel("Idle")
        self.status_samples = QLabel("0 scans")
        self.status_throughput = QLabel("— S/s measured")
        self.status_record = QLabel("Recording off")
        for widget in (self.status_connection, self.status_acquisition,
                       self.status_samples, self.status_throughput, self.status_record):
            self.statusBar().addPermanentWidget(widget)
        start_action = QAction("Start acquisition", self)
        start_action.setShortcut(QKeySequence("Ctrl+Return"))
        start_action.triggered.connect(self._start)
        self.addAction(start_action)
        stop_action = QAction("Stop acquisition", self)
        stop_action.setShortcut(QKeySequence("Escape"))
        stop_action.triggered.connect(self._stop)
        self.addAction(stop_action)
        file_menu = self.menuBar().addMenu("File")
        for label, callback, shortcut in (
            ("New Workspace", self._new_workspace, "Ctrl+N"),
            ("Open Project or Preset", self._open_workspace, "Ctrl+O"),
            ("Save", self._save, "Ctrl+S"),
            ("Save As", self._save_as, "Ctrl+Shift+S"),
            ("Export CSV", self._export_csv, "Ctrl+E"),
            ("Reset Workspace", self._reset_workspace, ""),
        ):
            action = file_menu.addAction(label, callback)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction("Add Analog Scope", lambda: self._add_scope_dialog("analog"))
        view_menu.addAction("Add Digital Scope", lambda: self._add_scope_dialog("digital"))
        view_menu.addAction("Reset Hardware Range", self._reset_view)
        view_menu.addAction("Dark Theme", lambda: self._apply_theme("dark"))
        view_menu.addAction("Light Theme", lambda: self._apply_theme("light"))
        edit_menu = self.menuBar().addMenu("Edit")
        undo = edit_menu.addAction("Undo workspace edit", self._undo)
        undo.setShortcut(QKeySequence("Ctrl+Z"))
        redo = edit_menu.addAction("Redo workspace edit", self._redo)
        redo.setShortcut(QKeySequence("Ctrl+Shift+Z"))
        tools_menu = self.menuBar().addMenu("Tools")
        tools_menu.addAction("Math Channel Manager", self._manage_math)
        help_menu = self.menuBar().addMenu("Help")
        help_menu.addAction("About Open6009", self._about)
        help_menu.addAction("About This Device", self._about_device)
        help_menu.addAction("Documentation", self._open_documentation)
        help_menu.addAction("Keyboard Shortcuts", self._shortcuts)

    def _left_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(300)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(12)
        heading = QLabel("DEVICE & CHANNELS")
        heading.setStyleSheet("font-size: 11px; font-weight: 700; color: #94a5b7;")
        layout.addWidget(heading)
        inputs = QGroupBox("Analog inputs")
        grid = QVBoxLayout(inputs)
        self.channel_enabled: list[QPushButton] = []
        self.channel_labels: list[QPushButton] = []
        self.channel_names: list[QLineEdit] = []
        self.channel_colors = [str(self.settings.value(f"ai{channel}_color", color))
                               for channel, color in enumerate(CHANNEL_COLORS)]
        for channel in range(8):
            enabled = QPushButton()
            enabled.setCheckable(True)
            enabled.setChecked(False)
            enabled.setFixedHeight(48)
            enabled.setToolTip(f"Click to enable AI{channel}; right-click for name, color, pinout")
            enabled.clicked.connect(lambda checked, ch=channel: self._channel_toggled(ch, checked))
            name = QLineEdit(str(self.settings.value(f"ai{channel}_name", "")), inputs)
            name.setMaxLength(64)
            name.hide()  # The card context menu owns editing; no extra input widget is shown.
            enabled.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            enabled.customContextMenuRequested.connect(
                lambda point, ch=channel, button=enabled:
                self._channel_menu(ch, button, point))
            grid.addWidget(enabled)
            self.channel_enabled.append(enabled)
            self.channel_labels.append(enabled)
            self.channel_names.append(name)
            self.workspace.rename(f"AI{channel}", name.text())
            self._style_channel_card(channel)
        layout.addWidget(inputs)

        digital = QGroupBox("Digital inputs · software-polled")
        digital_grid = QVBoxLayout(digital)
        self.digital_enabled: dict[str, QPushButton] = {}
        self.digital_names: dict[str, QLineEdit] = {}
        for offset in range(12):
            signal_id = f"P0.{offset}" if offset < 8 else f"P1.{offset - 8}"
            name = QLineEdit(str(self.settings.value(
                f"{signal_id.replace('.', '_').lower()}_name", "")), digital)
            name.setMaxLength(64)
            name.hide()
            self.workspace.rename(signal_id, name.text())
            card = QPushButton()
            card.setCheckable(True)
            card.setFixedHeight(48)
            card.setToolTip(f"Click to poll {signal_id}; right-click to rename or locate terminal")
            card.clicked.connect(lambda checked, key=signal_id:
                                 self._digital_toggled(key, checked))
            card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            card.customContextMenuRequested.connect(
                lambda point, key=signal_id, button=card:
                self._digital_card_menu(key, button, point))
            digital_grid.addWidget(card)
            self.digital_enabled[signal_id] = card
            self.digital_names[signal_id] = name
            self._style_digital_card(signal_id)
        note = QLabel("Cards choose digital lines; Start begins polling and Stop ends it. "
                      "Analog and digital capture cannot run simultaneously. Timestamps mark host reads, "
                      "not exact hardware edges. Port reads may set the whole port to input.")
        note.setWordWrap(True)
        digital_grid.addWidget(note)
        controls = QHBoxLayout()
        self.digital_read_buttons: list[QPushButton] = []
        self.digital_states: list[QLabel] = []
        for port in range(2):
            read = QPushButton(f"Read P{port}")
            state = QLabel("—")
            read.clicked.connect(lambda _checked=False, p=port: self._read_port(p))
            controls.addWidget(read)
            controls.addWidget(state)
            self.digital_read_buttons.append(read)
            self.digital_states.append(state)
        digital_grid.addLayout(controls)
        layout.addWidget(digital)

        counter = QGroupBox("Counter · CTR0")
        counter_layout = QVBoxLayout(counter)
        counter_name = QLineEdit(str(self.settings.value("ctr0_name", "")))
        counter_name.setPlaceholderText("CTR0")
        counter_name.setMaxLength(64)
        counter_name.editingFinished.connect(
            lambda: self._rename_output("CTR0", counter_name))
        self.counter_name = counter_name
        self.workspace.rename("CTR0", counter_name.text())
        counter_layout.addWidget(counter_name)
        counter_layout.addWidget(QLabel("CTR0 / PFI0 · terminal 29"))
        counter_actions = QHBoxLayout()
        self.counter_start = QPushButton("Start")
        self.counter_read = QPushButton("Read")
        self.counter_stop = QPushButton("Stop")
        self.counter_value = QLabel("—")
        self.counter_start.clicked.connect(lambda: self.worker.submit("counter_start"))
        self.counter_read.clicked.connect(lambda: self.worker.submit("counter_read"))
        self.counter_stop.clicked.connect(lambda: self.worker.submit("counter_stop"))
        for widget in (self.counter_start, self.counter_read, self.counter_stop, self.counter_value):
            counter_actions.addWidget(widget)
        counter_layout.addLayout(counter_actions)
        layout.addWidget(counter)
        caveat = QLabel("Counter edge accuracy needs an external pulse reference.")
        caveat.setWordWrap(True)
        caveat.setStyleSheet("color: #93a2b0; font-size: 11px;")
        layout.addWidget(caveat)
        layout.addStretch()
        scroll.setWidget(body)
        return scroll

    def _waveform_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        heading = QHBoxLayout()
        label = QLabel("WORKSPACE · SHARED MEASURED SIGNALS")
        label.setStyleSheet("font-size: 11px; font-weight: 700; color: #94a5b7;")
        heading.addWidget(label)
        heading.addStretch()
        layout.addLayout(heading)
        pg.setConfigOptions(antialias=False, useOpenGL=False)
        self.scope_splitter = QSplitter(Qt.Orientation.Vertical)
        self.scope_splitter.setChildrenCollapsible(False)
        for scope in self.workspace.scopes:
            self._append_scope_widget(scope)
        self.empty_scope_label = QLabel(
            "No scopes yet\n\nUse the Analog Scope or Digital Scope button in the toolbar.")
        self.empty_scope_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_scope_label.setStyleSheet("color: #8c9aa8; font-size: 16px;")
        layout.addWidget(self.empty_scope_label, 1)
        self.scope_scroll = QScrollArea()
        self.scope_scroll.setWidgetResizable(True)
        self.scope_scroll.setWidget(self.scope_splitter)
        layout.addWidget(self.scope_scroll, 1)
        self._sync_first_scope_aliases()
        return panel

    def _append_scope_widget(self, scope: Scope) -> None:
        widget = ScopeWidget(scope, self.workspace, self.channel_colors)
        if hasattr(self, "range") and self.range.currentData() is not None:
            widget.set_hardware_range(float(self.range.currentData()))
        widget.edit_requested.connect(self._edit_scope)
        widget.delete_requested.connect(self._delete_scope)
        widget.duplicate_requested.connect(self._duplicate_scope)
        widget.reset_requested.connect(self._clear_scope_view)
        widget.transfer_requested.connect(self._transfer_scope_signals)
        widget.move_requested.connect(self._move_scope)
        widget.collapsed_changed.connect(self._scope_collapsed)
        widget.follow_live.setChecked(True)
        self.scope_widgets.append(widget)
        self.scope_splitter.addWidget(widget)

    def _sync_first_scope_aliases(self) -> None:
        # Retain V1's public-facing widget attributes for existing integrations.
        if self.scope_widgets:
            first = self.scope_widgets[0]
            self.plot = first.plot
            self.follow_live = first.follow_live
            self.plot_hint = first.note
            self.curves = [first._curves.get(f"AI{channel}") for channel in range(8)]
        else:
            self.plot = None
            self.follow_live = None
            self.plot_hint = self.empty_scope_label
            self.curves = []
        self._legend_channels: set[int] = set()
        self.empty_scope_label.setVisible(not self.scope_widgets)
        self.scope_scroll.setVisible(bool(self.scope_widgets))

    def _right_panel(self) -> QWidget:
        panel = QScrollArea()
        panel.setWidgetResizable(True)
        panel.setMinimumWidth(300)
        panel.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        outputs = QGroupBox("Analog outputs · commanded, not measured")
        form = QGridLayout(outputs)
        self.ao_values: list[QDoubleSpinBox] = []
        self.ao_apply: list[QPushButton] = []
        self.ao_state: list[QLabel] = []
        self.ao_names: list[QLineEdit] = []
        for channel in range(2):
            name = QLineEdit(str(self.settings.value(f"ao{channel}_name", "")))
            name.setPlaceholderText(f"AO{channel}")
            name.editingFinished.connect(lambda ch=channel, edit=name:
                                         self._rename_output(f"AO{ch}", edit))
            voltage = QDoubleSpinBox()
            voltage.setRange(0, 5)
            voltage.setDecimals(3)
            voltage.setSuffix(" V")
            apply = QPushButton(f"Apply AO{channel}")
            apply.clicked.connect(lambda _checked=False, ch=channel: self._apply_ao(ch))
            status = QLabel("Disarmed · not set this session")
            status.setStyleSheet("color: #93a2b0; font-size: 11px;")
            status.setWordWrap(True)
            form.addWidget(name, channel * 4, 0, 1, 2)
            form.addWidget(QLabel(f"AO{channel} · terminal {14 + channel}"), channel * 4 + 1, 0)
            form.addWidget(voltage, channel * 4 + 1, 1)
            form.addWidget(apply, channel * 4 + 2, 0, 1, 2)
            form.addWidget(status, channel * 4 + 3, 0, 1, 2)
            self.ao_values.append(voltage)
            self.ao_apply.append(apply)
            self.ao_state.append(status)
            self.ao_names.append(name)
            self.workspace.rename(f"AO{channel}", name.text())
        layout.addWidget(outputs)

        digital_output = QGroupBox("Digital outputs · whole-port direction")
        digital_grid = QGridLayout(digital_output)
        self.digital_values: list[QSpinBox] = []
        self.digital_write_buttons: list[QPushButton] = []
        for port in range(2):
            value = QSpinBox()
            value.setRange(0, 255 if port == 0 else 15)
            value.setDisplayIntegerBase(16)
            write = QPushButton(f"Apply P{port}")
            write.clicked.connect(lambda _checked=False, p=port: self._write_port(p))
            digital_grid.addWidget(QLabel(f"P{port} · {'8' if port == 0 else '4'} lines"), port, 0)
            digital_grid.addWidget(value, port, 1)
            digital_grid.addWidget(write, port, 2)
            self.digital_values.append(value)
            self.digital_write_buttons.append(write)
        note = QLabel("Explicit Apply configures the entire port as output. "
                      "Open-collector HIGH releases a line.")
        note.setWordWrap(True)
        digital_grid.addWidget(note, 2, 0, 1, 3)
        layout.addWidget(digital_output)

        heading = QLabel("ACQUISITION")
        heading.setStyleSheet("font-size: 11px; font-weight: 700; color: #94a5b7;")
        layout.addWidget(heading)
        group = QGroupBox("Configuration")
        form = QFormLayout(group)
        self.mode = QComboBox()
        self.mode.addItems(("RSE", "Differential"))
        self.mode.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Terminal", self.mode)
        self.range = QComboBox()
        self.range.addItem("±10 V", 10.0)
        self.range.currentIndexChanged.connect(self._update_scope_ranges)
        form.addRow("Range", self.range)
        self.acq_mode = QComboBox()
        self.acq_mode.addItems(("Continuous", "Finite"))
        self.acq_mode.currentIndexChanged.connect(self._settings_changed)
        form.addRow("Run mode", self.acq_mode)
        self.rate = QSpinBox()
        self.rate.setRange(1, 48000)
        self.rate.setValue(int(self.settings.value("rate", 1000)))
        self.rate.setSuffix(" S/s/ch")
        self.rate.valueChanged.connect(self._settings_changed)
        form.addRow("Sample rate", self.rate)
        self.finite_count = QSpinBox()
        self.finite_count.setRange(1, 4096)
        self.finite_count.setValue(int(self.settings.value("finite_count", 100)))
        self.finite_count.setToolTip(
            "Analog scans per channel, or digital reads per active port")
        form.addRow("Finite count", self.finite_count)
        self.window_seconds = QDoubleSpinBox()
        self.window_seconds.setRange(0.1, 60)
        self.window_seconds.setDecimals(1)
        self.window_seconds.setValue(float(self.settings.value("display_seconds", 5)))
        self.window_seconds.setSuffix(" s")
        form.addRow("Visible window", self.window_seconds)
        self.digital_poll_hz = QSpinBox()
        self.digital_poll_hz.setRange(1, 10)
        self.digital_poll_hz.setValue(int(self.settings.value("digital_poll_hz", 2)))
        self.digital_poll_hz.setSuffix(" reads/s/port")
        self.digital_poll_hz.setToolTip("Software polls of active digital ports. "
                                       "Not a hardware sample clock.")
        self.digital_poll_hz.valueChanged.connect(self._set_digital_poll_interval)
        form.addRow("Digital polling", self.digital_poll_hz)
        self.rate_summary = QLabel()
        self.rate_summary.setWordWrap(True)
        form.addRow("", self.rate_summary)
        layout.addWidget(group)

        recording = QGroupBox("Recording · calibrated CSV")
        record_layout = QVBoxLayout(recording)
        self.record_enabled = QCheckBox("Record this acquisition")
        record_layout.addWidget(self.record_enabled)
        self.record_path = QLineEdit()
        self.record_path.setPlaceholderText("Choose a CSV destination")
        record_layout.addWidget(self.record_path)
        browse = QPushButton("Choose file…")
        browse.clicked.connect(self._browse_recording)
        record_layout.addWidget(browse)
        layout.addWidget(recording)
        stats = QGroupBox("Live statistics")
        stats_layout = QFormLayout(stats)
        self.stats_channels = QLabel("—")
        self.stats_scans = QLabel("0")
        self.stats_rate = QLabel("—")
        self.stats_duration = QLabel("—")
        self.stats_voltage = QLabel("—")
        self.stats_voltage.setWordWrap(True)
        for title, widget in (("Channels", self.stats_channels), ("Scans/ch", self.stats_scans),
                              ("Measured rate", self.stats_rate), ("Duration", self.stats_duration),
                              ("Latest batch", self.stats_voltage)):
            stats_layout.addRow(title, widget)
        layout.addWidget(stats)
        limitation = QLabel("Continuous: RSE ±10 V. Differential: finite, one channel.\n"
                            "Floating inputs do not establish voltage accuracy.")
        limitation.setWordWrap(True)
        limitation.setStyleSheet("color: #93a2b0; font-size: 11px;")
        layout.addWidget(limitation)
        layout.addStretch()
        self._settings_changed()
        panel.setWidget(body)
        return panel

    def _rename_output(self, signal_id: str, edit: QLineEdit) -> None:
        try:
            if self.workspace.signals[signal_id].name != edit.text().strip():
                self._remember_workspace_edit()
            self.workspace.rename(signal_id, edit.text())
        except ValueError as exc:
            edit.setText(self.workspace.signals[signal_id].name)
            self._on_error(str(exc))
            return
        self.settings.setValue(f"{signal_id.replace('.', '_').lower()}_name",
                               self.workspace.signals[signal_id].name)

    def _wire_worker(self) -> None:
        self.worker.state_changed.connect(self._set_state)
        self.worker.batch_ready.connect(self._on_batch)
        self.worker.result_ready.connect(self._on_result)
        self.worker.error_occurred.connect(self._on_error)
        self.worker.finished.connect(self._worker_finished)

    def _apply_theme(self, name: str) -> None:
        self._theme = name
        self.setStyleSheet(LIGHT if name == "light" else DARK)
        self.theme_button.setText("◐  Dark" if name == "light" else "☀  Light")
        self.theme_button.setToolTip(
            "Switch to dark theme" if name == "light" else "Switch to light theme")
        for scope in self.scope_widgets:
            scope.plot.setBackground("#ffffff" if name == "light" else "#111820")
        self.settings.setValue("theme", name)

    def _toggle_theme(self) -> None:
        self._apply_theme("light" if self._theme == "dark" else "dark")

    def _rename_channel(self, channel: int) -> None:
        signal_id = f"AI{channel}"
        try:
            if self.workspace.signals[signal_id].name != self.channel_names[channel].text().strip():
                self._remember_workspace_edit()
            self.workspace.rename(signal_id, self.channel_names[channel].text())
        except ValueError as exc:
            self.channel_names[channel].setText(self.workspace.signals[signal_id].name)
            self._on_error(str(exc))
            return
        self.settings.setValue(f"ai{channel}_name", self.workspace.signals[signal_id].name)
        self._style_channel_card(channel)
        for widget in self.scope_widgets:
            widget.update_scope(self.workspace.scope(widget.scope_id))

    def _style_channel_card(self, channel: int) -> None:
        card = self.channel_enabled[channel]
        active = card.isChecked()
        color = QColor(self.channel_colors[channel])
        name = self.workspace.signals[f"AI{channel}"].name or f"Analog Input {channel}"
        card.setText(f"{'●' if active else '○'}  {name}\n     AI{channel} · terminal {AI_PINS[channel]}")
        if active:
            card.setStyleSheet(
                f"QPushButton {{ text-align: left; padding-left: 12px; "
                f"background: rgba({color.red()}, {color.green()}, {color.blue()}, 35); "
                f"border: 2px solid {color.name()}; border-radius: 8px; "
                f"color: #edf5fa; font-weight: 650; }}")
            glow = QGraphicsDropShadowEffect(card)
            glow.setBlurRadius(17)
            glow.setOffset(0, 0)
            glow.setColor(color)
            card.setGraphicsEffect(glow)
        else:
            card.setStyleSheet(
                "QPushButton { text-align: left; padding-left: 12px; "
                "background: #20272f; border: 1px solid #37424d; "
                "border-radius: 8px; color: #82909d; }"
                "QPushButton:hover { background: #2a333d; color: #d4dde6; }")
            card.setGraphicsEffect(None)

    def _style_digital_card(self, signal_id: str) -> None:
        card = self.digital_enabled[signal_id]
        active = card.isChecked()
        offset = int(signal_id[1]) * 8 + int(signal_id[3:])
        color = QColor(DIGITAL_COLORS[offset])
        name = self.workspace.signals[signal_id].name or f"Digital Input {signal_id}"
        card.setText(f"{'●' if active else '○'}  {name}\n     {signal_id} · terminal {17 + offset}")
        if active:
            card.setStyleSheet(
                f"QPushButton {{ text-align: left; padding-left: 12px; "
                f"background: rgba({color.red()}, {color.green()}, {color.blue()}, 35); "
                f"border: 2px solid {color.name()}; border-radius: 8px; "
                "color: #edf5fa; font-weight: 650; }}")
            glow = QGraphicsDropShadowEffect(card)
            glow.setBlurRadius(17)
            glow.setOffset(0, 0)
            glow.setColor(color)
            card.setGraphicsEffect(glow)
        else:
            card.setStyleSheet(
                "QPushButton { text-align: left; padding-left: 12px; "
                "background: #20272f; border: 1px solid #37424d; "
                "border-radius: 8px; color: #82909d; }"
                "QPushButton:hover { background: #2a333d; color: #d4dde6; }")
            card.setGraphicsEffect(None)

    def _digital_toggled(self, signal_id: str, checked: bool) -> None:
        port = int(signal_id[1])
        try:
            if checked and port in self._digital_output_ports:
                raise ValueError(f"P{port} is configured for output; read it as input first")
            self.workspace.set_active(signal_id, checked)
        except ValueError as exc:
            card = self.digital_enabled[signal_id]
            card.blockSignals(True)
            card.setChecked(not checked)
            card.blockSignals(False)
            self.statusBar().showMessage(str(exc), 10000)
        self._style_digital_card(signal_id)
        self._settings_changed()

    def _digital_card_menu(self, signal_id: str, button: QPushButton,
                           point: object) -> None:
        menu = QMenu(button)
        menu.addAction("Rename…", lambda: self._edit_digital_name(signal_id))
        offset = int(signal_id[1]) * 8 + int(signal_id[3:])
        menu.addAction("Locate on device", lambda: self._show_pinout(17 + offset))
        menu.exec(button.mapToGlobal(point))

    def _edit_digital_name(self, signal_id: str) -> None:
        name, accepted = QInputDialog.getText(
            self, f"Rename {signal_id}", "Display name:",
            text=self.workspace.signals[signal_id].name)
        if accepted:
            edit = self.digital_names[signal_id]
            edit.setText(name)
            self._rename_output(signal_id, edit)
            self._style_digital_card(signal_id)
            for widget in self.scope_widgets:
                if signal_id in widget._signals:
                    widget.update_scope(self.workspace.scope(widget.scope_id))

    def _pick_scope_signals(self, initial: tuple[str, ...],
                            kind: str = "analog") -> tuple[str, ...] | None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{kind.title()} scope signals")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            "Select active software-polled digital lines. Poll times are not edge times."
            if kind == "digital" else
            "Select active measured analog inputs. One input may appear in multiple scopes."))
        choices = QListWidget()
        choices.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        for signal_id, signal in self.workspace.signals.items():
            if not signal.active or not self.workspace._plottable(signal_id, kind):
                continue
            row = QListWidgetItem(self.workspace._friendly_label(signal_id))
            row.setData(Qt.ItemDataRole.UserRole, signal_id)
            choices.addItem(row)
            row.setSelected(signal_id in initial)
        layout.addWidget(choices)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return tuple(item.data(Qt.ItemDataRole.UserRole) for item in choices.selectedItems())

    def _rebuild_scopes(self) -> None:
        for widget in self.scope_widgets:
            widget.setParent(None)
            widget.deleteLater()
        self.scope_widgets.clear()
        for scope in self.workspace.scopes:
            self._append_scope_widget(scope)
        self._sync_first_scope_aliases()

    def _remember_workspace_edit(self) -> None:
        self._undo_stack.append(self.workspace.to_dict())
        self._undo_stack = self._undo_stack[-50:]
        self._redo_stack.clear()

    def _restore_workspace_edit(self, payload: dict[str, object]) -> None:
        self.workspace = Workspace.from_dict(payload)
        for channel in range(8):
            signal = self.workspace.signals[f"AI{channel}"]
            box = self.channel_enabled[channel]
            box.blockSignals(True)
            box.setChecked(signal.active)
            box.blockSignals(False)
            self.channel_names[channel].setText(signal.name)
            self._style_channel_card(channel)
        for channel, edit in enumerate(self.ao_names):
            edit.setText(self.workspace.signals[f"AO{channel}"].name)
        for signal_id, edit in self.digital_names.items():
            edit.setText(self.workspace.signals[signal_id].name)
            card = self.digital_enabled[signal_id]
            card.blockSignals(True)
            card.setChecked(self.workspace.signals[signal_id].active)
            card.blockSignals(False)
            self._style_digital_card(signal_id)
        self.counter_name.setText(self.workspace.signals["CTR0"].name)
        self._rebuild_scopes()
        self._recalculate_math_display()
        self._settings_changed()
        self._refresh_plot()

    def _undo(self) -> None:
        if self._undo_stack:
            self._redo_stack.append(self.workspace.to_dict())
            self._restore_workspace_edit(self._undo_stack.pop())

    def _redo(self) -> None:
        if self._redo_stack:
            self._undo_stack.append(self.workspace.to_dict())
            self._restore_workspace_edit(self._redo_stack.pop())

    def _add_scope_dialog(self, kind: str = "analog") -> None:
        signals = self._pick_scope_signals((), kind)
        if signals is None:
            return
        self._remember_workspace_edit()
        scope = self.workspace.add_scope(signals, kind=kind)
        self._append_scope_widget(scope)
        self._sync_first_scope_aliases()

    def _edit_scope(self, scope_id: str) -> None:
        scope = self.workspace.scope(scope_id)
        name, accepted = QInputDialog.getText(self, "Edit scope", "Scope name:", text=scope.name)
        if not accepted:
            return
        signals = self._pick_scope_signals(tuple(scope.signals), scope.kind)
        if signals is None:
            return
        self._remember_workspace_edit()
        self.workspace.set_scope_signals(scope_id, signals)
        scope.name = name.strip()[:64] or scope.name
        self._rebuild_scopes()

    def _duplicate_scope(self, scope_id: str) -> None:
        source = self.workspace.scope(scope_id)
        self._remember_workspace_edit()
        self.workspace.add_scope(tuple(source.signals), name=f"{source.name} copy",
                                 kind=source.kind)
        self._rebuild_scopes()

    def _delete_scope(self, scope_id: str) -> None:
        scope = self.workspace.scope(scope_id)
        answer = QMessageBox.question(
            self, "Delete scope view",
            f"Delete {scope.name}? Acquired data and active hardware channels are preserved.")
        if answer == QMessageBox.StandardButton.Yes:
            self._remember_workspace_edit()
            self.workspace.delete_scope(scope_id)
            self._rebuild_scopes()

    def _clear_scope_view(self, scope_id: str) -> None:
        scope = self.workspace.scope(scope_id)
        answer = QMessageBox.question(
            self, "Clear scope display",
            f"Clear the displayed history in {scope.name}? Raw recordings and other "
            "scope views remain intact.")
        if answer == QMessageBox.StandardButton.Yes:
            widget = next(item for item in self.scope_widgets if item.scope_id == scope_id)
            latest = (max((points[-1][0] for points in self._digital_history.values() if points),
                          default=None) if scope.kind == "digital" else
                      self._time[-1] if self._time else None)
            widget.clear_view(latest)

    def _transfer_scope_signals(self, source_id: str, signals: object,
                                operation: str) -> None:
        selected = tuple(signals)
        if operation.endswith("_new"):
            self._remember_workspace_edit()
            target = self.workspace.add_scope(kind=self.workspace.scope(source_id).kind)
        else:
            choices = [(scope.name, scope.id) for scope in self.workspace.scopes
                       if scope.id != source_id
                       and scope.kind == self.workspace.scope(source_id).kind]
            if not choices:
                self.statusBar().showMessage("Add another scope first", 5000)
                return
            label, accepted = QInputDialog.getItem(
                self, "Target scope", "Move or copy to:", [name for name, _ in choices],
                0, False)
            if not accepted:
                return
            self._remember_workspace_edit()
            target = self.workspace.scope(dict(choices)[label])
        self.workspace.transfer(source_id, target.id, selected,
                                copy=operation.startswith("copy"))
        self._rebuild_scopes()

    def _move_scope(self, scope_id: str, delta: int) -> None:
        index = next(i for i, scope in enumerate(self.workspace.scopes) if scope.id == scope_id)
        if index + delta < 0 or index + delta >= len(self.workspace.scopes):
            return
        self._remember_workspace_edit()
        self.workspace.move_scope(scope_id, delta)
        self._rebuild_scopes()

    def _scope_collapsed(self, scope_id: str, collapsed: bool) -> None:
        self._remember_workspace_edit()
        self.workspace.scope(scope_id).collapsed = collapsed

    def _manage_math(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Math channels · aligned analog scans")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            "Use stable AI0…AI7 and M1… handles. V(0.5) is a 0.5 V constant. "
            "Math is display-only in this V2 stage; full-fidelity CSV records physical AI."))
        entries = QListWidget()
        layout.addWidget(entries)

        def refresh() -> None:
            entries.clear()
            for definition in self.workspace.math.values():
                signal = self.workspace.signals[definition.id]
                row = QListWidgetItem(
                    f"{definition.handle} · {signal.name or definition.handle} = "
                    f"{definition.expression} [{definition.unit}]")
                row.setData(Qt.ItemDataRole.UserRole, definition.id)
                entries.addItem(row)

        def selected_id() -> str | None:
            row = entries.currentItem()
            return row.data(Qt.ItemDataRole.UserRole) if row is not None else None

        def edit(identifier: str | None) -> None:
            current = self.workspace.math.get(identifier) if identifier else None
            name, accepted = QInputDialog.getText(
                dialog, "Math channel", "Display name:",
                text=self.workspace.signals[identifier].name if current else "")
            if not accepted:
                return
            expression, accepted = QInputDialog.getText(
                dialog, "Math expression", "Expression:",
                text=current.expression if current else "AI0 - V(0.0)")
            if not accepted:
                return
            before = self.workspace.to_dict()
            try:
                if current:
                    self.workspace.edit_math(identifier, expression)
                    self.workspace.rename(identifier, name)
                else:
                    self.workspace.add_math(name, expression)
            except ValueError as exc:
                QMessageBox.warning(dialog, "Invalid math channel", str(exc))
                return
            self._undo_stack.append(before)
            self._redo_stack.clear()
            self._recalculate_math_display()
            self._rebuild_scopes()
            refresh()

        def remove() -> None:
            identifier = selected_id()
            if identifier is None:
                return
            before = self.workspace.to_dict()
            try:
                self.workspace.delete_math(identifier)
            except ValueError as exc:
                QMessageBox.warning(dialog, "Math channel is in use", str(exc))
                return
            self._undo_stack.append(before)
            self._redo_stack.clear()
            self._recalculate_math_display()
            self._rebuild_scopes()
            refresh()

        buttons = QHBoxLayout()
        for label, callback in (
            ("Add", lambda: edit(None)), ("Edit", lambda: edit(selected_id())
             if selected_id() else None), ("Delete", remove),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        refresh()
        dialog.exec()

    def _choose_color(self, channel: int) -> None:
        color = QColorDialog.getColor(QColor(self.channel_colors[channel]), self,
                                      f"AI{channel} trace color")
        if color.isValid():
            self.channel_colors[channel] = color.name()
            self._style_channel_card(channel)
            for widget in self.scope_widgets:
                curve = widget._curves.get(f"AI{channel}")
                if curve is not None:
                    curve.setPen(pg.mkPen(color.name(), width=1.5))
            self.settings.setValue(f"ai{channel}_color", color.name())

    def _channel_menu(self, channel: int, button: QPushButton, point: object) -> None:
        menu = QMenu(self)
        menu.addAction("Rename channel", lambda: self._edit_channel_name(channel))
        menu.addAction("Locate on device", lambda: self._show_pinout(AI_PINS[channel]))
        menu.addAction("Change trace color", lambda: self._choose_color(channel))
        menu.exec(button.mapToGlobal(point))

    def _edit_channel_name(self, channel: int) -> None:
        name, accepted = QInputDialog.getText(
            self, f"Rename AI{channel}", "Display name:",
            text=self.workspace.signals[f"AI{channel}"].name)
        if accepted:
            self.channel_names[channel].setText(name)
            self._rename_channel(channel)

    def _show_pinout(self, selected_pin: int | None = None) -> None:
        PinoutDialog(self.workspace, selected_pin=selected_pin, parent=self).exec()

    def _choose_record_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Default recording directory",
            str(self.settings.value("record_directory", str(Path.home()))))
        if directory:
            self.settings.setValue("record_directory", directory)

    def _acquisition_preset(self) -> dict[str, object]:
        return {
            "mode": "differential" if self.mode.currentIndex() == 1 else "rse",
            "voltage_range": float(self.range.currentData()),
            "rate_per_channel": self.rate.value(),
            "finite_scans": self.finite_count.value(),
            "run_mode": "finite" if self.acq_mode.currentIndex() == 1 else "continuous",
            "display_seconds": self.window_seconds.value(),
            "digital_poll_hz": self.digital_poll_hz.value(),
            "recording_provenance": self._last_recording_meta,
        }

    def _save(self) -> bool:
        if self._project_path is None:
            return self._save_as()
        try:
            config = self._acquisition_preset()
            if self._project_path.suffix == ".o6w":
                save_workspace(self._project_path, self.workspace, config)
            else:
                recording_csv = self._last_recording_path if self._project_include_data else None
                source = (self._project_path if self._loaded_project_has_data
                          and recording_csv is None and self._project_include_data else None)
                save_project(self._project_path, self.workspace, config,
                             recording_csv=recording_csv, source_project=source)
            self.statusBar().showMessage(f"Saved {self._project_path}", 8000)
            return True
        except (OSError, ValueError, TypeError) as exc:
            self._on_error(f"Cannot save project: {exc}")
            return False

    def _save_as(self) -> bool:
        filename, selected_filter = QFileDialog.getSaveFileName(
            self, "Save Open6009 project or workspace", str(Path.home() / "Open6009.o6p"),
            "Open6009 project (*.o6p);;Workspace preset (*.o6w)")
        if not filename:
            return False
        path = Path(filename)
        if path.suffix not in {".o6p", ".o6w"}:
            path = path.with_suffix(".o6w" if "*.o6w" in selected_filter else ".o6p")
        if path.exists() and QMessageBox.question(
            self, "Replace saved file", f"Replace {path.name}?") != QMessageBox.StandardButton.Yes:
            return False
        include = False
        if path.suffix == ".o6p" and (
            self._last_recording_path is not None or self._loaded_project_has_data
        ):
            include = QMessageBox.question(
                self, "Include recorded data",
                "Include the complete recorded CSV and raw USB sample sidecar in this project?\n"
                "Yes: self-contained data. No: workspace and settings only."
            ) == QMessageBox.StandardButton.Yes
        previous_path, previous_include = self._project_path, self._project_include_data
        self._project_path, self._project_include_data = path, include
        # A loaded recording may be used as the source when saving to a new path.
        if self._loaded_project_has_data and self._last_recording_path is None and include:
            self._project_path = previous_path
            try:
                save_project(path, self.workspace, self._acquisition_preset(),
                             source_project=previous_path)
            except (OSError, ValueError, TypeError) as exc:
                self._project_path, self._project_include_data = previous_path, previous_include
                self._on_error(f"Cannot save project: {exc}")
                return False
            self._project_path, self._project_include_data = path, include
            self.statusBar().showMessage(f"Saved {path}", 8000)
            return True
        if not self._save():
            self._project_path, self._project_include_data = previous_path, previous_include
            return False
        self._loaded_project_has_data = include
        return True

    def _open_workspace(self) -> None:
        if self._state != "Disconnected":
            QMessageBox.warning(self, "Disconnect first",
                                "Disconnect the device before opening a project or preset. "
                                "Opening a file never applies physical output commands.")
            return
        if self._time or self._project_path:
            answer = QMessageBox.question(
                self, "Open workspace",
                "Opening a file replaces the current unsaved workspace display. Save first?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel)
            if answer not in (QMessageBox.StandardButton.Save,
                              QMessageBox.StandardButton.Discard):
                return
            if answer == QMessageBox.StandardButton.Save and not self._save():
                return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Open Open6009 project or workspace", str(Path.home()),
            "Open6009 files (*.o6p *.o6w)")
        if not filename:
            return
        path = Path(filename)
        try:
            if path.suffix == ".o6w":
                workspace, config = load_workspace(path)
                preview = None
            elif path.suffix == ".o6p":
                workspace, config, preview = load_project(path)
            else:
                raise ValueError("unsupported file extension")
            self._apply_loaded_workspace(workspace, config, preview)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            self._on_error(f"Cannot open project: {exc}")
            return
        self._project_path = path
        self._project_include_data = preview is not None
        self._loaded_project_has_data = preview is not None
        self._last_recording_path = None
        self._last_recording_meta = config.get("recording_provenance", {})
        self.statusBar().showMessage(f"Opened {path} · outputs remain disarmed", 8000)

    def _apply_loaded_workspace(self, workspace: Workspace, config: dict[str, object],
                                preview: object) -> None:
        mode = config.get("mode")
        rate = config.get("rate_per_channel")
        finite = config.get("finite_scans")
        duration = config.get("display_seconds")
        voltage_range = config.get("voltage_range")
        run_mode = config.get("run_mode")
        digital_poll_hz = config.get("digital_poll_hz", 2)
        if (mode not in {"rse", "differential"} or not isinstance(rate, int)
                or isinstance(rate, bool)
                or not 1 <= rate <= 48000 or not isinstance(finite, int)
                or not 1 <= finite <= 4096 or not isinstance(duration, (float, int))
                or not 0.1 <= duration <= 60 or run_mode not in {"finite", "continuous"}
                or type(digital_poll_hz) is not int or not 1 <= digital_poll_hz <= 10
                or voltage_range not in (
                    {10.0} if mode == "rse" else DIFFERENTIAL_RANGES
                ) or (mode == "differential" and run_mode != "finite")):
            raise ValueError("unsupported acquisition settings")
        active = [int(key[2:]) for key in workspace.signals
                  if key.startswith("AI") and workspace.signals[key].active]
        if mode == "differential" and (len(active) > 1 or any(ch > 3 for ch in active)):
            raise ValueError("differential preset conflicts with active analog channels")
        if len(active) * rate > 48000:
            raise ValueError("preset exceeds the aggregate sample-rate limit")
        if run_mode == "finite" and (finite / rate > 10 or (len(active) > 1
            and mode != "rse")):
            raise ValueError("preset exceeds finite acquisition limits")
        self.workspace = workspace
        for channel in range(8):
            box = self.channel_enabled[channel]
            box.blockSignals(True)
            box.setChecked(workspace.signals[f"AI{channel}"].active)
            box.blockSignals(False)
            self.channel_names[channel].setText(workspace.signals[f"AI{channel}"].name)
            self._style_channel_card(channel)
        for channel, edit in enumerate(self.ao_names):
            edit.setText(workspace.signals[f"AO{channel}"].name)
        for signal_id, edit in self.digital_names.items():
            edit.setText(workspace.signals[signal_id].name)
            card = self.digital_enabled[signal_id]
            card.blockSignals(True)
            card.setChecked(workspace.signals[signal_id].active)
            card.blockSignals(False)
            self._style_digital_card(signal_id)
        self.counter_name.setText(workspace.signals["CTR0"].name)
        self.mode.setCurrentIndex(1 if mode == "differential" else 0)
        chosen_range = self.range.findData(config.get("voltage_range"))
        if chosen_range < 0:
            raise ValueError("unsupported voltage range in project")
        self.range.setCurrentIndex(chosen_range)
        self.acq_mode.setCurrentIndex(1 if config.get("run_mode") == "finite" else 0)
        self.rate.setValue(rate)
        self.finite_count.setValue(finite)
        self.window_seconds.setValue(duration)
        self.digital_poll_hz.setValue(digital_poll_hz)
        self._time.clear()
        self._digital_epoch = None
        for points in self._digital_history.values():
            points.clear()
        for values in self._values.values():
            values.clear()
        if preview is not None:
            self._time.extend(preview.times)
            for row in preview.volts:
                for channel in range(8):
                    slot = preview.channels.index(channel) if channel in preview.channels else None
                    self._values[channel].append(row[slot] if slot is not None else float("nan"))
        self._recalculate_math_display()
        self._rebuild_scopes()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._refresh_plot()

    def _new_workspace(self) -> None:
        if self._state != "Disconnected":
            QMessageBox.warning(self, "Disconnect first", "Disconnect before creating a workspace.")
            return
        if self._time or self._project_path:
            answer = QMessageBox.question(
                self, "New workspace", "Discard the current unsaved workspace display and begin new?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel)
            if answer not in (QMessageBox.StandardButton.Save,
                              QMessageBox.StandardButton.Discard):
                return
            if answer == QMessageBox.StandardButton.Save and not self._save():
                return
        self._apply_loaded_workspace(Workspace(), {
            "mode": "rse", "voltage_range": 10.0, "rate_per_channel": 1000,
            "finite_scans": 100, "run_mode": "continuous", "display_seconds": 5.0,
        }, None)
        self._project_path = None
        self._last_recording_path = None
        self._last_recording_meta = {}
        self._loaded_project_has_data = False
        self._project_include_data = False

    def _reset_workspace(self) -> None:
        if self._state != "Disconnected":
            QMessageBox.warning(self, "Disconnect first",
                                "Stop acquisition and disconnect before resetting the workspace.")
            return
        summary = (f"{len(self.workspace.scopes)} scope(s), "
                   f"{sum(bool(item.name) for item in self.workspace.signals.values())} "
                   f"custom channel name(s), and {len(self._time)} displayed scan(s) "
                   "will be removed. Saved files are preserved; the USB device is not reset.")
        answer = QMessageBox.question(
            self, "Protected workspace reset", summary + "\nSave project first?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if answer not in (QMessageBox.StandardButton.Save,
                          QMessageBox.StandardButton.Discard):
            return
        if answer == QMessageBox.StandardButton.Save and not self._save():
            return
        typed, accepted = QInputDialog.getText(
            self, "Confirm reset", "Type RESET to clear the current workspace:")
        if accepted and typed == "RESET":
            self._apply_loaded_workspace(Workspace(), {
                "mode": "rse", "voltage_range": 10.0, "rate_per_channel": 1000,
                "finite_scans": 100, "run_mode": "continuous", "display_seconds": 5.0,
            }, None)
            self._project_path = None
            self._last_recording_path = None
            self._last_recording_meta = {}
            self._loaded_project_has_data = False
            self._project_include_data = False

    def _export_csv(self) -> None:
        if self._last_recording_path is None and not self._loaded_project_has_data:
            QMessageBox.warning(self, "No complete recording",
                                "Record and complete an acquisition before exporting full-fidelity CSV.")
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export complete CSV", str(Path.home() / "open6009-export.csv"), "CSV (*.csv)")
        if not filename:
            return
        path = Path(filename).with_suffix(".csv")
        if path.exists() and QMessageBox.question(
            self, "Replace CSV", f"Replace {path.name}?") != QMessageBox.StandardButton.Yes:
            return
        try:
            export_recording_csv(
                path, csv_path=self._last_recording_path,
                project_path=self._project_path if self._last_recording_path is None else None)
        except (OSError, ValueError, TypeError) as exc:
            self._on_error(f"Cannot export CSV: {exc}")
            return
        self.statusBar().showMessage(f"Exported {path}", 8000)

    def _about(self) -> None:
        QMessageBox.information(
            self, "About Open6009",
            "Open6009 V2 development · independent Linux USB-6009 backend.\n"
            "Measured AI and commanded outputs are distinct. Output voltage and "
            "counter edge accuracy still require external validation.")

    def _about_device(self) -> None:
        QMessageBox.information(
            self, "About This Device",
            f"{self.device_name.text()}\nConnection: {self._state}\n"
            "Native AI: finite/continuous RSE; bounded single-channel differential finite.\n"
            "Digital I/O: port-level operations. AO: deliberate manual commands only.\n"
            "Counter: basic start/read/stop; pulse accuracy not electrically validated.")

    def _open_documentation(self) -> None:
        readme = Path(__file__).resolve().parents[3] / "README.md"
        if not readme.is_file() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(readme))):
            QMessageBox.warning(self, "Documentation unavailable",
                                f"Could not open the local project guide at {readme}")

    def _shortcuts(self) -> None:
        QMessageBox.information(
            self, "Keyboard shortcuts",
            "Ctrl+Enter: start acquisition\nEscape: stop\n"
            "Ctrl+N: new workspace\nCtrl+O: open\nCtrl+S: save\n"
            "Ctrl+Shift+S: save as\nCtrl+E: export CSV")

    def _mode_changed(self) -> None:
        if self.mode.currentIndex() == 1:
            selected = [i for i, box in enumerate(self.channel_enabled) if box.isChecked()]
            removed = selected[1:] + [i for i in selected if i >= 4]
            referenced = [f"AI{i}" for i in set(removed)
                          if self.workspace.references(f"AI{i}")]
            if referenced:
                self.mode.blockSignals(True)
                self.mode.setCurrentIndex(0)
                self.mode.blockSignals(False)
                self.statusBar().showMessage(
                    "Remove " + ", ".join(referenced) + " from scopes before changing mode",
                    8000)
                return
        self.range.clear()
        if self.mode.currentIndex() == 0:
            self.range.addItem("±10 V", 10.0)
            self.acq_mode.model().item(0).setEnabled(True)
        else:
            for value in sorted(DIFFERENTIAL_RANGES):
                self.range.addItem(f"±{value:g} V", value)
            self.range.setCurrentIndex(self.range.findData(10.0))
            self.acq_mode.setCurrentIndex(1)
            self.acq_mode.model().item(0).setEnabled(False)
            selected = [i for i, box in enumerate(self.channel_enabled) if box.isChecked()]
            for channel in selected[1:]:
                self.channel_enabled[channel].click()
            for channel in range(4, 8):
                if self.channel_enabled[channel].isChecked():
                    self.channel_enabled[channel].click()
        for channel in range(8):
            self.channel_enabled[channel].setEnabled(self.mode.currentIndex() == 0 or channel < 4)
        self._settings_changed()

    def _channel_toggled(self, channel: int, checked: bool) -> None:
        try:
            self.workspace.set_active(f"AI{channel}", checked)
        except ValueError as exc:
            box = self.channel_enabled[channel]
            box.blockSignals(True)
            box.setChecked(True)
            box.blockSignals(False)
            self.statusBar().showMessage(str(exc), 10000)
            if self.isVisible():
                QMessageBox.warning(self, "Channel is in use", str(exc))
            return
        self._style_channel_card(channel)
        if checked and self.mode.currentIndex() == 1:
            for other, box in enumerate(self.channel_enabled):
                if other != channel and box.isChecked():
                    box.click()
        self._settings_changed()

    def _settings_changed(self) -> None:
        if not hasattr(self, "rate_summary"):
            return
        count = sum(box.isChecked() for box in self.channel_enabled)
        digital_count = sum(card.isChecked() for card in self.digital_enabled.values())
        aggregate = count * self.rate.value()
        self.rate_summary.setText(
            f"{count} analog · {digital_count} digital selected · "
            f"{aggregate:,} analog S/s aggregate / 48,000 max")
        self.rate_summary.setStyleSheet("color: #ff8e92;" if aggregate > 48000
                                        else "color: #8fbcae;")
        self.finite_count.setEnabled(self.acq_mode.currentIndex() == 1)
        self.settings.setValue("rate", self.rate.value())
        self.settings.setValue("finite_count", self.finite_count.value())
        self.settings.setValue("display_seconds", self.window_seconds.value())

    def _update_scope_ranges(self) -> None:
        if self.range.currentData() is None:
            return
        for scope in self.scope_widgets:
            scope.set_hardware_range(float(self.range.currentData()))

    def _selection(self) -> AcquisitionSettings:
        channels = tuple(index for index, box in enumerate(self.channel_enabled) if box.isChecked())
        digital_ports = tuple(port for port in range(2) if any(
            self.digital_enabled[f"P{port}.{bit}"].isChecked()
            for bit in range(8 if port == 0 else 4)))
        mode = TerminalMode.RSE if self.mode.currentIndex() == 0 else TerminalMode.DIFFERENTIAL
        selection = AcquisitionSettings(
            channels, mode, float(self.range.currentData()), self.rate.value(),
            self.finite_count.value() if self.acq_mode.currentIndex() == 1 else None,
            self.window_seconds.value(),
            self.record_path.text().strip() if self.record_enabled.isChecked() else None,
            digital_ports=digital_ports,
            digital_poll_hz=self.digital_poll_hz.value(),
            digital_generation=self._digital_generation,
        )
        selection.validate()
        if self.record_enabled.isChecked() and not selection.record_path:
            raise ValueError("choose a CSV file before enabling recording")
        return selection

    def _browse_recording(self) -> None:
        directory = str(self.settings.value("record_directory", str(Path.home())))
        filename, _ = QFileDialog.getSaveFileName(self, "Record acquisition", directory,
                                                  "CSV data (*.csv)")
        if filename:
            self.record_path.setText(filename)
            self.record_enabled.setChecked(True)
            self.settings.setValue("record_directory", str(Path(filename).parent))

    def _toggle_connection(self) -> None:
        if self._connected:
            self._set_state("Stopping", "Disconnecting")
            self.worker.submit("disconnect")
        else:
            self._set_state("Connecting", "Opening USB interface")
            self.worker.submit("connect")

    def _start(self) -> None:
        if self._state != "Connected":
            return
        analog_active = any(card.isChecked() for card in self.channel_enabled)
        digital_active = any(card.isChecked() for card in self.digital_enabled.values())
        if not analog_active and digital_active:
            if self.record_enabled.isChecked():
                self._on_error("digital CSV recording is not available yet; turn Record off")
                return
            self._digital_generation += 1
            self._digital_acquiring = True
            self._combined_digital = False
            self._digital_poll_paused = False
            self._digital_poll_pending.clear()
            self._digital_counts = {0: 0, 1: 0}
            self._digital_target = (self.finite_count.value()
                                    if self.acq_mode.currentIndex() == 1 else None)
            self._digital_epoch = None
            for points in self._digital_history.values():
                points.clear()
            for scope in self.scope_widgets:
                if scope.kind == "digital":
                    scope._cleared_at = None
            self._samples = 0
            self.stats_channels.setText(", ".join(
                key for key, card in self.digital_enabled.items() if card.isChecked()))
            self.stats_scans.setText("0")
            self.stats_rate.setText(f"Configured {self.digital_poll_hz.value()} reads/s/port")
            self.stats_duration.setText("0.0 s")
            self.stats_voltage.setText("Digital LOW/HIGH states · host-timed")
            self.status_samples.setText("0 port reads")
            self.status_throughput.setText("— reads/s")
            self._set_state("Acquiring", "Software-polled digital input")
            self.status_record.setText("Recording off")
            self.digital_poll_timer.start()
            self._poll_digital_inputs()
            return
        if analog_active and digital_active:
            if self.record_enabled.isChecked():
                self._on_error(
                    "combined analog/digital recording is not available yet; turn Record off")
                return
            self._digital_generation += 1
        try:
            selection = self._selection()
        except ValueError as exc:
            self._on_error(str(exc))
            return
        self._time.clear()
        for values in self._values.values():
            values.clear()
        self._math_values.clear()
        self._combined_digital = bool(selection.digital_ports)
        self._digital_acquiring = False
        self._digital_poll_paused = False
        self._digital_poll_pending.clear()
        self._digital_counts = {0: 0, 1: 0}
        self._digital_target = None
        self._digital_epoch = None
        for points in self._digital_history.values():
            points.clear()
        for scope in self.scope_widgets:
            scope._cleared_at = None
        self._samples = 0
        self._last_started_settings = selection
        detail = ("Starting combined analog + digital acquisition"
                  if self._combined_digital else "Starting USB acquisition")
        self._set_state("Acquiring", detail)
        self.status_record.setText("Recording" if selection.record_path else "Recording off")
        self.worker.submit("acquire", selection)

    def _stop(self) -> None:
        if self._state == "Acquiring":
            if self._digital_acquiring:
                self._digital_acquiring = False
                self.digital_poll_timer.stop()
                self._digital_poll_pending.clear()
                self._set_state("Connected", "Digital acquisition stopped")
                return
            self._set_state("Stopping", "Stopping after current read")
            self.worker.submit("stop")

    def _set_state(self, state: str, detail: str = "") -> None:
        self._state = state
        self._connected = state in {"Connected", "Acquiring", "Stopping"}
        color = {"Disconnected": "#8794a1", "Connecting": "#e0b76a",
                 "Connected": "#69d4af", "Acquiring": "#5cc8ff",
                 "Stopping": "#e0b76a", "Error": "#ff7d9d"}.get(state, "#8794a1")
        self.state_label.setText(f"● {state}")
        self.state_label.setStyleSheet(f"color: {color}; font-weight: 700;")
        self.connect_button.setText("Disconnect" if self._connected else "Connect")
        busy = state in {"Acquiring", "Stopping", "Connecting"}
        self.connect_button.setEnabled(not busy)
        self.start_button.setEnabled(state == "Connected")
        self.stop_button.setEnabled(state == "Acquiring")
        for widget in (self.mode, self.range, self.acq_mode, self.rate,
                       self.window_seconds, self.digital_poll_hz, self.record_enabled,
                       self.record_path, self.toolbar_record, *self.channel_enabled,
                       *self.digital_enabled.values()):
            widget.setEnabled(not busy)
        self.finite_count.setEnabled(not busy and self.acq_mode.currentIndex() == 1)
        if not busy and self.mode.currentIndex() == 1:
            for channel in range(4, 8):
                self.channel_enabled[channel].setEnabled(False)
        for button in (*self.ao_apply, *self.digital_read_buttons, *self.digital_write_buttons,
                       self.counter_start, self.counter_read, self.counter_stop):
            button.setEnabled(state == "Connected")
        self.status_connection.setText(f"USB: {'connected' if self._connected else 'disconnected'}")
        self.status_acquisition.setText(state)
        if detail:
            self.statusBar().showMessage(detail, 5000)

    def _on_batch(self, batch: PlotBatch) -> None:
        self._time.extend(batch.times)
        for channel in range(8):
            slot = batch.channels.index(channel) if channel in batch.channels else None
            self._values[channel].extend(value[slot] if slot is not None else float("nan")
                                         for value in batch.voltages)
        if self.workspace.math:
            order = self.workspace.math_order()
            for row in batch.voltages:
                inputs = {f"AI{channel}": row[slot]
                          for slot, channel in enumerate(batch.channels)}
                for identifier in order:
                    try:
                        result = self.workspace._math_programs[identifier].evaluate(inputs)
                    except (KeyError, ValueError, OverflowError):
                        result = nan
                    self._math_values.setdefault(identifier, deque(maxlen=20000)).append(result)
                    inputs[identifier] = result
        self._samples = batch.scans_total
        self.stats_channels.setText(", ".join(f"AI{channel}" for channel in batch.channels))
        self.stats_scans.setText(f"{batch.scans_total:,}")
        measured = (batch.scans_total * len(batch.channels) / batch.elapsed_seconds
                    if batch.elapsed_seconds else 0)
        self.stats_rate.setText(f"{measured:,.0f} S/s aggregate")
        self.stats_duration.setText(f"{batch.elapsed_seconds:.1f} s")
        if batch.first_channel_volts_min_mean_max is not None:
            lower, average, upper = batch.first_channel_volts_min_mean_max
            self.stats_voltage.setText(
                f"AI{batch.channels[0]}: {lower:+.4f} / {average:+.4f} / {upper:+.4f} V\n"
                "min / mean / max")
        self.status_samples.setText(f"{batch.scans_total:,} scans/ch")
        self.status_throughput.setText(f"{measured:,.0f} S/s measured")
        if self.scope_widgets:
            self.plot_hint.setText("Measured input; check the axis units and channel wiring.")

    def _reset_view(self) -> None:
        for scope in self.scope_widgets:
            scope.home()

    def _recalculate_math_display(self) -> None:
        self._math_values.clear()
        if not self.workspace.math:
            return
        order = self.workspace.math_order()
        physical = {f"AI{channel}": list(self._values[channel]) for channel in range(8)}
        for index in range(len(self._time)):
            inputs = {key: samples[index] for key, samples in physical.items()}
            for identifier in order:
                try:
                    value = self.workspace._math_programs[identifier].evaluate(inputs)
                except (KeyError, ValueError, OverflowError):
                    value = nan
                self._math_values.setdefault(identifier, deque(maxlen=20000)).append(value)
                inputs[identifier] = value

    def _refresh_plot(self) -> None:
        for widget in self.scope_widgets:
            if widget.kind == "digital":
                widget.render_digital(self._digital_history, self.window_seconds.value())
        if not self._time:
            return
        visible = {channel for channel in range(8)
                   if self.channel_enabled[channel].isChecked()}
        for widget in self.scope_widgets:
            widget.render(self._time, self._values, self._math_values,
                          self.window_seconds.value(), visible)
        if self.scope_widgets:
            self._legend_channels = {
                int(signal_id[2:]) for signal_id in self.scope_widgets[0]._signals
                if signal_id.startswith("AI") and self.scope_widgets[0]._visible[signal_id]
                and self.channel_enabled[int(signal_id[2:])].isChecked()
            }

    def _on_result(self, name: str, value: object) -> None:
        if name == "probe" and value:
            self.device_name.setText(f"Available: {value}")
        elif name == "connected":
            self.device_name.setText(str(value))
            self._digital_poll_paused = False
            if self._state != "Acquiring":
                self._combined_digital = False
            self._digital_epoch = None
            for points in self._digital_history.values():
                points.clear()
        elif name == "disconnected":
            self.device_name.setText("No device connected")
            self._digital_acquiring = False
            self._combined_digital = False
            self.digital_poll_timer.stop()
            self._digital_output_ports.clear()
            self._digital_poll_pending.clear()
            for state in self.digital_states:
                state.setText("Unknown after disconnect" if "Requested output" in state.text()
                              else "—")
            for state in self.ao_state:
                state.setText("Physical state unknown after disconnect"
                              if "Requested" in state.text() else "Disarmed · not set this session")
            self.counter_value.setText("—")
        elif name == "acquisition_finished":
            self._combined_digital = False
            self.status_record.setText("Saved" if value["recording"] else "Recording off")
            if value["recording"]:
                self._last_recording_path = Path(value["recording"])
                self._last_recording_meta = {
                    "calibration_replies": list(value.get("calibration_replies", ())),
                    "device_identity": self.device_name.text(),
                    "source": "Open6009 native USB-6009 acquisition",
                    "channels": list(self._last_started_settings.channels)
                    if self._last_started_settings else [],
                    "mode": self._last_started_settings.mode.value
                    if self._last_started_settings else None,
                    "voltage_range": self._last_started_settings.voltage_range
                    if self._last_started_settings else None,
                    "rate_per_channel": self._last_started_settings.rate_per_channel
                    if self._last_started_settings else None,
                }
                self._loaded_project_has_data = False
                self.record_enabled.setChecked(False)
                self.statusBar().showMessage(f"Saved {value['recording']}", 8000)
        elif name == "digital_read":
            port, bits = value
            self._digital_output_ports.discard(port)
            self.digital_states[port].setText(f"Input: 0x{bits:02X} · {bits:0{8 if port == 0 else 4}b}")
        elif name == "digital_poll":
            port, bits, generation = value
            if (not (self._digital_acquiring or self._combined_digital)
                    or generation != self._digital_generation):
                return
            self._digital_poll_pending.discard(port)
            self._digital_output_ports.discard(port)
            self.digital_states[port].setText(
                f"Input: 0x{bits:02X} · {bits:0{8 if port == 0 else 4}b}")
            now = monotonic()
            if self._digital_epoch is None:
                self._digital_epoch = now
            elapsed = now - self._digital_epoch
            for bit in range(8 if port == 0 else 4):
                signal_id = f"P{port}.{bit}"
                if self.workspace.signals[signal_id].active:
                    self._digital_history[signal_id].append((elapsed, (bits >> bit) & 1))
            self._digital_counts[port] += 1
            total = sum(self._digital_counts.values())
            if not self._combined_digital:
                self.status_samples.setText(f"{total:,} port reads")
                self.status_throughput.setText(
                    f"{total / elapsed:,.1f} reads/s measured" if elapsed > 0 else "— reads/s")
                self.stats_duration.setText(f"{elapsed:.1f} s")
                self.stats_scans.setText(str(total))
            self._refresh_plot()
            if self._digital_target is not None:
                ports = [p for p in range(2) if any(
                    self.workspace.signals[f"P{p}.{bit}"].active
                    for bit in range(8 if p == 0 else 4))]
                if all(self._digital_counts[p] >= self._digital_target for p in ports):
                    self._stop()
        elif name == "digital_write":
            port, bits = value
            self._digital_output_ports.add(port)
            self.digital_states[port].setText(f"Requested output: 0x{bits:02X}")
        elif name == "ao_write":
            channel, voltage, code = value
            self.ao_state[channel].setText(f"Requested {voltage:.3f} V · DAC code {code}")
        elif name == "counter_read":
            raw, count = value
            self.counter_value.setText(f"{count:,} (raw {raw})")
        elif name == "counter":
            self.counter_value.setText(str(value))

    def _on_error(self, message: str) -> None:
        self._last_error = message
        if self._digital_poll_pending:
            self._digital_poll_paused = True
        self._digital_poll_pending.clear()
        if self._digital_acquiring:
            self._digital_acquiring = False
            self.digital_poll_timer.stop()
            if self._state in {"Connected", "Acquiring"}:
                self._set_state("Connected", "Digital acquisition failed")
        self._combined_digital = False
        self.status_record.setText("Incomplete" if self.record_enabled.isChecked()
                                   else "Recording off")
        self.statusBar().showMessage(f"Error: {message}", 15000)
        if self.isVisible():
            QMessageBox.warning(self, "Open6009 operation failed", message)

    def _apply_ao(self, channel: int) -> None:
        if self._state != "Connected":
            return
        voltage = self.ao_values[channel].value()
        answer = QMessageBox.question(self, "Apply analog output",
                                      f"Request AO{channel} = {voltage:.3f} V?\n"
                                      "The actual voltage has not been independently measured.")
        if answer == QMessageBox.StandardButton.Yes:
            self.worker.submit("ao_write", (channel, voltage))

    def _write_port(self, port: int) -> None:
        if self._state != "Connected":
            return
        if any(self.workspace.signals[f"P{port}.{bit}"].active
               for bit in range(8 if port == 0 else 4)):
            self.statusBar().showMessage(
                f"Disable P{port} input cards before configuring this port as output", 10000)
            return
        value = self.digital_values[port].value()
        answer = QMessageBox.question(self, "Write digital output",
                                      f"Configure all P{port} lines as outputs and request 0x{value:02X}?\n"
                                      "High releases an open-collector line.")
        if answer == QMessageBox.StandardButton.Yes:
            self.worker.submit("digital_write", (port, value))

    def _read_port(self, port: int) -> None:
        if self._state != "Connected":
            return
        if port in self._digital_output_ports:
            answer = QMessageBox.question(
                self, "Switch digital port to input",
                f"Reading P{port} may configure the whole port as input. Continue?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._digital_poll_paused = False
        self.worker.submit("digital_read", port)

    def _set_digital_poll_interval(self) -> None:
        rate = self.digital_poll_hz.value()
        self.settings.setValue("digital_poll_hz", rate)
        if hasattr(self, "digital_poll_timer"):
            self.digital_poll_timer.setInterval(1000 // rate)

    def _poll_digital_inputs(self) -> None:
        if not self._digital_acquiring or self._state != "Acquiring" or self._digital_poll_paused:
            return
        for port in range(2):
            if port in self._digital_poll_pending or port in self._digital_output_ports:
                continue
            if self._digital_target is not None and self._digital_counts[port] >= self._digital_target:
                continue
            if any(self.workspace.signals[f"P{port}.{bit}"].active
                   for bit in range(8 if port == 0 else 4)):
                self._digital_poll_pending.add(port)
                self.worker.submit("digital_poll", (port, self._digital_generation))

    def _worker_finished(self) -> None:
        self._worker_done = True
        if self._closing:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: object) -> None:
        self.plot_timer.stop()
        self.digital_poll_timer.stop()
        if not self._worker_done:
            self._closing = True
            self.worker.submit("shutdown")
            event.ignore()
            self.hide()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        self.settings.setValue("scope_splitter", self.scope_splitter.saveState())
        event.accept()


def run() -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Open6009")
    app.setOrganizationName("Open6009")
    app.setDesktopFileName("open6009")
    window = MainWindow()
    window.show()
    return app.exec()
