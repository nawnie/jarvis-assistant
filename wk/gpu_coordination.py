"""Bounded GPU-start mutex shared by Jarvis processes in this Windows session.

This coordinates Jarvis's own Comfy submission and model/eyes startup. Other
applications do not participate; every caller must still check GPU telemetry
and Comfy queue state after acquiring it.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from contextlib import contextmanager

_thread_lock = threading.RLock()
_MUTEX_NAME = r"Local\JarvisAssistantGpuStart-v1"
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80


@contextmanager
def hold(timeout_seconds: float = 5):
    """Serialize GPU starts; raise promptly if another Jarvis operation owns it."""
    seconds = max(0.0, float(timeout_seconds))
    started = time.monotonic()
    if not _thread_lock.acquire(timeout=seconds):
        raise TimeoutError("another Jarvis GPU operation is in progress")
    handle = None
    owned = False
    kernel = None
    try:
        if os.name == "nt":
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
            kernel.ReleaseMutex.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.CreateMutexW(None, False, _MUTEX_NAME)
            if not handle:
                raise OSError(ctypes.get_last_error(), "could not create Jarvis GPU mutex")
            remaining_ms = max(0, int((seconds - (time.monotonic() - started)) * 1000))
            result = kernel.WaitForSingleObject(handle, remaining_ms)
            if result not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
                raise TimeoutError("another Jarvis GPU operation is in progress")
            owned = True
        yield
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        if handle:
            kernel.CloseHandle(handle)
        _thread_lock.release()
