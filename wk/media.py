"""Media awareness: what is playing right now (song / video title, artist, which app, playing or paused).

Source: Windows' own media controls (the same info as the volume flyout / lock screen), through
WinRT GlobalSystemMediaTransportControlsSessionManager - so it works for Spotify, browsers playing
YouTube, VLC, the Media Player app and anything else that reports to Windows. Read-only.

Uses:
  * "what song is this?" - chat and quick-ask get a one-line "Now playing" context;
  * break nudges wait while a video plays fullscreen (brain.decide_nudge reads ctx.media_playing);
  * the Now page shows the track under the focused app.

Cost: a background thread asks Windows every 10 s (a few milliseconds each time). Everyone else
reads the cached answer, so nothing here ever blocks the GUI or a chat reply.
"""
import asyncio
import threading
import time

POLL_SECONDS = 10
PLAYING = 4          # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING


async def _read():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    manager = await Manager.request_async()
    session = manager.get_current_session()
    if session is None:
        return None
    props = await session.try_get_media_properties_async()
    status = session.get_playback_info().playback_status
    return {"title": (props.title or "").strip(), "artist": (props.artist or "").strip(),
            "app": (session.source_app_user_model_id or "").split("!")[0],
            "playing": int(status) == PLAYING, "at": time.time()}


def read_now():
    """Ask Windows right now (blocking, ~ms). None if nothing is playing or the API is unavailable."""
    try:
        return asyncio.run(_read())
    except Exception:
        return None


class MediaWatch(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="jarvis-media")
        self.current = None

    def run(self):
        while True:
            self.current = read_now()
            time.sleep(POLL_SECONDS)


_watch = None
_start_lock = threading.Lock()


def current():
    """The cached 'now playing' dict (or None). Starts the watcher on first use."""
    global _watch
    with _start_lock:
        if _watch is None:
            _watch = MediaWatch()
            _watch.start()
    return _watch.current


def is_playing():
    now = current()
    return bool(now and now["playing"])


def describe(now=None):
    """'Blinding Lights - The Weeknd (Spotify), playing' - or '' when there's nothing to say."""
    now = now if now is not None else current()
    if not now or not now.get("title"):
        return ""
    app = now["app"].replace(".exe", "").split(".")[-1] if now.get("app") else ""
    who = f" - {now['artist']}" if now.get("artist") else ""
    return f"{now['title']}{who}" + (f" ({app})" if app else "") + (", playing" if now["playing"] else ", paused")


def context_line():
    """One line for the model's context, or '' (no noise when nothing is playing)."""
    text = describe()
    return f"Now playing on Shawn's PC (from Windows' media controls): {text}" if text else ""
