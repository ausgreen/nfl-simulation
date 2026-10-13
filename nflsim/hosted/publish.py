"""Where the worker's files go. Every publisher has put(path, data, content_type, max_age); `path` is relative
(index.html, games.json, status.json, state/<event>.json) and `max_age` is the CDN/browser cache time in seconds.

LocalDir writes to a directory (development; serve it with `python -m http.server -d <dir>`). The S3-compatible
publisher for Cloudflare R2 / AWS S3 comes with deployment (docs/hosting-plan.md, phase 3).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# cache time per published path (first matching prefix wins)
MAX_AGE = (("state/", 3), ("games.json", 3), ("status.json", 0), ("index.html", 300))


def max_age(path: str) -> int:
    return next((s for prefix, s in MAX_AGE if path.startswith(prefix)), 60)


class LocalDir:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, path: str, data: bytes, content_type: str, max_age: int) -> None:
        """Write atomically (temp file + rename), so a server reading the file never sees half of it."""
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        except BaseException:
            os.unlink(tmp)
            raise
