"""ReviewKit entry point: local API server + desktop window."""
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import api


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int) -> uvicorn.Server:
    config = uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
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
