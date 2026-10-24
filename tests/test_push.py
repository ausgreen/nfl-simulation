"""Pushed updates: the hub streams each published JSON file to the pages that asked for it."""

import http.client
import json

from nflsim.hosted.publish import LocalDir
from nflsim.hosted.push import Hub, Tee


def _events(port, path, n):
    """The first n events of a stream, as (name, data)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream"
    out, name = [], None
    while len(out) < n:
        line = r.fp.readline().decode().rstrip("\n")
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            out.append((name, json.loads(line[6:])))
    conn.close()
    return out


def test_listeners_get_the_latest_copy_then_each_new_one_they_asked_for(tmp_path):
    hub = Hub(0, root=tmp_path, keepalive=0.2).start()
    try:
        pub = Tee(LocalDir(tmp_path), hub)
        pub.put("status.json", b'{"live":1}', "application/json", 0)
        pub.put("game.html", b"<html></html>", "text/html", 300)  # pages aren't pushed
        import threading
        import time

        def later():
            time.sleep(0.3)
            pub.put("summary.json", b'[{"event":"1"}]', "application/json", 3)  # not asked for
            pub.put("status.json", b'{"live":2}', "application/json", 0)
        threading.Thread(target=later).start()
        got = _events(hub.port, "/events?only=status.json", 3)
        files = [d for name, d in got if name == "file"]
        assert files[0] == {"path": "status.json", "body": {"live": 1}}
        assert {"path": "status.json", "body": {"live": 2}} in files
        assert all(d["path"] == "status.json" for d in files)
        assert ("ping", {}) in got  # keepalive while nothing changed
        # the same server serves the published files
        conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=5)
        conn.request("GET", "/game.html")
        assert conn.getresponse().read() == b"<html></html>"
        conn.request("GET", "/../secret")
        assert conn.getresponse().status == 404
    finally:
        hub.stop()
