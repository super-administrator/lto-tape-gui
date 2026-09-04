from __future__ import annotations

import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple


@dataclass
class CommandResult:
    ok: bool
    command: List[str]
    stdout: str
    stderr: str
    return_code: int


def device_list_has_device(output: str, device_id: str) -> bool:
    """Return whether ltfs device_list contains the requested hardware ID.

    IBM LTFS may return exit code 1 even when it prints a valid device list,
    so callers must not use the process exit code as the sole signal.
    """
    device_id = device_id.strip()
    if not output or not device_id:
        return False
    pattern = re.compile(
        r"Serial\s+Number\s*=\s*" + re.escape(device_id) + r"(?=\s|,|$)",
        re.IGNORECASE,
    )
    return any(pattern.search(line) for line in output.splitlines())


def health_check_media_not_ready(output: str) -> bool:
    """Detect a healthy drive with no cartridge ready for the TUR command."""
    normalized = output.lower()
    return (
        "test unit ready" in normalized
        and ("no medium" in normalized or bool(re.search(r"\b3a00\b", normalized)))
    )


def mounted_ltfs_device_id(source: str) -> str:
    """Extract the LTFS device ID from a /proc/mounts source field."""
    source = source.strip()
    return source[5:] if source.lower().startswith("ltfs:") else ""


def is_ltfs_mount_source(source: str, fstype: str) -> bool:
    return "ltfs" in fstype.lower() or bool(mounted_ltfs_device_id(source))


def is_unformatted_media(output: str) -> bool:
    """Recognize only explicit non-LTFS/unformatted-media mount failures."""
    normalized = output.lower()
    patterns = (
        "not formatted",
        "not partitioned for ltfs",
        "not formatted for ltfs",
        "non-ltfs",
        "non ltfs",
        "invalid ltfs label",
        "medium is not an ltfs",
    )
    return any(pattern in normalized for pattern in patterns)


class TapeCommandRunner:
    def __init__(self, command_timeout_sec: int = 180, backup_timeout_sec: int = 86400):
        self.command_timeout_sec = command_timeout_sec
        self.backup_timeout_sec = backup_timeout_sec
        self._proc_lock = threading.Lock()
        self._active_proc: Optional[subprocess.Popen] = None
        self._cancelled_pids: set[int] = set()
        self._mount_proc: Optional[subprocess.Popen] = None

    def _start_process(self, command: List[str], **kwargs) -> subprocess.Popen:
        # A dedicated session lets cancellation terminate every child spawned by a tool.
        return subprocess.Popen(command, start_new_session=True, **kwargs)

    def _set_active_proc(self, proc: Optional[subprocess.Popen]) -> None:
        with self._proc_lock:
            self._active_proc = proc

    def _was_cancelled(self, proc: subprocess.Popen) -> bool:
        with self._proc_lock:
            return proc.pid in self._cancelled_pids

    def _clear_cancelled(self, proc: subprocess.Popen) -> None:
        with self._proc_lock:
            self._cancelled_pids.discard(proc.pid)

    def _stop_process_group(self, proc: subprocess.Popen, force: bool = False) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass

    def cancel_current(self) -> bool:
        with self._proc_lock:
            proc = self._active_proc
            if proc is not None:
                self._cancelled_pids.add(proc.pid)
        if proc is None or proc.poll() is not None:
            return False
        self._stop_process_group(proc)
        return True

    def _result_from_process(
        self,
        proc: subprocess.Popen,
        command: List[str],
        stdout: str,
        stderr: str,
        timed_out: bool = False,
    ) -> CommandResult:
        if self._was_cancelled(proc):
            return CommandResult(False, command, stdout.strip(), "Command cancelled by user.", 130)
        if timed_out:
            return CommandResult(
                False,
                command,
                stdout.strip(),
                f"Command timed out after the configured limit.\n{stderr.strip()}".strip(),
                124,
            )
        return CommandResult(proc.returncode == 0, command, stdout.strip(), stderr.strip(), proc.returncode)

    def run(self, command: List[str], timeout_sec: Optional[int] = None) -> CommandResult:
        timeout_sec = self.command_timeout_sec if timeout_sec is None else timeout_sec
        try:
            proc = self._start_process(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as exc:
            return CommandResult(False, command, "", str(exc), 127)
        except Exception as exc:
            return CommandResult(False, command, "", str(exc), 1)

        self._set_active_proc(proc)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout_sec)
                return self._result_from_process(proc, command, stdout or "", stderr or "")
            except subprocess.TimeoutExpired:
                self._stop_process_group(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    self._stop_process_group(proc, force=True)
                    stdout, stderr = proc.communicate()
                return self._result_from_process(proc, command, stdout or "", stderr or "", timed_out=True)
        finally:
            self._set_active_proc(None)
            self._clear_cancelled(proc)
            if proc.stdout is not None:
                proc.stdout.close()

    def run_stream(
        self,
        command: List[str],
        line_cb: Optional[Callable[[str], None]] = None,
        progress_cb: Optional[Callable[[int], None]] = None,
        timeout_sec: Optional[int] = None,
    ) -> CommandResult:
        timeout_sec = self.backup_timeout_sec if timeout_sec is None else timeout_sec
        try:
            proc = self._start_process(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            return CommandResult(False, command, "", str(exc), 127)
        except Exception as exc:
            return CommandResult(False, command, "", str(exc), 1)

        self._set_active_proc(proc)
        out_lines: List[str] = []
        percent_re = re.compile(r"(\d{1,3})%")
        start = time.monotonic()
        timed_out = False

        def handle_line(line: str) -> None:
            line = line.rstrip("\n")
            if not line:
                return
            out_lines.append(line)
            if line_cb:
                line_cb(line)
            if progress_cb:
                match = percent_re.search(line)
                if match:
                    progress_cb(max(0, min(100, int(match.group(1)))))

        try:
            if proc.stdout is not None:
                while proc.poll() is None:
                    if time.monotonic() - start > timeout_sec:
                        timed_out = True
                        self._stop_process_group(proc)
                        handle_line(f"Command timed out after {timeout_sec} seconds.")
                        break
                    ready, _, _ = select.select([proc.stdout], [], [], 0.5)
                    if ready:
                        handle_line(proc.stdout.readline())

                if proc.poll() is None:
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._stop_process_group(proc, force=True)
                        proc.wait()

                for line in proc.stdout:
                    handle_line(line)

            proc.wait()
            return self._result_from_process(proc, command, "\n".join(out_lines), "", timed_out=timed_out)
        finally:
            self._set_active_proc(None)
            self._clear_cancelled(proc)
            if proc.stdout is not None:
                proc.stdout.close()

    def mount_info(self, mount_point: str) -> Tuple[bool, str, str]:
        resolved = str(Path(mount_point).resolve())
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    source, target, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
                    if target == resolved:
                        return True, fstype, source
        except OSError:
            return False, "", ""
        return False, "", ""

    def is_ltfs_mounted(self, mount_point: str) -> bool:
        mounted, fstype, source = self.mount_info(mount_point)
        return mounted and is_ltfs_mount_source(source, fstype)

    def is_path_on_ltfs(self, path: str) -> bool:
        candidate = Path(path).resolve()
        for current in (candidate, *candidate.parents):
            if self.is_ltfs_mounted(str(current)):
                return True
        return False

    def list_ltfs_devices(self) -> CommandResult:
        return self.run(["ltfs", "-o", "device_list"])

    def mount_ltfs(self, device_id: str, mount_point: str, wait_timeout_sec: int = 60) -> CommandResult:
        if self.is_ltfs_mounted(mount_point):
            return CommandResult(False, ["ltfs"], "", "Mount point is already in use.", 1)
        Path(mount_point).mkdir(parents=True, exist_ok=True)
        command = ["ltfs", "-o", f"devname={device_id}", mount_point]
        log_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        try:
            proc = self._start_process(command, stdout=log_file, stderr=subprocess.STDOUT)
        except FileNotFoundError as exc:
            log_file.close()
            return CommandResult(False, command, "", str(exc), 127)
        except Exception as exc:
            log_file.close()
            return CommandResult(False, command, "", str(exc), 1)

        with self._proc_lock:
            self._active_proc = proc
            self._mount_proc = proc

        deadline = time.monotonic() + wait_timeout_sec
        try:
            while time.monotonic() < deadline:
                if self._was_cancelled(proc):
                    return CommandResult(False, command, "", "Mount cancelled by user.", 130)
                if self.is_ltfs_mounted(mount_point):
                    return CommandResult(True, command, "LTFS mount is ready.", "", 0)
                time.sleep(0.5)

            self._stop_process_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._stop_process_group(proc, force=True)
                proc.wait()
            if proc.poll() is not None:
                log_file.flush()
                log_file.seek(0)
                details = log_file.read().strip()
                return CommandResult(
                    False,
                    command,
                    details,
                    "LTFS exited before the mount became ready.",
                    proc.returncode,
                )
            return CommandResult(False, command, "", f"LTFS mount did not become ready within {wait_timeout_sec} seconds.", 124)
        finally:
            log_file.close()
            self._set_active_proc(None)
            self._clear_cancelled(proc)
            if not self.is_ltfs_mounted(mount_point):
                with self._proc_lock:
                    if self._mount_proc is proc:
                        self._mount_proc = None

    def stop_managed_mount(self) -> None:
        with self._proc_lock:
            proc = self._mount_proc
            self._mount_proc = None
        if proc is not None and proc.poll() is None:
            self._stop_process_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._stop_process_group(proc, force=True)
                proc.wait()

    def release_device(self, device_id: str, mount_point: str, mode: str = "auto") -> CommandResult:
        with_mount = ["ltfs", "-o", f"devname={device_id}", "-o", "release_device", mount_point]
        without_mount = ["ltfs", "-o", f"devname={device_id}", "-o", "release_device"]
        if mode == "with_mount_point":
            return self.run(with_mount)
        if mode == "without_mount_point":
            return self.run(without_mount)
        first = self.run(with_mount)
        if first.ok:
            return first
        second = self.run(without_mount)
        if second.ok:
            return second
        return CommandResult(False, with_mount, "\n".join(x for x in [first.stdout, second.stdout] if x), "\n".join(x for x in [first.stderr, second.stderr] if x), second.return_code)

    def unmount(self, mount_point: str) -> CommandResult:
        result = self.run(["umount", mount_point])
        if result.ok or result.return_code == 32:
            self.stop_managed_mount()
        return result

    def format_ltfs(
        self,
        device_id: str,
        force: bool = False,
        volume_label: str = "",
    ) -> CommandResult:
        command = ["mkltfs", "-d", device_id]
        volume_label = volume_label.strip()
        if volume_label:
            command.extend(["-n", volume_label])
        if force:
            command.append("-f")
        return self.run(command)

    def probe_tape_contents(
        self,
        device_id: str,
        mount_point: str,
        wait_timeout_sec: int = 60,
        release_mode: str = "auto",
    ) -> CommandResult:
        """Temporarily mount LTFS and report whether the root contains entries."""
        command = ["ltfs", "-o", f"devname={device_id}", mount_point]
        mounted = self.mount_ltfs(device_id, mount_point, wait_timeout_sec=wait_timeout_sec)
        if not mounted.ok:
            diagnostic = "\n".join(part for part in (mounted.stdout, mounted.stderr) if part)
            if is_unformatted_media(diagnostic):
                return CommandResult(
                    True,
                    command,
                    "TAPE_CONTENT_STATE=unformatted\n磁带尚未格式化为 LTFS，可继续格式化。\n" + diagnostic,
                    "",
                    0,
                )
            return CommandResult(
                False,
                command,
                mounted.stdout,
                "无法安全读取磁带内容；已阻止格式化。\n" + mounted.stderr,
                mounted.return_code,
            )

        try:
            entries = list(Path(mount_point).iterdir())
            state = "data" if entries else "empty"
            output = f"TAPE_CONTENT_STATE={state}\n根目录项目数={len(entries)}"
            return CommandResult(True, command, output, "", 0)
        except OSError as exc:
            return CommandResult(False, command, "", f"读取磁带目录失败：{exc}", 1)
        finally:
            unmount_result = self.unmount(mount_point)
            if not unmount_result.ok and unmount_result.return_code != 32:
                self.stop_managed_mount()
            self.release_device(device_id, mount_point, mode=release_mode)

    def retension_tape(self, tape_device: str = "/dev/nst0") -> CommandResult:
        # Retension is the maintenance cycle used after long-term storage.
        return self.run(["mt", "-f", tape_device, "retension"])

    def eject_tape(self, tape_device: str = "/dev/st0") -> CommandResult:
        # Linux mt's offline operation unloads the cartridge and may rewind first.
        return self.run(["mt", "-f", tape_device, "offline"])

    def health_check(self, diagnostic_device: str = "/dev/sg1") -> CommandResult:
        # ITDT read-only mode collects drive information without writing or unloading media.
        command = [
            "itdt", "-f", diagnostic_device, "-w", "2",
            "devinfo", "tur", "runtimeinfo", "devicestatistics", "reqsense",
        ]
        attempts = []
        for attempt in range(3):
            result = self.run(command)
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
            attempts.append(f"Health check attempt {attempt + 1}/3:\n{output}".strip())
            if result.ok or not health_check_media_not_ready(output) or attempt == 2:
                if len(attempts) > 1:
                    result.stdout = "\n\n".join(attempts)
                    result.stderr = ""
                return result
            time.sleep(2)
        return result

    def backup_rsync(
        self,
        source_dir: str,
        target_dir: str,
        line_cb: Optional[Callable[[str], None]] = None,
        progress_cb: Optional[Callable[[int], None]] = None,
        inplace: bool = False,
        ignore_existing: bool = False,
    ) -> CommandResult:
        command = ["rsync", "-avh"]
        if inplace:
            command.append("--inplace")
        if ignore_existing:
            command.append("--ignore-existing")
        command.extend(["--info=progress2", source_dir, target_dir])
        return self.run_stream(command, line_cb, progress_cb)

    def backup_ordered_copy(self, source_dir: str, target_dir: str, line_cb: Optional[Callable[[str], None]] = None, progress_cb: Optional[Callable[[int], None]] = None) -> CommandResult:
        # -a also preserves attributes; LTFS on this host rejects that xattr operation.
        return self.run_stream(["ltfs_ordered_copy", "-r", source_dir, target_dir], line_cb, progress_cb)

    def backup_queue(
        self,
        sources: List[str],
        target_dir: str,
        ordered_copy: bool = False,
        line_cb: Optional[Callable[[str], None]] = None,
        progress_cb: Optional[Callable[[int], None]] = None,
        inplace: bool = False,
        ignore_existing: bool = False,
    ) -> CommandResult:
        """Back up each source in order and stop the queue on the first failure."""
        if not sources:
            return CommandResult(False, ["backup-queue"], "", "No backup sources selected.", 2)

        outputs: List[str] = []
        total = len(sources)
        for index, source in enumerate(sources):
            marker = f"备份队列 [{index + 1}/{total}] 开始: {source}"
            outputs.append(marker)
            if line_cb:
                line_cb(marker)

            def item_progress(value: int, item_index: int = index) -> None:
                if progress_cb:
                    progress_cb(int(((item_index + value / 100) / total) * 100))

            if ordered_copy:
                result = self.backup_ordered_copy(source, target_dir, line_cb, item_progress)
            else:
                result = self.backup_rsync(
                    source,
                    target_dir,
                    line_cb,
                    item_progress,
                    inplace=inplace,
                    ignore_existing=ignore_existing,
                )

            if result.stdout:
                outputs.append(result.stdout)
            if not result.ok:
                failure = f"备份队列 [{index + 1}/{total}] 失败，后续项目未执行: {source}"
                if line_cb:
                    line_cb(failure)
                return CommandResult(
                    False,
                    ["backup-queue", "ordered-copy" if ordered_copy else "rsync"],
                    "\n".join(outputs),
                    "\n".join(part for part in (failure, result.stderr) if part),
                    result.return_code,
                )

            completed = f"备份队列 [{index + 1}/{total}] 完成: {source}"
            outputs.append(completed)
            if line_cb:
                line_cb(completed)
            if progress_cb:
                progress_cb(int(((index + 1) / total) * 100))

        return CommandResult(
            True,
            ["backup-queue", "ordered-copy" if ordered_copy else "rsync"],
            "\n".join(outputs),
            "",
            0,
        )

    def estimate_source_bytes(self, source_dir: str) -> int:
        source = Path(source_dir)
        if source.is_file():
            return source.stat().st_size
        total = 0
        for path in source.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total

    def mount_free_bytes(self, mount_point: str) -> int:
        return shutil.disk_usage(mount_point).free
