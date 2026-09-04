import json
from pathlib import Path


class AppConfig:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.data = self._load()

    def _load(self) -> dict:
        with self.config_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    @property
    def device_id(self) -> str:
        return self.data.get("device_id", "")

    @property
    def mount_point(self) -> str:
        return self.data.get("mount_point", "/mnt/tape")

    @property
    def tape_device(self) -> str:
        return self.data.get("tape_device", "/dev/nst0")

    @property
    def eject_device(self) -> str:
        return self.data.get("eject_device", "/dev/st0")

    @property
    def diagnostic_device(self) -> str:
        return self.data.get("diagnostic_device", "/dev/sg0")

    @property
    def runtime_state_path(self) -> Path:
        default_path = self.config_path.parent.parent / "runtime_hours.txt"
        configured_path = Path(self.data.get("runtime_state_path", default_path))
        if configured_path.is_absolute():
            return configured_path
        return self.config_path.parent.parent / configured_path

    @property
    def default_backup_source(self) -> str:
        return self.data.get("default_backup_source", "/data")

    @property
    def use_ordered_copy_default(self) -> bool:
        return bool(self.data.get("use_ordered_copy_default", False))

    @property
    def use_inplace_default(self) -> bool:
        return bool(self.data.get("use_inplace_default", True))

    @property
    def use_ignore_existing_default(self) -> bool:
        return bool(self.data.get("use_ignore_existing_default", True))

    @property
    def command_timeout_sec(self) -> int:
        return int(self.data.get("command_timeout_sec", 180))

    @property
    def backup_timeout_sec(self) -> int:
        return int(self.data.get("backup_timeout_sec", 86400))

    @property
    def mount_wait_timeout_sec(self) -> int:
        return int(self.data.get("mount_wait_timeout_sec", 60))

    @property
    def release_mode(self) -> str:
        # auto | with_mount_point | without_mount_point
        return str(self.data.get("release_mode", "auto"))

    @property
    def minimum_free_bytes(self) -> int:
        # Leave safety space for LTFS index updates and capacity reporting variance.
        return int(self.data.get("minimum_free_bytes", 10 * 1024 * 1024 * 1024))
