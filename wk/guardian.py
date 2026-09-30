"""Read-only PC health checks with a baseline for new startup and GPU entries."""

import json
import shutil
import subprocess
import threading
import time
import psutil

from . import config


DRIVES = ("C:\\", "D:\\", "F:\\", "G:\\")
ALERT_COOLDOWN = 6 * 3600


def baseline_path():
    return config.DATA_DIR / "guardian_baseline.json"


def startup_names():
    """Read Run-entry names only, never their potentially sensitive command lines."""
    import winreg

    names = set()
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_name in (r"Software\Microsoft\Windows\CurrentVersion\Run",
                         r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ) as key:
                    for index in range(winreg.QueryInfoKey(key)[1]):
                        name = winreg.EnumValue(key, index)[0]
                        names.add(f"{hive}:{name}")
            except OSError:
                continue
    return names


def disk_warnings():
    warnings = []
    for drive in DRIVES:
        try:
            usage = psutil.disk_usage(drive)
        except OSError:
            continue
        free_gb = usage.free / (1024 ** 3)
        if usage.percent >= 90 or free_gb < 15:
            warnings.append(f"{drive} is {usage.percent:.0f}% full ({free_gb:.1f} GiB free)")
    return warnings


def physical_disk_warnings():
    """Surface Windows Storage health when it explicitly says Warning or Unhealthy."""
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
           "Get-PhysicalDisk | Select-Object FriendlyName,HealthStatus | ConvertTo-Json -Compress"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else []
        rows = data if isinstance(data, list) else [data]
        return [f"Disk {row.get('FriendlyName', '(unnamed)')}: Windows reports {row['HealthStatus']}"
                for row in rows if isinstance(row, dict) and row.get("HealthStatus") in ("Warning", "Unhealthy")]
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return []


def gpu_compute_processes():
    """Return compute PID/name/VRAM; this is a new-owner notice, not a malware verdict."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        result = subprocess.run([exe, "--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            return {}
        found = {}
        for line in result.stdout.splitlines():
            try:
                pid_text, mb_text = [part.strip() for part in line.split(",", 1)]
                pid, mb = int(pid_text), int(mb_text)
                if mb >= 1024:
                    found[str(pid)] = {"name": psutil.Process(pid).name(), "mb": mb}
            except (ValueError, psutil.Error):
                continue
        return found
    except (OSError, subprocess.TimeoutExpired):
        return {}


class Guardian:
    def __init__(self):
        self.last_alert = {}
        self._lock = threading.Lock()

    def check(self, now=None):
        with self._lock:
            return self._check(now)

    def _check(self, now=None):
        """Return fresh alerts; baseline the first observation without claiming it is new."""
        now = time.time() if now is None else now
        current = {"startup": sorted(startup_names()), "gpu": gpu_compute_processes()}
        target = baseline_path()
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        target.write_text(json.dumps(current, indent=2), encoding="utf-8")
        candidates = disk_warnings() + physical_disk_warnings()
        if old:
            for name in sorted(set(current["startup"]) - set(old.get("startup", []))):
                candidates.append(f"New Windows startup entry: {name.split(':', 1)[-1]}")
            for pid, item in current["gpu"].items():
                if pid not in old.get("gpu", {}):
                    candidates.append(f"New GPU compute process: {item['name']} (PID {pid}, {item['mb']} MiB)")
        fresh = []
        for item in candidates:
            if now - self.last_alert.get(item, -ALERT_COOLDOWN) >= ALERT_COOLDOWN:
                fresh.append(item)
                self.last_alert[item] = now
        return fresh
