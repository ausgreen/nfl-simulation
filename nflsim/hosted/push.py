"""Push every published file to open pages as it changes (Server-Sent Events), instead of each page polling.

`Hub(port, root).start()` runs a small threaded HTTP server next to the worker:

    GET /events?only=state/<event>.json,kalshi.json,status.json
        text/event-stream. On connect, the latest copy of each listed file (every JSON file without `only`), then
        each new copy as the worker publishes it: `event: file`, data {"path", "body"}. `event: ping` every
        `keepalive` seconds, so the page knows the stream is alive and proxies don't close it.
    GET /<path>
        the file under `root` (the worker's --out directory), so pages and events share an origin when testing
        locally; 404 without `root`.

`Tee(publisher, hub)` is a publisher that also hands every JSON file to the hub. The pages fall back to polling
when /events isn't there (e.g. served from R2 with no route to the worker).
"""

from __future__ import annotations

import json
import mimetypes
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


class Hub:
    def __init__(self, port: int = 8787, root: Path | str | None = None, host: str = "127.0.0.1",
                 keepalive: float = 15.0, allow_origin: str | None = None):
        self.port, self.root, self.host, self.keepalive = port, Path(root) if root else None, host, keepalive
        self.allow_origin = allow_origin  # for pages on another origin (credentials are allowed)
        self.latest: dict[str, bytes] = {}
        self.subs: list[tuple[queue.Queue, set | None]] = []
        self.lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None

    def send(self, path: str, data: bytes) -> None:
        """A file was published: keep it and pass it to every listener that wants it."""
        with self.lock:
            self.latest[path] = data
            subs = list(self.subs)
        for q, only in subs:
            if only is None or path in only:
                q.put((path, data))

    def listeners(self) -> int:
        with self.lock:
            return len(self.subs)

    def start(self) -> "Hub":
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cors(self):
                if hub.allow_origin:
                    self.send_header("Access-Control-Allow-Origin", hub.allow_origin)
                    self.send_header("Access-Control-Allow-Credentials", "true")

            def do_GET(self):
                u = urlparse(self.path)
                if u.path.rstrip("/").endswith("/events") or u.path == "/events":
                    return self._events(parse_qs(u.query).get("only", [None])[-1])
                return self._file(unquote(u.path))

            def _file(self, path: str):
                if hub.root is None:
                    return self.send_error(404)
                f = (hub.root / path.lstrip("/")).resolve()
                if f.is_dir():
                    f = f / "index.html"
                if not f.is_relative_to(hub.root.resolve()) or not f.is_file():
                    return self.send_error(404)
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _events(self, only: str | None):
                wanted = {p for p in only.split(",") if p} if only else None
                q: queue.Queue = queue.Queue()
                with hub.lock:
                    first = [(p, d) for p, d in hub.latest.items() if wanted is None or p in wanted]
                    hub.subs.append((q, wanted))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self._cors()
                self.end_headers()
                try:
                    for p, d in first:
                        self._event("file", p, d)
                    while True:
                        try:
                            p, d = q.get(timeout=hub.keepalive)
                            self._event("file", p, d)
                        except queue.Empty:
                            self.wfile.write(b"event: ping\ndata: {}\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with hub.lock:
                        hub.subs = [s for s in hub.subs if s[0] is not q]

            def _event(self, name: str, path: str, data: bytes):
                # the body is compact JSON (one line), so it can go into the data field as is
                self.wfile.write(b"event: " + name.encode() + b"\ndata: {\"path\":" + json.dumps(path).encode()
                                 + b",\"body\":" + data + b"}\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="push-hub", daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class Tee:
    """A publisher that also hands each JSON file to a Hub."""

    def __init__(self, publisher, hub: Hub):
        self.publisher, self.hub = publisher, hub

    def put(self, path: str, data: bytes, content_type: str, max_age: int) -> None:
        self.publisher.put(path, data, content_type, max_age)
        if path.endswith(".json"):
            self.hub.send(path, data)

    def __getattr__(self, name):
        return getattr(self.publisher, name)
