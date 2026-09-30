"""Typed, host-enforced ADB access for one owner-authorized Android device.

This module does not infer device permission from model choice or a USB connection.
An owner-maintained local policy is required. The model never receives a generic
ADB command, local destination, SMS send call, or arbitrary settings key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
import ctypes
from pathlib import Path, PurePosixPath
from typing import Callable

from . import config


POLICY_PATH = config.DATA_DIR / "phone_access.v1.json"
ADB_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
SAFE_QUERY = re.compile(r"^[A-Za-z0-9 _.-]{1,80}$")
SERIAL = re.compile(r"^[A-Za-z0-9._:-]{4,80}$")
SETTINGS = {
    "screen_off_timeout": ("system", "screen_off_timeout", {"30000", "60000", "120000", "300000"}),
    "auto_rotate": ("system", "accelerometer_rotation", {"0", "1"}),
}


def _owner_confirm(device: str, recipient: str, body: str) -> bool:
    """Native owner decision outside the model/tool arguments; default is No."""
    if os.name != "nt":
        return False
    detail = (f"Device: {device}\nRecipient: {recipient}\n\nExact message:\n{body}\n\n"
              "Open this draft in Android Messages? Sending still requires your tap on the phone.")
    flags = 0x0004 | 0x0030 | 0x0100 | 0x40000  # Yes/No, warning, default No, topmost
    return ctypes.windll.user32.MessageBoxW(None, detail, "Jarvis phone draft confirmation", flags) == 6


def _owner_confirm_setting(device: str, name: str, value: str) -> bool:
    if os.name != "nt":
        return False
    detail = (f"Device: {device}\nSetting: {name}\nExact new value: {value}\n\n"
              "Apply this supported Android setting now?")
    flags = 0x0004 | 0x0030 | 0x0100 | 0x40000
    return ctypes.windll.user32.MessageBoxW(None, detail, "Jarvis phone setting confirmation", flags) == 6


class AdbTransport:
    def __init__(self, exe: Path = ADB_EXE):
        self.exe = exe

    def run(self, args: list[str], timeout: int = 15) -> bytes:
        if not self.exe.is_file():
            raise RuntimeError("ADB is unavailable on this PC")
        result = subprocess.run([str(self.exe), *args], capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            raise RuntimeError("ADB returned an error; inspect the device locally")
        return result.stdout


def _remote(path: str) -> str:
    if (not isinstance(path, str) or not path.startswith("/") or len(path) > 512
            or "//" in path or "\\" in path or any(not char.isprintable() for char in path)):
        raise ValueError("invalid Android path")
    parts = PurePosixPath(path).parts
    if any(part in (".", "..") for part in path.split("/")) or len(parts) < 3:
        raise ValueError("invalid Android path")
    return str(PurePosixPath(path))


class PhoneTools:
    def __init__(self, policy_path: Path = POLICY_PATH, transport: AdbTransport | None = None,
                 confirm: Callable[[str, str, str], bool] | None = None,
                 setting_confirm: Callable[[str, str, str], bool] | None = None):
        self.policy_path = policy_path
        self.transport = transport or AdbTransport()
        self.confirm = confirm or _owner_confirm
        self.setting_confirm = setting_confirm or _owner_confirm_setting
        self._drafts: dict[str, dict] = {}
        self._settings_pending: dict[str, dict] = {}
        self._draft_lock = threading.Lock()

    def _policy(self) -> tuple[str, list[str]]:
        try:
            policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise PermissionError("phone access is disabled until the owner configures a local policy") from error
        if not isinstance(policy, dict) or set(policy) != {"version", "serial", "selected_folders", "enabled"} or policy["version"] != 1 or policy["enabled"] is not True:
            raise PermissionError("phone access policy is not enabled")
        serial = policy["serial"]
        roots = policy["selected_folders"]
        if not isinstance(serial, str) or not SERIAL.fullmatch(serial) or not isinstance(roots, list) or not 1 <= len(roots) <= 5:
            raise PermissionError("phone access policy is invalid")
        validated = [_remote(root) for root in roots]
        if len(set(validated)) != len(validated):
            raise PermissionError("phone access policy contains duplicate roots")
        return serial, validated

    def _device(self) -> tuple[str, list[str]]:
        serial, roots = self._policy()
        output = self.transport.run(["devices"], timeout=8).decode("utf-8", "replace")
        devices = [line.split("\t", 1)[0] for line in output.splitlines() if "\tdevice" in line]
        if devices != [serial]:
            raise PermissionError("exactly the one authorized Android device must be connected")
        return serial, roots

    def _shell(self, serial: str, command: str, *args: str, timeout: int = 15) -> str:
        # Fixed command name plus POSIX-quoted, validated arguments. No model
        # supplied shell string or executable reaches this boundary.
        remote = " ".join([command, *(shlex.quote(arg) for arg in args)])
        return self.transport.run(["-s", serial, "shell", remote], timeout=timeout).decode("utf-8", "replace")

    def _selected(self, path: str, serial: str, roots: list[str]) -> str:
        target = _remote(path)
        # Android symlinks and /sdcard aliases are resolved on the device before
        # checking selected roots. A lexical prefix alone is insufficient.
        resolved = self._shell(serial, "realpath", target).strip()
        if not resolved.startswith("/") or "\n" in resolved:
            raise PermissionError("Android path could not be verified")
        resolved = _remote(resolved)
        root_real = [_remote(self._shell(serial, "realpath", root).strip()) for root in roots]
        if not any(resolved == root or resolved.startswith(root.rstrip("/") + "/") for root in root_real):
            raise PermissionError("Android path is outside selected folders")
        return resolved

    def list_folder(self, path: str) -> dict:
        serial, roots = self._device()
        selected = self._selected(path, serial, roots)
        output = self._shell(serial, "ls", "-1", selected)
        entries = [line[:160] for line in output.splitlines()[:200] if line and "/" not in line]
        return {"device": serial, "folder": selected, "entries": entries, "truncated": len(output.splitlines()) > 200}

    def search_names(self, folder: str, query: str) -> dict:
        serial, roots = self._device()
        selected = self._selected(folder, serial, roots)
        if not isinstance(query, str) or not SAFE_QUERY.fullmatch(query):
            raise ValueError("search must be a bounded file-name phrase")
        output = self._shell(serial, "find", selected, "-maxdepth", "3", "-type", "f", "-iname", f"*{query}*", timeout=20)
        matches = [line for line in output.splitlines() if line.startswith(selected.rstrip("/") + "/")][:100]
        return {"device": serial, "folder": selected, "matches": matches, "truncated": len(output.splitlines()) > 100}

    def read_text(self, path: str, max_bytes: int = 32768) -> dict:
        serial, roots = self._device()
        selected = self._selected(path, serial, roots)
        if type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
            raise ValueError("max_bytes must be 1-65536")
        command = "head -c " + str(max_bytes + 1) + " " + shlex.quote(selected)
        output = self.transport.run(["-s", serial, "exec-out", command], timeout=20)
        if len(output) > max_bytes:
            raise ValueError("phone file exceeds requested read limit")
        return {"device": serial, "file": selected, "bytes": len(output),
                "sha256": hashlib.sha256(output).hexdigest(), "text": output.decode("utf-8-sig")}

    def pull_file(self, path: str) -> dict:
        serial, roots = self._device()
        selected = self._selected(path, serial, roots)
        command = "head -c 2000001 " + shlex.quote(selected)
        output = self.transport.run(["-s", serial, "exec-out", command], timeout=30)
        if len(output) > 2_000_000:
            raise ValueError("phone file exceeds 2 MB pull limit")
        destination = config.DATA_DIR / "phone-pulls"
        destination.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(destination)):
            raise PermissionError("phone pull directory cannot be a link")
        name = PurePosixPath(selected).name[:80]
        final = destination / f"{hashlib.sha256(selected.encode()).hexdigest()[:16]}-{name}"
        fd, temp = tempfile.mkstemp(prefix=".phone-", dir=destination)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(output)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, final)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return {"device": serial, "source": selected, "saved_to": str(final),
                "bytes": len(output), "sha256": hashlib.sha256(output).hexdigest()}

    def compose_message(self, recipient: str, body: str) -> dict:
        serial, _ = self._device()
        if not isinstance(recipient, str) or not re.fullmatch(r"\+?[0-9]{7,15}", recipient):
            raise ValueError("recipient must be an exact phone number")
        if not isinstance(body, str) or not 1 <= len(body) <= 1000 or "\x00" in body:
            raise ValueError("message body must contain 1-1000 characters")
        draft_id = uuid.uuid4().hex
        with self._draft_lock:
            self._drafts = {key: draft for key, draft in self._drafts.items()
                            if draft["expires_at"] > time.time()}
            self._drafts[draft_id] = {"device": serial, "recipient": recipient, "body": body,
                                      "expires_at": time.time() + 120}
        return {"draft_id": draft_id, "device": serial, "recipient": recipient, "body": body,
                "state": "awaiting_explicit_owner_confirmation", "sent": False,
                "prepared_at": int(time.time())}

    def open_confirmed_draft(self, draft_id: str, device: str, recipient: str, body: str) -> dict:
        """Open Android's composer only after a fresh host-side human approval.

        The callback is supplied by trusted desktop UI code, never by the model.
        A model cannot turn an approval boolean into authority. Android still
        requires its own Send tap; this route never claims the SMS was sent.
        """
        if not isinstance(draft_id, str) or not re.fullmatch(r"[0-9a-f]{32}", draft_id):
            raise ValueError("invalid draft ID")
        with self._draft_lock:
            draft = self._drafts.pop(draft_id, None)  # one use, even when denied
        if draft is None or draft["expires_at"] <= time.time():
            raise PermissionError("message draft expired or was already reviewed")
        if (device, recipient, body) != (draft["device"], draft["recipient"], draft["body"]):
            raise PermissionError("message draft changed since preview")
        if self.confirm is None or not self.confirm(device, recipient, body) or draft["expires_at"] <= time.time():
            raise PermissionError("fresh owner confirmation was not granted")
        serial, _ = self._device()
        if serial != device:
            raise PermissionError("authorized device changed since preview")
        self._shell(serial, "am", "start", "-a", "android.intent.action.SENDTO",
                    "-d", "smsto:" + recipient, "--es", "sms_body", body)
        return {"device": serial, "recipient": recipient, "body": body,
                "state": "composer_opened_send_unconfirmed", "sent": False}

    def prepare_setting(self, name: str, value: str) -> dict:
        serial, _ = self._device()
        setting = SETTINGS.get(name)
        if setting is None or not isinstance(value, str) or value not in setting[2]:
            raise ValueError("setting/value pair is not in the supported allowlist")
        change_id = uuid.uuid4().hex
        with self._draft_lock:
            self._settings_pending = {key: change for key, change in self._settings_pending.items()
                                      if change["expires_at"] > time.time()}
            self._settings_pending[change_id] = {"device": serial, "name": name, "value": value,
                                                 "expires_at": time.time() + 120}
        return {"change_id": change_id, "device": serial, "setting": name, "value": value,
                "state": "awaiting_explicit_owner_confirmation", "applied": False}

    def apply_confirmed_setting(self, change_id: str, device: str, name: str, value: str) -> dict:
        if not isinstance(change_id, str) or not re.fullmatch(r"[0-9a-f]{32}", change_id):
            raise ValueError("invalid setting change ID")
        with self._draft_lock:
            change = self._settings_pending.pop(change_id, None)
        if change is None or change["expires_at"] <= time.time():
            raise PermissionError("setting change expired or was already reviewed")
        if (device, name, value) != (change["device"], change["name"], change["value"]):
            raise PermissionError("setting change differs from preview")
        if not self.setting_confirm(device, name, value) or change["expires_at"] <= time.time():
            raise PermissionError("fresh owner confirmation was not granted")
        serial, _ = self._device()
        if serial != device:
            raise PermissionError("authorized device changed since preview")
        setting = SETTINGS[name]
        self._shell(serial, "settings", "put", setting[0], setting[1], value)
        observed = self._shell(serial, "settings", "get", setting[0], setting[1]).strip()
        return {"device": serial, "setting": name, "requested": value,
                "observed": observed[:32], "verified": observed == value}
