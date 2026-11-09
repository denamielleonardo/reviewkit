"""ReviewKit entry point: local API server + desktop window."""
import os
import socket
import sys
import threading
import time
import webbrowser

from .paths import data_dir, resource_dir


def _ensure_std_streams() -> None:
    """A windowed (no-console) exe starts with sys.stdout/stderr = None, which breaks
    uvicorn's logging and anything else that writes to them. Send them to a log file."""
    if sys.stdout is None or sys.stderr is None:
        log = open(data_dir() / "reviewkit.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log


_ensure_std_streams()

import uvicorn  # noqa: E402  (must come after the stream fix)

from . import api  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int) -> uvicorn.Server:
    config = uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio",
                            log_config=None)  # no colour formatter that probes isatty()
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server


def main() -> None:
    port = int(os.environ.get("REVIEWKIT_PORT") or 0) or _free_port()
    server = _serve(port)
    url = f"http://127.0.0.1:{port}/"

    if "--browser" in sys.argv or "--server-only" in sys.argv:  # --server-only: headless (CI smoke test)
        if "--browser" in sys.argv:
            webbrowser.open(url)
        print(f"ReviewKit running at {url} (Ctrl+C to quit)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    _unblock_own_files()
    try:
        import webview

        width, height = _window_size(webview)
        window = webview.create_window("ReviewKit", url, width=width, height=height, min_size=(480, 520))
        api.WINDOW["ref"] = window
        webview.start()
        server.should_exit = True
        return
    except Exception:  # noqa: BLE001  window could not open: use the default browser instead
        import traceback

        traceback.print_exc()  # goes to %APPDATA%\ReviewKit\reviewkit.log
        api.WINDOW["ref"] = None
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def _window_size(webview) -> tuple[int, int]:
    """1280x840, shrunk to fit small or scaled screens (the UI is responsive below that)."""
    try:
        screen = webview.screens[0]
        return min(1280, int(screen.width * 0.9)), min(840, int(screen.height * 0.88))
    except Exception:  # noqa: BLE001
        return 1280, 840


def _unblock_own_files() -> None:
    """Remove the 'downloaded from the internet' mark (Zone.Identifier) from the app's own files.

    Windows copies that mark onto every file extracted from a downloaded zip, and .NET then
    refuses to load pythonnet's Python.Runtime.dll, so the desktop window cannot start.
    """
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return
    roots = {os.path.dirname(sys.executable), str(resource_dir())}
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                try:
                    os.remove(os.path.join(dirpath, name) + ":Zone.Identifier")
                except OSError:
                    pass  # not marked (the usual case) or not removable


if __name__ == "__main__":
    main()
