"""HTTPS requests that don't hang on an unreachable server address.

A host name usually resolves to several addresses (GitHub Pages has four). Python tries them in order
and gives each one the request's full timeout, so if the first address is unreachable from the
user's network every request waits 15-60 s before the next address is tried, and slow networks
turn that into failed updates. Here the addresses are tried a quarter of a second apart and the
first to connect is used; once connected, the request's normal timeout applies to the response.
"""
import http.client
import socket
import threading
import urllib.request

CONNECT_TIMEOUT = 3.0


STAGGER = 0.25  # start the next address if the previous one hasn't connected by then (like browsers do)


def _create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **_):
    """Connect to whichever of the host's addresses answers first; each gets CONNECT_TIMEOUT."""
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"Couldn't resolve {host}")
    limit = CONNECT_TIMEOUT
    if isinstance(timeout, (int, float)):
        limit = min(limit, timeout)
    done = threading.Event()
    lock = threading.Lock()
    result: dict = {"sock": None, "errors": [], "finished": 0}

    def attempt(info):
        family, kind, proto, _, addr = info
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(limit)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
        except OSError as e:
            sock.close()
            with lock:
                result["errors"].append(e)
                result["finished"] += 1
                if result["finished"] == len(infos):
                    done.set()
            return
        with lock:
            result["finished"] += 1
            if result["sock"] is None and not result.get("gave_up"):
                result["sock"] = sock
                done.set()
                return
        sock.close()  # another address won the race, or the caller already gave up

    for i, info in enumerate(infos):
        threading.Thread(target=attempt, args=(info,), daemon=True).start()
        if i < len(infos) - 1 and done.wait(STAGGER) and result["sock"] is not None:
            break
    done.wait(limit + 1)
    with lock:
        sock = result["sock"]
        if sock is None:
            result["gave_up"] = True
            raise result["errors"][-1] if result["errors"] else TimeoutError(f"Couldn't connect to {host}")
    sock.settimeout(None if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout)
    return sock


class _FastHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection


class _FastHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_FastHTTPSConnection, req, context=self._context)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_FastHTTPConnection, req)


_opener = urllib.request.build_opener(_HTTPSHandler(), _HTTPHandler())


def urlopen(req, timeout: float):
    """Like urllib.request.urlopen (proxies and HTTP errors behave the same), with fast fail-over."""
    return _opener.open(req, timeout=timeout)
