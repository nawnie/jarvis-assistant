"""Jarvis Assistant - an always-on, local AI assistant that lives in the system tray.

Run with pythonw (no console). Flags:
  --hidden   start straight into the tray (used by start-with-Windows)

Only ONE Jarvis ever runs: launching it again just brings the running window to
the front (see wk/instance.py). The shortcuts start it with the plain Python
interpreter, so Jarvis is a single process rather than a venv launcher + child.
"""
import ctypes
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Use the project's own packages even when started by the plain Python interpreter.
# (The venv's pythonw.exe is only a launcher that starts a second process; going
# direct keeps Jarvis to exactly one process in Task Manager.)
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
VENV_SITE = APP_DIR / ".venv" / "Lib" / "site-packages"
if sys.prefix == sys.base_prefix and VENV_SITE.is_dir():
    import site
    site.addsitedir(str(VENV_SITE))
sys.path.insert(0, str(APP_DIR))

from wk import instance  # noqa: E402  (must come before anything that opens a window)

# Windows groups taskbar buttons by "App User Model ID". Without our own ID the
# window is filed under pythonw.exe and the taskbar shows Python's logo instead
# of Jarvis's icon. This must be set before the first window is created.
APP_ID = "Shawn.JarvisAssistant"


def hand_over_to_running_jarvis():
    """Another Jarvis owns the lock: ask it to show its window, then this copy exits.
    Retries for a few seconds in case that Jarvis is itself still starting up."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    QCoreApplication(sys.argv)
    deadline = time.time() + 8
    while time.time() < deadline:
        sock = QLocalSocket()
        sock.connectToServer(instance.SHOW_SERVER)
        if sock.waitForConnected(300):
            sock.write(b"show")
            sock.waitForBytesWritten(500)
            sock.disconnectFromServer()
            return
        time.sleep(0.3)


def main():
    # this is the single-instance section: the lock is taken before any window can exist
    if not instance.claim():
        hand_over_to_running_jarvis()
        return 0

    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication

    from wk import config
    from wk.brain import Engine
    from wk.ui import MainWindow, Tray, make_icon, save_ico

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass  # not on Windows: nothing to fix
    app = QApplication(sys.argv)
    app.setApplicationName("Jarvis Assistant")
    app.setWindowIcon(make_icon(True))
    app.setQuitOnLastWindowClosed(False)  # closing the window leaves it running in the tray

    # later launches connect here to say "show your window" (we hold the lock, so the name is ours)
    QLocalServer.removeServer(instance.SHOW_SERVER)
    server = QLocalServer()
    server.listen(instance.SHOW_SERVER)

    # the .ico used by the desktop/startup shortcuts is drawn from the same code as the tray icon,
    # rewritten each start so a design change never leaves a stale shortcut icon behind
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    save_ico(config.ICON_PATH)

    engine = Engine()
    window = MainWindow(engine)
    tray = Tray(engine, window, app)
    tray.show()
    app.aboutToQuit.connect(engine.shutdown)  # release the global mouse hook on Quit

    # the phone companion reaches Jarvis through this loopback-only API (see wk/remote_api.py)
    if engine.cfg["remote_api_enabled"]:
        from wk.remote_api import RemoteAPI
        remote = RemoteAPI(engine, int(engine.cfg["remote_api_port"]))
        try:
            remote.start()
            app.aboutToQuit.connect(remote.stop)
        except OSError as exc:
            engine.store.add_event("error", f"Phone access (local API) could not start: {exc}")
    server.newConnection.connect(lambda: (server.nextPendingConnection(), tray.open_window()))

    if "--hidden" not in sys.argv:
        window.show()
        window.play_intro()   # the HUD start-up sequence (~1.7 s, click-through)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
