"""如影核心：局域网 ADB 设备发现。

三层策略（结果合并去重）：
1. zeroconf 直查 mDNS 广播（Android 11+ 无线调试）：
   - `_adb-tls-connect._tcp` —— 无线调试开启时常驻广播，含当前调试端口
   - `_adb-tls-pairing._tcp` —— 仅在「使用配对码配对设备」弹窗期间广播
2. adb 自带的 `adb mdns services`（由调用方传入结果，本模块负责合并）
3. 经典端口扫描：本机所在 /24 网段的 5555 端口（`adb tcpip 5555` 的老设备）

zeroconf 缺失时自动降级为策略 2 + 3。
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Iterable, Optional

try:
    from zeroconf import ServiceBrowser, Zeroconf

    HAS_ZEROCONF = True
except ImportError:  # pragma: no cover - 依赖缺失时降级
    HAS_ZEROCONF = False

MDNS_CONNECT_TYPE = "_adb-tls-connect._tcp.local."
MDNS_PAIRING_TYPE = "_adb-tls-pairing._tcp.local."

# 每种服务类型最多解析的实例数，避免广播风暴时拖长扫描
_MAX_INFOS_PER_TYPE = 8


def local_ipv4() -> Optional[str]:
    """取默认路由出口的本机 IPv4。

    UDP connect 不真正发包，仅让内核选择路由，因此无外网环境同样可用。
    """
    for probe in (("8.8.8.8", 80), ("10.255.255.255", 1)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(probe)
                return s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            continue
    return None


def subnet_prefix(ip: str, prefix_len: int = 24) -> Optional[str]:
    """由本机 IP 得到网段前缀（仅支持 /24），如 192.168.1.23 → "192.168.1." 。"""
    if prefix_len != 24:
        return None
    parts = (ip or "").split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return None
    return ".".join(parts[:3]) + "."


async def sweep_port(prefix: str, start: int, end: int, port: int, timeout: float = 0.4) -> list[str]:
    """并发探测 prefix{start..end} 的 TCP 端口，返回可连接的 IP 列表。"""
    async def probe(ip: str) -> Optional[str]:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return ip
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return None

    hosts = [f"{prefix}{i}" for i in range(start, end + 1)]
    results = await asyncio.gather(*(probe(h) for h in hosts))
    return [ip for ip in results if ip]


def merge_candidates(*groups: Iterable[tuple[str, int, str]]) -> list[dict]:
    """合并多来源候选并按 (ip, port) 去重，来源记录在 sources 中。"""
    merged: dict[tuple[str, int], dict] = {}
    for group in groups:
        for ip, port, source in group or []:
            key = (str(ip), int(port))
            entry = merged.get(key)
            if entry is None:
                merged[key] = {"ip": ip, "port": int(port), "sources": [source]}
            elif source not in entry["sources"]:
                entry["sources"].append(source)
    return list(merged.values())


def _mdns_browse(timeout: float) -> dict:
    """同步 mDNS 浏览（应在 worker 线程中运行）。

    返回 {"connect": [(ip, port)], "pairing": [(ip, port)]}。
    """
    out: dict[str, list] = {"connect": [], "pairing": []}
    zc = Zeroconf()
    found: dict[str, list[str]] = {}
    lock = threading.Lock()

    class _Listener:
        def add_service(self, zc_ref, svc_type, name):
            with lock:
                found.setdefault(svc_type, []).append(name)

        def update_service(self, zc_ref, svc_type, name):
            pass

        def remove_service(self, zc_ref, svc_type, name):
            pass

    buckets = {MDNS_CONNECT_TYPE: "connect", MDNS_PAIRING_TYPE: "pairing"}
    try:
        for svc_type in buckets:
            ServiceBrowser(zc, svc_type, _Listener())
        time.sleep(timeout)
        for svc_type, names in found.items():
            key = buckets.get(svc_type)
            if not key:
                continue
            for name in names[:_MAX_INFOS_PER_TYPE]:
                try:
                    info = zc.get_service_info(svc_type, name, 800)
                except Exception:  # noqa: BLE001 - 单个实例解析失败不影响其余
                    continue
                if not info or not info.port:
                    continue
                addrs = list(info.parsed_addresses() or [])
                ipv4 = [a for a in addrs if ":" not in a]
                ip = (ipv4 or addrs)[0]
                out[key].append((ip, int(info.port)))
        return out
    finally:
        zc.close()


async def mdns_adb_services(timeout: float = 4.0) -> dict:
    """异步浏览无线调试 mDNS 广播；未安装 zeroconf 时返回空结果。"""
    if not HAS_ZEROCONF:
        return {"connect": [], "pairing": []}
    return await asyncio.to_thread(_mdns_browse, timeout)
