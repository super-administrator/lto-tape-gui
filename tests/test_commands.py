import sys
import threading
import time
import unittest
from unittest.mock import patch

from tape_gui.commands import (
    CommandResult,
    TapeCommandRunner,
    device_list_has_device,
    health_check_media_not_ready,
    is_unformatted_media,
    is_ltfs_mount_source,
    mounted_ltfs_device_id,
)


class TapeCommandRunnerTests(unittest.TestCase):
    def test_device_list_accepts_valid_output_with_nonzero_exit_code(self):
        output = (
            "Device Name = /dev/sg1, Vendor ID = IBM, Product ID = ULT3580-HH9, "
            "Serial Number = 10WT044393\n"
        )
        self.assertTrue(device_list_has_device(output, "10WT044393"))

    def test_device_list_does_not_match_another_serial_or_error_text(self):
        self.assertFalse(device_list_has_device("error: 10WT044393 not found", "10WT044393"))
        self.assertFalse(device_list_has_device("Serial Number = 10WT044394", "10WT044393"))

    def test_mounted_ltfs_device_id_is_read_from_mount_source(self):
        self.assertEqual(mounted_ltfs_device_id("ltfs:10WT044393"), "10WT044393")
        self.assertEqual(mounted_ltfs_device_id("/dev/sda1"), "")
        self.assertTrue(is_ltfs_mount_source("ltfs:10WT044393", "fuse"))
        self.assertFalse(is_ltfs_mount_source("/dev/sda1", "ext4"))

    def test_unformatted_media_is_distinguished_from_generic_mount_failure(self):
        self.assertTrue(is_unformatted_media("LTFS: medium is not formatted for LTFS"))
        self.assertTrue(is_unformatted_media("medium is not partitioned for LTFS"))
        self.assertFalse(is_unformatted_media("device is busy"))
        self.assertFalse(is_unformatted_media("medium consistency check failed"))

    def test_device_list_can_be_read_from_stderr(self):
        stderr = "Device Name = /dev/sg1, Serial Number = 10WT044393, Product Name = [ULT3580-HH9]."
        self.assertTrue(device_list_has_device(stderr, "10WT044393"))

    def test_tape_maintenance_commands_use_the_expected_devices(self):
        runner = TapeCommandRunner()
        with patch.object(runner, "run") as run:
            runner.retension_tape("/dev/nst0")
            run.assert_called_once_with(["mt", "-f", "/dev/nst0", "retension"])
            run.reset_mock()
            runner.eject_tape("/dev/st0")
            run.assert_called_once_with(["mt", "-f", "/dev/st0", "offline"])

    def test_ordered_copy_omits_unsupported_attribute_preservation(self):
        runner = TapeCommandRunner()
        with patch.object(runner, "run_stream") as run_stream:
            runner.backup_ordered_copy("/source", "/target")
            run_stream.assert_called_once_with(["ltfs_ordered_copy", "-r", "/source", "/target"], None, None)

    def test_format_can_set_volume_label(self):
        runner = TapeCommandRunner()
        with patch.object(runner, "run") as run:
            runner.format_ltfs("10WT044393", force=True, volume_label="庭审录像1")
            run.assert_called_once_with([
                "mkltfs", "-d", "10WT044393", "-n", "庭审录像1", "-f",
            ])

    def test_format_omits_optional_volume_label_when_empty(self):
        runner = TapeCommandRunner()
        with patch.object(runner, "run") as run:
            runner.format_ltfs("10WT044393", volume_label="  ")
            run.assert_called_once_with(["mkltfs", "-d", "10WT044393"])

    def test_rsync_uses_selected_incremental_options(self):
        runner = TapeCommandRunner()
        with patch.object(runner, "run_stream") as run_stream:
            run_stream.return_value = CommandResult(True, [], "", "", 0)
            runner.backup_rsync("/source", "/target", inplace=True, ignore_existing=True)
            self.assertEqual(
                run_stream.call_args.args[0],
                [
                    "rsync", "-avh", "--inplace", "--ignore-existing",
                    "--info=progress2", "/source", "/target",
                ],
            )

    def test_backup_queue_runs_in_order(self):
        runner = TapeCommandRunner()
        completed = CommandResult(True, ["rsync"], "ok", "", 0)
        with patch.object(runner, "backup_rsync", return_value=completed) as backup:
            result = runner.backup_queue(
                ["/source/one", "/source/two"],
                "/target",
                inplace=True,
                ignore_existing=True,
            )

        self.assertTrue(result.ok)
        self.assertEqual([call.args[0] for call in backup.call_args_list], ["/source/one", "/source/two"])

    def test_backup_queue_stops_after_first_failure(self):
        runner = TapeCommandRunner()
        failed = CommandResult(False, ["rsync"], "", "write failed", 5)
        with patch.object(runner, "backup_rsync", return_value=failed) as backup:
            result = runner.backup_queue(["/source/one", "/source/two"], "/target")

        self.assertFalse(result.ok)
        self.assertEqual(backup.call_count, 1)
        self.assertIn("后续项目未执行", result.stderr)

    def test_health_check_is_read_only_itdt_command(self):
        runner = TapeCommandRunner()
        with patch.object(runner, "run") as run:
            run.return_value = CommandResult(True, [], "ok", "", 0)
            runner.health_check("/dev/sg1")
            run.assert_called_once_with([
                "itdt", "-f", "/dev/sg1", "-w", "2",
                "devinfo", "tur", "runtimeinfo", "devicestatistics", "reqsense",
            ])

    def test_health_check_without_media_is_not_treated_as_unknown_output(self):
        output = "Issuing test unit ready... Operation FAILED with errno 5 Input/output error\n0000 3A00 3000"
        self.assertTrue(health_check_media_not_ready(output))
        self.assertFalse(health_check_media_not_ready("Issuing test unit ready... Input/output error"))
        self.assertFalse(health_check_media_not_ready("Issuing test unit ready... OK"))

    def test_stream_timeout_works_when_command_produces_no_output(self):
        runner = TapeCommandRunner(backup_timeout_sec=1)
        started = time.monotonic()

        result = runner.run_stream([sys.executable, "-c", "import time; time.sleep(30)"])

        self.assertEqual(result.return_code, 124)
        self.assertFalse(result.ok)
        self.assertLess(time.monotonic() - started, 8)

    def test_cancel_marks_running_stream_as_cancelled(self):
        runner = TapeCommandRunner(backup_timeout_sec=30)
        results = []

        thread = threading.Thread(
            target=lambda: results.append(
                runner.run_stream([sys.executable, "-c", "import time; time.sleep(30)"])
            )
        )
        thread.start()

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not runner.cancel_current():
            time.sleep(0.05)

        thread.join(timeout=8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].return_code, 130)
        self.assertIn("cancelled", results[0].stderr.lower())


if __name__ == "__main__":
    unittest.main()
