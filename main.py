"""如影（astrbot_plugin_ruying）—— 让机器人通过无线 ADB 操控安卓设备。

如影随形：机器人像影子一样"看到"并"操作"你的安卓手机/平板。

- 设备管理（聊天指令，仅管理员）：无线调试配对、连接登记、多设备与默认设备
- 日常操作（指令 + LLM 函数调用，管理员/白名单）：截屏看屏、界面控件坐标、
  点击/滑动/输入文字/按键、查询与启动应用、拉取文件、电量状态
- 高风险能力（默认关闭）：任意 adb shell 命令执行

典型玩法：在聊天里对机器人说「帮我在手机上打开 B 站搜一下如影插件」，
LLM 会自动 截屏/读控件 → 点击 → 输入 → 确认结果。
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import sys
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# mcp.types 由 AstrBot 核心自带；存在时截图可作为 ImageContent 返回给多模态 LLM
try:
    import mcp.types as _mcp_types

    _HAS_MCP = True
except ImportError:  # pragma: no cover
    _HAS_MCP = False

# File 消息组件按平台适配器不同而可用性不同，缺失时降级为文本路径
try:
    from astrbot.api.message_components import File

    _HAS_FILE = True
except ImportError:  # pragma: no cover
    File = None
    _HAS_FILE = False

PLUGIN_NAME = "astrbot_plugin_ruying"
PLUGIN_VERSION = "0.3.6"

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 关键：AstrBot 热重载插件时只重新执行 main.py，sys.modules 里缓存的旧版
# ruying_core.* 不会被刷新，导致新旧代码混用，这里主动清缓存。
for _mod in [m for m in sys.modules if m == "ruying_core" or m.startswith("ruying_core.")]:
    del sys.modules[_mod]

from ruying_core import (  # noqa: E402
    Adb,
    AdbError,
    COMMON_APPS,
    DeviceRegistry,
    KEYCODES,
    describe_state,
    format_ui_text,
    is_admin,
    is_ip_port,
    op_allowed,
    parse_ui_hierarchy,
    png_dimensions,
    split_ip_port,
)
from ruying_core import discover  # noqa: E402


def _data_dir() -> str:
    """AstrBot 数据目录下的插件数据目录；拿不到时退回相对路径。"""
    try:
        from astrbot.api.star import StarTools

        return str(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:  # noqa: BLE001
        return os.path.join("data", "plugin_data", PLUGIN_NAME)


def _png_tool_result(png: bytes, caption: str):
    """把截图包装成 CallToolResult：LLM 可直接看到图片 + 文字说明。"""
    return _mcp_types.CallToolResult(
        content=[
            _mcp_types.ImageContent(
                type="image",
                data=base64.b64encode(png).decode("ascii"),
                mimeType="image/png",
            ),
            _mcp_types.TextContent(type="text", text=caption),
        ]
    )


@register(PLUGIN_NAME, "track-tech", "无线 ADB 安卓设备操控（截屏/触控/应用/文件/Shell）", PLUGIN_VERSION)
class RuyingPlugin(Star):
    """如影 · 无线 ADB 安卓设备操控。发送 /如影 查看指令帮助。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # adb 路径/超时惰性读取：每次执行时取当前配置值，
        # WebUI 修改 adb_path 后无需重载插件即可生效。
        self.adb = Adb(
            path_provider=lambda: self._cfg("adb_path", "adb"),
            timeout_provider=lambda: self._cfg("command_timeout", 20),
        )
        self.data_dir = _data_dir()
        os.makedirs(self.data_dir, exist_ok=True)
        self.shot_dir = os.path.join(self.data_dir, "screens")
        self.download_dir = os.path.join(self.data_dir, "downloads")
        os.makedirs(self.shot_dir, exist_ok=True)
        os.makedirs(self.download_dir, exist_ok=True)
        self.store = DeviceRegistry(os.path.join(self.data_dir, "devices.json"))
        logger.info(f"[如影] 插件已加载，数据目录：{self.data_dir}")

    async def terminate(self):
        """插件卸载：无后台任务需要清理。"""
        pass

    # ------------------------------------------------------------------
    # 配置与权限
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default=None):
        try:
            val = self.config.get(key, default)
            return val if val not in (None, "") else default
        except Exception:  # noqa: BLE001
            return default

    def _whitelist(self) -> list[str]:
        val = self.config.get("whitelist") or []
        if isinstance(val, str):
            return [val.strip()] if val.strip() else []
        return [str(x).strip() for x in val if str(x).strip()]

    def _op_denied(self, event: AstrMessageEvent) -> str:
        """日常操作权限校验；返回空串表示允许，否则返回拒绝原因。"""
        if op_allowed(event, self._whitelist()):
            return ""
        return "权限不足：如影的设备操作仅限管理员或白名单会话使用（可在插件配置中添加白名单）。"

    def _admin_denied(self, event: AstrMessageEvent) -> str:
        if is_admin(event):
            return ""
        return "权限不足：该指令仅限 AstrBot 管理员使用。"

    def _shell_enabled(self) -> bool:
        return bool(self._cfg("enable_shell_tool", False))

    def _vision_enabled(self) -> bool:
        return bool(self._cfg("enable_vision", True)) and _HAS_MCP

    # ------------------------------------------------------------------
    # 设备解析与自动重连
    # ------------------------------------------------------------------
    def _device_brief(self) -> str:
        devs = self.store.all()
        if not devs:
            return "（空）"
        default = self.store.default() or {}
        parts = []
        for d in devs:
            mark = "★" if d["alias"] == default.get("alias") else ""
            parts.append(f"{mark}{d['alias']}({d['ip']}:{d['port']})")
        return " ".join(parts)

    async def _ensure_online(self, serial: str, dev: dict) -> bool:
        """检查设备在线；掉线时按配置自动重连（含 mDNS 端口重发现）。"""
        devs = await self.adb.devices()
        for d in devs:
            if d["serial"] == serial and d["state"] == "device":
                return True
        if not bool(self._cfg("auto_reconnect", True)):
            return False

        ok, _ = await self.adb.connect(dev["ip"], int(dev["port"]))
        if ok:
            devs = await self.adb.devices()
            if any(d["serial"] == serial and d["state"] == "device" for d in devs):
                return True

        # 无线调试重启后端口会变化：mDNS 重发现
        for svc in await self.adb.mdns_services():
            if svc["ip"] == dev["ip"] and "connect" in svc["service"]:
                ok, _ = await self.adb.connect(svc["ip"], svc["port"])
                if ok:
                    devs = await self.adb.devices()
                    new_serial = f"{svc['ip']}:{svc['port']}"
                    if any(d["serial"] == new_serial and d["state"] == "device" for d in devs):
                        updated = self.store.update_port(dev.get("alias"), svc["port"])
                        if updated:
                            dev["port"] = updated["port"]
                        else:
                            dev["port"] = svc["port"]
                        return True
        return False

    async def _resolve_serial(self, device_arg: str = "") -> tuple[str, str]:
        """解析设备 → (serial, 错误信息)。成功时错误信息为空串。"""
        dev = None
        arg = (device_arg or "").strip()
        if arg:
            dev = self.store.get(arg)
            if dev is None:
                if is_ip_port(arg):
                    ip, port = split_ip_port(arg)
                    dev = {"alias": arg, "ip": ip, "port": port}
                elif re.match(r"^\d{1,3}(\.\d{1,3}){3}$", arg):
                    # 经典 adb over Wi-Fi（adb tcpip 5555）场景
                    dev = {"alias": arg, "ip": arg, "port": 5555}
                else:
                    return "", f"找不到设备「{arg}」。已登记：{self._device_brief()}，也可直接用 IP:端口 指定。"
        if dev is None:
            dev = self.store.default()
            if dev is None:
                devs = self.store.all()
                if len(devs) == 1:
                    dev = devs[0]
                elif not devs:
                    return "", (
                        "尚未登记任何设备。请管理员：手机开启「开发者选项→无线调试→使用配对码配对设备」后，"
                        "发送 /如影 pair <ip:配对端口> <配对码>，再 /如影 connect <ip:调试端口> [别名]。"
                    )
                else:
                    return "", (
                        f"存在多台设备且未设置默认：{self._device_brief()}。"
                        "请用 /如影 use <别名> 指定默认设备，或在操作时指明设备。"
                    )
        serial = f"{dev['ip']}:{int(dev['port'])}"
        try:
            if not await self._ensure_online(serial, dev):
                return "", (
                    f"设备 {dev.get('alias')}({serial}) 无法连接。请确认手机与机器人在同一网络、"
                    "「无线调试」已开启；重启无线调试后端口会变化，请用 /如影 connect <ip:新端口> 更新。"
                )
        except AdbError as e:
            return "", f"adb 执行失败：{e}"
        # mDNS 重发现可能更新了端口，以最新注册信息为准
        serial = f"{dev['ip']}:{int(dev['port'])}"
        return serial, ""

    # ------------------------------------------------------------------
    # 截图存储
    # ------------------------------------------------------------------
    def _save_screenshot(self, png: bytes) -> str:
        # 纳秒时间戳：LLM 循环中可能一秒内连拍多张，秒级时间戳会同名覆盖
        path = os.path.join(self.shot_dir, f"shot_{time.time_ns()}.png")
        with open(path, "wb") as f:
            f.write(png)
        self._cleanup_screenshots()
        return path

    def _cleanup_screenshots(self) -> None:
        try:
            keep = int(self._cfg("keep_screenshots", 10) or 10)
            files = [
                os.path.join(self.shot_dir, f)
                for f in os.listdir(self.shot_dir)
                if f.endswith(".png")
            ]
            files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for old in files[max(0, keep):]:
                os.remove(old)
        except OSError:
            pass

    async def _ensure_awake(self, serial: str) -> None:
        """屏幕熄灭时自动唤醒并尝试上滑过锁屏（尽力而为，失败不影响主操作）。

        熄屏状态下截屏是纯黑图、控件 dump 也只是锁屏内容，因此屏幕相关操作前调用。
        若设备设置了锁屏密码，唤醒后只能到锁屏页——由 LLM 看图后告知用户，无法也不应远程绕过。
        """
        if not bool(self._cfg("auto_wake", True)):
            return
        try:
            out = await self.adb.shell(serial, "dumpsys display | grep mScreenState", timeout=10)
            if "ON" not in out.upper():
                await self.adb.shell(serial, "input", "keyevent", "KEYCODE_WAKEUP", timeout=10)
                await asyncio.sleep(1.0)
                size = await self.adb.screen_size(serial)
                if size:
                    w, h = size
                    await self.adb.shell(
                        serial, "input", "swipe",
                        str(w // 2), str(int(h * 0.85)), str(w // 2), str(int(h * 0.35)), "250",
                        timeout=10,
                    )
                await asyncio.sleep(0.5)
        except Exception:  # noqa: BLE001 - 唤醒失败不打断主操作
            pass

    # ==================================================================
    # 聊天指令
    # ==================================================================
    HELP_TEXT = """📱 如影 · 无线 ADB 设备操控

【管理 · 仅管理员】
/如影 pair <ip:配对端口> <配对码> —— 首次配对（手机：开发者选项→无线调试→使用配对码配对设备）
/如影 connect <ip:调试端口> [别名] —— 连接并登记设备
/如影 devices —— 查看已登记设备与在线状态
/如影 use <别名|ip:端口> —— 设为默认设备
/如影 forget <别名|ip:端口> —— 移除登记
/如影 disconnect [ip:端口] —— 断开 adb 连接
/如影 status —— adb 版本与设备状态
/如影 scan —— 扫描局域网并自动连接发现的设备

【操作 · 管理员/白名单】
/如影 shot [设备] —— 截屏并发送到会话
/如影 tap <x> <y> —— 点击坐标
/如影 swipe <x1> <y1> <x2> <y2> [毫秒] —— 滑动
/如影 text <内容> —— 输入文字（中文需设备安装 ADBKeyboard）
/如影 key <键名> —— 按键：back/home/menu/power/volume_up/volume_down/enter/del/recents/wake/sleep
/如影 current —— 当前前台应用
/如影 apps [关键词] —— 列出已安装应用（第三方）
/如影 launch <包名或应用名> —— 启动应用
/如影 pull <设备内路径> —— 拉取文件
/如影 shell <命令> —— 任意 shell（默认关闭，仅管理员）

💡 也可以直接用自然语言让我操作手机，例如「帮我在手机上打开微信」。"""

    @filter.command("如影", alias={"ruying"})
    async def ruying_cmd(self, event: AstrMessageEvent):
        """如影指令入口：/如影 [子命令]"""
        raw = (event.message_str or "").strip()
        parts = raw.split(None, 2)
        sub = parts[1].lower() if len(parts) >= 2 else ""
        rest = parts[2].strip() if len(parts) >= 3 else ""

        if sub in ("", "help", "帮助"):
            yield event.plain_result(self.HELP_TEXT)
        elif sub == "pair":
            async for r in self._cmd_pair(event, rest):
                yield r
        elif sub == "connect":
            async for r in self._cmd_connect(event, rest):
                yield r
        elif sub == "devices":
            async for r in self._cmd_devices(event):
                yield r
        elif sub == "use":
            async for r in self._cmd_use(event, rest):
                yield r
        elif sub == "forget":
            async for r in self._cmd_forget(event, rest):
                yield r
        elif sub == "disconnect":
            async for r in self._cmd_disconnect(event, rest):
                yield r
        elif sub == "status":
            async for r in self._cmd_status(event):
                yield r
        elif sub in ("scan", "扫描"):
            async for r in self._cmd_scan(event):
                yield r
        elif sub == "shot":
            async for r in self._cmd_shot(event, rest):
                yield r
        elif sub == "tap":
            async for r in self._cmd_tap(event, rest):
                yield r
        elif sub == "swipe":
            async for r in self._cmd_swipe(event, rest):
                yield r
        elif sub == "text":
            async for r in self._cmd_text(event, rest):
                yield r
        elif sub == "key":
            async for r in self._cmd_key(event, rest):
                yield r
        elif sub == "current":
            async for r in self._cmd_current(event, rest):
                yield r
        elif sub == "apps":
            async for r in self._cmd_apps(event, rest):
                yield r
        elif sub == "launch":
            async for r in self._cmd_launch(event, rest):
                yield r
        elif sub == "pull":
            async for r in self._cmd_pull(event, rest):
                yield r
        elif sub == "shell":
            async for r in self._cmd_shell(event, rest):
                yield r
        else:
            yield event.plain_result(f"未知的子命令「{sub}」。\n\n{self.HELP_TEXT}")

    # ------------------------------------------------------------------
    # 管理子指令
    # ------------------------------------------------------------------
    async def _cmd_pair(self, event: AstrMessageEvent, rest: str):
        if err := self._admin_denied(event):
            yield event.plain_result(err)
            return
        toks = rest.split()
        if len(toks) < 2 or not is_ip_port(toks[0]):
            yield event.plain_result(
                "用法：/如影 pair <ip:配对端口> <配对码>\n"
                "手机上：开发者选项 → 无线调试 → 使用配对码配对设备，"
                "把弹出的 IP 地址、配对端口和 6 位配对码发给我。"
                "注意配对端口每次打开都不相同，请即开即用。"
            )
            return
        ip, port = split_ip_port(toks[0])
        code = toks[1]
        try:
            ok, msg = await self.adb.pair(ip, port, code)
        except AdbError as e:
            yield event.plain_result(f"❌ 配对失败：{e}")
            return
        if ok:
            yield event.plain_result(
                f"✅ 配对成功！\n下一步：回到手机「无线调试」主页面，"
                f"那里显示的 IP 地址和端口（与配对端口不同）用于连接：\n"
                f"/如影 connect {ip}:<调试端口> [别名]"
            )
        else:
            yield event.plain_result(
                f"❌ 配对失败：{msg[:200]}\n配对码或端口可能已过期（配对弹窗关闭即失效），请重新打开再试。"
            )

    async def _cmd_connect(self, event: AstrMessageEvent, rest: str):
        if err := self._admin_denied(event):
            yield event.plain_result(err)
            return
        toks = rest.split()
        if not toks or not is_ip_port(toks[0]):
            yield event.plain_result(
                "用法：/如影 connect <ip:调试端口> [别名]\n"
                "调试端口是「无线调试」主页面显示的端口（不是配对端口）。"
            )
            return
        ip, port = split_ip_port(toks[0])
        alias = toks[1] if len(toks) > 1 else ""
        try:
            ok, msg = await self.adb.connect(ip, port)
        except AdbError as e:
            yield event.plain_result(f"❌ 连接失败：{e}")
            return
        if not ok:
            yield event.plain_result(
                f"❌ 连接失败：{msg[:200]}\n"
                "若从未配对过，请先执行 /如影 pair；若手机无线调试刚重启，端口可能已变化。"
            )
            return
        dev = self.store.add(ip, port, alias)
        # 顺手确认在线状态与机型
        state = "在线"
        model = ""
        try:
            for d in await self.adb.devices():
                if d["serial"] == f"{ip}:{port}" and d["state"] == "device":
                    model = d.get("model") or ""
        except AdbError:
            pass
        default_mark = "（默认）" if self.store.default()["alias"] == dev["alias"] else ""
        yield event.plain_result(
            f"✅ 已连接并登记设备：{dev['alias']} {default_mark}\n"
            f"serial: {ip}:{port}" + (f"\n机型: {model}" if model else "") + f"\n状态: {state}\n"
            "现在可以用自然语言或 /如影 shot 直接操作这台设备。"
        )

    async def _cmd_devices(self, event: AstrMessageEvent):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        registered = self.store.all()
        default = self.store.default() or {}
        try:
            live = {d["serial"]: d for d in await self.adb.devices()}
        except AdbError as e:
            yield event.plain_result(f"adb 执行失败：{e}")
            return
        if not registered:
            yield event.plain_result("还没有登记任何设备。\n请管理员使用 /如影 pair → /如影 connect 添加。")
            return
        lines = ["📱 已登记设备："]
        for d in registered:
            serial = f"{d['ip']}:{d['port']}"
            info = live.get(serial)
            state = describe_state(info["state"]) if info else "未连接"
            model = (info or {}).get("model") or ""
            mark = "★" if d["alias"] == default.get("alias") else "　"
            line = f"{mark} {d['alias']} —— {serial} · {state}"
            if model:
                line += f" · {model}"
            lines.append(line)
        lines.append("\n★ 为默认设备；/如影 use <别名> 切换。")
        yield event.plain_result("\n".join(lines))

    async def _cmd_use(self, event: AstrMessageEvent, rest: str):
        if err := self._admin_denied(event):
            yield event.plain_result(err)
            return
        key = rest.strip()
        if not key:
            yield event.plain_result(f"用法：/如影 use <别名|ip:端口>。当前设备：{self._device_brief()}")
            return
        dev = self.store.set_default(key)
        if dev:
            yield event.plain_result(f"✅ 默认设备已设为 {dev['alias']}({dev['ip']}:{dev['port']})")
        else:
            yield event.plain_result(f"❌ 找不到设备「{key}」。已登记：{self._device_brief()}")

    async def _cmd_forget(self, event: AstrMessageEvent, rest: str):
        if err := self._admin_denied(event):
            yield event.plain_result(err)
            return
        key = rest.strip()
        if not key:
            yield event.plain_result(f"用法：/如影 forget <别名|ip:端口>。当前设备：{self._device_brief()}")
            return
        dev = self.store.remove(key)
        if dev:
            yield event.plain_result(
                f"✅ 已移除 {dev['alias']}({dev['ip']}:{dev['port']})。"
                + (f"当前默认：{self._device_brief()}" if self.store.all() else "")
            )
        else:
            yield event.plain_result(f"❌ 找不到设备「{key}」。已登记：{self._device_brief()}")

    async def _cmd_disconnect(self, event: AstrMessageEvent, rest: str):
        if err := self._admin_denied(event):
            yield event.plain_result(err)
            return
        key = rest.strip()
        serial = None
        if key:
            dev = self.store.get(key)
            serial = f"{dev['ip']}:{dev['port']}" if dev else (key if is_ip_port(key) else None)
            if serial is None:
                yield event.plain_result(f"❌ 找不到设备「{key}」。已登记：{self._device_brief()}")
                return
        try:
            msg = await self.adb.disconnect(serial)
            yield event.plain_result(f"✅ 已断开：{msg or serial or '全部无线连接'}")
        except AdbError as e:
            yield event.plain_result(f"❌ 断开失败：{e}")

    async def _cmd_status(self, event: AstrMessageEvent):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        try:
            ver = await self.adb.version()
        except AdbError as e:
            yield event.plain_result(f"❌ {e}")
            return
        try:
            live = await self.adb.devices()
        except AdbError as e:
            yield event.plain_result(f"❌ {e}")
            return
        lines = [f"ADB：{ver}", "在线设备："]
        if live:
            for d in live:
                lines.append(
                    f"  {d['serial']} · {describe_state(d['state'])}"
                    + (f" · {d['model']}" if d["model"] else "")
                )
        else:
            lines.append("  （无）")
        lines.append(f"已登记：{self._device_brief()}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 操作子指令
    # ------------------------------------------------------------------
    async def _cmd_shot(self, event: AstrMessageEvent, rest: str):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        serial, e = await self._resolve_serial(rest)
        if e:
            yield event.plain_result(f"❌ {e}")
            return
        await self._ensure_awake(serial)
        try:
            png = await self.adb.screencap(serial)
        except AdbError as ex:
            yield event.plain_result(f"❌ {ex}")
            return
        path = self._save_screenshot(png)
        dims = png_dimensions(png)
        size = f"（{dims[0]}x{dims[1]}）" if dims else ""
        yield event.image_result(path)
        yield event.plain_result(f"📱 截图完成{size}")

    async def _cmd_tap(self, event: AstrMessageEvent, rest: str):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        toks = rest.split()
        if len(toks) < 2 or not all(t.lstrip("-").isdigit() for t in toks[:2]):
            yield event.plain_result("用法：/如影 tap <x> <y>（可用 /如影 shot 查看坐标）")
            return
        serial, e = await self._resolve_serial(toks[2] if len(toks) > 2 else "")
        if e:
            yield event.plain_result(f"❌ {e}")
            return
        try:
            await self.adb.shell(serial, "input", "tap", toks[0], toks[1])
            yield event.plain_result(f"✅ 已点击 ({toks[0]}, {toks[1]})")
        except AdbError as ex:
            yield event.plain_result(f"❌ {ex}")

    async def _cmd_swipe(self, event: AstrMessageEvent, rest: str):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        toks = rest.split()
        if len(toks) < 4 or not all(t.lstrip("-").isdigit() for t in toks[:4]):
            yield event.plain_result("用法：/如影 swipe <x1> <y1> <x2> <y2> [毫秒]")
            return
        dur = toks[4] if len(toks) > 4 and toks[4].isdigit() else "300"
        serial, e = await self._resolve_serial(toks[5] if len(toks) > 5 else "")
        if e:
            yield event.plain_result(f"❌ {e}")
            return
        try:
            await self.adb.shell(serial, "input", "swipe", toks[0], toks[1], toks[2], toks[3], dur)
            yield event.plain_result(f"✅ 已滑动 ({toks[0]},{toks[1]}) → ({toks[2]},{toks[3]})，用时 {dur}ms")
        except AdbError as ex:
            yield event.plain_result(f"❌ {ex}")

    async def _cmd_text(self, event: AstrMessageEvent, rest: str):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        text = rest.strip()
        if not text:
            yield event.plain_result("用法：/如影 text <要输入的内容>")
            return
        toks = text.rsplit(None, 1)
        dev_arg = toks[1] if len(toks) > 1 and self.store.get(toks[1]) else ""
        if dev_arg:
            text = toks[0]
        serial, e = await self._resolve_serial(dev_arg)
        if e:
            yield event.plain_result(f"❌ {e}")
            return
        try:
            result = await self._input_text(serial, text)
            yield event.plain_result(result)
        except AdbError as ex:
            yield event.plain_result(f"❌ {ex}")

    async def _cmd_key(self, event: AstrMessageEvent, rest: str):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        toks = rest.split()
        key = toks[0] if toks else ""
        if not key:
            yield event.plain_result(f"用法：/如影 key <键名>。支持：{'、'.join(KEYCODES.keys())}")
            return
        keycode = KEYCODES.get(key.lower()) or KEYCODES.get(key)
        if not keycode:
            yield event.plain_result(f"不支持的键名「{key}」。支持：{'、'.join(KEYCODES.keys())}")
            return
        serial, e = await self._resolve_serial(toks[1] if len(toks) > 1 else "")
        if e:
            yield event.plain_result(f"❌ {e}")
            return
        try:
            await self.adb.shell(serial, "input", "keyevent", keycode)
            yield event.plain_result(f"✅ 已按键 {key}")
        except AdbError as ex:
            yield event.plain_result(f"❌ {ex}")

    async def _cmd_current(self, event: AstrMessageEvent, rest: str):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        serial, e = await self._resolve_serial(rest)
        if e:
            yield event.plain_result(f"❌ {e}")
            return
        try:
            line = await self.adb.current_activity(serial)
            yield event.plain_result(f"前台应用：{line or '未获取到'}")
        except AdbError as ex:
            yield event.plain_result(f"❌ {ex}")

    async def _cmd_apps(self, event: AstrMessageEvent, rest: str):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        toks = rest.split()
        kw = toks[0].lower() if toks else ""
        dev_arg = toks[1] if len(toks) > 1 and self.store.get(toks[1]) else ""
        serial, e = await self._resolve_serial(dev_arg)
        if e:
            yield event.plain_result(f"❌ {e}")
            return
        try:
            pkgs = await self.adb.list_packages(serial)
        except AdbError as ex:
            yield event.plain_result(f"❌ {ex}")
            return
        if kw:
            pkgs = [p for p in pkgs if kw in p.lower()]
        if not pkgs:
            yield event.plain_result(f"没有匹配「{kw}」的应用。" if kw else "设备上没有第三方应用。")
            return
        shown = pkgs[:50]
        tail = f"\n…… 共 {len(pkgs)} 个" if len(pkgs) > 50 else ""
        yield event.plain_result("已安装应用：\n" + "\n".join(shown) + tail)

    async def _cmd_launch(self, event: AstrMessageEvent, rest: str):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        toks = rest.split()
        if not toks:
            yield event.plain_result("用法：/如影 launch <包名或应用名>（如 com.tencent.mm 或 微信）")
            return
        dev_arg = toks[1] if len(toks) > 1 and self.store.get(toks[1]) else ""
        serial, e = await self._resolve_serial(dev_arg)
        if e:
            yield event.plain_result(f"❌ {e}")
            return
        try:
            pkg = await self._resolve_package(serial, toks[0])
            if not pkg:
                yield event.plain_result(
                    f"❌ 无法识别应用「{toks[0]}」。可用 /如影 apps 查看包名，"
                    "或先用常见应用名（微信/QQ/抖音/B站/淘宝/支付宝…）。"
                )
                return
            out = await self.adb.launch_app(serial, pkg)
            if "error" in out.lower() or "no activities" in out.lower():
                yield event.plain_result(f"❌ 启动 {pkg} 失败：{out[:200]}")
            else:
                yield event.plain_result(f"✅ 已启动 {pkg}")
        except AdbError as ex:
            yield event.plain_result(f"❌ {ex}")

    def _pull_local_name(self, remote: str) -> str:
        """由设备路径取本地文件名；路径异常（空/./..）时用时间戳兜底。"""
        fname = os.path.basename((remote or "").strip().rstrip("/"))
        if fname in ("", ".", ".."):
            fname = f"file_{int(time.time())}"
        return fname

    async def _cmd_pull(self, event: AstrMessageEvent, rest: str):
        if err := self._op_denied(event):
            yield event.plain_result(err)
            return
        toks = rest.split()
        if not toks:
            yield event.plain_result("用法：/如影 pull <设备内路径>")
            return
        dev_arg = toks[1] if len(toks) > 1 and self.store.get(toks[1]) else ""
        serial, e = await self._resolve_serial(dev_arg)
        if e:
            yield event.plain_result(f"❌ {e}")
            return
        remote = toks[0]
        fname = self._pull_local_name(remote)
        local = os.path.join(self.download_dir, fname)
        try:
            await self.adb.pull(serial, remote, local)
        except AdbError as ex:
            yield event.plain_result(f"❌ {ex}")
            return
        sent = False
        if _HAS_FILE:
            try:
                yield event.chain_result([File(name=fname, file=local)])
                sent = True
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"[如影] 文件消息发送失败：{ex}")
        if not sent:
            yield event.plain_result(f"📁 文件已保存到服务器：{local}")
        else:
            yield event.plain_result(f"📁 文件已拉取：{remote}")

    async def _cmd_shell(self, event: AstrMessageEvent, rest: str):
        if err := self._admin_denied(event):
            yield event.plain_result(err)
            return
        if not self._shell_enabled():
            yield event.plain_result(
                "任意 shell 执行默认关闭（高风险）。"
                "请管理员在 WebUI 插件配置中开启「允许执行任意 adb shell 命令」后再使用。"
            )
            return
        cmd = rest.strip()
        if not cmd:
            yield event.plain_result("用法：/如影 shell <命令>（在手机上执行）")
            return
        serial, e = await self._resolve_serial("")
        if e:
            yield event.plain_result(f"❌ {e}")
            return
        try:
            out = await self.adb.shell(serial, cmd, timeout=max(30, self.adb.default_timeout))
        except AdbError as ex:
            yield event.plain_result(f"❌ {ex}")
            return
        if len(out) > 3000:
            out = out[:3000] + f"\n……（输出过长，已截断，共 {len(out)} 字符）"
        yield event.plain_result(f"$ {cmd}\n{out or '（无输出）'}")

    # ------------------------------------------------------------------
    # 内部共用
    # ------------------------------------------------------------------
    async def _input_text(self, serial: str, text: str) -> str:
        """输入文字：ASCII 直接 input text；非 ASCII 需要 ADBKeyboard。"""
        if Adb.is_ascii_text(text):
            await self.adb.shell(serial, "input", "text", Adb.escape_input_text(text))
            return f"✅ 已输入：{text}"
        if await self.adb.has_adb_keyboard(serial):
            quoted = Adb.escape_input_text(text)
            await self.adb.shell(
                serial, "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", quoted
            )
            return f"✅ 已通过 ADBKeyboard 输入：{text}"
        raise AdbError(
            "adb 的 input text 不支持中文等非 ASCII 字符。"
            "请在手机上安装 ADBKeyboard 输入法（github.com/senzhk/ADBKeyBoard）并设为当前输入法后重试；"
            "或改用剪贴板粘贴方案。"
        )

    async def _resolve_package(self, serial: str, name: str) -> str:
        name = (name or "").strip()
        if not name:
            return ""
        low = name.lower()
        if low in COMMON_APPS:
            return COMMON_APPS[low]
        if re.match(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$", low):
            return low
        try:
            pkgs = await self.adb.list_packages(serial)
        except AdbError:
            return ""
        for k, v in COMMON_APPS.items():
            if k in low or low in k:
                return v
        hits = [p for p in pkgs if low in p.lower()]
        return hits[0] if hits else ""

    # ------------------------------------------------------------------
    # 局域网扫描
    # ------------------------------------------------------------------
    async def _scan_candidates(self) -> tuple[list[dict], list[tuple[str, int]]]:
        """发现局域网内的 ADB 设备。

        返回 (连接候选 [{ip, port, sources}], 配对模式设备 [(ip, 配对端口)])。
        """
        connect_groups: list[list[tuple[str, int, str]]] = []
        pairing: list[tuple[str, int]] = []

        # 1) adb 自带 mDNS（零依赖，openscreen 后端正常时可靠）
        try:
            for svc in await self.adb.mdns_services():
                if "pairing" in svc["service"]:
                    pairing.append((svc["ip"], svc["port"]))
                elif "connect" in svc["service"]:
                    connect_groups.append([(svc["ip"], svc["port"], "adb-mdns")])
        except AdbError:
            pass

        # 2) zeroconf 直查广播（不依赖 adb 的 mDNS 后端健康度）
        if discover.HAS_ZEROCONF:
            try:
                res = await discover.mdns_adb_services(timeout=4.5)
                connect_groups.append([(ip, port, "mdns") for ip, port in res["connect"]])
                pairing.extend(res["pairing"])
            except Exception as e:  # noqa: BLE001 - 扫描失败不影响其余策略
                logger.warning(f"[如影] zeroconf 扫描失败：{e}")

        # 3) 经典 5555 端口扫描（adb tcpip 模式的老设备）
        if bool(self._cfg("scan_port_5555", True)):
            ip = discover.local_ipv4()
            prefix = discover.subnet_prefix(ip) if ip else None
            if prefix:
                try:
                    hosts = await discover.sweep_port(prefix, 1, 254, 5555, timeout=0.5)
                    connect_groups.append([(h, 5555, "tcp5555") for h in hosts])
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[如影] 5555 端口扫描失败：{e}")

        return discover.merge_candidates(*connect_groups), sorted(set(pairing))

    def _unique_alias(self, base: str) -> str:
        base = re.sub(r"\s+", "_", (base or "").strip())[:24] or "dev"
        if not self.store.has_alias(base):
            return base
        for i in range(2, 100):
            candidate = f"{base}_{i}"
            if not self.store.has_alias(candidate):
                return candidate
        return f"{base}_{int(time.time())}"

    def _register_scanned(self, ip: str, port: int, model: str) -> str:
        """登记扫描到的设备：已登记直接复用，同 IP 端口变化则更新，新设备按机型起别名。"""
        existing = self.store.get(f"{ip}:{port}")
        if existing:
            return existing["alias"]
        for d in self.store.all():
            if d.get("ip") == ip:
                self.store.update_port(d["alias"], port)
                return d["alias"]
        alias = self._unique_alias(model or ip)
        self.store.add(ip, port, alias)
        return alias

    async def _do_scan(self) -> dict:
        """执行扫描-连接-登记流程，返回结构化结果（指令与 LLM 工具共用）。"""
        candidates, pairing = await self._scan_candidates()
        live = {d["serial"]: d for d in await self.adb.devices()}
        online = {s for s, d in live.items() if d["state"] == "device"}
        already: list[str] = []
        newly: list[tuple[str, str]] = []
        unauthorized: list[tuple[str, str]] = []
        failed: list[str] = []

        for c in candidates:
            serial = f"{c['ip']}:{c['port']}"
            if serial in online:
                already.append(serial)
                continue
            try:
                ok, _ = await self.adb.connect(c["ip"], c["port"])
            except AdbError as e:
                failed.append(f"{serial}（{e}）")
                continue
            if not ok:
                failed.append(serial)
                continue
            state, model = "", ""
            try:
                for d in await self.adb.devices():
                    if d["serial"] == serial:
                        state = d.get("state") or ""
                        model = d.get("model") or ""
            except AdbError:
                pass
            alias = self._register_scanned(c["ip"], c["port"], model)
            if state == "unauthorized":
                unauthorized.append((alias, serial))
            else:
                newly.append((alias, serial))

        if newly and self.store.default() is None:
            self.store.set_default(newly[0][0])
        return {
            "candidates": candidates,
            "pairing": pairing,
            "already": already,
            "newly": newly,
            "unauthorized": unauthorized,
            "failed": failed,
        }

    def _format_scan_report(self, res: dict) -> str:
        default_alias = (self.store.default() or {}).get("alias", "")
        lines = ["🔍 扫描完成："]
        if res["newly"]:
            lines.append("✅ 新连接：")
            for alias, serial in res["newly"]:
                lines.append(f"  {alias}({serial})" + ("（默认）" if alias == default_alias else ""))
        if res["unauthorized"]:
            lines.append("⚠️ 已连接待授权：")
            for alias, serial in res["unauthorized"]:
                lines.append(f"  {alias}({serial}) —— 请在手机上允许调试授权并勾选「一律允许」")
        if res["already"]:
            lines.append("✅ 已在线：" + "、".join(res["already"]))
        if res["failed"]:
            lines.append("❌ 连接失败：" + "、".join(res["failed"]))
        if res["pairing"]:
            lines.append(
                "ⓘ 发现处于配对模式的设备："
                + "、".join(f"{ip}:{port}" for ip, port in res["pairing"])
                + "，请发送 /如影 pair <ip:配对端口> <配对码> 完成配对。"
            )
        return "\n".join(lines)

    async def _cmd_scan(self, event: AstrMessageEvent):
        if err := self._admin_denied(event):
            yield event.plain_result(err)
            return
        yield event.plain_result(
            "🔍 正在扫描局域网（mDNS 无线调试广播 + 经典 5555 端口），约需 5~10 秒……"
        )
        try:
            res = await self._do_scan()
        except AdbError as e:
            yield event.plain_result(f"❌ 扫描失败：{e}")
            return
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"❌ 扫描异常：{e}")
            return

        if not res["candidates"]:
            text = (
                "🔍 扫描完成：未发现任何设备。\n"
                "请确认：① 手机与机器人在同一 Wi-Fi；② 手机「开发者选项 → 无线调试」已开启（Android 11+）；"
                "③ 老设备需先 USB 连接执行 adb tcpip 5555。"
            )
            if res["pairing"]:
                text += "\n\nⓘ 发现处于配对模式的设备：" + "、".join(
                    f"{ip}:{port}" for ip, port in res["pairing"]
                ) + "，请发送 /如影 pair <ip:配对端口> <配对码> 完成配对。"
            yield event.plain_result(text)
            return

        yield event.plain_result(self._format_scan_report(res))

    # ==================================================================
    # LLM 函数调用工具
    # ==================================================================
    def _guard_tool(self, event: AstrMessageEvent) -> str:
        """LLM 工具统一权限入口：管理员/白名单。返回错误文本或空串。"""
        return self._op_denied(event)

    async def _tool_serial(self, device: str) -> tuple[str, str]:
        return await self._resolve_serial(device or "")

    @staticmethod
    def _int(val, name: str) -> int:
        try:
            return int(float(val))
        except (TypeError, ValueError):
            raise AdbError(f"参数 {name} 不是有效数字：{val!r}")

    @filter.llm_tool(name="ruying_screenshot")
    async def tool_screenshot(self, event: AstrMessageEvent, device: str = ""):
        """截取安卓设备当前屏幕并返回截图。若你是多模态模型将直接看到屏幕内容；需要精确控件坐标请再调用 ruying_get_ui。

Args:
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        await self._ensure_awake(serial)
        try:
            png = await self.adb.screencap(serial)
        except AdbError as ex:
            yield f"截图失败：{ex}"
            return
        path = self._save_screenshot(png)
        dims = png_dimensions(png)
        size_txt = f"{dims[0]}x{dims[1]}" if dims else "未知"
        caption = (
            f"截图成功，分辨率 {size_txt}，本地文件 {path}。"
            "如需可点击元素的精确坐标，请调用 ruying_get_ui。"
        )
        if self._vision_enabled():
            yield _png_tool_result(png, caption)
        else:
            yield event.image_result(path)
            yield caption

    @filter.llm_tool(name="ruying_get_ui")
    async def tool_get_ui(self, event: AstrMessageEvent, device: str = ""):
        """获取安卓设备当前屏幕的界面层级：屏幕分辨率与可点击/含文本元素的坐标、文字、资源 ID，用于确定点击或滑动的位置。看不懂界面内容时配合 ruying_screenshot 使用。

Args:
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        await self._ensure_awake(serial)
        try:
            xml = await self.adb.ui_dump(serial)
        except AdbError as ex:
            yield f"获取界面层级失败：{ex}"
            return
        parsed = parse_ui_hierarchy(xml)
        if parsed.get("error"):
            yield f"解析界面数据失败：{parsed['error']}"
            return
        if not parsed.get("screen"):
            try:
                parsed["screen"] = await self.adb.screen_size(serial)
            except AdbError:
                pass
        yield format_ui_text(parsed)

    @filter.llm_tool(name="ruying_tap")
    async def tool_tap(self, event: AstrMessageEvent, x: float = 0, y: float = 0, device: str = ""):
        """在安卓设备屏幕上点击指定坐标。请先用 ruying_get_ui 或 ruying_screenshot 确定目标位置。

Args:
        x(number): 横向坐标（像素，屏幕左上角为原点）
        y(number): 纵向坐标（像素）
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        await self._ensure_awake(serial)
        try:
            px, py = self._int(x, "x"), self._int(y, "y")
            await self.adb.shell(serial, "input", "tap", str(px), str(py))
            yield f"已点击 ({px},{py})。可用 ruying_screenshot 确认结果。"
        except AdbError as ex:
            yield f"点击失败：{ex}"

    @filter.llm_tool(name="ruying_swipe")
    async def tool_swipe(
        self,
        event: AstrMessageEvent,
        x1: float = 0,
        y1: float = 0,
        x2: float = 0,
        y2: float = 0,
        duration_ms: float = 300,
        device: str = "",
    ):
        """在安卓设备屏幕上从一点滑动到另一点（可用于滚动列表、翻页、返回手势）。

Args:
        x1(number): 起点横向坐标（像素）
        y1(number): 起点纵向坐标（像素）
        x2(number): 终点横向坐标（像素）
        y2(number): 终点纵向坐标（像素）
        duration_ms(number): 滑动总时长（毫秒），默认 300
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        await self._ensure_awake(serial)
        try:
            vals = [self._int(v, n) for v, n in ((x1, "x1"), (y1, "y1"), (x2, "x2"), (y2, "y2"))]
            dur = max(50, self._int(duration_ms, "duration_ms"))
            await self.adb.shell(serial, "input", "swipe", *[str(v) for v in vals], str(dur))
            yield f"已滑动 ({vals[0]},{vals[1]}) → ({vals[2]},{vals[3]})，用时 {dur}ms。"
        except AdbError as ex:
            yield f"滑动失败：{ex}"

    @filter.llm_tool(name="ruying_input_text")
    async def tool_input_text(self, event: AstrMessageEvent, text: str = "", device: str = ""):
        """向安卓设备当前焦点输入框输入文字。英文数字可直接输入；中文需要设备已安装并启用 ADBKeyboard 输入法。输入前通常需要先点击目标输入框。

Args:
        text(string): 要输入的文字内容
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        if not str(text or "").strip():
            yield "未提供要输入的文字。"
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        await self._ensure_awake(serial)
        try:
            yield await self._input_text(serial, str(text))
        except AdbError as ex:
            yield f"输入失败：{ex}"

    @filter.llm_tool(name="ruying_press_key")
    async def tool_press_key(self, event: AstrMessageEvent, key: str = "", device: str = ""):
        """按下安卓设备的系统按键，如返回、主页、最近任务、电源、音量等。支持：back/home/menu/power/wake/sleep/volume_up/volume_down/enter/del/tab/recents/notification/相机。

Args:
        key(string): 键名（back、home、recents、volume_up 等，支持中文如「返回」「主页」）
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        k = str(key or "").strip()
        keycode = KEYCODES.get(k.lower()) or KEYCODES.get(k)
        if not keycode:
            yield f"不支持的键名「{k}」。支持：{'、'.join(KEYCODES.keys())}"
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        try:
            await self.adb.shell(serial, "input", "keyevent", keycode)
            yield f"已按键 {k}。"
        except AdbError as ex:
            yield f"按键失败：{ex}"

    @filter.llm_tool(name="ruying_current_app")
    async def tool_current_app(self, event: AstrMessageEvent, device: str = ""):
        """查询安卓设备当前前台的应用/Activity。

Args:
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        try:
            line = await self.adb.current_activity(serial)
            yield f"前台应用：{line or '未获取到（可能系统版本不兼容）'}"
        except AdbError as ex:
            yield f"查询失败：{ex}"

    @filter.llm_tool(name="ruying_list_apps")
    async def tool_list_apps(self, event: AstrMessageEvent, keyword: str = "", device: str = ""):
        """列出安卓设备上已安装的第三方应用包名，可按关键词过滤。用于找到要启动的应用的包名。

Args:
        keyword(string): 过滤关键词（包含匹配，可为空）
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        try:
            pkgs = await self.adb.list_packages(serial)
        except AdbError as ex:
            yield f"查询失败：{ex}"
            return
        kw = str(keyword or "").strip().lower()
        if kw:
            pkgs = [p for p in pkgs if kw in p.lower()]
        if not pkgs:
            yield f"没有匹配「{kw}」的应用。" if kw else "设备上没有第三方应用。"
            return
        shown = pkgs[:60]
        tail = f"\n…… 共 {len(pkgs)} 个" if len(pkgs) > 60 else ""
        yield "已安装应用（包名）：\n" + "\n".join(shown) + tail

    @filter.llm_tool(name="ruying_launch_app")
    async def tool_launch_app(self, event: AstrMessageEvent, app: str = "", device: str = ""):
        """在安卓设备上启动一个应用。app 可为包名或常见应用名（如 微信/QQ/抖音/B站/淘宝/支付宝/设置 等），无法识别时会返回建议。

Args:
        app(string): 包名（如 com.tencent.mm）或应用名称（如 微信）
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        await self._ensure_awake(serial)
        try:
            pkg = await self._resolve_package(serial, str(app or ""))
            if not pkg:
                yield (
                    f"无法识别应用「{app}」。可调用 ruying_list_apps 查看包名，"
                    "或使用常见应用名（微信/QQ/抖音/B站/淘宝/支付宝/京东/拼多多/美团/高德地图/网易云音乐/小红书/微博/知乎/设置）。"
                )
                return
            out = await self.adb.launch_app(serial, pkg)
            if "error" in out.lower() or "no activities" in out.lower():
                yield f"启动 {pkg} 失败：{out[:200]}"
            else:
                yield f"已启动 {pkg}。可用 ruying_screenshot 或 ruying_current_app 确认。"
        except AdbError as ex:
            yield f"启动失败：{ex}"

    @filter.llm_tool(name="ruying_pull_file")
    async def tool_pull_file(self, event: AstrMessageEvent, remote_path: str = "", device: str = ""):
        """从安卓设备拉取一个文件（如截图、录音、文档）到机器人服务器，并尝试发送到当前会话。

Args:
        remote_path(string): 设备内的文件绝对路径（如 /sdcard/DCIM/photo.jpg）
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        remote = str(remote_path or "").strip()
        if not remote:
            yield "未提供设备内文件路径。"
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        fname = self._pull_local_name(remote)
        local = os.path.join(self.download_dir, fname)
        try:
            await self.adb.pull(serial, remote, local)
        except AdbError as ex:
            yield f"拉取失败：{ex}"
            return
        if _HAS_FILE:
            try:
                yield event.chain_result([File(name=fname, file=local)])
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"[如影] 文件消息发送失败：{ex}")
        yield f"文件已从设备拉取并保存：{local}"

    @filter.llm_tool(name="ruying_shell")
    async def tool_shell(self, event: AstrMessageEvent, command: str = "", device: str = ""):
        """在安卓设备上执行任意 adb shell 命令并返回输出（高风险，默认关闭，仅管理员）。仅在其他工具无法满足需求时使用。

Args:
        command(string): 要执行的 shell 命令（如 settings put global window_animation_scale 0）
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._admin_denied(event):
            yield err
            return
        if not self._shell_enabled():
            yield "任意 shell 执行已在插件配置中关闭（高风险能力默认关闭）。请管理员在 WebUI 中开启后再试。"
            return
        cmd = str(command or "").strip()
        if not cmd:
            yield "未提供要执行的命令。"
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        try:
            out = await self.adb.shell(serial, cmd, timeout=max(30, self.adb.default_timeout))
        except AdbError as ex:
            yield f"执行失败：{ex}"
            return
        if len(out) > 4000:
            out = out[:4000] + f"\n……（输出过长已截断，共 {len(out)} 字符）"
        yield f"$ {cmd}\n{out or '（命令执行完成，无输出）'}"

    @filter.llm_tool(name="ruying_scan_devices")
    async def tool_scan_devices(self, event: AstrMessageEvent):
        """扫描局域网并自动连接发现的安卓设备（仅管理员可用）。当用户要求"扫描/发现/连接手机"或设备尚未登记时使用；前提是手机已开启「无线调试」且与机器人同一 Wi-Fi。
    """
        if err := self._admin_denied(event):
            yield err
            return
        try:
            res = await self._do_scan()
        except AdbError as ex:
            yield f"扫描失败：{ex}"
            return
        except Exception as ex:  # noqa: BLE001
            yield f"扫描异常：{ex}"
            return
        parts = []
        if res["newly"]:
            default_alias = (self.store.default() or {}).get("alias", "")
            items = ", ".join(
                f"{alias}({serial})" + ("，已设为默认设备" if alias == default_alias else "")
                for alias, serial in res["newly"]
            )
            parts.append(f"新连接 {len(res['newly'])} 台：{items}")
        if res["unauthorized"]:
            parts.append(
                "已连接但待授权（需用户在手机弹窗中允许调试授权）："
                + ", ".join(f"{alias}({serial})" for alias, serial in res["unauthorized"])
            )
        if res["already"]:
            parts.append("原本已在线：" + ", ".join(res["already"]))
        if res["failed"]:
            parts.append("连接失败：" + ", ".join(res["failed"]))
        if res["pairing"]:
            parts.append(
                "发现处于配对模式的设备："
                + ", ".join(f"{ip}:{port}" for ip, port in res["pairing"])
                + "。自动配对需要 6 位配对码，请让用户在手机「无线调试→使用配对码配对设备」中查看，并由管理员用 /如影 pair 指令完成。"
            )
        if not parts:
            parts.append(
                "未发现任何设备。可能原因：手机与机器人不在同一 Wi-Fi；「开发者选项→无线调试」未开启；"
                "老设备需先 USB 执行 adb tcpip 5555。"
            )
        yield "扫描完成：" + "；".join(parts)

    @filter.llm_tool(name="ruying_device_status")
    async def tool_device_status(self, event: AstrMessageEvent, device: str = ""):
        """查询安卓设备状态：电量与充电状态、系统版本、屏幕分辨率、前台应用。

Args:
        device(string): 设备别名或 IP:端口，留空使用默认设备
    """
        if err := self._guard_tool(event):
            yield err
            return
        serial, e = await self._tool_serial(device)
        if e:
            yield e
            return
        parts = []
        try:
            bat = await self.adb.battery(serial)
            pct = bat.get("percent")
            if pct is not None:
                parts.append(f"电量 {pct}%{'（充电中）' if bat.get('charging') else ''}")
        except AdbError:
            pass
        try:
            ver = await self.adb.android_version(serial)
            if ver:
                parts.append(f"Android {ver}")
        except AdbError:
            pass
        try:
            size = await self.adb.screen_size(serial)
            if size:
                parts.append(f"分辨率 {size[0]}x{size[1]}")
        except AdbError:
            pass
        try:
            line = await self.adb.current_activity(serial)
            if line:
                parts.append(f"前台 {line}")
        except AdbError:
            pass
        yield f"设备 {serial} 状态：" + ("；".join(parts) if parts else "部分信息获取失败（设备仍在线）")
