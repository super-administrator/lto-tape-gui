import os
import unittest
from pathlib import Path

# Must be set before importing PySide6 so CI and development hosts need no display server.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QTextEdit

from tape_gui.config import AppConfig
from tape_gui.commands import CommandResult
from tape_gui.main import FormatConfirmDialog, HealthCheckDialog, TapeGuiMainWindow, health_check_interpretation


class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_loads_without_starting_hardware_operations(self):
        project_root = Path(__file__).resolve().parents[1]
        config = AppConfig(project_root / "config" / "default.json")
        window = TapeGuiMainWindow(config)
        try:
            self.assertEqual(window.windowTitle(), "LTO LTFS Manager")
            self.assertEqual(window.menuBar().actions(), [])
            self.assertEqual(window.device_id_edit.text(), config.device_id)
            self.assertEqual(window.mount_point_edit.text(), config.mount_point)
            self.assertTrue(window.btn_start_backup.isEnabled())
            self.assertFalse(window.btn_cancel_task.isEnabled())
            self.assertEqual(window.progress.value(), 0)
            self.assertEqual(window.btn_eject.text(), "弹出磁带")
            self.assertEqual(window.btn_unmount.text(), "卸载挂载")
            self.assertEqual(window.btn_format.text(), "格式化")
            self.assertEqual(window.btn_rewind.text(), "卷带")
            self.assertEqual(window.btn_health.text(), "健康度检查")
            self.assertEqual(window.btn_source_pick.text(), "选择源文件/夹")
            window.source_edit.setPlainText("/data/one\n/data/two\n/data/one")
            self.assertEqual(window._source_paths(), ["/data/one", "/data/two"])
            self.assertIsNotNone(window.runtime_meter)
            self.assertIsNone(window.runtime_meter._poh)
            self.assertIsNone(window.runtime_meter._mmh)
            self.assertIn("上传: 0", window.bottom_label.text())
            window.runtime_meter.set_values("956", "311")
            window.runtime_meter.resize(250, 44)
            window.runtime_meter.grab()
            self.assertEqual(window.use_ordered_copy.text(), "LTFS 顺序优化")
            self.assertEqual(window.use_ignore_existing.text(), "增量备份")
            self.assertTrue(window.use_inplace.isChecked())
            self.assertTrue(window.use_ignore_existing.isChecked())
            window.use_ordered_copy.setChecked(True)
            self.assertFalse(window.use_inplace.isChecked())
            self.assertFalse(window.use_ignore_existing.isChecked())
            window.use_ignore_existing.setChecked(True)
            self.assertFalse(window.use_ordered_copy.isChecked())
        finally:
            window.close()

    def test_health_report_contains_chinese_interpretation(self):
        output = (
            "Issuing Get Device Information ...\n"
            "devtype = SCSI Tape\n"
            "Issuing test unit ready...\n"
            "Querying Dynamic Runtime Information...\n"
            "Dynamic Runtime Attribute Values:\n"
            " Lifetime POH 956 Lifetime MMH 311\n"
            " Lifetime Media Loads 78 Lifetime Cleaning Op. 1\n"
            " Hard Write Errors 0 Hard Read Errors 0"
        )
        result = CommandResult(True, ["itdt"], output, "", 0)
        dialog = HealthCheckDialog(result)
        try:
            self.assertIn("设备识别", health_check_interpretation(result, output))
            self.assertIn("累计通电时间   [数据] 956 小时", health_check_interpretation(result, output))
            self.assertIn("磁头剩余寿命   [未提供]", health_check_interpretation(result, output))
            self.assertGreaterEqual(len(dialog.findChildren(QTextEdit)), 2)
        finally:
            dialog.close()

    def test_format_dialog_requires_confirmation_when_data_exists(self):
        dialog = FormatConfirmDialog("10WT044393", data_present=True)
        try:
            self.assertIn("已有数据", dialog.findChildren(QLabel)[0].text())
            self.assertFalse(dialog.force_checkbox.isChecked())
            dialog.volume_label_edit.setText("庭审录像1")
            self.assertEqual(dialog.volume_label_edit.text(), "庭审录像1")
            yes_button = dialog.findChildren(QDialogButtonBox)[0].button(QDialogButtonBox.Yes)
            self.assertFalse(yes_button.isEnabled())
            dialog.force_checkbox.setChecked(True)
            self.assertTrue(yes_button.isEnabled())
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
