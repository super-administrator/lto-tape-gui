import tempfile
import unittest
from pathlib import Path

from tape_gui.runtime_state import RuntimeHours, estimated_poh, load_runtime_hours, save_runtime_hours


class RuntimeStateTests(unittest.TestCase):
    def test_poh_accumulates_only_within_the_same_boot(self):
        state = RuntimeHours(poh=956, mmh=311, boot_id="boot-a", uptime_seconds=3600)
        self.assertEqual(estimated_poh(state, "boot-a", 3 * 3600 + 3599), 958)
        self.assertEqual(estimated_poh(state, "boot-b", 10 * 3600), 956)
        self.assertEqual(state.mmh, 311)

    def test_runtime_hours_round_trip_uses_a_text_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_hours.txt"
            saved = RuntimeHours(956, 311, "boot-a", 123.5, "2026-09-02T00:00:00+00:00")
            save_runtime_hours(path, saved)
            self.assertIn("poh=956", path.read_text(encoding="utf-8"))
            self.assertEqual(load_runtime_hours(path), saved)
