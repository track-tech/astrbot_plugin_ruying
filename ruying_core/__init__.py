"""如影（astrbot_plugin_ruying）核心逻辑包。"""

from .adb import Adb, AdbError, COMMON_APPS, KEYCODES, describe_state, is_ip_port, split_ip_port
from .devices import DeviceRegistry
from .screen import format_ui_text, parse_ui_hierarchy, png_dimensions
from .safety import is_admin, op_allowed, whitelisted

__all__ = [
    "Adb",
    "AdbError",
    "COMMON_APPS",
    "KEYCODES",
    "DeviceRegistry",
    "describe_state",
    "format_ui_text",
    "is_admin",
    "is_ip_port",
    "op_allowed",
    "parse_ui_hierarchy",
    "png_dimensions",
    "split_ip_port",
    "whitelisted",
]
