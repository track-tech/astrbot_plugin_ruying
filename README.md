# 如影 · astrbot_plugin_ruying

> 如影随形：机器人像影子一样“看到”并“操作”你的安卓设备。

AstrBot 插件——通过**无线 ADB**（Android 11+ 无线调试，无需 USB 线、无需 root）把安卓手机/平板交给机器人：LLM 可自主截屏看屏、读取界面控件坐标、点击/滑动/输入文字、启动应用、拉取文件；也可以在聊天里用指令直接操作。

适配 AstrBot `>=4.10.4,<5`（已在 **v4.28.0** 源码级核验：LLM 工具、权限、截图注入 LLM、命令解析等依赖 API 均一致）。纯 Python 标准库实现，无额外 Python 依赖。

## 功能总览

| 能力 | 说明 | 权限 |
| --- | --- | --- |
| 设备发现 | `/如影 scan` 扫描局域网自动连接：mDNS 广播（Android 11+）+ adb mdns + 5555 端口 | 仅管理员 |
| 屏幕理解 | 截图（多模态模型可直接“看到”）、uiautomator 控件坐标 | 管理员 / 白名单 |
| 触控输入 | 点击、滑动、文字输入、系统按键（返回/主页/电源/音量…） | 管理员 / 白名单 |
| 应用与文件 | 查询/启动应用、前台应用、文件拉取、电量状态 | 管理员 / 白名单 |
| 设备管理 | 无线调试配对、连接登记、多设备/默认设备切换 | 仅管理员 |
| 任意 Shell | 在手机上执行任意命令（**默认关闭**） | 仅管理员 + 配置开关 |

## 安装

1. 把本目录放入 AstrBot 的 `data/plugins/astrbot_plugin_ruying/`（或通过插件市场/仓库安装）。`requirements.txt` 中的 `zeroconf` 会随插件自动安装，用于局域网 mDNS 设备发现；缺失时扫描自动降级。
2. 在**宿主机**安装 [Android platform-tools](https://developer.android.com/tools/releases/platform-tools)（含 `adb`）：
   - Windows：解压后可在插件配置 `adb_path` 填完整路径，如 `C:\platform-tools\adb.exe`；
   - Linux/macOS：`apt install adb` / `brew install android-platform-tools`，或加入 PATH。
3. 手机与 AstrBot 所在机器需在**同一局域网**。

## 快速开始（Android 11+ 无线调试）

在聊天里依次执行（均为管理员指令）：

```
1. 手机：设置 → 开发者选项 → 无线调试 → 开启
2. 点「使用配对码配对设备」，弹出 IP、配对端口、6 位配对码
3. /如影 pair 192.168.1.23:34567 123456     ← IP:配对端口 + 配对码，即开即用
4. 回到「无线调试」主页面，记下那里显示的 IP:端口（与配对端口不同）
5. /如影 connect 192.168.1.23:37855 我的手机 ← IP:调试端口 + 可选取别名
6. 直接对机器人说：「帮我在手机上打开B站搜一下如影」🎉
```

> **懒人路线**：手机开着无线调试时，直接发 `/如影 scan`，或用自然语言说「帮我扫一下局域网连接手机」，插件会通过 mDNS 广播自动发现并连接（发现处于配对模式的设备也会提示你去配对），大多数情况可以跳过上面的手动步骤。
>
> 老设备（Android 10-）走经典方案：USB 连一次执行 `adb tcpip 5555` 后拔线，再 `/如影 connect <ip>:5555`。
> 「无线调试」关闭再开启后**端口会变**——插件默认开启自动重连，失败时会通过 adb mDNS 自动重发现新端口；也可手动 `/如影 connect <ip:新端口>` 更新。

## 聊天指令

### 管理（仅管理员）

| 指令 | 说明 |
| --- | --- |
| `/如影 pair <ip:配对端口> <配对码>` | 首次配对（配对码弹窗关闭即失效，要即开即用） |
| `/如影 connect <ip:调试端口> [别名]` | 连接并登记设备，默认成为默认设备 |
| `/如影 devices` | 已登记设备与在线状态（★ 为默认） |
| `/如影 use <别名\|ip:端口>` | 切换默认设备 |
| `/如影 forget <别名\|ip:端口>` | 移除登记 |
| `/如影 disconnect [ip:端口]` | 断开 adb 连接 |
| `/如影 scan` | 扫描局域网并自动连接发现的设备（mDNS + 5555 端口） |
| `/如影 status` | adb 版本与当前在线设备 |

### 操作（管理员 / 白名单会话）

| 指令 | 说明 |
| --- | --- |
| `/如影 shot [设备]` | 截屏并发送到会话 |
| `/如影 tap <x> <y>` | 点击坐标 |
| `/如影 swipe <x1> <y1> <x2> <y2> [毫秒]` | 滑动 |
| `/如影 text <内容>` | 输入文字（中文需设备安装 ADBKeyboard，见 FAQ） |
| `/如影 key <键名>` | back/home/menu/power/volume_up/volume_down/enter/del/recents/wake/sleep（支持中文：返回/主页/音量加…） |
| `/如影 current` | 当前前台应用 |
| `/如影 apps [关键词]` | 列出第三方应用包名 |
| `/如影 launch <包名或应用名>` | 启动应用（支持 微信/QQ/抖音/B站 等常用名） |
| `/如影 pull <设备内路径>` | 拉取文件并发送 |
| `/如影 shell <命令>` | 任意 shell（需在配置中开启，仅管理员） |

## LLM 函数工具

日常使用**不需要记指令**：开启函数调用后，LLM 会自主组合以下工具完成任务（名称带 `ruying_` 前缀）：

- `ruying_screenshot` —— 截屏。默认降采样为长边 720 的 JPEG 回传给模型（本地保留原始 PNG），支持 `region` 局部截图、`digest` 纯文字摘要、无变化自动去重；配置 `enable_vision` 开启且模型支持视觉时图片直接注入 LLM 上下文。
- `ruying_get_ui` —— 读取当前界面可交互元素的**精确坐标/文字/资源 ID**（uiautomator），支持 `max_nodes` 数量上限与 `filter_kw` 关键词过滤。这是点击定位的首选。
- `ruying_tap_and_wait` —— 点击后轮询等待界面稳定，直接返回变化摘要（新增/消失元素）与当前界面，**无需再截图**。
- `ruying_wait_for` —— 轮询等待指定文字/资源 ID 出现（如页面加载完成，默认等 8 秒），出现即返回，**替代反复截图**。
- `ruying_tap` / `ruying_swipe` / `ruying_input_text` / `ruying_press_key` —— 触控与输入。
- `ruying_current_app` / `ruying_list_apps` / `ruying_launch_app` —— 应用查询与启动。
- `ruying_pull_file` —— 从设备拉取文件发送到会话。
- `ruying_device_status` —— 电量/充电、系统版本、分辨率、前台应用。
- `ruying_scan_devices` —— 扫描局域网并自动连接设备（仅管理员，自然语言说「帮我扫一下局域网连接手机」即可触发）。
- `ruying_auto` —— **子 agent**：把多步任务（如「打开B站搜索如影并进入第一个视频」）交给如影自主完成，模型可从已配置的模型商中任选，独立上下文不污染主对话；单步操作请用上面的单步工具。
- `ruying_shell` —— 任意命令（默认关闭，仅管理员）。

## 配置项（WebUI 插件配置）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `adb_path` | `adb` | adb 可执行文件路径 |
| `command_timeout` | 20 | 单条 adb 命令超时（秒） |
| `auto_reconnect` | true | 操作前自动重连（含 mDNS 端口重发现） |
| `scan_port_5555` | true | `/如影 scan` 时是否扫描内网 5555 端口（老设备） |
| `auto_wake` | true | 截屏/看控件/触控前自动唤醒熄屏的设备（上滑尝试过锁屏；有密码只能到锁屏页） |
| `shot_max_edge` | 720 | 回传给 LLM 的截图长边上限（0=原图），本地保留原始 PNG |
| `shot_quality` | 70 | 回传给 LLM 的截图 JPEG 质量 |
| `shot_dedup` | true | 无操作间隔的重复截图只回「屏幕未变化」文字，不回图 |
| `keep_downloads` | 20 | pull 拉取文件本地保留数量，超出自动清理最旧的 |
| `subagent_enabled` | true | 启用子 agent 工具 `ruying_auto` |
| `subagent_provider_id` | （空） | 子 agent 使用的模型商 ID，留空跟随会话模型 |
| `subagent_max_steps` | 15 | 子 agent 单次任务最大步数 |
| `whitelist` | [] | 额外授权的会话：完整 `unified_msg_origin`（如 `aiocqhttp:GroupMessage:12345`）或纯会话 ID |
| `enable_shell_tool` | false | 允许 LLM/指令执行任意 shell（高风险） |
| `enable_vision` | true | 截图以图片返回给多模态模型（模型不支持时自动降级） |
| `keep_screenshots` | 10 | 本地保留截图数量 |

## 子 agent（多步任务自主模式）

在聊天里说「帮我在手机上打开B站搜一下如影」这类多步任务时，主对话模型可调用 `ruying_auto` 把整个任务交给**如影子 agent**：

- 独立上下文：中途的 UI 读取、截图摘要全部留在子 agent 内部，只回最终结果——主对话历史不被污染，这是省 token 的结构性方案
- 模型可自定义：`/如影 providers` 查看已配置的模型商，`/如影 agent_provider <ID>` 为子 agent 单独选一个模型（建议选**带视觉能力**的，看屏更准；主对话模型可以继续用便宜的纯文本模型）
- 权限延续：子 agent 里的工具仍受管理员/白名单约束
- 单步操作（截个图、点一下）直接说就行，不必进子 agent

## 安全建议

- 设备操作等同于把手机交出去，`whitelist` 建议只加**私人会话**。
- `enable_shell_tool` 保持关闭，除非你明确需要；开启后仅管理员可用，但任意命令可造成不可逆后果（卸载、清数据等）。
- 配对操作会改变设备调试授权状态，请确认操作环境网络可信。

## 常见问题

- **中文输入不动？** `adb shell input text` 系统限制只支持 ASCII。方案：手机安装 [ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard) 输入法并启用，插件会自动检测并改用广播输入。
- **重启手机/重开无线调试后连不上？** 端口变了。保持 `auto_reconnect` 开启（mDNS 自动重发现），或重新 `/如影 connect`。
- **`ruying_get_ui` 返回空元素？** 目标应用禁止辅助功能读取（游戏、部分视频页）。改用 `ruying_screenshot` 让模型按截图估算坐标。
- **截图一片黑？** 熄屏导致的黑屏已由 `auto_wake` 自动处理（v0.3.6 起）；剩余场景是部分应用（银行/视频 DRM）主动禁止截屏，属系统层面限制。
- **子 agent 长任务做到一半被截断？** 平台对单次工具调用有超时上限（默认约 300 秒）。在 AstrBot 配置中调大 `tool_call_timeout`，或把大任务拆成多个小任务分派。
- **中文输入不动？** v0.4.1 起优先走**剪贴板粘贴**（无需安装任何东西；原生/Pixel/多数海外 ROM 可用）。ColorOS/部分国产 ROM 会终止 shell 的剪贴板访问，此时仍需安装 [ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard) 并设为当前输入法；临时方案是让模型改输英文或 URL。
- **改了 adb_path 还是报「找不到 adb：adb」？** 生效时机分三种：① WebUI 插件配置里修改并保存 → **立即生效**（v0.3.4 起惰性读取，无需重载）；② 手改配置文件（data/config/xxx_config.json）→ 需重载插件；③ 若重载发生在**同一轮对话进行中**，该轮使用的工具集在开始时已快照、仍指向旧插件实例（AstrBot 核心层行为），新开一轮对话即恢复。
- **模型看不到截图？** 需要同时满足：`enable_vision` 开启 + 使用多模态模型 + AstrBot 版本支持工具图片注入。不满足时自动降级为“图片发给用户 + 文字给模型”。

## 开发与测试

```bash
python _test/test_ruying.py   # 桩模块离线测试（FakeAdb，115 项断言）
```

目录结构：

```
astrbot_plugin_ruying/
├── main.py            # 插件入口：指令、LLM 工具、设备解析/自动重连
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt   # zeroconf（局域网发现）
└── ruying_core/
    ├── adb.py         # adb asyncio 封装与解析纯函数
    ├── devices.py     # 设备注册表（JSON 持久化）
    ├── discover.py    # 局域网发现：mDNS 广播 + 5555 端口扫描
    ├── screen.py      # uiautomator XML / PNG 解析
    └── safety.py      # 管理员 + 白名单校验
```
