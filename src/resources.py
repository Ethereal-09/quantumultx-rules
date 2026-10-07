"""Verified source cache and rollback of managed output writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urldefrag

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from qx_rules import BuildError, public_url


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def local_path(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise BuildError(f"路径超出项目目录: {value}")
    return path


def http_session():
    client = requests.Session()
    client.headers["User-Agent"] = "QuantumultX-Rules-Builder/2.0"
    client.mount("https://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])))
    return client


def download(client, url):
    public_url(url)
    # Fragment is a QX/parser parameter; it must not be sent to the HTTP server.
    # Adapter retries do not cover failures while streaming the response body.
    for attempt in range(3):
        try:
            with client.get(urldefrag(url)[0], timeout=(10, 40), stream=True) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > 20 * 1024 * 1024:
                        raise BuildError("资源超过 20 MiB")
                return bytes(data)
        except requests.RequestException:
            if attempt == 2:
                raise


def text_content(content):
    text = content.decode("utf-8-sig")
    if not text.strip() or text.lstrip().lower().startswith(("<!doctype html", "<html")):
        raise BuildError("资源为空或返回 HTML 错误页")
    return text


class SourceStore:
    def __init__(self, root, fetch, offline=False, strict=True):
        self.root, self.fetch, self.offline, self.strict = root, fetch, offline, strict
        path = root / "upstream/lock.json"
        self.old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.lock, self.writes, self.statuses = {}, {}, []
        self.memo = {}

    def get(self, identifier, url, kind, validator):
        public_url(url)
        if identifier in self.memo:
            if self.lock[identifier]["url"] != url:
                raise BuildError("缓存 ID 对应多个 URL")
            return self.memo[identifier]
        extension = {"filter": "list", "rewrite": "conf", "script": "js"}[kind]
        path = f"upstream/{kind}/{identifier}.{extension}"
        metadata = self.old.get(identifier, {})
        def cached():
            if metadata.get("url") != url or metadata.get("path") != path:
                raise BuildError(f"{identifier}: 没有与来源匹配的快照")
            content = (self.root / path).read_bytes()
            if sha256(content) != metadata.get("sha256"):
                raise BuildError(f"{identifier}: 快照哈希校验失败")
            return content
        state, failure = "offline" if self.offline else "downloaded", None
        try:
            content = cached() if self.offline else self.fetch(urldefrag(url)[0])
            if isinstance(content, str):
                content = content.encode("utf-8")
            count = validator(text_content(content))
            previous = metadata.get("rules", 0) if metadata.get("url") == url else 0
            if previous and count < previous * 0.7:
                raise BuildError(f"规则数量由 {previous} 降为 {count}，超过 30% 门槛")
        except (requests.RequestException, UnicodeError, BuildError, OSError) as exc:
            if self.offline or self.strict:
                raise BuildError(f"{identifier}: {exc}") from exc
            content = cached()
            count = validator(text_content(content))
            state, failure = "cached", str(exc)
        self.writes[path] = content
        self.lock[identifier] = {"url": url, "kind": kind, "path": path, "sha256": sha256(content), "rules": count}
        status = {"id": identifier, "state": state}
        if failure:
            status["warning"] = failure
        self.statuses.append(status)
        self.memo[identifier] = content
        return content


def publish(root, writes, obsolete=(), check=False):
    """Validate destinations, stage every byte, then replace; rollback on I/O error."""
    changed = [name for name, content in writes.items() if not (root / name).exists() or (root / name).read_bytes() != content]
    obsolete = [name for name in obsolete if (root / name).exists()]
    for name in [*writes, *obsolete]:
        path = local_path(root, name)
        if not name.startswith(("dist/", "upstream/")) or path == root.resolve():
            raise BuildError(f"不允许写入或清理非生成文件: {name}")
    if check:
        if changed or obsolete:
            raise BuildError("产物不一致: " + ", ".join(changed + obsolete))
        return []
    if not changed and not obsolete:
        return []
    (root / ".build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root / ".build", prefix="publish-") as temp:
        staging = Path(temp)
        old, applied = {}, []
        for index, name in enumerate(changed):
            (staging / str(index)).write_bytes(writes[name])
        for name in [*changed, *obsolete]:
            path = root / name
            old[name] = path.read_bytes() if path.exists() else None
        try:
            for index, name in enumerate(changed):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging / str(index), path)
                applied.append(name)
            for name in obsolete:
                (root / name).unlink()
                applied.append(name)
        except OSError:
            for name in reversed(applied):
                path = root / name
                if old[name] is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(old[name])
            raise
    return changed + obsolete
