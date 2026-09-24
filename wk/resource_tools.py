"""Bounded, user-directed process inspection and termination for Jarvis."""
import os
import re
import subprocess
import time

import psutil


PROTECTED = {"system", "registry", "idle", "smss.exe", "csrss.exe", "wininit.exe",
             "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe", "explorer.exe"}


def _gpu_memory():
    """Compute-process VRAM if the installed driver exposes it; Windows may omit WDDM apps."""
    try:
        result = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            return {}
        found = {}
        for line in result.stdout.splitlines():
            match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", line)
            if match:
                found[int(match.group(1))] = int(match.group(2))
        return found
    except (OSError, subprocess.TimeoutExpired):
        return {}


def heavy_processes(limit=12):
    """Snapshot RAM, CPU and exposed compute VRAM without command lines or private arguments."""
    found = []
    for proc in psutil.process_iter(["pid", "name", "memory_info", "create_time"]):
        try:
            proc.cpu_percent(interval=None)
        except (psutil.Error, OSError):
            continue
    time.sleep(0.12)
    gpu = _gpu_memory()
    for proc in psutil.process_iter(["pid", "name", "memory_info", "create_time"]):
        try:
            info = proc.info
            if info["pid"] == os.getpid():
                continue
            rss = info["memory_info"].rss if info["memory_info"] else 0
            cpu = proc.cpu_percent(interval=None)
            found.append((rss, info["pid"], info["name"] or "unknown", info["create_time"] or 0.0,
                          round(cpu, 1), gpu.get(info["pid"])))
        except (psutil.Error, OSError, KeyError):
            continue
    found.sort(reverse=True)
    return [{"pid": pid, "name": name, "ram_mb": round(rss / 1048576), "created": created,
             "cpu_percent": cpu, "vram_mb": vram}
            for rss, pid, name, created, cpu, vram in found[:limit]]


def processes_text():
    lines = ["Top processes by RAM (read-only snapshot):"]
    for row in heavy_processes():
        vram = f"{row['vram_mb']} MiB" if row["vram_mb"] is not None else "unreported"
        lines.append(f"- {row['name']} PID {row['pid']}: {row['ram_mb']} MiB RAM, "
                     f"{row['cpu_percent']}% CPU, VRAM {vram}; identity {row['created']:.3f}")
    lines.append("To stop one, Shawn must send /stop PID IDENTITY using the exact values above. "
                 "Jarvis never stops a foreign process from a model suggestion alone.")
    return "\n".join(lines)


def terminate_selected(pid, created):
    """Stop only the exact noncritical process Shawn identified by PID and start time."""
    pid = int(pid)
    created = float(created)
    if pid <= 4 or pid == os.getpid():
        raise ValueError("that process is protected")
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - created) > 0.005:
            raise ValueError("process identity changed; refresh /processes")
        name = proc.name()
        if name.lower() in PROTECTED:
            raise ValueError("that system process is protected")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            return f"Sent a stop request to {name} (PID {pid}); it is still running. No force kill was attempted."
        return f"Stopped {name} (PID {pid}) at Shawn's explicit request."
    except psutil.NoSuchProcess:
        return f"PID {pid} already exited."
    except (psutil.AccessDenied, psutil.ZombieProcess) as exc:
        raise ValueError(f"cannot stop PID {pid}: {exc.__class__.__name__}") from None


def stop_command(text):
    match = re.fullmatch(r"/stop\s+(\d+)\s+(\d+(?:\.\d+)?)", text.strip(), re.I)
    if not match:
        return "Use /processes, then send /stop PID IDENTITY using the values shown."
    try:
        return terminate_selected(match.group(1), match.group(2))
    except ValueError as exc:
        return str(exc)
