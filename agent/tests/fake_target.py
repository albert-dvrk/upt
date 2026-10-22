"""Крошечный stdlib-сервер, повторяющий поведение mock-хостинга для тестов агента."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def content(vid: int, size: int) -> bytes:
    out, i = bytearray(), 0
    while len(out) < size:
        out += hashlib.sha256(f"{vid}:{i}".encode()).digest()
        i += 1
    return bytes(out[:size])


class Target:
    def __init__(self) -> None:
        self.found = {1, 2, 3, 7}
        self.forbidden = {5}
        self.size = 300_000
        self.titles = {}
        self.fail_next_pages = 0  # сколько ближайших запросов страницы вернуть 500
        self.rate_limit_pages = 0  # сколько ближайших запросов страницы вернуть 429
        self.drop_next_files = 0  # сколько ближайших загрузок оборвать на середине
        self.corrupt = set()  # id, для которых сообщаем неверный sha256
        self.page_hits = 0
        self.file_hits = 0
        self.ranges: list[str | None] = []
        self.lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "Target":
        target = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # тишина в выводе тестов
                pass

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):
                parts = self.path.strip("/").split("/")
                with target.lock:
                    if parts[0] == "files":
                        return self._file(int(parts[1]))
                    vid = int(parts[0])
                    target.page_hits += 1
                    if target.rate_limit_pages > 0:
                        target.rate_limit_pages -= 1
                        return self._send(429, b"{}", {"Retry-After": "0"})
                    if target.fail_next_pages > 0:
                        target.fail_next_pages -= 1
                        return self._send(500, b"{}")
                if vid in target.forbidden:
                    return self._send(403, b"{}")
                if vid not in target.found:
                    return self._send(404, b"{}")
                data = content(vid, target.size)
                digest = "0" * 64 if vid in target.corrupt else hashlib.sha256(data).hexdigest()
                meta = {
                    "id": vid, "title": target.titles.get(vid, f"Видео {vid}"),
                    "page_url": f"{target.base}/{vid}", "mime_type": "video/mp4",
                    "download_url": f"{target.base}/files/{vid}/video.mp4", "size_bytes": len(data),
                    "sha256": digest, "downloadable": True,
                }
                self._send(200, json.dumps(meta).encode(), {"Content-Type": "application/json"})

            def _file(self, vid):
                target.file_hits += 1
                data = content(vid, target.size)
                rng = self.headers.get("Range")
                target.ranges.append(rng)
                start = 0
                code = 200
                headers = {"Accept-Ranges": "bytes"}
                if rng:
                    start = int(rng.split("=")[1].split("-")[0])
                    code = 206
                    headers["Content-Range"] = f"bytes {start}-{len(data)-1}/{len(data)}"
                body = data[start:]
                drop = target.drop_next_files > 0
                if drop:
                    target.drop_next_files -= 1
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if drop:
                    self.wfile.write(body[: len(body) // 2])
                    self.wfile.flush()
                    self.connection.close()
                    return
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def template(self) -> str:
        return self.base + "/{id}"


__all__ = ["Target", "content", "time"]
