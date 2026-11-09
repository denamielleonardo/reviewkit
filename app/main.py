"""ReviewKit entry point: local API server + desktop window."""
import os
import socket
import sys
import threading
import time
import webbrowser

from .paths import data_dir


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

    try:
        import webview
    except Exception:  # pywebview unavailable: fall back to the browser
        webbrowser.open(url)
        while True:
            time.sleep(1)

    window = webview.create_window("ReviewKit", url, width=1280, height=840, min_size=(960, 640))
    api.WINDOW["ref"] = window
    webview.start()
    server.should_exit = True


if __name__ == "__main__":
    main()
