"""如影核心：权限校验（管理员 + 白名单）。"""

from __future__ import annotations

from typing import Iterable, Optional


def is_admin(event) -> bool:
    try:
        return bool(event.is_admin())
    except Exception:  # noqa: BLE001
        return False


def normalize_entries(entries: Optional[Iterable]) -> list[str]:
    out = []
    for e in entries or []:
        s = str(e).strip()
        if s:
            out.append(s)
    return out


def whitelisted(event, entries: Optional[Iterable]) -> bool:
    """白名单条目可为完整 unified_msg_origin 或纯会话 ID，命中其一即通过。"""
    items = normalize_entries(entries)
    if not items:
        return False
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    sid = ""
    try:
        sid = str(event.get_session_id() or "")
    except Exception:  # noqa: BLE001
        pass
    return any(e == umo or e == sid for e in items)


def op_allowed(event, whitelist: Optional[Iterable]) -> bool:
    """日常设备操作（指令/LLM 工具）权限：管理员或白名单会话。"""
    return is_admin(event) or whitelisted(event, whitelist)
