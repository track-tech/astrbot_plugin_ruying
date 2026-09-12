"""如影核心：屏幕数据解析（uiautomator XML / PNG 头）。"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from typing import Optional

BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def png_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    """从 PNG 字节解析宽高（IHDR 块），失败返回 None。"""
    if (
        isinstance(data, (bytes, bytearray))
        and len(data) >= 24
        and bytes(data[:8]) == b"\x89PNG\r\n\x1a\n"
        and bytes(data[12:16]) == b"IHDR"
    ):
        w, h = struct.unpack(">II", bytes(data[16:24]))
        return w, h
    return None


def parse_ui_hierarchy(xml_text: str, max_lines: int = 90) -> dict:
    """解析 uiautomator dump 的 XML，产出适合 LLM 阅读的紧凑元素列表。

    仅保留 可点击 / 有文本 / 有 content-desc 的节点，避免输出冗长无用的
    布局容器；坐标为元素中心点（可直接用于 input tap）。
    """
    result: dict = {"screen": None, "lines": [], "elements": [], "error": None}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        result["error"] = f"界面数据不是有效 XML（{e}）"
        return result

    def bounds_center(b: str) -> Optional[tuple[int, int, int, int]]:
        m = BOUNDS_RE.search(b or "")
        if not m:
            return None
        l, t, r, btm = (int(m.group(i)) for i in range(1, 5))
        if r <= l or btm <= t:
            return None
        return l, t, r, btm

    # 根节点 bounds 的右/下即屏幕尺寸
    for node in root.iter("node"):
        bb = bounds_center(node.get("bounds", ""))
        if bb:
            result["screen"] = (bb[2], bb[3])
            break

    seen_bounds: set = set()
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        rid = (node.get("resource-id") or "").strip()
        clickable = (node.get("clickable") or "").lower() == "true"
        if not (clickable or text or desc):
            continue
        bb = bounds_center(node.get("bounds", ""))
        if not bb:
            continue
        l, t, r, btm = bb
        cx, cy = (l + r) // 2, (t + btm) // 2
        key = (cx, cy, text, desc, clickable)
        if key in seen_bounds:
            continue
        seen_bounds.add(key)

        parts = [f"({cx},{cy})"]
        if clickable:
            parts.append("[可点]")
        if text:
            parts.append(f'文本="{text}"')
        if desc:
            parts.append(f'描述="{desc}"')
        if rid:
            parts.append(f'id="{rid}"')

        result["elements"].append(
            {"x": cx, "y": cy, "text": text, "desc": desc, "id": rid, "clickable": clickable}
        )
        if len(result["lines"]) < max_lines:
            result["lines"].append(" ".join(parts))

    return result


def format_ui_text(parsed: dict, fallback_screen: Optional[tuple[int, int]] = None) -> str:
    """把 parse_ui_hierarchy 的结果格式化为给 LLM 的文本。"""
    screen = parsed.get("screen") or fallback_screen
    lines: list[str] = []
    if screen:
        lines.append(f"屏幕分辨率：{screen[0]}x{screen[1]}（坐标可直接用于点击/滑动）")
    elems = parsed.get("elements") or []
    body = parsed.get("lines") or []
    if not body:
        lines.append(
            "未读到任何可交互元素：目标界面可能禁止辅助功能读取（游戏/视频页），"
            "请改用 ruying_screenshot 观察后按比例估算坐标。"
        )
    else:
        extra = len(elems) - len(body)
        lines.append(f"可交互元素 {len(elems)} 个：")
        lines.extend(body)
        if extra > 0:
            lines.append(f"……（还有 {extra} 个未显示）")
    return "\n".join(lines)
