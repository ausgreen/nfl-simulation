"""Where the worker's files go. Every publisher has put(path, data, content_type, max_age); `path` is relative
(index.html, games.json, status.json, state/<event>.json) and `max_age` is the CDN/browser cache time in seconds.

LocalDir writes to a directory (development; serve it with `python -m http.server -d <dir>`). S3Compatible writes
to a Cloudflare R2 (or AWS S3) bucket, served by the CDN at nfl.austingreen.net (docs/deploy.md); it needs the
`hosted` extra (boto3): `uv sync --extra hosted`.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
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


class S3Compatible:
    """An S3 bucket: Cloudflare R2 by default. The CDN takes each file's cache time from its Cache-Control header."""

    def __init__(self, bucket: str, endpoint: str | None, key_id: str, secret: str, client=None):
        self.bucket = bucket
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3", endpoint_url=endpoint, aws_access_key_id=key_id, aws_secret_access_key=secret,
                region_name="auto" if endpoint else None,
                config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 3, "mode": "standard"},
                              # R2 doesn't need boto3's default checksums on every upload
                              request_checksum_calculation="when_required",
                              response_checksum_validation="when_required"))
        self.s3 = client

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ, client=None) -> "S3Compatible":
        """R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_ACCOUNT_ID (the endpoint is the account's R2
        endpoint; S3_ENDPOINT overrides it, e.g. for AWS)."""
        missing = [k for k in ("R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY") if not env.get(k)]
        if not (env.get("R2_ACCOUNT_ID") or env.get("S3_ENDPOINT")):
            missing.append("R2_ACCOUNT_ID")
        if missing:
            raise SystemExit(f"--publish r2 needs these environment variables: {', '.join(missing)}")
        endpoint = env.get("S3_ENDPOINT") or f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
        return cls(env["R2_BUCKET"], endpoint, env["R2_ACCESS_KEY_ID"], env["R2_SECRET_ACCESS_KEY"], client)

    def put(self, path: str, data: bytes, content_type: str, max_age: int) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=path, Body=data, ContentType=content_type,
                           CacheControl=f"public, max-age={max_age}" if max_age else "no-cache")
