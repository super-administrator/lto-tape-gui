from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QRect, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QPlainTextEdit,
    QStatusBar,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from tape_gui.commands import (
    CommandResult,
    TapeCommandRunner,
    device_list_has_device,
    health_check_media_not_ready,
    is_ltfs_mount_source,
    mounted_ltfs_device_id,
)
from tape_gui.config import AppConfig
from tape_gui.runtime_state import (
    calibrated_runtime_hours,
    estimated_poh,
    load_runtime_hours,
    save_runtime_hours,
)


class DonutUsageWidget(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._value = 0
        self.setMinimumSize(170, 170)

    def set_value(self, value: int) -> None:
        self._value = max(0, min(100, value))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(12, 12, -12, -12)
        start_angle = 90 * 16

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#e5e7eb"))
        painter.drawEllipse(rect)

        painter.setBrush(QColor("#1d4ed8"))
        span = -int(360 * 16 * self._value / 100)
        painter.drawPie(rect, start_angle, span)

        inner = rect.adjusted(24, 24, -24, -24)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(inner)

        painter.setPen(QColor("#111827"))
        painter.drawText(inner, Qt.AlignCenter, f"{self._value}%")


class OdometerWidget(QWidget):
    """Compact mechanical-meter style display for lifetime drive counters."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._poh: Optional[str] = None
        self._mmh: Optional[str] = None
        self.setMinimumSize(280, 58)

    def set_values(self, poh: Optional[str], mmh: Optional[str]) -> None:
        self._poh = poh
        self._mmh = mmh
        self.update()

    def _draw_counter(self, painter: QPainter, label: str, value: Optional[str], y: int) -> None:
        painter.setPen(QColor("#27313a"))
        painter.setFont(self.font())
        painter.setFont(self.font())
        painter.drawText(2, y + 16, label)

        digits = (value or "------")[-6:].rjust(6, "-")
        x = 38
        for digit in digits:
            rect = QRect(x, y, 20, 21)
            painter.setPen(QColor("#6b6654"))
            painter.setBrush(QColor("#e8e1c8"))
            painter.drawRect(rect)
            painter.setPen(QColor("#111827"))
            painter.setFont(self.font())
            painter.drawText(rect, Qt.AlignCenter, digit)
            x += 21
        painter.setPen(QColor("#27313a"))
        painter.drawText(x + 4, y + 16, "h")

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor("#4b5563"))
        painter.setBrush(QColor("#d9d3bc"))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 4, 4)
        self._draw_counter(painter, "POH", self._poh, 6)
        self._draw_counter(painter, "MMH", self._mmh, 32)


class FormatConfirmDialog(QDialog):
    def __init__(self, device_id: str, data_present: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("确认格式化")
        self.volume_label_edit = QLineEdit()
        self.volume_label_edit.setPlaceholderText("可选，例如：庭审录像1")
        if data_present:
            message = (
                f"检测到磁带已有数据。\n设备ID: {device_id}\n"
                "格式化将删除磁带上的全部数据，且无法恢复。"
            )
            force_text = "我确认删除磁带全部数据并强制格式化 (-f)"
        else:
            message = f"未检测到磁带文件。\n是否确认格式化磁带为 LTFS？\n设备ID: {device_id}"
            force_text = "强制格式化 (-f)"

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(message))
        layout.addWidget(QLabel("磁带卷标："))
        layout.addWidget(self.volume_label_edit)
        self.force_checkbox = QCheckBox(force_text)
        layout.addWidget(self.force_checkbox)

        btns = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
        if data_present:
            btns.button(QDialogButtonBox.Yes).setEnabled(False)
            self.force_checkbox.toggled.connect(btns.button(QDialogButtonBox.Yes).setEnabled)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)


def extract_health_metric(output: str, label: str) -> Optional[str]:
    match = re.search(rf"{re.escape(label)}\s+([0-9]+)", output, re.IGNORECASE)
    return match.group(1) if match else None


def health_check_interpretation(result: CommandResult, output: str) -> str:
    """Convert ITDT output into a short SMART-like Chinese explanation."""
    normalized = output.lower()
    media_not_ready = health_check_media_not_ready(output)
    rows = ["健康度项目解读", "=" * 28]

    if "devtype = scsi tape" in normalized:
        rows.append("设备识别       [正常] 已识别为 SCSI 流式磁带机。")
    else:
        rows.append("设备识别       [注意] 未在输出中确认磁带机类型。")

    if media_not_ready:
        rows.append("磁带介质       [提示] 当前未就绪，Sense 3A00 通常表示未检测到磁带。")
    elif result.ok and "test unit ready" in normalized:
        rows.append("磁带介质       [正常] 磁带机已响应就绪检查。")
    else:
        rows.append("磁带介质       [异常] 就绪检查未通过，请结合 Sense 数据判断。")

    if "dynamic runtime attribute values" in normalized:
        rows.append("运行信息       [已读取] 已取得磁带机动态运行信息。")
    else:
        rows.append("运行信息       [提示] 未取得动态运行信息，可能与介质未就绪有关。")

    if "sense data" in normalized or "error sense data" in normalized:
        if "3a00" in normalized:
            rows.append("Sense 信息     [提示] ASC/ASCQ=3A/00：介质未装入或尚未就绪。")
        elif "operation failed" in normalized:
            rows.append("Sense 信息     [异常] 设备返回 Sense/操作失败信息，请保留日志供维护分析。")
        else:
            rows.append("Sense 信息     [已读取] 检查输出包含 Sense 数据。")
    else:
        rows.append("Sense 信息     [正常] 未发现错误 Sense 数据。")

    rows.extend(["", "运行状况", "=" * 28])
    poh = extract_health_metric(output, "Lifetime POH")
    mmh = extract_health_metric(output, "Lifetime MMH")
    loads = extract_health_metric(output, "Lifetime Media Loads")
    cleaning = extract_health_metric(output, "Lifetime Cleaning Op.")
    power_cycles = extract_health_metric(output, "Lifetime Power Cycles")
    write_errors = extract_health_metric(output, "Hard Write Errors")
    read_errors = extract_health_metric(output, "Hard Read Errors")
    tape_meters = extract_health_metric(output, "Lt Meters Tape Processed")
    rows.append(f"累计通电时间   [数据] {poh} 小时。" if poh else "累计通电时间   [未提供] ITDT 未返回 Lifetime POH。")
    rows.append(f"累计磁带运动   [数据] {mmh} 小时（MMH，不代表剩余寿命）。" if mmh else "累计磁带运动   [未提供] ITDT 未返回 Lifetime MMH。")
    rows.append(f"装带次数       [数据] {loads} 次。" if loads else "装带次数       [未提供]。")
    rows.append(f"清洁操作次数   [数据] {cleaning} 次。" if cleaning else "清洁操作次数   [未提供]。")
    rows.append(f"累计通电循环   [数据] {power_cycles} 次。" if power_cycles else "累计通电循环   [未提供]。")
    rows.append(f"硬写入错误     [数据] {write_errors} 次。" if write_errors else "硬写入错误     [未提供]。")
    rows.append(f"硬读取错误     [数据] {read_errors} 次。" if read_errors else "硬读取错误     [未提供]。")
    rows.append(f"累计磁带长度   [数据] {tape_meters} 米。" if tape_meters else "累计磁带长度   [未提供]。")
    rows.append("磁头剩余寿命   [未提供] 当前 ITDT 输出未提供剩余寿命百分比。")

    if result.ok:
        rows.append("综合结论       [正常] ITDT 检查命令完成。")
    elif media_not_ready:
        rows.append("综合结论       [提示] 磁带机可访问，当前主要问题是介质未就绪。")
    else:
        rows.append(f"综合结论       [异常] ITDT 返回码 {result.return_code}，建议结合原始日志处理。")

    rows.append("说明：该检查用于设备与当前介质状态，不等同于磁带寿命百分比或完整 SMART 预测。")
    return "\n".join(rows)


class HealthCheckDialog(QDialog):
    def __init__(self, result: CommandResult, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("健康度检查结果")
        self.resize(900, 720)

        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        media_not_ready = health_check_media_not_ready(output)
        if result.ok:
            summary = "检查完成：磁带机响应正常。"
        elif media_not_ready:
            summary = "检查完成：磁带机可访问，但当前未放置磁带。"
        else:
            summary = f"检查完成：发现异常（返回码 {result.return_code}），请查看详细信息。"

        layout = QVBoxLayout(self)
        summary_label = QLabel(summary)
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        details = QTextEdit()
        details.setReadOnly(True)
        details.setPlainText(output or "未收到 ITDT 输出。")
        details.setMinimumHeight(240)
        layout.addWidget(QLabel("ITDT 原始输出"))
        layout.addWidget(details, 1)

        layout.addWidget(QLabel("中文解读"))
        interpretation = QTextEdit()
        interpretation.setReadOnly(True)
        interpretation.setPlainText(health_check_interpretation(result, output))
        interpretation.setMinimumHeight(180)
        layout.addWidget(interpretation, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class CommandWorker(QThread):
    line = Signal(str)
    progress = Signal(int)
    finished_result = Signal(object, str)

    def __init__(
        self,
        title: str,
        task: Callable[..., CommandResult],
        stream: bool = False,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.title = title
        self.task = task
        self.stream = stream

    def run(self) -> None:
        try:
            if self.stream:
                result = self.task(self.line.emit, self.progress.emit)
            else:
                result = self.task()
        except Exception as exc:  # pragma: no cover
            result = CommandResult(
                ok=False,
                command=["internal"],
                stdout="",
                stderr=str(exc),
                return_code=1,
            )
        self.finished_result.emit(result, self.title)


@dataclass
class DeviceState:
    drive_online: bool = False
    mounted: bool = False


class TapeGuiMainWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.cfg = config
        self.runner = TapeCommandRunner(
            command_timeout_sec=self.cfg.command_timeout_sec,
            backup_timeout_sec=self.cfg.backup_timeout_sec,
        )
        self.workers: list[CommandWorker] = []
        self.state = DeviceState()
        self.current_task_title: str = ""
        self.current_worker: Optional[CommandWorker] = None
        self.validated_device_id: str = ""
        self.runtime_hours = load_runtime_hours(self.cfg.runtime_state_path)

        self.setWindowTitle("LTO LTFS Manager")
        self.resize(1320, 860)
        self._apply_style()
        self._build_ui()
        self._refresh_usage()
        self._refresh_runtime_meter()
        self.runtime_timer = QTimer(self)
        self.runtime_timer.timeout.connect(self._refresh_runtime_meter)
        self.runtime_timer.start(60_000)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f8fafc; }
            QGroupBox { border: 1px solid #cbd5e1; border-radius: 10px; margin-top: 10px; background: #ffffff; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 2px 6px; color: #0f172a; }
            QPushButton { background: #1d4ed8; color: white; border-radius: 8px; padding: 8px 12px; }
            QPushButton:hover { background: #1e40af; }
            QLineEdit, QPlainTextEdit, QTextEdit { border: 1px solid #cbd5e1; border-radius: 8px; padding: 6px; background: #ffffff; }
            QProgressBar { border: 1px solid #cbd5e1; border-radius: 8px; text-align: center; }
            QProgressBar::chunk { background: #16a34a; border-radius: 8px; }
            """
        )

    def _build_ui(self) -> None:
        self._build_menu()

        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(16)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setSpacing(16)

        device_group = QGroupBox("磁带机状态区")
        device_layout = QGridLayout(device_group)
        device_layout.setHorizontalSpacing(10)
        device_layout.setVerticalSpacing(10)

        self.device_id_edit = QLineEdit(self.cfg.device_id)
        self.mount_point_edit = QLineEdit(self.cfg.mount_point)

        self.btn_list = QPushButton("扫描设备ID")
        self.btn_mount = QPushButton("挂载磁带")
        self.btn_unmount = QPushButton("卸载挂载")
        self.btn_eject = QPushButton("弹出磁带")
        self.btn_format = QPushButton("格式化")
        self.btn_rewind = QPushButton("卷带")
        self.btn_health = QPushButton("健康度检查")

        self.btn_list.clicked.connect(self.on_list_devices)
        self.btn_mount.clicked.connect(self.on_mount)
        self.btn_unmount.clicked.connect(self.on_unmount_release)
        self.btn_eject.clicked.connect(self.on_eject)
        self.btn_format.clicked.connect(self.on_format)
        self.btn_rewind.clicked.connect(self.on_rewind)
        self.btn_health.clicked.connect(self.on_health_check)
        self.device_id_edit.textChanged.connect(self.on_device_id_changed)

        device_layout.addWidget(QLabel("设备ID (devname):"), 0, 0)
        device_layout.addWidget(self.device_id_edit, 0, 1, 1, 3)
        device_layout.addWidget(QLabel("挂载目录:"), 1, 0)
        device_layout.addWidget(self.mount_point_edit, 1, 1, 1, 3)
        device_layout.addWidget(self.btn_list, 2, 0)
        device_layout.addWidget(self.btn_mount, 2, 1)
        device_layout.addWidget(self.btn_unmount, 2, 2)
        device_layout.addWidget(self.btn_eject, 2, 3)

        backup_group = QGroupBox("文件备份区")
        backup_layout = QGridLayout(backup_group)
        backup_layout.setHorizontalSpacing(10)
        backup_layout.setVerticalSpacing(10)

        self.source_edit = QPlainTextEdit(self.cfg.default_backup_source)
        self.source_edit.setPlaceholderText("每行一个源文件或源文件夹")
        self.source_edit.setMaximumHeight(76)
        self.target_edit = QLineEdit(self.cfg.mount_point)
        self.btn_source_pick = QPushButton("选择源文件/夹")
        self.btn_target_pick = QPushButton("选择磁带目录")
        self.btn_start_backup = QPushButton("开始备份")
        self.btn_cancel_task = QPushButton("取消当前任务")
        self.use_ordered_copy = QCheckBox("LTFS 顺序优化")
        self.use_ordered_copy.setToolTip("使用 ltfs_ordered_copy -ar，适合大量文件夹，不能与 rsync 选项同时使用")
        self.use_ordered_copy.setChecked(self.cfg.use_ordered_copy_default)
        self.use_inplace = QCheckBox("原地写入")
        self.use_inplace.setToolTip("rsync --inplace：直接写入目标文件")
        self.use_inplace.setChecked(self.cfg.use_inplace_default)
        self.use_ignore_existing = QCheckBox("增量备份")
        self.use_ignore_existing.setToolTip("rsync --ignore-existing：按文件名跳过磁带上已有文件")
        self.use_ignore_existing.setChecked(self.cfg.use_ignore_existing_default)
        self._syncing_backup_options = False
        self.use_ordered_copy.toggled.connect(self._on_ordered_copy_toggled)
        self.use_inplace.toggled.connect(self._on_rsync_option_toggled)
        self.use_ignore_existing.toggled.connect(self._on_rsync_option_toggled)
        if self.use_ordered_copy.isChecked():
            self._on_ordered_copy_toggled(True)

        self.btn_source_pick.clicked.connect(self.on_pick_source)
        self.btn_target_pick.clicked.connect(self.on_pick_target)
        self.btn_start_backup.clicked.connect(self.on_backup)
        self.btn_cancel_task.clicked.connect(self.on_cancel_task)

        backup_layout.addWidget(QLabel("源路径（每行一项）:"), 0, 0)
        backup_layout.addWidget(self.source_edit, 0, 1, 1, 2)
        backup_layout.addWidget(self.btn_source_pick, 0, 3)
        backup_layout.addWidget(QLabel("目标目录:"), 1, 0)
        backup_layout.addWidget(self.target_edit, 1, 1, 1, 2)
        backup_layout.addWidget(self.btn_target_pick, 1, 3)
        backup_layout.addWidget(self.use_ordered_copy, 2, 0)
        backup_layout.addWidget(self.use_inplace, 2, 1)
        backup_layout.addWidget(self.use_ignore_existing, 2, 2)
        backup_layout.addWidget(self.btn_start_backup, 2, 3)
        backup_layout.addWidget(self.btn_cancel_task, 3, 3)

        self.task_controls = [
            self.device_id_edit,
            self.mount_point_edit,
            self.btn_list,
            self.btn_mount,
            self.btn_unmount,
            self.btn_eject,
            self.btn_format,
            self.btn_rewind,
            self.btn_health,
            self.source_edit,
            self.target_edit,
            self.btn_source_pick,
            self.btn_target_pick,
            self.btn_start_backup,
            self.use_ordered_copy,
            self.use_inplace,
            self.use_ignore_existing,
        ]
        self.btn_cancel_task.setEnabled(False)

        status_group = QGroupBox("状态显示区")
        status_layout = QVBoxLayout(status_group)
        status_layout.setSpacing(8)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress_label = QLabel("等待任务")
        status_layout.addWidget(self.progress)
        status_layout.addWidget(self.progress_label)

        log_group = QGroupBox("日志区")
        log_layout = QVBoxLayout(log_group)
        log_layout.setSpacing(8)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)

        left_layout.addWidget(device_group)
        left_layout.addWidget(backup_group)
        left_layout.addWidget(status_group)
        left_layout.addWidget(log_group, 1)

        right = self._build_right_panel()

        root_layout.addWidget(left, 3)
        root_layout.addWidget(right, 1)

        self.setCentralWidget(root)

        bar = QStatusBar(self)
        self.setStatusBar(bar)
        self.runtime_meter = OdometerWidget()
        self.runtime_meter.setToolTip("POH 按本次系统启动后的时间估算；MMH 仅以健康度检查的实际读数校准。")
        self.bottom_label = QLabel("上传: 0 | 下载: 0 | 磁带: 未挂载")
        status_panel = QWidget()
        status_panel_layout = QVBoxLayout(status_panel)
        status_panel_layout.setContentsMargins(0, 0, 0, 0)
        status_panel_layout.setSpacing(4)
        status_panel_layout.addWidget(self.runtime_meter)
        status_panel_layout.addWidget(self.bottom_label, alignment=Qt.AlignRight)
        bar.addPermanentWidget(status_panel)

        self._log("应用已启动。")

    def _build_menu(self) -> None:
        # Reserved menus are intentionally omitted until they have real actions.
        return

    def _on_ordered_copy_toggled(self, checked: bool) -> None:
        if not checked or self._syncing_backup_options:
            return
        self._syncing_backup_options = True
        self.use_inplace.setChecked(False)
        self.use_ignore_existing.setChecked(False)
        self._syncing_backup_options = False

    def _on_rsync_option_toggled(self, checked: bool) -> None:
        if not checked or self._syncing_backup_options:
            return
        self._syncing_backup_options = True
        self.use_ordered_copy.setChecked(False)
        self._syncing_backup_options = False

    def _build_right_panel(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)

        state_group = QGroupBox("设备状态")
        state_layout = QFormLayout(state_group)

        self.drive_dot = self._make_dot()
        self.mount_dot = self._make_dot()
        self.drive_text = QLabel("磁带机未联机")
        self.mount_text = QLabel("磁带未挂载")

        drive_line = QWidget()
        drive_row = QHBoxLayout(drive_line)
        drive_row.setContentsMargins(0, 0, 0, 0)
        drive_row.addWidget(self.drive_dot)
        drive_row.addWidget(self.drive_text)

        mount_line = QWidget()
        mount_row = QHBoxLayout(mount_line)
        mount_row.setContentsMargins(0, 0, 0, 0)
        mount_row.addWidget(self.mount_dot)
        mount_row.addWidget(self.mount_text)

        state_layout.addRow("磁带机:", drive_line)
        state_layout.addRow("磁带挂载:", mount_line)

        usage_group = QGroupBox("磁带使用率")
        usage_layout = QVBoxLayout(usage_group)
        self.usage_donut = DonutUsageWidget()
        self.usage_text = QLabel("未挂载，无法统计")
        self.usage_text.setAlignment(Qt.AlignCenter)
        usage_layout.addWidget(self.usage_donut, alignment=Qt.AlignCenter)
        usage_layout.addWidget(self.usage_text)

        tape_actions = QGroupBox("磁带操作")
        tape_actions_layout = QVBoxLayout(tape_actions)
        tape_actions_layout.addWidget(self.btn_format)
        tape_actions_layout.addWidget(self.btn_rewind)
        tape_actions_layout.addWidget(self.btn_health)

        layout.addWidget(state_group)
        layout.addWidget(usage_group)
        layout.addWidget(tape_actions)
        layout.addStretch(1)
        return wrapper

    def _make_dot(self) -> QFrame:
        dot = QFrame()
        dot.setFixedSize(14, 14)
        dot.setStyleSheet("background:#ef4444;border-radius:7px;")
        return dot

    def _set_dot(self, dot: QFrame, ok: bool) -> None:
        color = "#22c55e" if ok else "#ef4444"
        dot.setStyleSheet(f"background:{color};border-radius:7px;")

    def _log(self, text: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.append(f"[{ts}] {text}")

    def _run_task(self, title: str, task: Callable[..., CommandResult], stream: bool = False) -> None:
        if self.current_worker is not None:
            self._log(f"拒绝启动 {title}: 当前任务尚未结束。")
            return
        self._log(f"执行任务: {title}")
        self.progress_label.setText(f"运行中: {title}")
        self.current_task_title = title

        worker = CommandWorker(title=title, task=task, stream=stream, parent=self)
        worker.line.connect(self._log)
        worker.progress.connect(self.progress.setValue)
        worker.finished_result.connect(self._on_task_finished)
        worker.finished.connect(lambda: self._cleanup_worker(worker))
        self.workers.append(worker)
        self.current_worker = worker
        self._set_task_running(True)
        worker.start()

    def _set_task_running(self, running: bool) -> None:
        for control in self.task_controls:
            control.setEnabled(not running)
        self.btn_cancel_task.setEnabled(running)

    def _cleanup_worker(self, worker: CommandWorker) -> None:
        if worker in self.workers:
            self.workers.remove(worker)

    def _on_task_finished(self, result: CommandResult, title: str) -> None:
        cmd = " ".join(result.command)
        self._log(f"任务完成: {title}")
        self._log(f"命令: {cmd}")
        self._log(f"返回码: {result.return_code}")
        if result.stdout:
            self._log(result.stdout)
        if result.stderr:
            self._log(result.stderr)
        self._log("-" * 60)

        device_id = self.device_id_edit.text().strip()
        device_output = f"{result.stdout}\n{result.stderr}"
        device_found = title == "扫描 LTFS 设备" and device_list_has_device(device_output, device_id)
        health_no_media = title == "健康度检查" and health_check_media_not_ready(device_output)
        if result.ok or device_found or health_no_media:
            suffix = "（未放置磁带）" if health_no_media else ""
            self.progress_label.setText(f"完成: {title}{suffix}")
        else:
            self.progress_label.setText(f"失败: {title}")
        self.current_task_title = ""
        self.current_worker = None
        self._set_task_running(False)
        self.progress.setRange(0, 100)

        if title == "检查磁带内容":
            if not result.ok:
                self.progress_label.setText("失败: 检查磁带内容，已阻止格式化")
                QMessageBox.critical(self, "无法安全格式化", result.stderr or "无法读取磁带内容。")
                return
            data_present = "TAPE_CONTENT_STATE=data" in result.stdout
            device_id = self.device_id_edit.text().strip()
            dlg = FormatConfirmDialog(device_id, data_present=data_present, parent=self)
            if dlg.exec() == QDialog.Accepted:
                self._run_task(
                    "格式化 LTFS",
                    lambda: self.runner.format_ltfs(
                        device_id,
                        force=dlg.force_checkbox.isChecked(),
                        volume_label=dlg.volume_label_edit.text(),
                    ),
                )
            return

        if title == "扫描 LTFS 设备":
            self.state.drive_online = device_found
            self.validated_device_id = device_id if device_found else ""
            if device_found and not result.ok:
                self._log("设备清单已识别目标设备；忽略 ltfs device_list 的非零返回码。")

        self.update_device_state()
        self._refresh_usage()
        if title == "健康度检查":
            usage_hours = extract_health_metric(device_output, "Lifetime POH")
            motion_hours = extract_health_metric(device_output, "Lifetime MMH")
            if usage_hours or motion_hours:
                self.runtime_hours = calibrated_runtime_hours(usage_hours, motion_hours)
                try:
                    save_runtime_hours(self.cfg.runtime_state_path, self.runtime_hours)
                    self._log(f"运行小时数已校准并保存: {self.cfg.runtime_state_path}")
                except OSError as exc:
                    self._log(f"运行小时数保存失败: {exc}")
            self._refresh_runtime_meter()
            HealthCheckDialog(result, self).exec()

    def _refresh_runtime_meter(self) -> None:
        poh = estimated_poh(self.runtime_hours)
        mmh = self.runtime_hours.mmh
        self.runtime_meter.set_values(
            str(poh) if poh is not None else None,
            str(mmh) if mmh is not None else None,
        )

    def _human_bytes(self, size: int) -> str:
        value = float(size)
        for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
            if value < 1024 or unit == "PB":
                return f"{value:.2f} {unit}"
            value /= 1024
        return f"{size} B"

    def _refresh_usage(self) -> None:
        mount_point = self.mount_point_edit.text().strip()
        if not mount_point or not self.runner.is_ltfs_mounted(mount_point):
            self.usage_donut.set_value(0)
            self.usage_text.setText("未挂载，无法统计")
            return
        try:
            usage = os.statvfs(mount_point)
            total = usage.f_blocks * usage.f_frsize
            free = usage.f_bavail * usage.f_frsize
            used = max(0, total - free)
            percent = int((used / total) * 100) if total else 0
            self.usage_donut.set_value(percent)
            self.usage_text.setText(
                f"已用 {self._human_bytes(used)} / 总计 {self._human_bytes(total)}"
            )
        except Exception as exc:
            self._log(f"使用率读取失败: {exc}")

    def update_device_state(self) -> None:
        mount_point = self.mount_point_edit.text().strip()
        mounted, fstype, source = self.runner.mount_info(mount_point) if mount_point else (False, "", "")
        self.state.mounted = mounted and is_ltfs_mount_source(source, fstype)
        mounted_device_id = mounted_ltfs_device_id(source) if self.state.mounted else ""
        configured_device_id = self.device_id_edit.text().strip()
        if self.state.mounted and mounted_device_id and configured_device_id == mounted_device_id:
            self.state.drive_online = True
            self.validated_device_id = configured_device_id
        elif self.state.mounted and mounted_device_id:
            self.state.drive_online = False
            self.validated_device_id = ""
        elif not configured_device_id:
            self.state.drive_online = False

        self._set_dot(self.drive_dot, self.state.drive_online)
        self._set_dot(self.mount_dot, self.state.mounted)

        self.drive_text.setText("磁带机已联机" if self.state.drive_online else "磁带机未联机")
        self.mount_text.setText("磁带已挂载" if self.state.mounted else "磁带未挂载")
        self.bottom_label.setText(
            f"上传: 0 | 下载: 0 | 磁带: {'已挂载' if self.state.mounted else '未挂载'}"
        )

    def on_device_id_changed(self, device_id: str) -> None:
        if device_id.strip() != self.validated_device_id:
            self.state.drive_online = False
            self.update_device_state()

    def on_list_devices(self) -> None:
        mount_point = self.mount_point_edit.text().strip()
        mounted, fstype, source = self.runner.mount_info(mount_point) if mount_point else (False, "", "")
        mounted_device_id = mounted_ltfs_device_id(source) if mounted and is_ltfs_mount_source(source, fstype) else ""
        configured_device_id = self.device_id_edit.text().strip()
        if mounted_device_id:
            if configured_device_id and configured_device_id != mounted_device_id:
                QMessageBox.warning(
                    self,
                    "设备已挂载",
                    f"当前挂载设备为 {mounted_device_id}，与输入的设备ID不一致，已跳过扫描。",
                )
                return
            self.state.drive_online = True
            self.validated_device_id = mounted_device_id
            self._log(f"检测到已挂载 LTFS 设备 {mounted_device_id}，跳过 device_list 扫描。")
            self.progress_label.setText("完成: 扫描设备ID（已从挂载状态确认）")
            self.update_device_state()
            return
        self._run_task("扫描 LTFS 设备", self.runner.list_ltfs_devices)

    def on_mount(self) -> None:
        device_id = self.device_id_edit.text().strip()
        mount_point = self.mount_point_edit.text().strip()
        if not device_id or not mount_point:
            QMessageBox.critical(self, "参数错误", "请填写设备ID和挂载目录")
            return
        if self.validated_device_id != device_id:
            QMessageBox.critical(self, "设备未验证", "请先扫描设备ID并确认当前设备在线。")
            return
        self._run_task(
            "挂载磁带",
            lambda: self.runner.mount_ltfs(
                device_id,
                mount_point,
                wait_timeout_sec=self.cfg.mount_wait_timeout_sec,
            ),
        )

    def on_unmount_release(self) -> None:
        mount_point = self.mount_point_edit.text().strip()
        if not mount_point:
            QMessageBox.critical(self, "参数错误", "请填写挂载目录")
            return

        if QMessageBox.question(self, "确认", f"是否执行卸载挂载点？\n{mount_point}") != QMessageBox.Yes:
            return

        self._run_task("卸载挂载", lambda: self.runner.unmount(mount_point))

    def on_eject(self) -> None:
        if self.runner.is_ltfs_mounted(self.mount_point_edit.text().strip()):
            QMessageBox.critical(self, "磁带仍已挂载", "请先执行“卸载挂载”，再弹出磁带。")
            return
        if not self.cfg.eject_device:
            QMessageBox.critical(self, "参数错误", "未配置磁带设备路径。")
            return
        message = (
            f"是否确认弹出磁带？\n设备: {self.cfg.eject_device}\n"
            "该操作会先将磁带倒带到初始位置，再执行卸载。"
        )
        if QMessageBox.question(self, "确认弹出", message) != QMessageBox.Yes:
            return
        self._run_task("弹出磁带", lambda: self.runner.eject_tape(self.cfg.eject_device))

    def on_rewind(self) -> None:
        if self.runner.is_ltfs_mounted(self.mount_point_edit.text().strip()):
            QMessageBox.critical(self, "磁带仍已挂载", "请先执行“卸载磁带”，再执行卷带。")
            return
        if not self.cfg.tape_device:
            QMessageBox.critical(self, "参数错误", "未配置磁带设备路径。")
            return
        message = (
            f"是否确认执行磁带卷带保养？\n设备: {self.cfg.tape_device}\n"
            "该操作用于长期存放后的磁带张力维护。"
        )
        if QMessageBox.question(self, "确认卷带", message) != QMessageBox.Yes:
            return
        self._run_task("卷带", lambda: self.runner.retension_tape(self.cfg.tape_device))

    def on_health_check(self) -> None:
        if self.runner.is_ltfs_mounted(self.mount_point_edit.text().strip()):
            QMessageBox.critical(self, "磁带仍已挂载", "请先执行“卸载磁带”，再进行健康度检查。")
            return
        if not self.cfg.diagnostic_device:
            QMessageBox.critical(self, "参数错误", "未配置健康度检查设备路径。")
            return
        self._run_task(
            "健康度检查",
            lambda: self.runner.health_check(self.cfg.diagnostic_device),
        )

    def on_format(self) -> None:
        device_id = self.device_id_edit.text().strip()
        if not device_id:
            QMessageBox.critical(self, "参数错误", "请填写设备ID")
            return
        if self.current_worker is not None:
            QMessageBox.critical(self, "任务运行中", "当前任务结束后才能格式化磁带。")
            return
        if self.runner.is_ltfs_mounted(self.mount_point_edit.text().strip()):
            QMessageBox.critical(self, "磁带已挂载", "请先执行“卸载挂载”，再执行格式化。")
            return
        if self.validated_device_id != device_id:
            QMessageBox.critical(self, "设备未验证", "请先扫描设备ID并确认当前设备在线。")
            return

        self._run_task(
            "检查磁带内容",
            lambda: self.runner.probe_tape_contents(
                device_id,
                self.mount_point_edit.text().strip(),
                wait_timeout_sec=self.cfg.mount_wait_timeout_sec,
                release_mode=self.cfg.release_mode,
            ),
        )

    def on_pick_source(self) -> None:
        choice = QMessageBox(self)
        choice.setWindowTitle("选择备份源")
        choice.setText("请选择源文件或源文件夹，可一次选择多个")
        file_button = choice.addButton("选择文件（可多选）", QMessageBox.AcceptRole)
        directory_button = choice.addButton("选择文件夹（可多选）", QMessageBox.AcceptRole)
        choice.addButton("取消", QMessageBox.RejectRole)
        choice.exec()

        current_sources = self._source_paths()
        start_path = current_sources[0] if current_sources else "/"
        if Path(start_path).is_file():
            start_path = str(Path(start_path).parent)

        if choice.clickedButton() is file_button:
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                "选择一个或多个源文件",
                start_path,
            )
        elif choice.clickedButton() is directory_button:
            paths = self._pick_source_directories(start_path)
        else:
            paths = []
        if paths:
            self.source_edit.setPlainText("\n".join(dict.fromkeys(paths)))

    def _source_paths(self) -> list[str]:
        lines = (line.strip() for line in self.source_edit.toPlainText().splitlines())
        return list(dict.fromkeys(line for line in lines if line))

    def _pick_source_directories(self, start_path: str) -> list[str]:
        dialog = QFileDialog(self, "选择一个或多个源文件夹", start_path)
        dialog.setFileMode(QFileDialog.Directory)
        dialog.setOption(QFileDialog.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        for view_type in (QListView, QTreeView):
            for view in dialog.findChildren(view_type):
                view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        if dialog.exec() != QDialog.Accepted:
            return []
        return [path for path in dialog.selectedFiles() if Path(path).is_dir()]

    def on_pick_target(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择目标目录", self.target_edit.text().strip() or "/")
        if path:
            self.target_edit.setText(path)

    def on_backup(self) -> None:
        sources = self._source_paths()
        target = self.target_edit.text().strip()

        if not sources or not target:
            QMessageBox.critical(self, "参数错误", "请选择至少一个源文件/夹并填写目标目录")
            return
        invalid_sources = [source for source in sources if not Path(source).is_file() and not Path(source).is_dir()]
        if invalid_sources:
            QMessageBox.critical(
                self,
                "源路径错误",
                "以下源文件或文件夹不存在或不可访问：\n" + "\n".join(invalid_sources),
            )
            return
        if not self.runner.is_path_on_ltfs(target):
            QMessageBox.critical(self, "目标错误", "目标目录不是已挂载的 LTFS 磁带目录，已阻止写入。")
            return

        try:
            source_bytes = sum(self.runner.estimate_source_bytes(source) for source in sources)
        except Exception as exc:
            QMessageBox.critical(self, "源目录检测失败", f"无法统计源数据大小，已阻止备份。\n{exc}")
            return

        try:
            free_bytes = self.runner.mount_free_bytes(target)
            free_desc = self._human_bytes(free_bytes)
        except Exception as exc:
            QMessageBox.critical(self, "容量检测失败", f"无法读取磁带剩余空间，已阻止备份。\n{exc}")
            return

        source_desc = self._human_bytes(source_bytes)
        source_list = "\n".join(f"{index}. {source}" for index, source in enumerate(sources, 1))
        msg = (
            f"是否确认按顺序将以下 {len(sources)} 项写入磁带目录 {target}？\n"
            f"{source_list}\n"
            f"源数据大小: {source_desc}\n"
            f"目标剩余空间: {free_desc}"
        )

        if QMessageBox.question(self, "备份确认", msg, QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return

        required_bytes = source_bytes + self.cfg.minimum_free_bytes
        if required_bytes > free_bytes:
            QMessageBox.warning(
                self,
                "空间不足",
                f"源数据与安全余量共需 {self._human_bytes(required_bytes)}，"
                f"磁带剩余 {self._human_bytes(free_bytes)}。",
            )
            return

        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        all_directories = all(Path(source).is_dir() for source in sources)
        use_ordered_copy = self.use_ordered_copy.isChecked() and all_directories
        if self.use_ordered_copy.isChecked() and not all_directories:
            self._log("队列包含单个文件，ltfs_ordered_copy 不适用，整批已改用 rsync。")
            self.use_ordered_copy.setChecked(False)

        if use_ordered_copy:
            # ordered_copy 各版本输出不稳定，使用忙碌条避免误导为 0%。
            self.progress.setRange(0, 0)
            task = lambda line_cb, progress_cb: self.runner.backup_queue(
                sources,
                target,
                ordered_copy=True,
                line_cb=line_cb,
                progress_cb=progress_cb,
            )
            self._run_task(f"备份队列({len(sources)}项, ltfs_ordered_copy)", task, stream=True)
        else:
            task = lambda line_cb, progress_cb: self.runner.backup_queue(
                sources,
                target,
                ordered_copy=False,
                line_cb=line_cb,
                progress_cb=progress_cb,
                inplace=self.use_inplace.isChecked(),
                ignore_existing=self.use_ignore_existing.isChecked(),
            )
            self._run_task(f"备份队列({len(sources)}项, rsync)", task, stream=True)

    def on_cancel_task(self) -> None:
        if not self.current_task_title:
            QMessageBox.information(self, "取消任务", "当前没有运行中的任务。")
            return
        if QMessageBox.question(self, "取消确认", f"是否取消任务：{self.current_task_title}？") != QMessageBox.Yes:
            return
        cancelled = self.runner.cancel_current()
        if cancelled:
            self._log(f"已请求取消任务: {self.current_task_title}")
            self.progress_label.setText(f"取消中: {self.current_task_title}")
        else:
            self._log("未找到可取消的外部命令进程。")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.current_worker is not None:
            QMessageBox.warning(self, "任务运行中", "请先取消或等待当前任务完成。")
            event.ignore()
            return
        if self.runner.is_ltfs_mounted(self.mount_point_edit.text().strip()):
            QMessageBox.warning(self, "磁带仍已挂载", "请先执行“卸载挂载”，避免遗留受管 LTFS 进程。")
            event.ignore()
            return
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    config_path = Path(__file__).resolve().parents[2] / "config" / "default.json"
    cfg = AppConfig(config_path=config_path)
    w = TapeGuiMainWindow(cfg)
    w.show()
    w.update_device_state()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
