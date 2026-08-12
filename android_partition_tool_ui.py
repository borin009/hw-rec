"""HW rec partition tool UI."""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

# PyInstaller's executable is also used to launch the bundled command-line
# helpers. Dispatch those child invocations before importing the GUI toolkit.
if getattr(sys, "frozen", False) and len(sys.argv) > 1:
    helper_name = Path(sys.argv[1]).name.casefold()
    if helper_name in {
        "direct_adb_usb.py",
        "direct_fastboot_usb.py",
        "huawei_update_app_scanner.py",
        "lun_slice_extractor.py",
    }:
        helper_path = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / helper_name
        sys.argv = [str(helper_path), *sys.argv[2:]]
        runpy.run_path(str(helper_path), run_name="__main__")
        raise SystemExit(0)

# The USB transport helper requires 32-bit Python, but PySide6 is bundled with
# the application's 64-bit runtime. Make accidental 32-bit UI launches repair
# themselves instead of failing at the PySide6 import.
if struct.calcsize("P") * 8 == 32:
    bundled_python = (
        Path(__file__).resolve().parent
        / "runtime"
        / "python64"
        / "python.exe"
    )
    if bundled_python.is_file():
        subprocess.Popen(
            [str(bundled_python), str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=str(Path(__file__).resolve().parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raise SystemExit(0)
    raise RuntimeError(
        "The GUI requires 64-bit Python and the bundled runtime was not found."
    )

from PySide6.QtCore import QProcess, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import license_client
import updater


APP_VERSION = "1.0.4"
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
MAIN_BACKEND = BASE_DIR / "direct_adb_usb.py"
LUN_EXTRACTOR = BASE_DIR / "lun_slice_extractor.py"
ADB_STREAM_WRITER = BASE_DIR / "direct_adb_usb.py"
DIRECT_FASTBOOT = BASE_DIR / "direct_fastboot_usb.py"
UPDATE_APP_SCANNER = BASE_DIR / "huawei_update_app_scanner.py"
PARTITION_PROFILES = BASE_DIR / "partition_profiles"
USB_RECOVERY_SCRIPT = BASE_DIR / "restart_recovery_usb.ps1"
APP_ICON = BASE_DIR / "HW-logo-red-transparent.ico"

# These contain the boot chain, hardware identity, security state, or an
# entire physical LUN.  A donor or mismatched copy can prevent even USB
# recovery from starting, so Recovery ADB must never write them.
RECOVERY_ADB_BLOCKED_TARGETS = {
    "lun0", "lun1", "lun2", "lun3", "gpt_header", "hisiufs_gpt",
    "gpt", "ptable", "vrl", "vrl_backup", "bl2", "fastboot",
    "hisee_encos", "veritykey", "ddr_para", "lowpower_para",
    "batt_tp_para", "hhee", "vector", "teeos", "trustfirmware",
    "modem_secure", "nvme", "certification", "oeminfo",
    "secure_storage", "modemnvm_factory", "modemnvm_backup",
    "modemnvm_img", "userdata",
}

# Expert queue mode permits verified backup targets after an exact model/serial
# confirmation. Manual image assignment remains separately protected.
RECOVERY_ADB_WRITE_ALLOWED_TARGETS: set[str] = set(RECOVERY_ADB_BLOCKED_TARGETS)

class MainWindow(QMainWindow):
    HEADERS = [
        "",
        "Disk",
        "No.",
        "Name",
        "Start",
        "End",
        "Size",
        "Remarks",
        "File Path",
    ]
    FASTBOOT_PARTITION_ORDER = (
        "hisiufs_gpt", "ptable", "xloader", "fastboot", "bl2", "boot", "dtbo", "vector",
        "fw_lpm3", "hhee", "vbmeta", "teeos", "trustfirmware", "sensorhub",
        "fw_hifi", "modemnvm_update", "modemnvm_cust", "recovery",
        "recovery_ramdisk", "recovery_vendor", "recovery_vbmeta", "preas",
        "preavs", "erecovery", "erecovery_ramdisk", "erecovery_vendor",
        "erecovery_vbmeta", "eng_vendor", "eng_system", "cache", "ramdisk",
        "super", "metadata", "vbmeta_system", "vbmeta_vendor", "vbmeta_odm",
        "vbmeta_hw_product", "vbmeta_cust", "isp_firmware", "modem_fw", "npu",
        "userdata", "patch", "kpatch", "modem_driver", "version", "preload",
    )

    def __init__(self) -> None:
        super().__init__()
        self.process: QProcess | None = None
        self.output = ""
        self.backup_folder = ""
        self.backup_parent = ""
        self.image_folder = ""
        self.raw_lun_images: dict[int, str] = {}
        self.device_lun_sizes: dict[int, int] = {}
        self.device_userdata_sizes: dict[int, int] = {}
        self.device_partition_sizes: dict[str, int] = {}
        self.device_partition_layout: dict[tuple[int, str], tuple[int, int]] = {}
        self.source_userdata_sizes: dict[int, int] = {}
        self.image_folder_layout_mismatches: dict[tuple[int, str], str] = {}
        self.whole_lun_preserve_gpt_luns: set[int] = set()
        self.device_model = ""
        self.device_serial = ""
        self.device_cpu = ""
        self.read_queue: list[tuple[int, str, str, int, str, int]] = []
        self.current_read: tuple[int, str, str, int] | None = None
        self.read_started_at = 0.0
        self.stop_requested = False
        self.completed_reads: list[int] = []
        self.backup_xml_rows: list[int] = []
        self.invalid_reads: list[str] = []
        self.create_backup_xml = False
        self.write_queue: list[tuple[int, str, str, str, int, int]] = []
        self.current_write: tuple[int, str, str, str, int, int] | None = None
        self.write_started_at = 0.0
        self.current_temp_image = ""
        self.update_app_cache_files: set[str] = set()
        self.huawei_sparse_next_index: dict[str, int] = {}
        self.dload_write_active = False
        self.post_flash_transport = "fastboot"
        self.current_write_phase = ""
        self.write_transport = "adb"
        self.write_last_progress_at = 0.0
        self.write_last_progress_bytes = 0
        self.write_speed_sample_at = 0.0
        self.write_speed_sample_bytes = 0
        self.write_live_speed = 0
        self.usb_recovery_attempted = False
        self.usb_recovery_original_error = ""
        self.auto_backup_running = False
        self.license_session = license_client.cached_session()
        self.write_watchdog = QTimer(self)
        self.write_watchdog.setInterval(1000)
        self.write_watchdog.timeout.connect(self._check_write_stall)
        self.speed_timer = QTimer(self)
        self.speed_timer.setInterval(500)
        self.speed_timer.timeout.connect(self._update_read_speed)
        self.setWindowTitle(f"HW rec v{APP_VERSION}")
        if APP_ICON.is_file():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            # Laptop-friendly default. On short displays, use the complete
            # usable desktop area without extending behind the taskbar.
            if available.height() < 850:
                width = available.width()
                height = available.height()
            else:
                width = min(1280, available.width())
                height = min(800, available.height())
        else:
            width, height = 1280, 800
        self.resize(width, height)
        self.setMinimumSize(1024, 650)
        self._build_ui()
        self._apply_style()
        self.log("Program ready. Connect the phone in Recovery ADB mode.")

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        self.check_button = QPushButton("Check Device")
        self.check_button.clicked.connect(self.check_device)
        self.backup_button = QPushButton("Read")
        self.backup_button.setEnabled(False)
        self.backup_button.clicked.connect(self.read_selected)
        self.write_button = QPushButton("Write")
        self.write_button.setObjectName("writeButton")
        self.write_button.setEnabled(False)
        self.write_button.clicked.connect(self.write_selected)
        self.flash_rec_button = QPushButton("FB Flash Rec")
        self.flash_rec_button.setObjectName("writeButton")
        self.flash_rec_button.clicked.connect(self.flash_recovery_ramdisk_zip)
        self.adb_pin_button = QPushButton("ADB Set PIN")
        self.adb_pin_button.clicked.connect(self.set_adb_pin)
        self.license_button = QPushButton("Account Login")
        self.license_button.clicked.connect(self.license_login)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_operation)
        self.lun_checkbox = QCheckBox("LUN")
        self.lun_checkbox.toggled.connect(self._toggle_all_luns)
        self.super_checkbox = QCheckBox("Super")
        self.super_checkbox.toggled.connect(self._toggle_super)
        self.fastboot_checkbox = QCheckBox("Fastboot")
        self.fastboot_checkbox.toggled.connect(self._fastboot_mode_changed)
        self.select_folder_button = QPushButton("Select Folder")
        self.select_folder_button.clicked.connect(self.select_image_folder)
        self.folder_label = QLabel("No backup folder selected")
        self.folder_label.setObjectName("folderLabel")

        toolbar.addWidget(self.check_button)
        toolbar.addWidget(self.backup_button)
        toolbar.addWidget(self.write_button)
        toolbar.addWidget(self.flash_rec_button)
        toolbar.addWidget(self.adb_pin_button)
        toolbar.addWidget(self.license_button)
        toolbar.addWidget(self.stop_button)
        toolbar.addWidget(self.lun_checkbox)
        toolbar.addWidget(self.super_checkbox)
        toolbar.addWidget(self.fastboot_checkbox)
        toolbar.addWidget(self.select_folder_button)
        toolbar.addWidget(self.folder_label, 1)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.horizontalHeaderItem(0).setText("☐")
        self.table.horizontalHeaderItem(0).setTextAlignment(Qt.AlignCenter)
        self.table.setAlternatingRowColors(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setContextMenuPolicy(Qt.NoContextMenu)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(8, QHeaderView.Stretch)
        self.table.setColumnHidden(7, True)
        widths = [42, 85, 60, 180, 110, 110, 110, 180]
        for column, width in enumerate(widths):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().sectionClicked.connect(self.header_clicked)
        self.table.itemSelectionChanged.connect(self._update_action_buttons)
        self.table.itemChanged.connect(self._update_action_buttons)
        self.table.cellDoubleClicked.connect(self.select_image_for_partition)
        layout.addWidget(self.table, 1)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(23)
        layout.addWidget(self.progress)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(145)
        self.log_box.setMaximumHeight(210)
        self.log_box.setFont(QFont("Consolas", 10))
        layout.addWidget(self.log_box)

        status = QStatusBar()
        self.setStatusBar(status)
        self.model_label = QLabel("Model: —")
        self.mode_label = QLabel("Mode: Recovery ADB")
        self.serial_label = QLabel("SN: —")
        self.cpu_label = QLabel("CPU: —")
        self.version_label = QLabel("Version: —")
        self.speed_label = QLabel("Speed: —")
        self.contact_label = QLabel(
            '<a style="color:#60a5fa;text-decoration:none;" '
            'href="https://wa.me/85598338393">Contact admin: +85598338393</a>'
        )
        self.contact_label.setOpenExternalLinks(True)
        self.contact_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.contact_label.setToolTip("Open WhatsApp chat with admin")
        for label in (
            self.mode_label,
            self.serial_label,
            self.model_label,
            self.cpu_label,
            self.version_label,
            self.contact_label,
        ):
            status.addWidget(label, 1)

    def _apply_style(self) -> None:
        if True:  # HW rec uses the dark theme exclusively.
            self.setStyleSheet(
                """
                QMainWindow, QWidget {
                    background: #111827;
                    color: #e5e7eb;
                    font-family: "Segoe UI";
                    font-size: 10pt;
                }
                QPushButton {
                    background: #2563eb;
                    color: white;
                    border: 0;
                    border-radius: 5px;
                    padding: 8px 15px;
                    font-weight: 600;
                }
                QPushButton:hover { background: #3b82f6; }
                QPushButton:pressed { background: #1d4ed8; }
                QPushButton:disabled {
                    background: #374151;
                    color: #9ca3af;
                }
                #writeButton { background: #dc2626; }
                #writeButton:hover { background: #ef4444; }
                #writeButton:pressed { background: #b91c1c; }
                #writeButton:disabled, #stopButton:disabled {
                    background: #374151;
                    color: #9ca3af;
                }
                #stopButton { background: #ea580c; }
                #stopButton:hover { background: #f97316; }
                #folderLabel { color: #9ca3af; padding-left: 6px; }
                QCheckBox { color: #e5e7eb; spacing: 6px; }
                QTableWidget {
                    background: #1f2937;
                    alternate-background-color: #1f2937;
                    color: #e5e7eb;
                    border: 1px solid #374151;
                    gridline-color: #374151;
                    selection-background-color: #d97706;
                    selection-color: white;
                }
                QTableWidget::item { padding: 3px 6px; }
                QHeaderView::section {
                    background: #18212f;
                    color: #d1d5db;
                    border: 0;
                    border-right: 1px solid #374151;
                    border-bottom: 1px solid #4b5563;
                    padding: 8px 5px;
                    font-weight: 600;
                }
                QProgressBar {
                    background: #374151;
                    color: white;
                    border: 0;
                    border-radius: 11px;
                    text-align: center;
                }
                QProgressBar::chunk {
                    background: #f59e0b;
                    border-radius: 11px;
                }
                QPlainTextEdit {
                    background: #0f172a;
                    border: 1px solid #374151;
                    color: #e5e7eb;
                    padding: 8px;
                }
                QStatusBar {
                    background: #18212f;
                    border-top: 1px solid #374151;
                }
                QStatusBar QLabel { padding: 7px 10px; }
                QToolTip {
                    background: #1f2937;
                    color: #f9fafb;
                    border: 1px solid #4b5563;
                }
                """
            )
            return
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #f7f8fa;
                color: #252932;
                font-family: "Segoe UI";
                font-size: 10pt;
            }
            QPushButton {
                background: #2563eb;
                color: white;
                border: 0;
                border-radius: 5px;
                padding: 8px 15px;
                font-weight: 600;
            }
            QPushButton:hover { background: #1d4ed8; }
            QPushButton:pressed { background: #1e40af; }
            QPushButton:disabled {
                background: #c7ccd5;
                color: #707682;
            }
            #writeButton { background: #dc2626; }
            #writeButton:hover { background: #b91c1c; }
            #writeButton:pressed { background: #991b1b; }
            #writeButton:disabled {
                background: #c7ccd5;
                color: #707682;
            }
            #stopButton { background: #ea580c; }
            #stopButton:hover { background: #c2410c; }
            #stopButton:disabled {
                background: #c7ccd5;
                color: #707682;
            }
            #folderLabel { color: #6b7280; padding-left: 6px; }
            QTableWidget {
                background: white;
                alternate-background-color: white;
                border: 1px solid #d8dce3;
                gridline-color: #e1e4e9;
                selection-background-color: #ffb844;
                selection-color: #20242b;
            }
            QTableWidget::item {
                padding: 3px 6px;
            }
            QHeaderView::section {
                background: #f4f5f7;
                color: #606775;
                border: 0;
                border-right: 1px solid #e3e6eb;
                border-bottom: 1px solid #d8dce3;
                padding: 8px 5px;
                font-weight: 600;
            }
            QProgressBar {
                background: #e4e6ea;
                border: 0;
                border-radius: 11px;
            }
            QProgressBar::chunk {
                background: #ffb33b;
                border-radius: 11px;
            }
            QPlainTextEdit {
                background: white;
                border: 1px solid #d8dce3;
                color: #1f2937;
                padding: 8px;
            }
            QStatusBar {
                background: #f1f3f6;
                border-top: 1px solid #d8dce3;
            }
            QStatusBar QLabel { padding: 7px 10px; }
            """
        )

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.appendPlainText(f"[{timestamp}] {message}")

    def replace_last_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        block = self.log_box.document().lastBlock()
        cursor = QTextCursor(block)
        cursor.movePosition(
            QTextCursor.EndOfBlock, QTextCursor.KeepAnchor
        )
        cursor.removeSelectedText()
        cursor.insertText(f"[{timestamp}] {message}")
        self.log_box.setTextCursor(cursor)
        self.log_box.ensureCursorVisible()

    def _follow_active_partition(self, row: int, name: str) -> None:
        """Keep the currently processed partition centered in the table."""
        if row < 0:
            wanted = name.casefold()
            row = next(
                (
                    candidate
                    for candidate in range(self.table.rowCount())
                    if self.table.item(candidate, 3)
                    and self.table.item(candidate, 3).text().casefold() == wanted
                ),
                -1,
            )
        if not 0 <= row < self.table.rowCount():
            return
        item = self.table.item(row, 3)
        if item is None:
            return
        self.table.setCurrentCell(row, 3)
        self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)

    def set_busy(self, busy: bool) -> None:
        self.check_button.setEnabled(not busy)
        if busy:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)

    def check_device(self, automatic_retry: bool = False) -> None:
        if not automatic_retry and not self.require_license():
            return
        if not automatic_retry:
            self.usb_recovery_attempted = False
        if self.fastboot_checkbox.isChecked():
            self._check_fastboot_device()
            return
        if not MAIN_BACKEND.exists():
            QMessageBox.critical(
                self,
                "Missing File",
                "direct_adb_usb.py must be in the same folder as this UI.",
            )
            return

        self.log("Checking device and loading partitions...")
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments(
            [str(MAIN_BACKEND), "info"]
        )
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._probe_finished)
        self.process.start()

    def _read_output(self) -> None:
        if self.process:
            self.output += bytes(self.process.readAllStandardOutput()).decode(
                "utf-8", "replace"
            )
            self.output += bytes(self.process.readAllStandardError()).decode(
                "utf-8", "replace"
            )

    def _probe_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._read_output()
        self.set_busy(False)
        if exit_code == 0 and "ADB read info: OK" in self.output:
            self.usb_recovery_attempted = False
            info = self._parse_info_output(self.output)
            display_model = self._display_model(info)
            self.device_model = display_model
            self.device_serial = info.get("SERIAL", "unknown")
            self.device_cpu = info.get("CPU", "unknown")
            self.mode_label.setText("Mode: Recovery ADB")
            self.model_label.setText(f"Model: {display_model}")
            self.serial_label.setText(f"SN: {info.get('SERIAL', 'unknown')}")
            self.cpu_label.setText(f"CPU: {info.get('CPU', 'unknown')}")
            self.version_label.setText(f"Version: {info.get('BUILD', 'unknown')}")
            self.read_partition_table()
        else:
            error = self.output.strip() or f"Probe stopped with exit code {exit_code}."
            error = re.sub(r"^ERROR:\s*", "", error, count=1)
            if (
                "WinError 121" in error
                and not self.usb_recovery_attempted
                and USB_RECOVERY_SCRIPT.is_file()
            ):
                self.usb_recovery_attempted = True
                self.usb_recovery_original_error = error
                self._restart_recovery_usb()
                return
            self.log("Device check failed.")
            QMessageBox.warning(self, "Device Check Failed", error)

    def _restart_recovery_usb(self) -> None:
        self.log("Recovery ADB timed out; restarting Huawei USB in Windows...")
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram("powershell.exe")
        self.process.setArguments(
            [
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", str(USB_RECOVERY_SCRIPT),
            ]
        )
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._recovery_usb_restart_finished)
        self.process.start()

    def _recovery_usb_restart_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self._read_output()
        if exit_code != 0:
            self.set_busy(False)
            error = self.output.strip() or self.usb_recovery_original_error
            self.log("Automatic Huawei USB restart failed.")
            QMessageBox.warning(self, "USB Recovery Failed", error)
            return
        self.log("Huawei USB restarted; retrying Check Device...")
        QTimer.singleShot(2000, lambda: self.check_device(True))

    def _check_fastboot_device(self) -> None:
        if not DIRECT_FASTBOOT.is_file():
            QMessageBox.critical(
                self, "Missing File", "direct_fastboot_usb.py is missing."
            )
            return
        self.log("Checking direct Python Fastboot USB interface...")
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments([str(DIRECT_FASTBOOT), "info"])
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._fastboot_probe_finished)
        self.process.start()

    def _fastboot_probe_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self._read_output()
        self.set_busy(False)
        if exit_code != 0:
            self.log("Direct Fastboot device check failed.")
            QMessageBox.warning(
                self, "Fastboot Check Failed", self.output.strip()
            )
            self._update_action_buttons()
            return
        product = re.search(r"^PRODUCT:(.*)$", self.output, re.MULTILINE)
        platform = re.search(r"^PLATFORM:(.*)$", self.output, re.MULTILINE)
        serial = re.search(r"^SERIAL:(.*)$", self.output, re.MULTILINE)
        build = re.search(r"^BUILD:(.*)$", self.output, re.MULTILINE)
        self.device_model = product.group(1).strip() if product else "unknown"
        self.device_cpu = platform.group(1).strip() if platform else "unknown"
        self.device_serial = serial.group(1).strip() if serial else "unknown"
        build_version = build.group(1).strip() if build else "unknown"
        self.mode_label.setText("Mode: Direct Fastboot USB")
        self.model_label.setText(f"Model: {self.device_model}")
        self.cpu_label.setText(f"CPU: {self.device_cpu}")
        self.serial_label.setText(f"SN: {self.device_serial}")
        self.version_label.setText(f"Version: {build_version}")
        self.replace_last_log("Direct Fastboot USB connection: OK")
        # Keep images already loaded with Select Folder (for example the
        # standalone super.bin row).  The generic Fastboot target list is only
        # useful when no folder/image selection currently exists.
        if self.table.rowCount() == 0:
            self._populate_fastboot_partition_targets()
        self._update_action_buttons()

    def _populate_fastboot_partition_targets(self) -> None:
        self.table.setRowCount(0)
        for number, name in enumerate(self.FASTBOOT_PARTITION_ORDER, start=1):
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                "", "FB", str(number), name, "-", "-", "-",
                "Double-click to select image", "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    item.setCheckState(
                        Qt.Checked if (
                            name.casefold() == "super"
                            and self.super_checkbox.isChecked()
                        ) else Qt.Unchecked
                    )
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, column, item)
        self.log(f"Loaded Fastboot partition targets: {self.table.rowCount()}")

    def select_image_for_partition(self, row: int, _column: int) -> None:
        if not 0 <= row < self.table.rowCount():
            return
        name_item = self.table.item(row, 3)
        if not name_item:
            return
        if not self.fastboot_checkbox.isChecked() and self._is_protected_row(row):
            if name_item.text().casefold() != "oeminfo":
                QMessageBox.warning(
                    self, "Protected Partition",
                    "This partition cannot be assigned manually in Recovery ADB mode.",
                )
                return
        selected, _filter = QFileDialog.getOpenFileName(
            self, f"Select image for {name_item.text()}",
            self.image_folder or self.backup_folder or self.backup_parent,
            "Partition images (*.img *.bin);;All files (*.*)",
        )
        if not selected:
            return
        path = Path(selected)
        size = path.stat().st_size
        self.table.item(row, 8).setText(str(path))
        if self.fastboot_checkbox.isChecked():
            self.table.item(row, 6).setText(self._format_size(size))
            self.table.item(row, 6).setData(Qt.UserRole, size)
            compatible = True
        else:
            expected_size = int(
                self.table.item(row, 6).data(Qt.UserRole) or 0
            )
            compatible = expected_size > 0 and size == expected_size
        if compatible:
            self.table.item(row, 7).setText("Manual image - ready to write")
            self.table.item(row, 0).setCheckState(Qt.Checked)
        else:
            self.table.item(row, 7).setText(
                "Manual image size does not match current partition"
            )
            self.table.item(row, 0).setCheckState(Qt.Unchecked)
            QMessageBox.warning(
                self,
                "Image Size Does Not Match Phone",
                f"Image: {self._format_size(size)}\n"
                f"Phone partition: {self._format_size(expected_size)}",
            )
        self.image_folder = str(path.parent)
        self.folder_label.setText(str(path.parent))
        self.log(f"Assigned {path.name} to {name_item.text()}")
        self._update_action_buttons()

    @staticmethod
    def _parse_info_output(output: str) -> dict[str, str]:
        info = {}
        for line in output.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            if key in {
                "MODEL", "SKU", "SERIAL", "ANDROID", "BUILD", "CPU", "ABI", "MODE"
            }:
                info[key] = value.strip()
        return info

    @staticmethod
    def _display_model(info: dict[str, str]) -> str:
        sku = info.get("SKU", "").strip()
        if sku and sku.lower() != "unknown":
            return sku
        return info.get("MODEL", "unknown")

    def _add_info_row(self, info: dict[str, str]) -> None:
        self.table.setRowCount(0)
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            "", "ADB", "0", self._display_model(info), "—", "—", "—",
            (
                f"Android {info.get('ANDROID', 'unknown')} | "
                f"{info.get('BUILD', 'unknown')}"
            ),
            "",
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
                item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, column, item)
        self.table.selectRow(row)

    def read_partition_table(self) -> None:
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments([str(MAIN_BACKEND), "partitions"])
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._partitions_finished)
        self.process.start()

    def _partitions_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self._read_output()
        self.set_busy(False)
        marker = "PARTITIONS_JSON:"
        if exit_code != 0 or marker not in self.output:
            error = self.output.strip() or "Partition list returned no data."
            self.log("Partition-table read failed.")
            QMessageBox.warning(self, "Partition Read Failed", error)
            return
        payload = self.output.split(marker, 1)[1].splitlines()[0]
        try:
            result = json.loads(payload)
        except json.JSONDecodeError as exc:
            self.log("Partition-table response was invalid.")
            QMessageBox.warning(self, "Partition Read Failed", str(exc))
            return
        partitions = result.get("partitions", [])
        partitions = self._apply_cpu_partition_profile(partitions)
        self._capture_device_capacities(partitions)
        self._populate_partitions(partitions)
        self.replace_last_log(
            f"Partitions loaded successfully: {len(partitions)}"
        )
        self._update_action_buttons()
        QTimer.singleShot(0, self._start_check_device_auto_backup)

    def _apply_cpu_partition_profile(self, partitions: list[dict]) -> list[dict]:
        """Correct live sysfs metadata with a compatible CPU XML profile."""
        cpu = re.sub(r"[^a-z0-9]+", "", self.device_cpu.casefold())
        profile_dir = PARTITION_PROFILES / cpu
        if not profile_dir.is_dir():
            return partitions

        saved: dict[tuple[int, int], tuple[str, int, int]] = {}
        try:
            for xml_path in sorted(profile_dir.glob("rawprogram*.xml")):
                root = ET.parse(xml_path).getroot()
                for program in root.findall(".//program"):
                    lun = int(program.get("physical_partition_number", "-1"))
                    filename = program.get("filename", "")
                    match = re.match(r"LUN\d+_(\d+)_", filename, re.IGNORECASE)
                    if not match:
                        continue
                    number = int(match.group(1))
                    label = program.get("label", "").strip()
                    start_4k = int(program.get("start_sector", "-1"))
                    sector_size = int(
                        program.get("SECTOR_SIZE_IN_BYTES", "4096")
                    )
                    size = (
                        int(program.get("num_partition_sectors", "-1"))
                        * sector_size
                    )
                    if number > 0 and label and start_4k >= 0 and size > 0:
                        saved[(lun, number)] = (label, start_4k, size)
        except (OSError, ValueError, ET.ParseError) as error:
            self.log(f"Ignored invalid {cpu} partition profile: {error}")
            return partitions

        live_keys: set[tuple[int, int]] = set()
        for partition in partitions:
            path = str(partition.get("path", ""))
            match = re.fullmatch(r"/dev/block/sd([a-d])(\d+)", path)
            if match:
                live_keys.add(
                    (ord(match.group(1)) - ord("a"), int(match.group(2)))
                )
        overlap = live_keys & saved.keys()
        if not saved or len(overlap) < max(1, int(len(saved) * 0.9)):
            self.log(
                f"Ignored {cpu} partition profile: live partition map is incompatible."
            )
            return partitions

        corrected = []
        changes = 0
        for partition in partitions:
            item = dict(partition)
            path = str(item.get("path", ""))
            match = re.fullmatch(r"/dev/block/sd([a-d])(\d+)", path)
            if match:
                key = (
                    ord(match.group(1)) - ord("a"), int(match.group(2))
                )
                reference = saved.get(key)
                if reference:
                    label, start_4k, size = reference
                    if str(item.get("name", "")).casefold() != label.casefold():
                        self.log(
                            f"Ignored {cpu} profile entry {key}: partition name differs."
                        )
                    else:
                        start_512 = start_4k * 8
                        if (
                            int(item.get("start_lba", 0)) != start_512
                            or int(item.get("size", 0)) != size
                        ):
                            changes += 1
                        item.update(
                            start_lba=start_512,
                            end_lba=start_512 + size // 512 - 1,
                            sectors=size // 512,
                            size=size,
                        )
            corrected.append(item)
        if changes:
            self.log(
                f"Applied {cpu} XML partition profile: corrected {changes} entries."
            )
        else:
            self.log(f"Verified live partition map against {cpu} XML profile.")
        return corrected

    def _start_check_device_auto_backup(self) -> None:
        if self.auto_backup_running:
            return
        wanted_rows = []
        for row in range(self.table.rowCount()):
            disk_item = self.table.item(row, 1)
            number_item = self.table.item(row, 2)
            check_item = self.table.item(row, 0)
            if not disk_item or not number_item or not check_item:
                continue
            try:
                number = int(number_item.text())
            except ValueError:
                continue
            disk = disk_item.text()
            wanted = (
                (disk == "LUN2" and number == 0)
                or (disk == "LUN3" and (number == 0 or 3 <= number <= 10))
            )
            check_item.setCheckState(Qt.Checked if wanted else Qt.Unchecked)
            if wanted:
                wanted_rows.append(row)

        if len(wanted_rows) != 10:
            self.log(
                f"Auto backup not started: expected 10 targets, found "
                f"{len(wanted_rows)}."
            )
            self._update_action_buttons()
            return
        self.auto_backup_running = True
        self.log("Auto backup selected: LUN2_0_GPT, LUN3_0_GPT, LUN3_3-10")
        self.read_selected()
        if not self.read_queue and not self.current_read:
            self.auto_backup_running = False

    def _capture_device_capacities(self, partitions: list[dict]) -> None:
        self.device_lun_sizes.clear()
        self.device_userdata_sizes.clear()
        self.device_partition_sizes.clear()
        self.device_partition_layout.clear()
        for partition in partitions:
            path = str(partition.get("path", ""))
            match = re.fullmatch(r"/dev/block/sd([a-d])(\d*)", path)
            if not match:
                continue
            lun = ord(match.group(1)) - ord("a")
            size = int(partition.get("size", 0))
            if not match.group(2):
                self.device_lun_sizes[lun] = size
            name = str(partition.get("name", "")).casefold()
            if match.group(2) and name:
                self.device_partition_sizes[name] = size
                self.device_partition_layout[(lun, name)] = (
                    int(partition.get("start_lba", 0)) // 8,
                    size,
                )
            if name == "userdata":
                self.device_userdata_sizes[lun] = size

    def _populate_partitions(self, partitions: list[dict]) -> None:
        self.table.setRowCount(0)
        organized = []
        for partition in partitions:
            block_path = str(partition.get("path", ""))
            match = re.fullmatch(r"/dev/block/sd([a-d])(\d*)", block_path)
            if not match:
                continue

            lun = ord(match.group(1)) - ord("a")
            partition_number = int(match.group(2) or 0)
            name = str(partition.get("name", "unknown"))
            size = int(partition.get("size", 0))
            start_512 = int(partition.get("start_lba", 0))

            if partition_number == 0 and lun >= 2:
                # Match the clean raw-program layout: the first 34 4K sectors
                # contain the primary GPT header and entry array.
                name = "GPT_Header"
                size = 34 * 4096
                start_lba = 0
                end_lba = 33
                remarks = ""
            else:
                start_lba = start_512 // 8
                sectors_4k = size // 4096
                end_lba = start_lba + sectors_4k - 1 if sectors_4k else 0
                remarks = ""
                if partition_number == 0:
                    name = f"LUN{lun}"

            organized.append(
                {
                    "lun": lun,
                    "number": partition_number,
                    "name": name,
                    "start_lba": start_lba,
                    "end_lba": end_lba,
                    "size": size,
                    "remarks": remarks,
                    "path": block_path,
                }
            )

        organized.sort(key=lambda item: (item["lun"], item["number"]))
        for partition in organized:
            row = self.table.rowCount()
            self.table.insertRow(row)
            size = int(partition.get("size", 0))
            size_text = self._format_size(size)
            lun = int(partition["lun"])
            number = int(partition["number"])
            name = str(partition["name"])
            values = [
                "",
                f"LUN{lun}",
                str(number),
                name,
                str(partition.get("start_lba", 0)),
                str(partition.get("end_lba", 0)),
                size_text,
                str(partition.get("remarks", "")),
                "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 6:
                    item.setData(Qt.UserRole, size)
                if column == 0:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    item.setCheckState(
                        Qt.Checked if (
                            name.casefold() == "super"
                            and self.super_checkbox.isChecked()
                        ) else Qt.Unchecked
                    )
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, column, item)
        if self.table.rowCount():
            self.table.selectRow(0)

    def _selected_rows(self) -> list[int]:
        rows: set[int] = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                rows.add(row)
        return sorted(rows)

    def _update_action_buttons(self, *_args) -> None:
        rows = self._selected_rows()
        busy = self.process is not None and self.process.state() != QProcess.NotRunning
        self.backup_button.setEnabled(
            bool(rows) and not busy and not self.fastboot_checkbox.isChecked()
        )
        self.write_button.setEnabled(bool(rows) and not busy)
        self.flash_rec_button.setEnabled(not busy)
        self.adb_pin_button.setEnabled(not busy)
        self.license_button.setEnabled(not busy)
        self._update_header_checkbox()

    def require_license(self) -> bool:
        self.license_session = license_client.cached_session()
        if self.license_session is not None:
            return True
        return self.license_login()

    def license_login(self) -> bool:
        username, accepted = QInputDialog.getText(
            self, "HW rec Login", "Enter username or 24-hour token:"
        )
        if not accepted or not username.strip():
            return False
        value = username.strip()
        password = ""
        if value.count(".") != 2:
            password, accepted = QInputDialog.getText(
                self, "HW rec Login", "Enter password:", QLineEdit.Password
            )
            if not accepted or not password:
                return False
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.license_session = (
                license_client.login_token(value)
                if value.count(".") == 2
                else license_client.login_account(value, password)
            )
        except Exception as error:
            QMessageBox.warning(self, "License Login Failed", str(error))
            return False
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(
            self, "License Active", "Login successful. This PC is authorized for 24 hours."
        )
        self.log("Account login successful; 24-hour session active.")
        self._update_action_buttons()
        return True

    def check_for_updates(self) -> None:
        try:
            release = updater.check_for_update(APP_VERSION)
        except Exception as error:
            self.log(f"Update check failed: {error}")
            return

        if release is None:
            return

        if not getattr(sys, "frozen", False):
            self.log(f"Update v{release.version} is available for packaged builds.")
            return
        try:
            downloaded = updater.download_verified(release)
            updater.launch_replacement(downloaded)
        except Exception as error:
            self.log(f"Automatic update failed: {error}")
            return
        QApplication.quit()

    def set_adb_pin(self) -> None:
        if not self.require_license():
            return
        answer = QMessageBox.warning(
            self, "Confirm ADB PIN",
            "Set the connected Android device lock-screen PIN to 123123?\n\n"
            "This changes the device security setting.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if not MAIN_BACKEND.is_file():
            QMessageBox.warning(
                self, "Missing Backend", f"Direct ADB backend not found:\n{MAIN_BACKEND}"
            )
            return
        self.log("Setting device lock-screen PIN over ADB.........")
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments(
            [str(MAIN_BACKEND), "shell", "locksettings", "set-pin", "123123"]
        )
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._adb_pin_finished)
        self.process.start()

    def _adb_pin_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self._read_output()
        self.set_busy(False)
        failed = exit_code != 0 or bool(
            re.search(r"\b(?:error|exception|failed|unauthorized)\b", self.output, re.I)
        )
        if failed:
            self.replace_last_log("Setting device lock-screen PIN over ADB.........failed")
            QMessageBox.warning(
                self, "Set PIN Failed",
                self.output.strip() or "The device rejected the locksettings command.",
            )
        else:
            self.replace_last_log("Setting device lock-screen PIN over ADB.........done")
            QMessageBox.information(self, "PIN Set", "Device PIN was set to 123123.")
        self._update_action_buttons()

    def flash_recovery_ramdisk_zip(self) -> None:
        """Extract recovery_ramdisk.img from ZIP and flash it over Fastboot."""
        if not self.require_license():
            return
        if (
            not self.device_model
            or self.device_model.casefold() == "unknown"
            or not self.device_serial
            or self.device_serial.casefold() == "unknown"
        ):
            QMessageBox.warning(
                self,
                "Fastboot Device Not Verified",
                "Run Check Device successfully before flashing recovery.",
            )
            return
        zip_path = BASE_DIR / "recovery_ramdisk.zip"
        if not zip_path.is_file():
            selected, _filter = QFileDialog.getOpenFileName(
                self, "Select recovery_ramdisk ZIP",
                self.image_folder or str(BASE_DIR), "ZIP archives (*.zip)"
            )
            if not selected:
                return
            zip_path = Path(selected)
        try:
            with zipfile.ZipFile(zip_path) as archive:
                members = [
                    info for info in archive.infolist()
                    if not info.is_dir()
                    and Path(info.filename).name.casefold() == "recovery_ramdisk.img"
                ]
                if len(members) != 1:
                    raise ValueError(
                        "ZIP must contain exactly one recovery_ramdisk.img file."
                    )
                member = members[0]
                if member.file_size <= 0:
                    raise ValueError("recovery_ramdisk.img is empty.")
                answer = QMessageBox.warning(
                    self, "Confirm FB Flash Recovery",
                    f"Archive: {zip_path.name}\n"
                    f"Image: recovery_ramdisk.img\n"
                    f"Size: {self._format_size(member.file_size)}\n"
                    f"Model: {self.device_model or 'not checked'}\n"
                    f"SN: {self.device_serial or 'not checked'}\n\n"
                    "Flash to recovery_ramdisk now?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
                descriptor, temp_name = tempfile.mkstemp(
                    prefix="kingunlock_recovery_", suffix=".img"
                )
                try:
                    with os.fdopen(descriptor, "wb") as destination:
                        with archive.open(member, "r") as source:
                            shutil.copyfileobj(source, destination, 4 * 1024 * 1024)
                except Exception:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    Path(temp_name).unlink(missing_ok=True)
                    raise
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
            QMessageBox.warning(self, "Invalid Recovery ZIP", str(error))
            return
        self.fastboot_checkbox.setChecked(True)
        self.write_transport = "fastboot"
        self.stop_requested = False
        self.current_temp_image = temp_name
        self.write_queue = [
            (-1, "recovery_ramdisk", temp_name, "recovery_ramdisk", member.file_size, 0)
        ]
        self.log(f"Recovery ZIP ready: {zip_path.name}")
        self._start_next_write()

    def read_selected(self) -> None:
        if not self.require_license():
            return
        if self.super_checkbox.isChecked():
            # Super mode is intentionally exclusive even if another row was
            # manually checked after enabling the toolbar option.
            self._toggle_super(True)
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Read Partition", "Select a partition first.")
            return
        if not self.backup_parent:
            self._choose_backup_parent()
            if not self.backup_parent:
                return
        self._prepare_backup_folder()
        if self.auto_backup_running:
            pending_rows = []
            ready_count = 0
            for row in rows:
                disk = self.table.item(row, 1).text()
                number = self.table.item(row, 2).text()
                name = self.table.item(row, 3).text()
                expected_size = int(
                    self.table.item(row, 6).data(Qt.UserRole) or 0
                )
                destination = Path(self.backup_folder) / (
                    f"{disk}_{number}_{name}.bin"
                )
                if (
                    expected_size > 0
                    and destination.is_file()
                    and destination.stat().st_size == expected_size
                ):
                    ready_count += 1
                    self.table.item(row, 0).setCheckState(Qt.Unchecked)
                    path_item = self.table.item(row, 8)
                    if path_item:
                        path_item.setText(str(destination))
                else:
                    pending_rows.append(row)
            if ready_count:
                self.log(f"Auto backup already ready: {ready_count}/10 files")
            rows = pending_rows
            if not rows:
                self.auto_backup_running = False
                self.log("Auto backup already complete; no files read again.")
                self._update_action_buttons()
                return
        selected_luns = {
            int(self.table.item(row, 1).text().removeprefix("LUN"))
            for row in rows
            if self.table.item(row, 1)
            and re.fullmatch(r"LUN[0-3]", self.table.item(row, 1).text())
        }
        selected_size = (
            sum(self.device_lun_sizes.get(lun, 0) for lun in selected_luns)
            if self.lun_checkbox.isChecked()
            else sum(
                int(self.table.item(row, 6).data(Qt.UserRole) or 0)
                for row in rows
            )
        )
        if self.lun_checkbox.isChecked() and any(
            not self.device_lun_sizes.get(lun) for lun in selected_luns
        ):
            QMessageBox.warning(
                self,
                "LUN Capacity Unknown",
                "Run Check Device again. A complete LUN backup requires the exact "
                "capacity of every selected LUN.",
            )
            return
        free_space = shutil.disk_usage(self.backup_folder).free
        required_space = selected_size
        if free_space < required_space:
            QMessageBox.warning(
                self,
                "Not Enough Free Space",
                f"Selected backup: {self._format_size(selected_size)}\n"
                f"Required free space: {self._format_size(required_space)}\n"
                f"Available: {self._format_size(free_space)}",
            )
            return
        self.log(
            f"Backup size: {self._format_size(selected_size)} | "
            f"Free space: {self._format_size(free_space)}"
        )
        self.read_queue = []
        self.completed_reads = []
        self.backup_xml_rows = list(rows)
        self.invalid_reads = []
        eligible_rows = [
            row
            for row in range(self.table.rowCount())
            if not self._is_select_all_excluded(row)
        ]
        self.create_backup_xml = self.lun_checkbox.isChecked() or (
            bool(eligible_rows) and set(rows) == set(eligible_rows)
        )
        if self.lun_checkbox.isChecked():
            for lun in range(4):
                lun_rows = [
                    row for row in rows
                    if self.table.item(row, 1).text() == f"LUN{lun}"
                ]
                if not lun_rows:
                    continue
                size = self.device_lun_sizes[lun]
                name = f"LUN{lun}"
                destination = str(Path(self.backup_folder) / f"{name}.bin")
                source = f"/dev/block/sd{chr(ord('a') + lun)}"
                self.read_queue.append(
                    (-1, name, destination, size, source, 0)
                )
        else:
            for row in rows:
                name = self.table.item(row, 3).text()
                disk = self.table.item(row, 1).text()
                number = self.table.item(row, 2).text()
                filename = f"{disk}_{number}_{name}.bin"
                destination = str(Path(self.backup_folder) / filename)
                size = int(self.table.item(row, 6).data(Qt.UserRole) or 0)
                lun = int(disk.removeprefix("LUN"))
                if name in {"GPT_Header", "LUN0", "LUN1"}:
                    source = f"/dev/block/sd{chr(ord('a') + lun)}"
                    self.read_queue.append(
                        (row, name, destination, size, source, 0)
                    )
                else:
                    self.read_queue.append(
                        (row, name, destination, size, "", 0)
                    )
        if not self.read_queue:
            QMessageBox.information(
                self, "Read Partition", "Select a named physical partition."
            )
            return
        self._start_next_read()

    def _start_next_read(self) -> None:
        if not self.read_queue:
            self.set_busy(False)
            self.auto_backup_running = False
            if self.create_backup_xml and self.backup_xml_rows:
                self._write_rawprogram_xml()
            self.create_backup_xml = False
            if self.lun_checkbox.isChecked():
                self.lun_checkbox.setChecked(False)
            self.log("All selected partitions read successfully.")
            self._update_action_buttons()
            return
        row, name, destination, expected_size, source, start_sector = (
            self.read_queue.pop(0)
        )
        self.current_read = (row, name, destination, expected_size)
        self._follow_active_partition(row, name)
        cancel_path = destination + ".cancel"
        try:
            Path(cancel_path).unlink()
        except OSError:
            pass
        self.read_started_at = time.monotonic()
        self.stop_requested = False
        self.log(f"Reading: {Path(destination).name}.........")
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        source_path = source or f"/dev/block/by-name/{name}"
        count = (expected_size + 4095) // 4096
        arguments = [
            str(MAIN_BACKEND),
            "pull",
            source_path,
            destination,
            str(expected_size),
            "--cancel-file",
            cancel_path,
        ]
        self.process.setArguments(arguments)
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._read_finished)
        self.process.start()
        self.stop_button.setEnabled(True)
        self.speed_timer.start()
        self._update_action_buttons()

    def _read_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self._read_output()
        self.speed_timer.stop()
        self.stop_button.setEnabled(False)
        if self.stop_requested:
            self.set_busy(False)
            self.auto_backup_running = False
            if self.current_read:
                self.replace_last_log(
                    f"Reading: {Path(self.current_read[2]).name}.........stopped"
                )
            self.read_queue.clear()
            self.current_read = None
            self.speed_label.setText("Speed: stopped")
            self._update_action_buttons()
            return
        if exit_code != 0:
            self.set_busy(False)
            self.auto_backup_running = False
            error = self.output.strip() or "Partition read failed."
            if self.current_read:
                try:
                    Path(self.current_read[2] + ".part").unlink()
                except OSError:
                    pass
                self.replace_last_log(
                    f"Reading: {Path(self.current_read[2]).name}.........failed"
                )
            self.read_queue.clear()
            QMessageBox.warning(self, "Read Partition Failed", error)
            self._update_action_buttons()
            return
        if self.current_read:
            row, _name, destination, expected_size = self.current_read
            completed = Path(destination)
            actual_size = completed.stat().st_size if completed.is_file() else 0
            if actual_size != expected_size:
                self.set_busy(False)
                self.auto_backup_running = False
                self.read_queue.clear()
                try:
                    completed.unlink()
                except OSError:
                    pass
                self.replace_last_log(
                    f"Reading: {Path(destination).name}.........failed"
                )
                QMessageBox.warning(
                    self,
                    "Read Partition Failed",
                    f"Size mismatch: expected {expected_size} bytes, "
                    f"received {actual_size}.",
                )
                self._update_action_buttons()
                return
            blank_data = not self._range_has_payload(completed, 0, expected_size)
            if blank_data:
                self.invalid_reads.append(Path(destination).name)
            elapsed = max(time.monotonic() - self.read_started_at, 0.001)
            self.speed_label.setText(
                f"Speed: {self._format_size(int(expected_size / elapsed))}/s"
            )
            if row >= 0:
                check_item = self.table.item(row, 0)
                if check_item:
                    check_item.setCheckState(Qt.Unchecked)
                path_item = self.table.item(row, 8)
                if path_item:
                    path_item.setText(destination)
                self.completed_reads.append(row)
            self.table.clearSelection()
            suffix = "done"
            self.replace_last_log(
                f"Reading: {Path(destination).name}.........{suffix}"
            )
        self.current_read = None
        self._start_next_read()

    def _update_read_speed(self) -> None:
        if not self.current_read:
            return
        _row, _name, destination, expected_size = self.current_read
        partial = Path(destination + ".part")
        transferred = partial.stat().st_size if partial.exists() else 0
        elapsed = max(time.monotonic() - self.read_started_at, 0.001)
        percent = min(100, int(transferred * 100 / expected_size))
        self.progress.setRange(0, 100)
        self.progress.setValue(percent)
        self.speed_label.setText(
            f"Speed: {self._format_size(int(transferred / elapsed))}/s ({percent}%)"
        )

    def stop_operation(self) -> None:
        if not self.process or self.process.state() == QProcess.NotRunning:
            return
        self.stop_requested = True
        if self.current_read:
            cancel_file = Path(self.current_read[2] + ".cancel")
            try:
                cancel_file.write_text("cancel", encoding="ascii")
            except OSError:
                pass
        if not self.process.waitForFinished(5000):
            self.process.kill()
            self.process.waitForFinished(3000)
        if self.current_read:
            partial = Path(self.current_read[2] + ".part")
            try:
                partial.unlink()
            except OSError:
                pass
            try:
                Path(self.current_read[2] + ".cancel").unlink()
            except OSError:
                pass

    def closeEvent(self, event) -> None:
        if self.process and self.process.state() != QProcess.NotRunning:
            self.stop_operation()
        self._cleanup_update_app_caches()
        event.accept()

    @staticmethod
    def _raw_lun_gpt_layout(image: Path, lun: int) -> dict[tuple[int, str], tuple[int, int]]:
        """Read a raw LUN's primary GPT as 4K start sectors and byte sizes."""
        with image.open("rb") as fp:
            block_size = 0
            for candidate in (4096, 512, 2048, 8192, 16384):
                fp.seek(candidate)
                if fp.read(8) == b"EFI PART":
                    block_size = candidate
                    break
            if not block_size:
                return {}
            fp.seek(block_size)
            header = fp.read(92)
            entries_lba, entry_count, entry_size = struct.unpack_from("<QII", header, 72)
            if not 0 < entry_count <= 16384 or not 128 <= entry_size <= 4096:
                raise ValueError("invalid GPT geometry")
            fp.seek(entries_lba * block_size)
            entries = fp.read(entry_count * entry_size)
            if len(entries) != entry_count * entry_size:
                raise ValueError("truncated GPT entries")
            layout: dict[tuple[int, str], tuple[int, int]] = {}
            for index in range(entry_count):
                entry = entries[index * entry_size:(index + 1) * entry_size]
                if entry[:16] == b"\x00" * 16:
                    continue
                first_lba, last_lba = struct.unpack_from("<QQ", entry, 32)
                if last_lba < first_lba:
                    raise ValueError("invalid GPT partition range")
                name = entry[56:min(entry_size, 128)].decode(
                    "utf-16le", errors="replace"
                ).rstrip("\x00").strip().casefold()
                if name:
                    start_4k = first_lba * block_size // 4096
                    layout[(lun, name)] = (
                        start_4k,
                        (last_lba - first_lba + 1) * block_size,
                    )
            return layout

    @staticmethod
    def _whole_lun_safety_errors(
        folder: Path,
        model: str,
        serial: str,
        images: dict[int, str],
        target_sizes: dict[int, int],
        layout_mismatches: dict[tuple[int, str], str],
        device_layout: dict[tuple[int, str], tuple[int, int]] | None = None,
    ) -> list[str]:
        """Return fail-closed validation errors for destructive raw-LUN writes."""
        errors: list[str] = []
        if not model or model.casefold() == "unknown" or not serial or serial.casefold() == "unknown":
            errors.append("Run Check Device successfully before writing a whole LUN.")
            return errors

        safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", model)
        safe_serial = re.sub(r"[^A-Za-z0-9._-]+", "_", serial)
        normalized_path = re.sub(r"[^a-z0-9]+", "", str(folder).casefold())
        normalized_model = re.sub(r"[^a-z0-9]+", "", safe_model.casefold())
        if normalized_model not in normalized_path:
            errors.append(
                f"Backup path does not identify connected model {safe_model}."
            )
        if layout_mismatches:
            errors.append("Backup GPT layout does not match the connected phone.")

        for lun, image_name in sorted(images.items()):
            image = Path(image_name)
            target_size = target_sizes.get(lun, 0)
            if not target_size:
                errors.append(f"LUN{lun}: connected-device capacity could not be verified.")
                continue
            if not image.is_file():
                errors.append(f"LUN{lun}: raw image is missing.")
                continue
            image_size = image.stat().st_size
            if image_size > target_size:
                errors.append(
                    f"LUN{lun}: image size {image_size} exceeds device capacity "
                    f"{target_size} bytes."
                )
            if image_size % 4096:
                errors.append(f"LUN{lun}: image size is not aligned to a 4096-byte sector.")
            try:
                raw_layout = MainWindow._raw_lun_gpt_layout(image, lun)
            except (OSError, ValueError, struct.error) as exc:
                errors.append(f"LUN{lun}: raw GPT is invalid ({exc}).")
                raw_layout = {}
            if raw_layout:
                for key, source_geometry in raw_layout.items():
                    connected_geometry = (device_layout or {}).get(key)
                    userdata_growth = (
                        key[1] == "userdata"
                        and connected_geometry is not None
                        and connected_geometry[0] == source_geometry[0]
                        and source_geometry[1] <= connected_geometry[1]
                    )
                    if connected_geometry != source_geometry and not userdata_growth:
                        errors.append(
                            f"LUN{lun} {key[1]}: raw GPT layout does not match the phone."
                        )
            elif lun >= 2 or image_size < target_size:
                errors.append(
                    f"LUN{lun}: image has no GPT and cannot be validated safely."
                )

            xml_path = folder / f"rawprogram{lun}.xml"
            if xml_path.is_file():
                try:
                    xml_root = ET.parse(xml_path).getroot()
                    xml_luns = {
                        int(node.get("physical_partition_number", "-1"))
                        for node in xml_root.findall(".//program")
                    }
                    if lun not in xml_luns:
                        errors.append(
                            f"LUN{lun}: rawprogram{lun}.xml does not describe this LUN."
                        )
                except (OSError, ET.ParseError, ValueError):
                    errors.append(f"LUN{lun}: rawprogram{lun}.xml is invalid.")
        return errors

    def _validate_whole_lun_source(self, folder: Path) -> list[str]:
        selected_luns = {
            int(self.table.item(row, 1).text().removeprefix("LUN"))
            for row in self._selected_rows()
            if self.table.item(row, 1)
            and re.fullmatch(r"LUN[0-3]", self.table.item(row, 1).text())
        }
        images = {
            lun: self.raw_lun_images.get(lun, str(folder / f"LUN{lun}.bin"))
            for lun in selected_luns
        }
        if not images:
            return ["No supported raw LUN image is selected."]
        errors = self._whole_lun_safety_errors(
            folder,
            self.device_model,
            self.device_serial,
            images,
            self.device_lun_sizes,
            self.image_folder_layout_mismatches,
            self.device_partition_layout,
        )
        self.whole_lun_preserve_gpt_luns.clear()
        if not errors:
            for lun, image in images.items():
                try:
                    raw_layout = self._raw_lun_gpt_layout(Path(image), lun)
                except (OSError, ValueError, struct.error):
                    continue
                source_userdata = raw_layout.get((lun, "userdata"))
                phone_userdata = self.device_partition_layout.get((lun, "userdata"))
                if source_userdata and phone_userdata and source_userdata != phone_userdata:
                    self.whole_lun_preserve_gpt_luns.add(lun)
        return errors

    def write_selected(self) -> None:
        if not self.require_license():
            return
        if self.fastboot_checkbox.isChecked():
            self._write_selected_fastboot()
            return
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Write", "Select a target first.")
            return
        if any(
            self.table.item(row, 1)
            and self.table.item(row, 1).text().strip().casefold() == "fb"
            for row in rows
        ):
            QMessageBox.warning(
                self,
                "Fastboot Mode Required",
                "The selected folder contains Fastboot images. Enable Fastboot, "
                "run Check Device, and then press Write again.",
            )
            return
        selected_paths = {
            row: self.table.item(row, 8).text()
            for row in rows
            if self.table.item(row, 8)
            and Path(self.table.item(row, 8).text()).is_file()
        }
        source_file = ""
        source_folder = ""
        if not self.lun_checkbox.isChecked() and len(selected_paths) == len(rows):
            pass
        elif len(rows) == 1 and not self.lun_checkbox.isChecked():
            row = rows[0]
            name = self.table.item(row, 3).text().strip()
            if name.casefold() == "oeminfo":
                source_folder = QFileDialog.getExistingDirectory(
                    self,
                    "Select Folder Containing OEMINFO Image",
                    self.image_folder or self.backup_folder or self.backup_parent,
                )
                if not source_folder:
                    return
                disk = self.table.item(row, 1).text().strip()
                number = self.table.item(row, 2).text().strip()
                try:
                    source_file = str(
                        self._find_oeminfo_image(
                            Path(source_folder), disk, number
                        )
                    )
                except ValueError as error:
                    QMessageBox.warning(self, "OEMINFO Image", str(error))
                    return
                self.image_folder = source_folder
                self.folder_label.setText(source_folder)
                self.table.item(row, 8).setText(source_file)
            else:
                source_file, _filter = QFileDialog.getOpenFileName(
                    self,
                    "Select Partition Image",
                    self.backup_folder or self.backup_parent,
                    "Binary images (*.bin *.img);;All files (*.*)",
                )
                if not source_file:
                    return
                source_folder = str(Path(source_file).parent)
        elif (
            self.lun_checkbox.isChecked()
            and self.image_folder
            and Path(self.image_folder).is_dir()
        ):
            source_folder = self.image_folder
        else:
            source_folder = QFileDialog.getExistingDirectory(
                self,
                "Select Write Image Folder",
                self.backup_folder or self.backup_parent,
            )
            if not source_folder:
                return

        jobs: list[tuple[int, str, str, str, int, int]] = []
        if self.lun_checkbox.isChecked():
            safety_errors = self._validate_whole_lun_source(Path(source_folder))
            if safety_errors:
                QMessageBox.critical(
                    self,
                    "Whole-LUN Write Blocked",
                    "Whole-LUN write failed the safety checks:\n\n"
                    + "\n".join(safety_errors[:20]),
                )
                return
            for lun in range(4):
                lun_rows = [
                    row for row in rows
                    if self.table.item(row, 1).text() == f"LUN{lun}"
                ]
                if not lun_rows:
                    continue
                end_lba = max(
                    int(self.table.item(row, 5).text()) for row in lun_rows
                )
                size = (end_lba + 1) * 4096
                filename = f"LUN{lun}.bin"
                image = self.raw_lun_images.get(
                    lun, str(Path(source_folder) / filename)
                )
                if Path(image).is_file():
                    size = Path(image).stat().st_size
                source_offset = 0
                if lun in self.whole_lun_preserve_gpt_luns:
                    source_offset = 34 * 4096
                    size -= source_offset
                target = f"/dev/block/sd{chr(ord('a') + lun)}"
                jobs.append((-1, f"LUN{lun}", image, target, size, source_offset))
        else:
            for row in rows:
                disk = self.table.item(row, 1).text()
                lun = int(disk.removeprefix("LUN"))
                number = self.table.item(row, 2).text()
                name = self.table.item(row, 3).text()
                filename = f"{disk}_{number}_{name}.bin"
                image = (
                    selected_paths.get(row)
                    or source_file
                    or str(Path(source_folder) / filename)
                )
                size = int(self.table.item(row, 6).data(Qt.UserRole) or 0)
                image_size = Path(image).stat().st_size
                if self._allows_compact_vbmeta_image(name, image_size, size):
                    # Huawei VBMeta payloads are signed structures much smaller
                    # than their reserved block partitions. Write the payload
                    # length, not the entire partition capacity.
                    size = image_size
                source_offset = 0
                raw_lun_image = self.raw_lun_images.get(lun)
                if raw_lun_image and Path(image) == Path(raw_lun_image):
                    source_offset = int(self.table.item(row, 4).text()) * 4096
                if name in {"GPT_Header", "LUN0", "LUN1"}:
                    target = f"/dev/block/sd{chr(ord('a') + lun)}"
                else:
                    target = f"/dev/block/by-name/{name}"
                jobs.append((row, name, image, target, size, source_offset))

        blocked_jobs = [
            job for job in jobs
            if job[1].casefold() in RECOVERY_ADB_BLOCKED_TARGETS
            and job[1].casefold() not in RECOVERY_ADB_WRITE_ALLOWED_TARGETS
        ]
        jobs = [
            job for job in jobs
            if job[1].casefold() not in RECOVERY_ADB_BLOCKED_TARGETS
            or job[1].casefold() in RECOVERY_ADB_WRITE_ALLOWED_TARGETS
        ]
        for row, name, _image, _target, _size, _offset in blocked_jobs:
            if row >= 0:
                check_item = self.table.item(row, 0)
                remarks_item = self.table.item(row, 7)
                if check_item:
                    check_item.setCheckState(Qt.Unchecked)
                if remarks_item:
                    remarks_item.setText("Security file")
            self.log(f"Skipped blocked write target: {name}")
        if not jobs:
            QMessageBox.information(
                self,
                "Nothing to Write",
                "All selected targets are blocked in Recovery ADB mode.",
            )
            self._update_action_buttons()
            return

        errors = []
        warnings = []
        for _row, name, image, _target, size, source_offset in jobs:
            path = Path(image)
            if not path.is_file():
                errors.append(f"{name}: file not found ({path.name})")
            elif path.stat().st_size < source_offset + size:
                errors.append(
                    f"{name}: selected range ends at "
                    f"{source_offset + size} bytes, but the image has "
                    f"{path.stat().st_size} bytes"
                    )
            else:
                if _target.startswith("/dev/block/by-name/"):
                    target_size = self.device_partition_sizes.get(name.casefold())
                    if target_size and size > target_size:
                        errors.append(
                            f"{name}: source is {self._format_size(size)}, but "
                            f"phone partition is only "
                            f"{self._format_size(target_size)}"
                        )
                    elif not target_size:
                        warnings.append(
                            f"{name}: phone partition size could not be verified. "
                            "Run Check Device first."
                        )
                elif self.lun_checkbox.isChecked():
                    lun = int(name.removeprefix("LUN"))
                    target_size = self.device_lun_sizes.get(lun)
                    if target_size and path.stat().st_size > target_size:
                        warnings.append(
                            f"{name}: image is {self._format_size(path.stat().st_size)}, "
                            f"but phone LUN is {self._format_size(target_size)}."
                        )
        if errors:
            QMessageBox.warning(
                self, "Write Validation Failed", "\n".join(errors[:20])
            )
            return

        if self.lun_checkbox.isChecked():
            entered_serial, accepted = QInputDialog.getText(
                self,
                "Confirm Whole-LUN Write",
                "This overwrites complete physical storage LUNs.\n"
                f"Type the connected device serial exactly to continue:\n"
                f"{self.device_serial}",
            )
            if not accepted or entered_serial.strip() != self.device_serial:
                QMessageBox.information(
                    self, "Whole-LUN Write Cancelled", "Serial confirmation did not match."
                )
                return

        for lun, source_size in sorted(self.source_userdata_sizes.items()):
            if not any(
                name in {f"LUN{lun}", "userdata"}
                for _r, name, _i, _t, _s, _o in jobs
            ):
                continue
            target_size = self.device_userdata_sizes.get(lun)
            if target_size and source_size > target_size:
                warnings.append(
                    f"LUN{lun} userdata: source is "
                    f"{self._format_size(source_size)}, but phone is "
                    f"{self._format_size(target_size)}."
                )
            elif not target_size:
                warnings.append(
                    f"LUN{lun} userdata capacity could not be compared. "
                    "Run Check Device first."
                )

        warning_text = ""
        if self.lun_checkbox.isChecked():
            warnings.insert(
                0,
                "Whole-LUN mode is enabled. Individual partition checkboxes "
                "do not remove data from a raw LUN image.",
            )
        if warnings:
            warning_text = (
                "\n\nCAPACITY WARNING:\n"
                + "\n".join(warnings)
                + "\n\nWriting a larger layout to a smaller phone may fail "
                "or make the device unbootable."
            )

        answer = QMessageBox.warning(
            self,
            "Confirm Write",
            f"Model: {self.device_model}\n"
            f"SN: {self.device_serial}\n"
            f"Targets: {len(jobs)}\n\n"
            "The selected device data will be overwritten."
            f"{warning_text}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.stop_requested = False
        self.write_queue = jobs
        self.write_transport = "adb"
        self._start_next_write()

    def _write_selected_fastboot(self) -> None:
        if (
            not self.device_model
            or self.device_model.casefold() == "unknown"
            or not self.device_serial
            or self.device_serial.casefold() == "unknown"
        ):
            QMessageBox.warning(
                self,
                "Fastboot Device Not Verified",
                "Run Check Device successfully before selecting Fastboot Write.",
            )
            return
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Fastboot Write", "Select a target first.")
            return
        jobs = []
        errors = []
        selected = set(rows)
        consumed: set[int] = set()
        for row in rows:
            if row in consumed:
                continue
            display_name = self.table.item(row, 3).text().strip()
            package = self.table.item(row, 1).text().strip()
            part_match = re.fullmatch(r"(.+) \(part (\d+)/(\d+)\)", display_name)
            grouped_rows = [row]
            name = display_name
            if part_match:
                name = part_match.group(1)
                total_parts = int(part_match.group(3))
                grouped_rows = [
                    candidate for candidate in rows
                    if self.table.item(candidate, 1).text().strip() == package
                    and re.fullmatch(
                        rf"{re.escape(name)} \(part \d+/{total_parts}\)",
                        self.table.item(candidate, 3).text().strip(),
                    )
                ]
                if len(grouped_rows) != total_parts:
                    errors.append(
                        f"{name}: select all {total_parts} UPDATE.APP parts"
                    )
                    consumed.update(grouped_rows)
                    continue
                grouped_rows.sort(
                    key=lambda candidate: int(
                        re.search(
                            r"\(part (\d+)/",
                            self.table.item(candidate, 3).text(),
                        ).group(1)
                    )
                )
            consumed.update(grouped_rows)

            virtual_specs = []
            normal_image = ""
            size = 0
            source_identity_parts = []
            for grouped_row in grouped_rows:
                image = self.table.item(grouped_row, 8).text().strip()
                row_size = int(
                    self.table.item(grouped_row, 6).data(Qt.UserRole) or 0
                )
                size += row_size
                if image.startswith("updateapp:"):
                    try:
                        embedded_specs = json.loads(
                            image.removeprefix("updateapp:")
                        )
                    except json.JSONDecodeError:
                        embedded_specs = []
                    if not embedded_specs:
                        errors.append(f"{name}: invalid UPDATE.APP payload list")
                        continue
                    virtual_specs.extend(embedded_specs)
                    source_identity_parts.extend(
                        str(spec["archive"]).casefold()
                        for spec in embedded_specs
                    )
                else:
                    virtual = self._parse_update_app_virtual_path(image)
                    if virtual:
                        virtual["size"] = row_size
                        virtual_specs.append(virtual)
                        source_identity_parts.append(
                            str(virtual["archive"]).casefold()
                        )
                    else:
                        normal_image = image
                        source_identity_parts.append(str(Path(image)).casefold())
            if virtual_specs and normal_image:
                errors.append(f"{name}: mixed UPDATE.APP and normal image sources")
                continue
            if virtual_specs:
                missing = [
                    spec["archive"] for spec in virtual_specs
                    if not Path(spec["archive"]).is_file()
                ]
                if missing:
                    errors.append(f"{name}: firmware ZIP file not found")
                    continue
                image = "updateapp:" + json.dumps(virtual_specs, separators=(",", ":"))
            else:
                image = normal_image
                path = Path(image)
                if not path.is_file():
                    errors.append(f"{name}: file not found")
                    continue
                size = path.stat().st_size
            source_identity = " ".join(source_identity_parts)
            if self.device_model and self.device_model.casefold() not in source_identity:
                errors.append(
                    f"{name}: backup path does not contain device model "
                    f"{self.device_model}"
                )
                continue
            target = "ptable" if name.casefold() == "hisiufs_gpt" else name
            if len(virtual_specs) > 1:
                for spec_index, spec in enumerate(virtual_specs):
                    spec_image = "updateapp:" + json.dumps(
                        [spec], separators=(",", ":")
                    )
                    spec_row = row if spec_index == len(virtual_specs) - 1 else -1
                    jobs.append(
                        (
                            spec_row, name, spec_image, target,
                            int(spec["size"]), 0,
                        )
                    )
            else:
                jobs.append((row, name, image, target, size, 0))
        if errors:
            QMessageBox.warning(self, "Fastboot Validation Failed", "\n".join(errors))
            return
        answer = QMessageBox.warning(
            self,
            "Confirm Direct Fastboot Write",
            f"Model: {self.device_model or 'not checked'}\n"
            f"SN: {self.device_serial or 'not checked'}\n"
            f"Targets: {len(jobs)}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.write_transport = "fastboot"
        self.stop_requested = False
        self.huawei_sparse_next_index.clear()
        self.dload_write_active = any(
            image.startswith("updateapp:")
            for _row, _name, image, _target, _size, _offset in jobs
        )
        self.write_queue = jobs
        self._start_next_write()

    @staticmethod
    def _parse_update_app_virtual_path(value: str) -> dict | None:
        match = re.fullmatch(r"(.+)::([^@]+)@(\d+)", value)
        if not match:
            return None
        return {
            "archive": match.group(1),
            "member": match.group(2),
            "offset": int(match.group(3)),
        }

    def _start_next_write(self) -> None:
        if not self.write_queue:
            self.set_busy(False)
            if self.lun_checkbox.isChecked():
                self.lun_checkbox.setChecked(False)
            self.log("All selected targets written successfully.")
            self._cleanup_update_app_caches()
            self.current_write = None
            self._update_action_buttons()
            self.post_flash_transport = self.write_transport
            self.dload_write_active = False
            self._start_misc_then_reboot()
            return
        self.current_write = self.write_queue.pop(0)
        row, name, image, target, size, source_offset = self.current_write
        self._follow_active_partition(row, name)
        raw_lun_source = any(
            Path(image) == Path(raw_path)
            for raw_path in self.raw_lun_images.values()
        )
        needs_extraction = (
            self.write_transport == "fastboot"
            and
            not self.lun_checkbox.isChecked()
            and raw_lun_source
            and name not in {"LUN0", "LUN1"}
        )
        if image.startswith("updateapp:"):
            self._start_update_app_extraction()
        elif needs_extraction:
            self._start_partition_extraction()
        else:
            self._launch_current_write(image, source_offset)

    def _start_update_app_extraction(self) -> None:
        if not self.current_write:
            return
        _row, name, image, _target, size, _source_offset = self.current_write
        try:
            specs = json.loads(image.removeprefix("updateapp:"))
            first_archive = Path(specs[0]["archive"])
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
            self.write_queue.clear()
            QMessageBox.warning(self, "Extraction Failed", str(error))
            return
        cache_path = first_archive.parent / (
            f".hwrec_{os.getpid()}_{first_archive.stem}_UPDATE.APP.cache"
        )
        cache_required = 0
        if not cache_path.is_file():
            try:
                with zipfile.ZipFile(first_archive) as archive:
                    cache_required = archive.getinfo(str(specs[0]["member"])).file_size
            except (OSError, KeyError, zipfile.BadZipFile) as error:
                self.write_queue.clear()
                QMessageBox.warning(self, "Extraction Failed", str(error))
                return
        required_space = cache_required + size + 64 * 1024 * 1024
        if shutil.disk_usage(first_archive.parent).free < required_space:
            self.set_busy(False)
            self.write_queue.clear()
            QMessageBox.warning(
                self, "Extraction Failed",
                f"Not enough free space beside {first_archive.name}. "
                f"Required: {self._format_size(required_space)}.",
            )
            return
        self.update_app_cache_files.add(str(cache_path))
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
        temp_path = first_archive.parent / (
            f".hwrec_{os.getpid()}_{time.time_ns()}_{safe_name}.img"
        )
        self.current_temp_image = str(temp_path)
        self.current_write_phase = "extract"
        self.write_started_at = time.monotonic()
        self.log(
            f"Extracting: {name} from UPDATE.APP "
            f"({self._format_size(size)})........."
        )
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments(
            [
                str(UPDATE_APP_SCANNER),
                "--extract-json", json.dumps(specs, separators=(",", ":")),
                "--output", str(temp_path),
                "--cache", str(cache_path),
            ]
        )
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_extract_output)
        self.process.finished.connect(self._extract_finished)
        self.process.start()
        self.stop_button.setEnabled(False)
        self._update_action_buttons()

    def _start_misc_then_reboot(self) -> None:
        zip_path = BASE_DIR / "misc.zip"
        if not zip_path.is_file():
            QMessageBox.warning(self, "Missing misc.zip", f"File not found:\n{zip_path}")
            return
        try:
            with zipfile.ZipFile(zip_path) as archive:
                members = [
                    info for info in archive.infolist()
                    if not info.is_dir()
                    and Path(info.filename).name.casefold() in {"misc.img", "misc.bin"}
                ]
                if len(members) != 1 or members[0].file_size <= 0:
                    raise ValueError("misc.zip must contain exactly one misc.img or misc.bin.")
                member = members[0]
                descriptor, temp_name = tempfile.mkstemp(
                    prefix="kingunlock_misc_", suffix=".img"
                )
                try:
                    with os.fdopen(descriptor, "wb") as destination:
                        with archive.open(member, "r") as source:
                            shutil.copyfileobj(source, destination, 1024 * 1024)
                except Exception:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    Path(temp_name).unlink(missing_ok=True)
                    raise
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
            QMessageBox.warning(self, "Invalid misc.zip", str(error))
            return
        self.current_temp_image = temp_name
        self.log(f"Flashing: misc from {zip_path.name}.........")
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        if self.post_flash_transport == "fastboot":
            self.process.setArguments(
                [
                    str(DIRECT_FASTBOOT),
                    "flash",
                    "misc",
                    temp_name,
                    "--expected-model",
                    self.device_model,
                    "--expected-platform",
                    self.device_cpu,
                    "--confirm",
                    "FLASH-PARTITION",
                ]
            )
        else:
            self.process.setArguments(
                [
                    str(ADB_STREAM_WRITER),
                    "push",
                    temp_name,
                    "/dev/block/by-name/misc",
                    str(member.file_size),
                ]
            )
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._misc_flash_finished)
        self.process.start()

    def _misc_flash_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self._read_output()
        self._cleanup_temp_image()
        success_marker = (
            "FLASH_OK" if self.post_flash_transport == "fastboot"
            else "WRITE_COMPLETE:"
        )
        if exit_code == 0 and success_marker in self.output:
            self.replace_last_log("Flashing: misc from misc.zip.........done")
            self._ask_reboot_after_misc()
            return
        self.set_busy(False)
        self.replace_last_log("Flashing: misc from misc.zip.........failed")
        QMessageBox.warning(
            self, "misc Flash Failed",
            self.output.strip() or "The misc partition was not flashed; reboot cancelled.",
        )
        self._update_action_buttons()

    def _ask_reboot_after_misc(self) -> None:
        self.set_busy(False)
        answer = QMessageBox.question(
            self,
            "Flash Complete",
            "All selected partitions and misc.zip were flashed successfully.\n\n"
            "Reboot phone now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            self.log("Reboot skipped by user.")
            self._update_action_buttons()
            return
        if self.post_flash_transport == "fastboot":
            self._start_fastboot_reboot()
        else:
            self._start_adb_reboot()

    def _start_adb_reboot(self) -> None:
        self.log("Sending Recovery ADB reboot command.........")
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments([str(MAIN_BACKEND), "shell", "reboot"])
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._adb_reboot_finished)
        self.process.start()

    def _adb_reboot_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self._read_output()
        self.set_busy(False)
        if exit_code == 0 or not self.output.strip():
            self.replace_last_log("Recovery ADB reboot command.........done")
        else:
            self.replace_last_log("Recovery ADB reboot command.........sent")
        self._update_action_buttons()

    def _start_fastboot_reboot(self) -> None:
        self.log("Sending Fastboot reboot/reset command.........")
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments([str(DIRECT_FASTBOOT), "reboot"])
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._fastboot_reboot_finished)
        self.process.start()

    def _fastboot_reboot_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self._read_output()
        self.set_busy(False)
        if exit_code == 0 and "REBOOT_OK" in self.output:
            self.replace_last_log("Fastboot reboot/reset command.........done")
        else:
            self.replace_last_log("Fastboot reboot/reset command.........failed")
            QMessageBox.warning(
                self, "Reboot Failed",
                self.output.strip() or "The phone did not accept the reboot command.",
            )
        self._update_action_buttons()

    def _start_partition_extraction(self) -> None:
        if not self.current_write:
            return
        _row, name, image, _target, size, source_offset = self.current_write
        if shutil.disk_usage(Path(image).parent).free < size + 64 * 1024 * 1024:
            self.set_busy(False)
            self.write_queue.clear()
            QMessageBox.warning(
                self,
                "Extraction Failed",
                f"Not enough free space beside {Path(image).name} to extract "
                f"{name} ({self._format_size(size)}).",
            )
            return
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
        temp_path = (
            Path(image).parent
            / f".kingunlock_{os.getpid()}_{time.time_ns()}_{safe_name}.bin"
        )
        self.current_temp_image = str(temp_path)
        self.current_write_phase = "extract"
        self.write_started_at = time.monotonic()
        self.log(
            f"Extracting: {name} from {Path(image).name} "
            f"({self._format_size(size)})........."
        )
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments(
            [
                str(LUN_EXTRACTOR),
                image,
                str(temp_path),
                str(source_offset),
                str(size),
            ]
        )
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_extract_output)
        self.process.finished.connect(self._extract_finished)
        self.process.start()
        self.stop_button.setEnabled(False)
        self._update_action_buttons()

    def _read_extract_output(self) -> None:
        if not self.process:
            return
        self.output += bytes(self.process.readAllStandardOutput()).decode(
            "utf-8", "replace"
        )
        cache_matches = re.findall(r"CACHE_PROGRESS:(\d+):(\d+)", self.output)
        if cache_matches:
            cached, cache_total = map(int, cache_matches[-1])
            elapsed = max(time.monotonic() - self.write_started_at, 0.001)
            percent = min(100, int(cached * 100 / cache_total))
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
            self.progress.setFormat(
                f"Prepare UPDATE.APP {percent}% | "
                f"{self._format_size(int(cached / elapsed))}/s"
            )
            self.speed_label.setText(
                f"Extract: {self._format_size(int(cached / elapsed))}/s "
                f"({percent}%)"
            )
        matches = re.findall(r"EXTRACT_PROGRESS:(\d+)", self.output)
        if not matches or not self.current_write:
            return
        extracted = int(matches[-1])
        total = self.current_write[4]
        elapsed = max(time.monotonic() - self.write_started_at, 0.001)
        percent = min(100, int(extracted * 100 / total))
        self.progress.setRange(0, 100)
        self.progress.setValue(percent)
        self.speed_label.setText(
            f"Extract: {self._format_size(int(extracted / elapsed))}/s "
            f"({percent}%)"
        )

    def _extract_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self._read_extract_output()
        if exit_code != 0 or not Path(self.current_temp_image).is_file():
            self.set_busy(False)
            self.write_queue.clear()
            error = self.output.strip() or "Partition extraction failed."
            self._cleanup_temp_image()
            self._cleanup_update_app_caches()
            self.replace_last_log("Partition extraction.........failed")
            QMessageBox.warning(self, "Extraction Failed", error)
            self._update_action_buttons()
            return
        if not self.current_write:
            self._cleanup_temp_image()
            return
        name = self.current_write[1]
        self.replace_last_log(f"Extracting: {name}.........done")
        self._launch_current_write(self.current_temp_image, 0)

    def _launch_current_write(self, image: str, source_offset: int) -> None:
        if not self.current_write:
            return
        _row, name, _original_image, target, size, _original_offset = (
            self.current_write
        )
        self.current_write_phase = "write"
        self.write_started_at = time.monotonic()
        self.write_last_progress_at = self.write_started_at
        self.write_last_progress_bytes = 0
        self.write_speed_sample_at = self.write_started_at
        self.write_speed_sample_bytes = 0
        self.write_live_speed = 0
        action = "Flash" if self.write_transport == "fastboot" else "Write"
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat(f"{action} 0% | waiting for device...")
        self.log(
            f"Writing: {name} from {Path(image).name} "
            f"({self._format_size(size)})........."
        )
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        if self.write_transport == "fastboot":
            sparse_sequence_key = self._huawei_sparse_sequence_key(
                target, _original_image
            )
            self.process.setProgram(sys.executable)
            self.process.setArguments(
                [
                    str(DIRECT_FASTBOOT),
                    "flash",
                    target,
                    image,
                    "--expected-model",
                    self.device_model,
                    "--confirm",
                    "FLASH-PARTITION",
                    "--huawei-start-index",
                    str(self.huawei_sparse_next_index.get(sparse_sequence_key, 0)),
                ]
            )
        else:
            self.process.setProgram(sys.executable)
            arguments = [
                str(ADB_STREAM_WRITER),
                "push",
                image,
                target,
                str(size),
            ]
            if source_offset:
                arguments.extend(["--source-offset", str(source_offset)])
                if name.startswith("LUN"):
                    arguments.extend(["--target-offset", str(source_offset)])
            self.process.setArguments(arguments)
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_write_output)
        self.process.finished.connect(self._write_finished)
        self.process.start()
        self.write_watchdog.start()
        self.stop_button.setEnabled(True)
        self._update_action_buttons()

    def _read_write_output(self) -> None:
        if not self.process:
            return
        self.output += bytes(self.process.readAllStandardOutput()).decode(
            "utf-8", "replace"
        )
        matches = re.findall(r"WRITE_PROGRESS:(\d+)", self.output)
        if matches:
            written = int(matches[-1])
        else:
            adb_percentages = re.findall(r"(\d{1,3})%", self.output)
            if not adb_percentages or not self.current_write:
                return
            written = int(self.current_write[4] * int(adb_percentages[-1]) / 100)
        if not self.current_write:
            return
        if written > self.write_last_progress_bytes:
            self.write_last_progress_bytes = written
            self.write_last_progress_at = time.monotonic()
        total = self.current_write[4]
        now = time.monotonic()
        sample_elapsed = max(now - self.write_speed_sample_at, 0.001)
        sample_bytes = max(0, written - self.write_speed_sample_bytes)
        if sample_bytes:
            self.write_live_speed = int(sample_bytes / sample_elapsed)
        live_speed = self.write_live_speed
        if written > self.write_speed_sample_bytes:
            self.write_speed_sample_at = now
            self.write_speed_sample_bytes = written
        complete_marker = (
            "FLASH_OK" in self.output or "WRITE_COMPLETE:" in self.output
        )
        percent = min(100, int(written * 100 / total))
        if written >= total and not complete_marker:
            # All bytes have reached dd/fastboot, but the device has not yet
            # confirmed its final flash/fsync result.
            percent = 99
        action = "Flash" if self.write_transport == "fastboot" else "Write"
        if percent == 99 and written >= total and not complete_marker:
            progress_text = f"{action} 99% | syncing device storage..."
        else:
            progress_text = (
                f"{action} {percent}% | {self._format_size(live_speed)}/s"
            )
        self.progress.setRange(0, 100)
        self.progress.setValue(percent)
        self.progress.setFormat(progress_text)
        self.speed_label.setText(f"Speed: {self._format_size(live_speed)}/s")

    def _check_write_stall(self) -> None:
        if (
            not self.current_write
            or self.current_write_phase != "write"
            or not self.process
            or self.process.state() == QProcess.NotRunning
        ):
            self.write_watchdog.stop()
            return
        if time.monotonic() - self.write_last_progress_at < 60:
            return
        self.write_watchdog.stop()
        self.log("Write stopped: no ADB progress for 60 seconds.")
        self.stop_operation()

    def _write_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self.write_watchdog.stop()
        self._read_write_output()
        if self.current_write:
            next_indexes = re.findall(r"HUAWEI_NEXT_INDEX:(\d+)", self.output)
            if next_indexes:
                target = self.current_write[3]
                sequence_key = self._huawei_sparse_sequence_key(
                    target, self.current_write[2]
                )
                self.huawei_sparse_next_index[sequence_key] = int(
                    next_indexes[-1]
                )
        if self.stop_requested:
            self.stop_requested = False
            self.write_queue.clear()
            self.set_busy(False)
            if self.current_write:
                self.replace_last_log(
                    f"Writing: {self.current_write[1]}.........cancelled"
                )
            self._cleanup_temp_image()
            self._cleanup_update_app_caches()
            self.current_write = None
            self.stop_button.setEnabled(False)
            self._update_action_buttons()
            return
        if exit_code != 0:
            self.set_busy(False)
            self.write_queue.clear()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.setFormat("Write FAILED | device did not commit data")
            error_lines = [
                line.strip()
                for line in self.output.splitlines()
                if line.strip()
                and not line.strip().startswith("WRITE_PROGRESS:")
            ]
            error = "\n".join(error_lines[-10:]) or "Write failed."
            if error_lines:
                try:
                    result = json.loads(error_lines[-1])
                    if isinstance(result, dict) and result.get("error"):
                        error = str(result["error"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if self.current_write:
                failed_name = self.current_write[1]
                self.replace_last_log(
                    f"Writing: {failed_name} from "
                    f"{Path(self.current_write[2]).name}.........failed"
                )
            self.log(f"Write error: {error}")
            self._cleanup_temp_image()
            self._cleanup_update_app_caches()
            QMessageBox.warning(self, "Write Failed", error)
            self._update_action_buttons()
            return
        if self.current_write:
            row, _name, image, _target, _size, _offset = self.current_write
            elapsed = max(time.monotonic() - self.write_started_at, 0.001)
            average_speed = int(_size / elapsed)
            action = "Flash" if self.write_transport == "fastboot" else "Write"
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.progress.setFormat(
                f"{action} complete | {self._format_size(average_speed)}/s avg"
            )
            self.speed_label.setText(
                f"Speed: {self._format_size(average_speed)}/s avg"
            )
            self.replace_last_log(
                f"Writing: {_name} from {Path(image).name}.........done"
            )
            self._cleanup_temp_image()
            if row >= 0:
                check_item = self.table.item(row, 0)
                if check_item:
                    check_item.setCheckState(Qt.Unchecked)
        self.current_write = None
        self._start_next_write()

    def _cleanup_temp_image(self) -> None:
        if not self.current_temp_image:
            return
        try:
            Path(self.current_temp_image).unlink()
        except OSError:
            pass
        try:
            Path(self.current_temp_image + ".part").unlink()
        except OSError:
            pass
        self.current_temp_image = ""

    def _cleanup_update_app_caches(self) -> None:
        for filename in self.update_app_cache_files:
            Path(filename).unlink(missing_ok=True)
            Path(filename + ".part").unlink(missing_ok=True)
        self.update_app_cache_files.clear()

    @staticmethod
    def _huawei_sparse_sequence_key(target: str, image: str) -> str:
        if image.startswith("updateapp:"):
            try:
                specs = json.loads(image.removeprefix("updateapp:"))
                first = specs[0]
                return (
                    f"{target.casefold()}|"
                    f"{str(first['archive']).casefold()}|"
                    f"{str(first['member']).casefold()}"
                )
            except (json.JSONDecodeError, IndexError, KeyError, TypeError):
                pass
        return f"{target.casefold()}|{str(image).casefold()}"

    def _write_rawprogram_xml(self) -> None:
        by_lun: dict[int, list[int]] = {}
        xml_rows = self.completed_reads or self.backup_xml_rows
        for row in xml_rows:
            name_item = self.table.item(row, 3)
            if name_item and name_item.text().strip().casefold() == "userdata":
                continue
            disk = self.table.item(row, 1).text()
            match = re.fullmatch(r"LUN(\d+)", disk)
            if not match:
                continue
            by_lun.setdefault(int(match.group(1)), []).append(row)

        for lun, rows in by_lun.items():
            root = ET.Element("data")
            for row in sorted(
                rows, key=lambda value: int(self.table.item(value, 2).text())
            ):
                number = int(self.table.item(row, 2).text())
                name = self.table.item(row, 3).text()
                if name.strip().casefold() == "userdata":
                    continue
                start_lba = int(self.table.item(row, 4).text())
                size = int(self.table.item(row, 6).data(Qt.UserRole) or 0)
                filename = Path(self.table.item(row, 8).text()).name
                ET.SubElement(
                    root,
                    "program",
                    {
                        "SECTOR_SIZE_IN_BYTES": "4096",
                        "file_sector_offset": "0",
                        "filename": filename,
                        "label": "PrimaryGPT" if name == "GPT_Header" else name,
                        "num_partition_sectors": str(size // 4096),
                        "physical_partition_number": str(lun),
                        "size_in_KB": f"{size / 1024:.1f}",
                        "sparse": "false",
                        "start_byte_hex": f"0x{start_lba * 4096:X}",
                        "start_sector": str(start_lba),
                    },
                )
            tree = ET.ElementTree(root)
            ET.indent(tree, space="  ")
            xml_path = Path(self.backup_folder) / f"rawprogram{lun}.xml"
            tree.write(xml_path, encoding="utf-8", xml_declaration=True)
            self.log(f"Created: {xml_path.name}")

    @staticmethod
    def _format_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
            value /= 1024
        return f"{size} B"

    @staticmethod
    def _range_has_payload(
        path: Path, offset: int, size: int, chunk_size: int = 256 * 1024
    ) -> bool:
        """Sample a range without reading huge images in full."""
        if offset < 0 or size <= 0 or path.stat().st_size < offset + size:
            return False
        if size <= 8 * 1024 * 1024:
            positions = range(offset, offset + size, chunk_size)
        else:
            last = max(offset, offset + size - chunk_size)
            positions = sorted({
                offset + ((last - offset) * index // 8)
                for index in range(9)
            })
        meaningful = 0
        sampled = 0
        with path.open("rb") as source:
            for position in positions:
                source.seek(position)
                data = source.read(min(chunk_size, offset + size - position))
                if not data:
                    continue
                sampled += len(data)
                # bytes.count runs in native code and avoids freezing the GUI
                # while validating many partition ranges from one raw LUN.
                meaningful += len(data) - data.count(0x00) - data.count(0xFF)
        minimum = max(256, min(4096, sampled // 4096))
        return meaningful >= minimum

    def _update_header_checkbox(self) -> None:
        header = self.table.horizontalHeaderItem(0)
        if not header:
            return
        eligible_rows = [
            row
            for row in range(self.table.rowCount())
            if not self._is_select_all_excluded(row)
        ]
        total = len(eligible_rows)
        checked = sum(
            1
            for row in eligible_rows
            if self.table.item(row, 0)
            and self.table.item(row, 0).checkState() == Qt.Checked
        )
        header.setText("☑" if total and checked == total else "◩" if checked else "☐")

    def header_clicked(self, column: int) -> None:
        if column != 0 or not self.table.rowCount():
            return
        eligible_rows = [
            row
            for row in range(self.table.rowCount())
            if not self._is_select_all_excluded(row)
        ]
        checked = all(
            self.table.item(row, 0)
            and self.table.item(row, 0).checkState() == Qt.Checked
            for row in eligible_rows
        )
        state = Qt.Unchecked if checked else Qt.Checked
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(
                    Qt.Unchecked if self._is_select_all_excluded(row) else state
                )

    def _uses_kirin_security_profile(self) -> bool:
        cpu = re.sub(r"[^a-z0-9]+", "", self.device_cpu.casefold())
        return cpu in {
            "kirin820",
            "kirin980",
            "kirin985",
            "kirin990",
            "kirin990e",
            "kirin9905g",
            "kirin9000",
            "kirin9000e",
        }

    def _is_protected_row(self, row: int) -> bool:
        name_item = self.table.item(row, 3)
        disk_item = self.table.item(row, 1)
        if not name_item:
            return False
        name = name_item.text().casefold()
        if (
            name in RECOVERY_ADB_BLOCKED_TARGETS
            and name not in RECOVERY_ADB_WRITE_ALLOWED_TARGETS
        ):
            return True
        if not self._uses_kirin_security_profile():
            return False
        if name in {
            "modem_secure",
            "nvme",
            "certification",
            "oeminfo",
            "secure_storage",
            "modemnvm_factory",
            "modemnvm_backup",
            "modemnvm_img",
        }:
            return True
        return (
            name == "gpt_header"
            and disk_item is not None
            and disk_item.text() in {"LUN2", "LUN3"}
        )

    def _is_select_all_excluded(self, row: int) -> bool:
        name_item = self.table.item(row, 3)
        name = name_item.text().casefold() if name_item else ""
        return name == "userdata"

    def _toggle_lun(self, lun: int, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        disk = f"LUN{lun}"
        for row in range(self.table.rowCount()):
            disk_item = self.table.item(row, 1)
            check_item = self.table.item(row, 0)
            if disk_item and check_item and disk_item.text() == disk:
                check_item.setCheckState(state)

    def _toggle_all_luns(self, checked: bool) -> None:
        for row in range(self.table.rowCount()):
            check_item = self.table.item(row, 0)
            if check_item:
                name_item = self.table.item(row, 3)
                is_userdata = name_item and name_item.text() == "userdata"
                state = (
                    Qt.Checked
                    if checked and not is_userdata
                    else Qt.Unchecked
                )
                check_item.setCheckState(state)

    def _toggle_super(self, checked: bool) -> None:
        if checked:
            self.lun_checkbox.blockSignals(True)
            self.lun_checkbox.setChecked(False)
            self.lun_checkbox.blockSignals(False)
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 3)
            check_item = self.table.item(row, 0)
            if not name_item or not check_item:
                continue
            is_super = name_item.text().strip().casefold() == "super"
            if checked:
                check_item.setCheckState(
                    Qt.Checked if is_super else Qt.Unchecked
                )
            elif is_super:
                check_item.setCheckState(Qt.Unchecked)
        self._update_action_buttons()

    def _fastboot_mode_changed(self, checked: bool) -> None:
        if checked:
            self.lun_checkbox.setChecked(False)
            self.mode_label.setText("Mode: Direct Fastboot USB")
            self.log(
                "Please connect phone in Fastboot mode "
                "(Phone Off, then hold Volume Down and connect USB)."
            )
        else:
            self.write_transport = "adb"
            self.mode_label.setText("Mode: Recovery")
        self._update_action_buttons()

    def choose_folder(self) -> None:
        if not self.table.rowCount():
            QMessageBox.information(
                self, "Select File", "Check the device first."
            )
            return
        files, _filter = QFileDialog.getOpenFileNames(
            self,
            "Select Partition Image",
            self.backup_folder or self.backup_parent,
            "Binary images (*.bin *.img);;All files (*.*)",
        )
        if not files:
            return

        expected_rows: dict[str, int] = {}
        for row in range(self.table.rowCount()):
            disk = self.table.item(row, 1).text()
            number = self.table.item(row, 2).text()
            name = self.table.item(row, 3).text()
            expected_rows[f"{disk}_{number}_{name}.bin".casefold()] = row
            expected_rows[f"{disk}_{number}_{name}.img".casefold()] = row

        assigned = 0
        rejected = []
        for filename in files:
            row = expected_rows.get(Path(filename).name.casefold())
            if row is not None:
                for old_row in range(self.table.rowCount()):
                    path_item = self.table.item(old_row, 8)
                    if (
                        old_row != row
                        and path_item
                        and Path(path_item.text()) == Path(filename)
                    ):
                        path_item.setText("")
                        self.table.item(old_row, 0).setCheckState(Qt.Unchecked)
                self.table.item(row, 8).setText(filename)
                expected_size = int(
                    self.table.item(row, 6).data(Qt.UserRole) or 0
                )
                actual_size = Path(filename).stat().st_size
                compatible = expected_size > 0 and actual_size == expected_size
                self.table.item(row, 0).setCheckState(
                    Qt.Checked if compatible else Qt.Unchecked
                )
                remarks_item = self.table.item(row, 7)
                if remarks_item:
                    if compatible:
                        remarks_item.setText("Image matches current phone")
                    else:
                        remarks_item.setText(
                            "Image size does not match current partition"
                        )
                if not compatible:
                    rejected.append(
                        f"{Path(filename).name}: image "
                        f"{self._format_size(actual_size)}, phone "
                        f"{self._format_size(expected_size)}"
                    )
                assigned += 1
        self.log(f"Selected files: {assigned}")
        if assigned != len(files):
            QMessageBox.information(
                self,
                "File Matching",
                f"Matched {assigned} of {len(files)} files by partition name.",
            )
        if rejected:
            QMessageBox.warning(
                self,
                "Image Size Does Not Match Phone",
                "These images were assigned but left unticked:\n\n"
                + "\n".join(rejected[:20]),
            )
        self._update_action_buttons()

    def select_image_folder(self) -> None:
        self.image_folder_layout_mismatches = {}
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Partition Image Folder",
            self.backup_folder or self.backup_parent,
        )
        if not folder:
            return

        self.image_folder = folder
        dload_archives = []
        for archive_path in Path(folder).rglob("*.zip"):
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    if any(
                        not info.is_dir()
                        and Path(info.filename).name.upper() == "UPDATE.APP"
                        for info in archive.infolist()
                    ):
                        dload_archives.append(archive_path)
            except (OSError, zipfile.BadZipFile):
                continue
        if dload_archives:
            self._start_dload_folder_scan(folder)
            return

        files = {
            path.name.casefold(): str(path)
            for path in Path(folder).iterdir()
            if path.is_file() and path.suffix.casefold() in {".bin", ".img"}
        }
        layout_mismatches = self._compare_backup_layout(Path(folder))
        self.image_folder_layout_mismatches = layout_mismatches
        raw_luns = {
            int(match.group(1)): path
            for filename, path in files.items()
            if (match := re.fullmatch(r"lun_?(\d+)\.(?:bin|img)", filename))
        }
        if self.super_checkbox.isChecked():
            super_image = (
                files.get("super.bin") or files.get("super.img")
            )
            if not super_image:
                QMessageBox.information(
                    self,
                    "Super Image Not Found",
                    "Super mode is enabled, but this folder does not contain "
                    "super.bin or super.img.",
                )
                return
            if self._has_super_companion_set(files):
                self.log(
                    "Super mode: complete super/recovery/vbmeta set detected; "
                    "matching all seven images to Recovery ADB partitions."
                )
                if self.fastboot_checkbox.isChecked():
                    self.fastboot_checkbox.setChecked(False)
            else:
                self._load_super_image_only(folder, super_image)
                return
        if self.fastboot_checkbox.isChecked():
            self._load_fastboot_folder(folder, files)
            return
        has_fastboot_images = any(
            path.is_file() and self._is_fastboot_image_file(path)
            for path in Path(folder).iterdir()
        )
        if not raw_luns and not self.table.rowCount() and has_fastboot_images:
            self.fastboot_checkbox.setChecked(True)
            self.log("Fastboot firmware folder detected automatically.")
            self._load_fastboot_folder(folder, files)
            return
        self.raw_lun_images = raw_luns
        if raw_luns:
            parsed_count, parse_errors = self._load_partitions_from_luns(raw_luns)
            if parsed_count:
                self.log(f"Parsed GPT partitions from folder: {parsed_count}")
            for error in parse_errors:
                self.log(error)

        self.lun_checkbox.blockSignals(True)
        # Parsed folders default to partition mode, where unchecked rows are
        # skipped and selected rows stream only their GPT byte ranges.
        self.lun_checkbox.setChecked(False)
        self.lun_checkbox.blockSignals(False)

        assigned = 0
        for row in range(self.table.rowCount()):
            self.table.item(row, 8).setText("")
            disk = self.table.item(row, 1).text()
            lun = int(disk.removeprefix("LUN"))
            number = self.table.item(row, 2).text()
            name = self.table.item(row, 3).text()

            if raw_luns:
                image = raw_luns.get(lun)
                size = int(self.table.item(row, 6).data(Qt.UserRole) or 0)
                offset = int(self.table.item(row, 4).text()) * 4096
                complete = bool(
                    image and Path(image).stat().st_size >= offset + size
                )
                payload_ok = bool(
                    complete
                    and self._range_has_payload(Path(image), offset, size)
                )
                excluded = self._is_select_all_excluded(row)
                layout_error = layout_mismatches.get((lun, name.casefold()))
                self.table.item(row, 0).setCheckState(
                    Qt.Checked
                    if complete and not excluded and not layout_error
                    else Qt.Unchecked
                )
                if image:
                    self.table.item(row, 8).setText(image)
                    if excluded:
                        self.table.item(row, 7).setText(
                            "Security file"
                        )
                    elif layout_error:
                        self.table.item(row, 7).setText(layout_error)
                    elif not complete:
                        self.table.item(row, 7).setText(
                            "Not fully present in image"
                        )
                continue

            base = f"{disk}_{number}_{name}"
            image = files.get(f"{base}.bin".casefold())
            if image is None:
                image = files.get(f"{base}.img".casefold())
            # Also accept conventional extracted firmware names such as
            # system.bin, hw_product.bin, oeminfo.bin, or vbmeta_system.img.
            if image is None:
                image = files.get(f"{name}.bin".casefold())
            if image is None:
                image = files.get(f"{name}.img".casefold())
            if image:
                self.table.item(row, 8).setText(image)
                image_path = Path(image)
                size = int(self.table.item(row, 6).data(Qt.UserRole) or 0)
                actual_size = image_path.stat().st_size
                complete = actual_size >= size
                exact_size = actual_size == size
                compact_vbmeta = self._allows_compact_vbmeta_image(
                    name, actual_size, size
                )
                payload_ok = bool(
                    complete and self._range_has_payload(image_path, 0, size)
                )
                excluded = self._is_select_all_excluded(row)
                layout_error = layout_mismatches.get((lun, name.casefold()))
                self.table.item(row, 0).setCheckState(
                    Qt.Checked
                    if (exact_size or compact_vbmeta) and not excluded
                    else Qt.Unchecked
                )
                if excluded:
                    self.table.item(row, 7).setText(
                        "Security file"
                    )
                elif compact_vbmeta:
                    self.table.item(row, 7).setText(
                        "Compact VBMeta image - ready to write"
                    )
                    assigned += 1
                elif not complete:
                    self.table.item(row, 7).setText(
                        "Image is smaller than partition"
                    )
                elif not exact_size:
                    self.table.item(row, 7).setText(
                        "Image is larger than partition"
                    )
                elif layout_error:
                    self.table.item(row, 7).setText(
                        "Image matches phone; backup XML layout differs"
                    )
                elif not payload_ok:
                    assigned += 1
                else:
                    assigned += 1
            else:
                self.table.item(row, 0).setCheckState(Qt.Unchecked)

        self.folder_label.setText(folder)
        if layout_mismatches:
            details = "\n".join(list(layout_mismatches.values())[:20])
            self.log(
                f"Backup layout mismatch: {len(layout_mismatches)} partition(s)"
            )
            QMessageBox.warning(
                self,
                "Backup Layout Does Not Match Phone",
                "The backup XML describes a different partition layout. "
                "Wrong-sized images and raw-LUN ranges were left unticked; "
                "individual images that exactly match the phone remain usable.\n\n"
                f"{details}",
            )
        if raw_luns:
            names = ", ".join(f"LUN{lun}" for lun in sorted(raw_luns))
            self.log(f"Loaded raw LUN images: {names}")
        else:
            self.log(f"Matched partition files: {assigned}")
        if assigned == 0 and not raw_luns:
            QMessageBox.information(
                self,
                "No Matching Files",
                "No images matched partition names in the selected folder.",
            )
        self._update_action_buttons()

    def _compare_backup_layout(self, folder: Path) -> dict[tuple[int, str], str]:
        """Compare saved rawprogram XML entries with the connected phone."""
        current: dict[tuple[int, str], tuple[int, int]] = {}
        for row in range(self.table.rowCount()):
            disk_item = self.table.item(row, 1)
            name_item = self.table.item(row, 3)
            start_item = self.table.item(row, 4)
            size_item = self.table.item(row, 6)
            if not all((disk_item, name_item, start_item, size_item)):
                continue
            match = re.fullmatch(r"LUN(\d+)", disk_item.text())
            if not match or not start_item.text().isdigit():
                continue
            current[(int(match.group(1)), name_item.text().casefold())] = (
                int(start_item.text()), int(size_item.data(Qt.UserRole) or 0)
            )

        mismatches: dict[tuple[int, str], str] = {}
        for xml_path in sorted(folder.glob("rawprogram*.xml")):
            try:
                root = ET.parse(xml_path).getroot()
            except (OSError, ET.ParseError):
                continue
            for program in root.findall(".//program"):
                try:
                    lun = int(program.get("physical_partition_number", "-1"))
                    label = program.get("label", "").strip()
                    name = (
                        "gpt_header"
                        if label.casefold() == "primarygpt"
                        else label.casefold()
                    )
                    saved_start = int(program.get("start_sector", "-1"))
                    sector_size = int(
                        program.get("SECTOR_SIZE_IN_BYTES", "4096")
                    )
                    saved_size = (
                        int(program.get("num_partition_sectors", "-1"))
                        * sector_size
                    )
                except ValueError:
                    continue
                key = (lun, name)
                connected = current.get(key)
                if connected is None:
                    continue
                current_start, current_size = connected
                if (saved_start, saved_size) != connected:
                    mismatches[key] = (
                        f"LUN{lun} {label}: backup start/size "
                        f"{saved_start}/{self._format_size(saved_size)}, phone "
                        f"{current_start}/{self._format_size(current_size)}"
                    )
        return mismatches

    def _load_super_image_only(self, folder: str, image: str) -> None:
        """Show only super.bin/img when the toolbar Super mode is enabled."""
        image_path = Path(image)
        size = image_path.stat().st_size
        self.table.setRowCount(1)
        values = [
            "", "LUN3", "1", "super", "-", "-",
            self._format_size(size), "Super image", str(image_path),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if column == 0:
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
                item.setTextAlignment(Qt.AlignCenter)
            if column == 6:
                item.setData(Qt.UserRole, size)
            self.table.setItem(0, column, item)
        self.raw_lun_images.clear()
        self.lun_checkbox.blockSignals(True)
        self.lun_checkbox.setChecked(False)
        self.lun_checkbox.blockSignals(False)
        self.folder_label.setText(folder)
        self.log(
            f"Super mode: loaded only {image_path.name} "
            f"({self._format_size(size)})"
        )
        self._update_action_buttons()

    @staticmethod
    def _has_super_companion_set(files: dict[str, str]) -> bool:
        """Return whether a folder has the complete seven-image Super set."""
        required = {
            "super.img",
            "recovery_ramdisk.img",
            "vbmeta_cust.img",
            "vbmeta_hw_product.img",
            "vbmeta_odm.img",
            "vbmeta_system.img",
            "vbmeta_vendor.img",
        }
        return required.issubset(files)

    @staticmethod
    def _allows_compact_vbmeta_image(
        name: str, image_size: int, partition_size: int
    ) -> bool:
        """Allow signed Huawei VBMeta payloads smaller than reserved partitions."""
        return (
            name.casefold() in {
                "vbmeta_cust",
                "vbmeta_hw_product",
                "vbmeta_odm",
                "vbmeta_system",
                "vbmeta_vendor",
            }
            and 0 < image_size <= partition_size
        )

    @staticmethod
    def _find_oeminfo_image(folder: Path, disk: str, number: str) -> Path:
        """Find one unambiguous OEMINFO image in a user-selected folder."""
        if not folder.is_dir():
            raise ValueError("The selected OEMINFO folder does not exist.")
        accepted_names = {
            "oeminfo.img",
            "oeminfo.bin",
            f"{disk}_{number}_oeminfo.img".casefold(),
            f"{disk}_{number}_oeminfo.bin".casefold(),
        }
        matches = [
            path for path in folder.iterdir()
            if path.is_file() and path.name.casefold() in accepted_names
        ]
        if not matches:
            raise ValueError(
                "No OEMINFO image was found. Expected oeminfo.img, "
                "oeminfo.bin, or the matching LUN-numbered OEMINFO filename."
            )
        if len(matches) > 1:
            names = ", ".join(sorted(path.name for path in matches))
            raise ValueError(
                f"Multiple OEMINFO images were found ({names}). Keep only the "
                "one intended for this phone in the selected folder."
            )
        return matches[0]

    def _start_dload_folder_scan(self, folder: str) -> None:
        if not UPDATE_APP_SCANNER.is_file():
            QMessageBox.warning(
                self, "Missing Scanner", f"File not found:\n{UPDATE_APP_SCANNER}"
            )
            return
        self.log("Huawei dload package detected; reading UPDATE.APP partitions...")
        self.set_busy(True)
        self.output = ""
        self.process = QProcess(self)
        self.process.setProgram(sys.executable)
        self.process.setArguments([str(UPDATE_APP_SCANNER), folder])
        self.process.setWorkingDirectory(str(BASE_DIR))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._dload_folder_scan_finished)
        self.process.start()

    def _dload_folder_scan_finished(
        self, exit_code: int, _status: QProcess.ExitStatus
    ) -> None:
        self._read_output()
        self.set_busy(False)
        try:
            if exit_code != 0:
                raise RuntimeError(self.output.strip() or "UPDATE.APP scan failed.")
            records = json.loads(self.output)
            if not isinstance(records, list) or not records:
                raise ValueError("No partition payloads were found in UPDATE.APP.")
        except (ValueError, json.JSONDecodeError, RuntimeError) as error:
            self.log("Huawei dload package scan failed.")
            QMessageBox.warning(self, "Dload Scan Failed", str(error))
            self._update_action_buttons()
            return

        self.table.setRowCount(0)
        grouped_records: list[list[dict]] = []
        group_indexes: dict[tuple[str, str], int] = {}
        for record in records:
            key = (str(record["package"]), str(record["name"]))
            if key not in group_indexes:
                group_indexes[key] = len(grouped_records)
                grouped_records.append([])
            grouped_records[group_indexes[key]].append(record)
        for number, group in enumerate(grouped_records, start=1):
            record = group[0]
            package = str(record["package"])
            name = str(record["name"])
            size = sum(int(part["size"]) for part in group)
            if len(group) == 1:
                virtual_path = (
                    f"{record['archive']}::{record['member']}@{record['offset']}"
                )
            else:
                specs = [
                    {
                        "archive": part["archive"],
                        "member": part["member"],
                        "offset": int(part["offset"]),
                        "size": int(part["size"]),
                    }
                    for part in group
                ]
                virtual_path = "updateapp:" + json.dumps(
                    specs, separators=(",", ":")
                )
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                "", package, str(number), name, "-", "-",
                self._format_size(size), "Huawei UPDATE.APP payload", virtual_path,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    item.setCheckState(
                        Qt.Checked if (
                            name.casefold() == "super"
                            and self.super_checkbox.isChecked()
                        ) else Qt.Unchecked
                    )
                    item.setTextAlignment(Qt.AlignCenter)
                if column == 6:
                    item.setData(Qt.UserRole, size)
                self.table.setItem(row, column, item)
        self.raw_lun_images.clear()
        self.folder_label.setText(self.image_folder)
        self.log(
            f"Loaded Huawei UPDATE.APP partitions: {len(grouped_records)} "
            f"({len(records)} payload records)"
        )
        self._update_action_buttons()

    def _load_fastboot_folder(
        self, folder: str, files: dict[str, str]
    ) -> None:
        """Load Fastboot images and infer their real target partitions."""
        root = Path(folder)
        image_paths = sorted(
            (path for path in root.iterdir()
             if path.is_file() and self._is_fastboot_image_file(path)),
            key=lambda path: str(path.relative_to(root)).casefold(),
        )
        manifest_map = self._read_fastboot_partition_map(root, image_paths)
        fallback_rank = {
            partition: number
            for number, partition in enumerate(self.FASTBOOT_PARTITION_ORDER)
        }
        images = []
        for fallback_order, path in enumerate(image_paths):
            mapped = manifest_map.get(path.resolve())
            if mapped:
                target, mapping_source, flash_order = mapped
            else:
                target, mapping_source = self._partition_from_image_name(path.name)
                flash_order = 1_000_000 + fallback_rank.get(
                    target.casefold(), len(fallback_rank) + fallback_order
                )
            if target:
                images.append((target, str(path), mapping_source, flash_order))
        images.sort(key=lambda item: (item[3], item[0].casefold()))
        self.table.setRowCount(0)
        protected = {
            "hisiufs_gpt", "gpt", "ptable", "oeminfo", "nvme",
            "certification", "secure_storage", "modem_secure",
            "modemnvm_factory", "modemnvm_backup", "modemnvm_img",
            "userdata",
        }
        for number, (name, path, mapping_source, _flash_order) in enumerate(images):
            row = self.table.rowCount()
            self.table.insertRow(row)
            size = Path(path).stat().st_size
            is_protected = name.casefold() in protected
            values = [
                "", "FB", str(number + 1), name, "-", "-",
                self._format_size(size),
                ("Critical - auto selected" if is_protected
                 else f"Fastboot image ({mapping_source})"),
                path,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if column == 0:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Checked)
                    item.setTextAlignment(Qt.AlignCenter)
                if column == 6:
                    item.setData(Qt.UserRole, size)
                self.table.setItem(row, column, item)
        self.image_folder = folder
        self.folder_label.setText(folder)
        mapped_count = len(images)
        self.log(
            f"Loaded Fastboot images: {len(images)} "
            f"({mapped_count} partition names mapped successfully)"
        )
        if not images:
            QMessageBox.information(
                self, "No Fastboot Images",
                "No Fastboot .img, .bin, or sparse-chunk files were found."
            )
        self._update_action_buttons()

    @staticmethod
    def _is_fastboot_image_file(path: Path) -> bool:
        name = path.name.casefold()
        return path.suffix.casefold() in {".img", ".bin"} or bool(
            re.search(r"\.img(?:_sparsechunk|\.sparsechunk|\.chunk)[._-]?\d+$", name)
        )

    @staticmethod
    def _partition_from_image_name(filename: str) -> tuple[str, str]:
        name = filename.casefold()
        name = re.sub(r"\.img(?:_sparsechunk|\.sparsechunk|\.chunk)[._-]?\d+$", "", name)
        name = re.sub(r"\.(?:img|bin)$", "", name)
        partition_aliases = {
            "hisiufs_gpt": "ptable",
        }
        if name in partition_aliases:
            return partition_aliases[name], "Huawei filename alias"
        if re.fullmatch(r"super(?:[._-](?:\d+|chunk[._-]?\d+))+", name):
            return "super", "split-super filename"
        if name.startswith("sec_") and len(name) > 4:
            return name[4:], "sec filename"
        if name.endswith("_sec") and len(name) > 4:
            return name[:-4], "sec filename"
        return name, "filename"

    @classmethod
    def _read_fastboot_partition_map(
        cls, folder: Path, images: list[Path]
    ) -> dict[Path, tuple[str, str, int]]:
        by_name: dict[str, list[Path]] = {}
        by_relative: dict[str, Path] = {}
        for image in images:
            by_name.setdefault(image.name.casefold(), []).append(image)
            by_relative[image.relative_to(folder).as_posix().casefold()] = image
        result: dict[Path, tuple[str, str, int]] = {}
        flash_order = 0

        def assign(filename: str, partition: str, source: str) -> None:
            nonlocal flash_order
            filename = filename.strip().strip('"\'').replace("\\", "/")
            partition = partition.strip().strip('"\'')
            if not filename or not partition:
                return
            relative = filename.removeprefix("./").casefold()
            candidates = ([by_relative[relative]] if relative in by_relative else
                          by_name.get(Path(filename).name.casefold(), []))
            if len(candidates) == 1:
                result[candidates[0].resolve()] = (partition, source, flash_order)
                flash_order += 1

        file_keys = ("filename", "file_name", "file", "image", "path")
        part_keys = ("partition", "partition_name", "label", "target", "name")
        for xml_path in folder.glob("*.xml"):
            try:
                xml_root = ET.parse(xml_path).getroot()
            except (ET.ParseError, OSError):
                continue
            for element in xml_root.iter():
                attrs = {key.casefold(): value for key, value in element.attrib.items()}
                filename = next((attrs[k] for k in file_keys if attrs.get(k)), "")
                partition = next((attrs[k] for k in part_keys if attrs.get(k)), "")
                if filename and partition:
                    assign(filename, partition, f"{xml_path.name} XML")
        command_re = re.compile(
            r"(?:^|\s)(?:fastboot(?:\.exe)?\s+)?flash\s+"
            r"(?:--?\S+\s+)*([A-Za-z0-9_.:-]+)\s+[\"']?([^\"'\s]+)",
            re.IGNORECASE,
        )
        for script_path in folder.iterdir():
            if script_path.suffix.casefold() not in {".txt", ".bat", ".cmd", ".sh"}:
                continue
            try:
                text = script_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in command_re.finditer(text):
                assign(match.group(2), match.group(1), f"{script_path.name} script")
        return result

    def _load_partitions_from_luns(
        self, raw_luns: dict[int, str]
    ) -> tuple[int, list[str]]:
        """Populate the table from GPT metadata inside selected raw LUN images."""
        partitions: list[dict] = []
        errors: list[str] = []
        parsed_count = 0
        self.source_userdata_sizes.clear()

        for lun, filename in sorted(raw_luns.items()):
            if not 0 <= lun <= 3:
                errors.append(f"LUN{lun}: only LUN0-LUN3 are supported.")
                continue

            path = Path(filename)
            file_size = path.stat().st_size
            disk_path = f"/dev/block/sd{chr(ord('a') + lun)}"

            try:
                with path.open("rb") as fp:
                    block_size = 0
                    for candidate in (4096, 512, 2048, 8192, 16384):
                        fp.seek(candidate)
                        if fp.read(8) == b"EFI PART":
                            block_size = candidate
                            break

                    if not block_size:
                        partitions.append(
                            {
                                "path": disk_path,
                                "name": f"LUN{lun}",
                                "size": file_size,
                                "start_lba": 0,
                            }
                        )
                        continue

                    fp.seek(block_size)
                    header = fp.read(92)
                    if len(header) != 92 or header[:8] != b"EFI PART":
                        raise ValueError("invalid GPT header")

                    entries_lba, entry_count, entry_size = struct.unpack_from(
                        "<QII", header, 72
                    )
                    if not 0 < entry_count <= 16384:
                        raise ValueError(f"invalid GPT entry count {entry_count}")
                    if entry_size < 128 or entry_size > 4096 or entry_size % 8:
                        raise ValueError(f"invalid GPT entry size {entry_size}")

                    partitions.append(
                        {
                            "path": disk_path,
                            "name": "GPT_Header",
                            "size": 34 * 4096,
                            "start_lba": 0,
                        }
                    )

                    fp.seek(entries_lba * block_size)
                    entries = fp.read(entry_count * entry_size)
                    if len(entries) != entry_count * entry_size:
                        raise ValueError("truncated GPT entry array")

                    for index in range(entry_count):
                        entry = entries[
                            index * entry_size : (index + 1) * entry_size
                        ]
                        if entry[:16] == b"\x00" * 16:
                            continue
                        first_lba, last_lba = struct.unpack_from("<QQ", entry, 32)
                        if last_lba < first_lba:
                            continue
                        name = (
                            entry[56:min(entry_size, 128)]
                            .decode("utf-16le", errors="replace")
                            .rstrip("\x00")
                            .strip()
                            or f"partition_{index + 1}"
                        )
                        size = (last_lba - first_lba + 1) * block_size
                        if name.casefold() == "userdata":
                            self.source_userdata_sizes[lun] = size
                        partitions.append(
                            {
                                "path": f"{disk_path}{index + 1}",
                                "name": name,
                                "size": size,
                                # _populate_partitions receives 512-byte LBAs.
                                "start_lba": first_lba * (block_size // 512),
                            }
                        )
                        parsed_count += 1
            except (OSError, ValueError, struct.error) as exc:
                errors.append(f"LUN{lun}: GPT parse failed: {exc}")

        if partitions:
            self._populate_partitions(partitions)
        return parsed_count, errors

    def _choose_backup_parent(self) -> None:
        parent = QFileDialog.getExistingDirectory(
            self, "Select Backup Parent Folder"
        )
        if parent:
            if not self.device_model or not self.device_serial:
                QMessageBox.information(
                    self,
                    "Check Device First",
                    "Check the device before selecting the backup folder.",
                )
                return
            self.backup_parent = parent
            self._prepare_backup_folder()
            self._update_action_buttons()

    def _prepare_backup_folder(self) -> None:
        if not self.backup_parent:
            return
        model = re.sub(r"[^A-Za-z0-9._-]+", "_", self.device_model)
        serial = re.sub(r"[^A-Za-z0-9._-]+", "_", self.device_serial)
        if not model or not serial:
            return

        userdata_size = 0
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 3)
            size_item = self.table.item(row, 6)
            if name_item and name_item.text() == "userdata" and size_item:
                userdata_size = int(size_item.data(Qt.UserRole) or 0)
                break
        userdata_gib = userdata_size / (1024**3)
        if userdata_size <= 0:
            size_label = "unknown"
        elif userdata_gib <= 128:
            size_label = "128GB"
        elif userdata_gib <= 256:
            size_label = "256GB"
        elif userdata_gib <= 512:
            size_label = "512GB"
        elif userdata_gib <= 1024:
            size_label = "1TB"
        else:
            size_label = self._format_size(userdata_size).replace(" ", "")
        if self.auto_backup_running:
            folder_name = f"{model}_{serial}_{size_label}_sec"
        else:
            backup_mode = "lun" if self.lun_checkbox.isChecked() else "xml"
            folder_name = f"{model}_{serial}_{size_label}_{backup_mode}_romking"
        folder = Path(self.backup_parent) / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        folder_changed = self.backup_folder != str(folder)
        self.backup_folder = str(folder)
        self.folder_label.setText(str(folder))
        if folder_changed:
            self.log(f"Backup folder: {folder.name}")


def dependency_check() -> None:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        raise SystemExit(
            "PySide6 is not installed.\nRun: py -3.12 -m pip install PySide6"
        )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("HW rec")
    if APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    window = MainWindow()
    window.show()
    QTimer.singleShot(3000, window.check_for_updates)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
