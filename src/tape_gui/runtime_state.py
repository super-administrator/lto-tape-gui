from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class RuntimeHours:
    poh: Optional[int] = None
    mmh: Optional[int] = None
    boot_id: str = ""
    uptime_seconds: float = 0.0
    calibrated_at: str = ""


def read_boot_context() -> tuple[str, float]:
    """Return the current Linux boot ID and uptime without failing on other hosts."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        uptime_seconds = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        return boot_id, uptime_seconds
    except (OSError, ValueError, IndexError):
        return "", 0.0


def load_runtime_hours(path: Path) -> RuntimeHours:
    try:
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
        return RuntimeHours(
            poh=int(values["poh"]) if values.get("poh", "").isdigit() else None,
            mmh=int(values["mmh"]) if values.get("mmh", "").isdigit() else None,
            boot_id=values.get("boot_id", ""),
            uptime_seconds=float(values.get("uptime_seconds", "0")),
            calibrated_at=values.get("calibrated_at", ""),
        )
    except (OSError, ValueError):
        return RuntimeHours()


def save_runtime_hours(path: Path, hours: RuntimeHours) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            "# LTO runtime counters; updated after a successful ITDT health check.",
            f"poh={'' if hours.poh is None else hours.poh}",
            f"mmh={'' if hours.mmh is None else hours.mmh}",
            f"boot_id={hours.boot_id}",
            f"uptime_seconds={hours.uptime_seconds:.3f}",
            f"calibrated_at={hours.calibrated_at}",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")


def calibrated_runtime_hours(poh: Optional[str], mmh: Optional[str]) -> RuntimeHours:
    boot_id, uptime_seconds = read_boot_context()
    return RuntimeHours(
        poh=int(poh) if poh and poh.isdigit() else None,
        mmh=int(mmh) if mmh and mmh.isdigit() else None,
        boot_id=boot_id,
        uptime_seconds=uptime_seconds,
        calibrated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def estimated_poh(hours: RuntimeHours, boot_id: Optional[str] = None, uptime_seconds: Optional[float] = None) -> Optional[int]:
    if hours.poh is None:
        return None
    if boot_id is None or uptime_seconds is None:
        boot_id, uptime_seconds = read_boot_context()
    if not boot_id or boot_id != hours.boot_id:
        return hours.poh
    elapsed_seconds = max(0.0, uptime_seconds - hours.uptime_seconds)
    return hours.poh + int(elapsed_seconds // 3600)
