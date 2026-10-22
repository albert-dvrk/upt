"""Сбор метрик сервера. psutil используется, если установлен; иначе — минимальный набор из stdlib."""

from __future__ import annotations

import os
import shutil
import time

try:  # pragma: no cover - зависит от окружения
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class MetricsCollector:
    def __init__(self, storage_path: str, counters) -> None:
        self.storage_path = storage_path
        self.counters = counters
        self._last = time.monotonic()
        self._last_ids = 0
        self._last_bytes = 0
        self._last_net = self._net()
        self._last_disk = self._disk_io()

    @staticmethod
    def _net():
        if psutil:
            n = psutil.net_io_counters()
            return n.bytes_recv, n.bytes_sent
        return None

    @staticmethod
    def _disk_io():
        if psutil:
            d = psutil.disk_io_counters()
            if d:
                return d.read_bytes, d.write_bytes
        return None

    def collect(self) -> dict:
        now = time.monotonic()
        dt_ = max(now - self._last, 1e-6)
        snap = self.counters.snapshot()
        m: dict = {
            "ids_per_sec": (snap["ids"] - self._last_ids) / dt_,
            "download_bps": (snap["bytes"] - self._last_bytes) / dt_,
            "active_checks": snap["active_checks"],
            "active_downloads": snap["active_downloads"],
            "http_status_counts": snap["status"],
        }
        self._last, self._last_ids, self._last_bytes = now, snap["ids"], snap["bytes"]
        try:
            usage = shutil.disk_usage(self.storage_path if os.path.isdir(self.storage_path) else "/")
            m["disk_total"], m["disk_free"] = usage.total, usage.free
        except OSError:
            pass
        if psutil:
            vm = psutil.virtual_memory()
            m.update(cpu_percent=psutil.cpu_percent(interval=None), ram_total=vm.total, ram_used=vm.used,
                     swap_used=psutil.swap_memory().used)
            net = self._net()
            if net and self._last_net:
                m["net_rx_bps"] = (net[0] - self._last_net[0]) / dt_
                m["net_tx_bps"] = (net[1] - self._last_net[1]) / dt_
            self._last_net = net
            disk = self._disk_io()
            if disk and self._last_disk:
                m["disk_read_bps"] = (disk[0] - self._last_disk[0]) / dt_
                m["disk_write_bps"] = (disk[1] - self._last_disk[1]) / dt_
            self._last_disk = disk
            try:
                m["open_files"] = psutil.Process().num_fds()
            except (AttributeError, psutil.Error):
                pass
        else:
            try:
                m["cpu_percent"] = min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100)
            except OSError:
                pass
        return m

    def resources(self) -> dict:
        usage = shutil.disk_usage(self.storage_path if os.path.isdir(self.storage_path) else "/")
        return {"cpu_count": os.cpu_count(), "disk_free_bytes": usage.free, "disk_total_bytes": usage.total}
