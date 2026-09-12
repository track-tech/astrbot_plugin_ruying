"""如影核心：无线 ADB 的 asyncio 封装。

所有 adb 交互均通过 asyncio 子进程执行，不阻塞事件循环；
解析函数（parse_*）保持为纯函数便于离线测试。
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

# 让 adb 在 Windows 后台运行时不弹出控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

STATE_TEXT = {
    "device": "在线",
    "offline": "离线",
    "unauthorized": "未授权（请在手机上允许 USB 调试授权弹窗）",
    "recovery": "recovery 模式",
    "sideload": "sideload 模式",
    "no permissions": "无权限",
}

_IP_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$")

# Android keycode 简称 → KEYCODE_
KEYCODES = {
    "back": "KEYCODE_BACK",
    "返回": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "主页": "KEYCODE_HOME",
    "menu": "KEYCODE_MENU",
    "菜单": "KEYCODE_MENU",
    "power": "KEYCODE_POWER",
    "电源": "KEYCODE_POWER",
    "wake": "KEYCODE_WAKEUP",
    "唤醒": "KEYCODE_WAKEUP",
    "sleep": "KEYCODE_SLEEP",
    "熄屏": "KEYCODE_SLEEP",
    "volume_up": "KEYCODE_VOLUME_UP",
    "音量加": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "音量减": "KEYCODE_VOLUME_DOWN",
    "mute": "KEYCODE_VOLUME_MUTE",
    "enter": "KEYCODE_ENTER",
    "回车": "KEYCODE_ENTER",
    "del": "KEYCODE_DEL",
    "删除": "KEYCODE_DEL",
    "tab": "KEYCODE_TAB",
    "recents": "KEYCODE_APP_SWITCH",
    "最近任务": "KEYCODE_APP_SWITCH",
    "notification": "KEYCODE_NOTIFICATION",
    "相机": "KEYCODE_CAMERA",
}

# 常用中文应用名 → 包名（找不到时仍会尝试模糊匹配包名）
COMMON_APPS = {
    "微信": "com.tencent.mm",
    "qq": "com.tencent.mobileqq",
    "抖音": "com.ss.android.ugc.aweme",
    "b站": "tv.danmaku.bili",
    "哔哩哔哩": "tv.danmaku.bili",
    " bilibili": "tv.danmaku.bili",
    "淘宝": "com.taobao.taobao",
    "支付宝": "com.eg.android.AlipayGphone",
    "京东": "com.jingdong.app.mall",
    "拼多多": "com.xunmeng.pinduoduo",
    "美团": "com.sankuai.meituan",
    "高德地图": "com.autonavi.minimap",
    "网易云音乐": "com.netease.cloudmusic",
    "小红书": "com.xingin.xhs",
    "微博": "com.sina.weibo",
    "知乎": "com.zhihu.android",
    "设置": "com.android.settings",
    "相机": "com.android.camera",
}


class AdbError(Exception):
    """adb 执行失败或输出不符合预期。"""


def describe_state(state: str) -> str:
    return STATE_TEXT.get(state, state)


def is_ip_port(text: str) -> bool:
    return bool(_IP_PORT_RE.match((text or "").strip()))


def split_ip_port(text: str) -> Optional[tuple[str, int]]:
    m = _IP_PORT_RE.match((text or "").strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


class Adb:
    """以子进程方式调用 adb CLI 的最小封装。"""

    def __init__(self, adb_path: str = "adb", timeout: float = 20.0):
        self.adb_path = (adb_path or "adb").strip() or "adb"
        self.default_timeout = max(5.0, float(timeout))

    # ------------------------------------------------------------------
    # 底层执行
    # ------------------------------------------------------------------
    async def run(self, *args, timeout: Optional[float] = None) -> tuple[Optional[int], bytes]:
        argv = [self.adb_path, *[str(a) for a in args]]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=_CREATE_NO_WINDOW,
            )
        except FileNotFoundError:
            raise AdbError(
                f"找不到 adb：{self.adb_path}。请安装 Android platform-tools，"
                "并在插件配置 adb_path 中填写完整路径，或将其加入 PATH。"
            )
        except OSError as e:
            raise AdbError(f"adb 无法启动：{e}")

        used_timeout = float(timeout or self.default_timeout)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=used_timeout)
        except asyncio.TimeoutError:
            self._kill(proc)
            raise AdbError(
                f"adb 命令超时（>{used_timeout:.0f}s）：adb {' '.join(argv[1:5])} …。"
                "无线网络较差时可在插件配置中调大 command_timeout。"
            )
        except Exception as e:  # noqa: BLE001
            self._kill(proc)
            raise AdbError(f"adb 命令执行失败：{e}")
        return proc.returncode, out

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    def _text(out: bytes) -> str:
        return out.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()

    async def version(self) -> str:
        _, out = await self.run("version", timeout=10)
        return self._text(out).splitlines()[0] if self._text(out) else ""

    # ------------------------------------------------------------------
    # 纯解析函数（便于离线测试）
    # ------------------------------------------------------------------
    @staticmethod
    def parse_devices(text: str) -> list[dict]:
        devices: list[dict] = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            m = re.match(r"^(\S+)\s+(\S+)(?:\s+(.*))?$", line)
            if not m:
                continue
            serial, state, rest = m.group(1), m.group(2), m.group(3) or ""
            model_m = re.search(r"model:([^\s]+)", rest)
            devices.append(
                {
                    "serial": serial,
                    "state": state,
                    "model": model_m.group(1) if model_m else "",
                }
            )
        return devices

    @staticmethod
    def parse_connect(text: str) -> tuple[bool, str]:
        low = (text or "").lower()
        if "already connected to" in low:
            return True, text.strip()
        if "connected to" in low and not any(
            k in low for k in ("failed", "cannot", "because", "error")
        ):
            return True, text.strip()
        return False, (text or "连接失败").strip()

    @staticmethod
    def parse_mdns_services(text: str) -> list[dict]:
        """解析 `adb mdns services` 输出，返回无线调试服务列表。

        每行形如：WirelessDebugConnect\\t_adb-tls-connect._tcp\\t192.168.1.23:37855
        """
        services: list[dict] = []
        for line in (text or "").splitlines():
            parts = re.split(r"\t+", line.strip())
            if len(parts) < 3:
                continue
            name, service, addr = parts[0], parts[1], parts[2]
            m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d+)$", addr)
            if not m:
                continue
            services.append(
                {"name": name, "service": service, "ip": m.group(1), "port": int(m.group(2))}
            )
        return services

    @staticmethod
    def parse_wm_size(text: str) -> Optional[tuple[int, int]]:
        for line in reversed((text or "").splitlines()):
            m = re.search(r"(\d+)x(\d+)", line)
            if m:
                return int(m.group(1)), int(m.group(2))
        return None

    @staticmethod
    def parse_battery(text: str) -> dict:
        info: dict = {}
        for line in (text or "").splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if key == "level":
                info["level"] = val
            elif key == "scale":
                info["scale"] = val
            elif key in ("powered", "USB powered", "AC powered", "Wireless powered"):
                info["charging"] = info.get("charging") or (val.lower() == "true")
            elif key == "status":
                # BATTERY_STATUS_CHARGING = 2, FULL = 5
                info["charging"] = info.get("charging") or val in ("2", "5")
        level = info.get("level")
        scale = info.get("scale") or "100"
        try:
            info["percent"] = round(int(level) * 100 / int(scale))
        except (ValueError, ZeroDivisionError, TypeError):
            info["percent"] = None
        return info

    @staticmethod
    def parse_resumed_activity(text: str) -> str:
        for line in (text or "").splitlines():
            line = line.strip()
            if "ResumedActivity" in line or "mCurrentFocus" in line or "mFocusedWindow" in line:
                return line
        return ""

    @staticmethod
    def escape_input_text(text: str) -> str:
        """`input text` 的参数转义：空格需写成 %s，特殊字符用设备端双引号包裹。"""
        t = (text or "").replace(" ", "%s")
        return '"' + t.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'

    @staticmethod
    def is_ascii_text(text: str) -> bool:
        try:
            (text or "").encode("ascii")
            return True
        except UnicodeEncodeError:
            return False

    # ------------------------------------------------------------------
    # 高层操作
    # ------------------------------------------------------------------
    async def devices(self) -> list[dict]:
        _, out = await self.run("devices", "-l")
        return self.parse_devices(self._text(out))

    async def pair(self, ip: str, pair_port: int, code: str) -> tuple[bool, str]:
        _, out = await self.run("pair", f"{ip}:{pair_port}", code, timeout=25)
        text = self._text(out)
        low = text.lower()
        if "success" in low or "已配对" in text or "successfully paired" in low:
            return True, text
        return False, text or "配对失败"

    async def connect(self, ip: str, port: int) -> tuple[bool, str]:
        _, out = await self.run("connect", f"{ip}:{port}", timeout=25)
        return self.parse_connect(self._text(out))

    async def disconnect(self, serial: Optional[str] = None) -> str:
        _, out = await self.run("disconnect", serial) if serial else await self.run("disconnect")
        return self._text(out)

    async def shell(self, serial: str, *cmd, timeout: Optional[float] = None) -> str:
        _, out = await self.run("-s", serial, "shell", *cmd, timeout=timeout)
        return self._text(out)

    async def screencap(self, serial: str, timeout: float = 30.0) -> bytes:
        """截屏返回 PNG 字节。优先 exec-out（二进制安全），失败退回 shell 模式并修复 CRLF。"""
        _, out = await self.run("-s", serial, "exec-out", "screencap", "-p", timeout=timeout)
        if out.startswith(PNG_MAGIC):
            return out
        _, out2 = await self.run("-s", serial, "shell", "screencap", "-p", timeout=timeout)
        fixed = out2.replace(b"\r\n", b"\n")
        if fixed.startswith(PNG_MAGIC):
            return fixed
        err = self._text(out) or self._text(out2)
        raise AdbError(f"截图失败：{(err or '设备返回了空数据')[:200]}")

    async def ui_dump(self, serial: str, timeout: float = 25.0) -> str:
        """uiautomator dump 并取回 XML 文本。"""
        remote = "/sdcard/ruying_window_dump.xml"
        last_msg = ""
        for cmd in (f"uiautomator dump --compressed {remote}", f"uiautomator dump {remote}"):
            last_msg = await self.shell(serial, cmd, timeout=timeout)
            _, xml = await self.run("-s", serial, "exec-out", "cat", remote, timeout=timeout)
            xml_text = xml.decode("utf-8", errors="replace").strip()
            if xml_text.startswith("<?xml") or "<hierarchy" in xml_text[:300]:
                await self.shell(serial, "rm", "-f", remote, timeout=10)
                return xml_text
        await self.shell(serial, "rm", "-f", remote, timeout=10)
        hint = (last_msg or "无输出").strip()
        raise AdbError(
            f"读取界面层级失败（应用可能禁止辅助功能读取，如游戏、视频播放页）：{hint[:200]}"
        )

    async def pull(self, serial: str, remote: str, local: str, timeout: float = 120.0) -> str:
        rc, out = await self.run("-s", serial, "pull", remote, local, timeout=timeout)
        text = self._text(out)
        if rc == 0 and os.path.exists(local):
            return local
        raise AdbError(f"拉取文件失败：{text[:200] or f'adb 退出码 {rc}'}")

    async def mdns_services(self) -> list[dict]:
        try:
            _, out = await self.run("mdns", "services", timeout=12)
        except AdbError:
            return []
        return self.parse_mdns_services(self._text(out))

    async def android_version(self, serial: str) -> str:
        return await self.shell(serial, "getprop", "ro.build.version.release", timeout=10)

    async def screen_size(self, serial: str) -> Optional[tuple[int, int]]:
        return self.parse_wm_size(await self.shell(serial, "wm", "size", timeout=10))

    async def battery(self, serial: str) -> dict:
        return self.parse_battery(await self.shell(serial, "dumpsys", "battery", timeout=15))

    async def current_activity(self, serial: str) -> str:
        out = await self.shell(
            serial, "dumpsys activity activities | grep -E 'ResumedActivity'", timeout=15
        )
        line = self.parse_resumed_activity(out)
        if line:
            return line
        out = await self.shell(serial, "dumpsys activity activities", timeout=20)
        return self.parse_resumed_activity(out)

    async def list_packages(self, serial: str, third_party_only: bool = True) -> list[str]:
        args = ["pm", "list", "packages"]
        if third_party_only:
            args.append("-3")
        out = await self.shell(serial, *args, timeout=20)
        pkgs = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkgs.append(line[len("package:"):])
        return pkgs

    async def launch_app(self, serial: str, package: str) -> str:
        return await self.shell(
            serial, "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1",
            timeout=20,
        )

    async def has_adb_keyboard(self, serial: str) -> bool:
        out = await self.shell(serial, "pm list packages com.android.adbkeyboard", timeout=10)
        return "com.android.adbkeyboard" in out
