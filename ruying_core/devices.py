"""如影核心：设备注册表（JSON 持久化）。

登记通过 `/如影 connect` 添加的无线设备：别名、IP、无线调试端口；
支持按 别名 / ip:端口 / ip 三种方式解析设备。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Optional

from .adb import is_ip_port, split_ip_port


class DeviceRegistry:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._devices: list[dict] = []
        self._default: str = ""
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            devices = data.get("devices") if isinstance(data, dict) else None
            if isinstance(devices, list):
                self._devices = [d for d in devices if isinstance(d, dict) and d.get("ip")]
            self._default = (data.get("default") or "") if isinstance(data, dict) else ""
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 —— 配置损坏时重建而非崩溃
            self._devices = []
            self._default = ""

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"devices": self._devices, "default": self._default},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # 增删查改
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_alias(alias: str) -> str:
        return re.sub(r"\s+", "_", (alias or "").strip())[:32]

    def add(self, ip: str, port: int, alias: str = "") -> dict:
        with self._lock:
            alias = self._norm_alias(alias) or ip
            dev = {
                "alias": alias,
                "ip": ip,
                "port": int(port),
                "added_at": int(time.time()),
            }
            self._devices = [d for d in self._devices if d.get("alias") != alias]
            self._devices.append(dev)
            if not self._default:
                self._default = alias
            self.save()
            return dev

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(d) for d in self._devices]

    def get(self, key: str) -> Optional[dict]:
        """按 别名 / ip:端口 / 纯 IP（唯一时）解析设备。"""
        key = (key or "").strip()
        if not key:
            return None
        with self._lock:
            for d in self._devices:
                if d.get("alias") == key:
                    return dict(d)
            if is_ip_port(key):
                ip, port = split_ip_port(key)
                for d in self._devices:
                    if d.get("ip") == ip and int(d.get("port", -1)) == port:
                        return dict(d)
            if ":" not in key:
                matches = [d for d in self._devices if d.get("ip") == key]
                if len(matches) == 1:
                    return dict(matches[0])
            return None

    def remove(self, key: str) -> Optional[dict]:
        dev = self.get(key)
        if not dev:
            return None
        with self._lock:
            self._devices = [d for d in self._devices if d.get("alias") != dev["alias"]]
            if self._default == dev["alias"]:
                self._default = ""
            self.save()
        return dev

    def has_alias(self, alias: str) -> bool:
        """别名是否已存在（精确匹配，不含 ip 解析语义）。"""
        with self._lock:
            return any(d.get("alias") == alias for d in self._devices)

    def set_default(self, key: str) -> Optional[dict]:
        dev = self.get(key)
        if not dev:
            return None
        with self._lock:
            self._default = dev["alias"]
            self.save()
        return dev

    def update_port(self, key: str, port: int) -> Optional[dict]:
        """mDNS 重发现新调试端口后更新登记。"""
        dev = self.get(key)
        if not dev:
            return None
        with self._lock:
            for d in self._devices:
                if d.get("alias") == dev["alias"]:
                    d["port"] = int(port)
                    self.save()
                    return dict(d)
        return None

    def default(self) -> Optional[dict]:
        with self._lock:
            for d in self._devices:
                if d.get("alias") == self._default:
                    return dict(d)
        return None
