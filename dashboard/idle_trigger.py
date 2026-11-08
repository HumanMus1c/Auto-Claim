"""空闲补跑：计划任务「空闲触发」的本地闸门。

每个签到任务除了每天的随机时刻，还挂了一条空闲触发器
（``MSFT_TaskIdleTrigger``）：机器空闲下来就立刻跑一次，专门兜住「睡了一整天」
「断电重启后整组时刻全错过」这类情况。随机时刻负责分散、避免固定模式，空闲
触发负责兜底，两条线并行。

**「空闲」是谁判的、按什么判**：判定不在本模块，也不是看内存或电源功率。
Windows 8 以后由系统电源子系统的「用户在场」检测决定，默认连续 **4 分钟**
没有键鼠输入即视为用户离开；任务计划程序同时要求所有处理器和磁盘在检测
区间内至少 80% 的时间处于空闲（Windows 7 是 90%）。内存占用不参与判定；
电源功率也不参与，但 AC/DC 切换和电量变化会触发一次重新检测。另外任何设置
了 ``ES_DISPLAY_REQUIRED`` 的程序（全屏视频、演示）会强制系统「不空闲」。

注意 ``IdleSettings/Duration`` 自 Windows 8 起**已弃用**（文档明确说明不再
使用，只是任务计划程序 UI 里还留着这个输入框）。所以注册时写的
``IDLE_MINUTES`` 只是让任务 XML 与真实行为对得上，真正生效的是系统那 4 分钟。

**为什么还要闸门**：空闲触发是按「进入空闲」点火的，用户每离开工位一次就会
响一次。一次签到要发请求、必要时还得把桌面端拉起来，没完没了地跑既费电，
也容易让账号活动显得异常。这里加一道很短的时间冷却：**最近 30 分钟内成功
跑过就跳过**。失败不进冷却——失败重试是另一条链路（控制台会在同一个进程里
立刻重试一次），冷却会把重试挡掉。

**为什么是 30 分钟而不是「今天跑过就跳过」**：后者看着更省，但在「机器一直
开着、人不在」的场景里会把当天剩下的定时点全部吃掉——那些点本来正是为了
「睡过去一次还有下一次」才排的。30 分钟比同一条任务里最密的定时间隔（Trae /
Qoder 首尾两点 45 分钟）还短，所以永远不会误伤正常的定时执行，只在「刚跑完
又立刻空闲」时去重。

判断「这次是不是空闲触发」用 ``GetLastInputInfo``：空闲触发点火时用户至少
已经离开 4 分钟，进程启动时若读到空闲时长超过 ``DETECT_SECONDS``，几乎只
可能来自它。定时触发偶尔也会撞上用户离开工位，但那时冷却窗口通常早就过了，
最多是把一次多余的补跑挡掉。

状态按模块各存一份小 JSON（``idle-guard-<kind>.json``）：三个脚本可能在同一
秒里各写各的，共用一份文件就得处理并发读改写，分开存最省事。

读写失败一律按「不跳过」处理——宁可多跑一次，也不能因为状态文件损坏把签到
彻底挡住。
"""

import datetime
import json
import os
import time

# 写进任务 XML 的空闲时长。Windows 8 以后该字段已弃用（实际由系统按
# 「4 分钟无输入 + CPU/磁盘不忙」判定），这里仍写 4：让任务计划程序 UI 里
# 显示的数字和真实行为一致，而不是写 10 却 4 分钟就点火。
IDLE_MINUTES = 4

# 进程启动时判定「这次由空闲触发」的门槛。必须明显低于系统那 4 分钟的缺席
# 门槛：点火时进程读到的空闲时长只会 ≥4 分钟，留 2 分钟余量兼容时钟粒度与
# 调度延迟；2 分钟也足以把「用户刚点完立即运行」排除在外。
DETECT_SECONDS = 2 * 60

# 冷却窗口。必须短于同一条任务里最密的定时间隔（Trae / Qoder 的 1 小时
# 基准点叠加正向抖动后，最小间隔 45 分钟），否则会吃掉正常的定时执行。
COOLDOWN_SECONDS = 30 * 60

STATE_PREFIX = "idle-guard-"
KINDS = ("workbuddy", "qoder", "trae")

# 本次进程里刚刚跳过的模块。app.py 用它来决定「跳过时不要重排触发时刻」——
# 否则每次空闲被跳过都会把当天已过的点重新抽一遍，白写一次任务。
_SKIPPED = {}

_LAST_INPUT = None


def default_data_dir():
    """状态落盘目录，与控制台 _diag_dir() / win_service 保持同一路径。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Auto-Claim", "dashboard")


def state_path(kind, data_dir=None):
    return os.path.join(data_dir or default_data_dir(),
                        STATE_PREFIX + str(kind) + ".json")


def _last_input_struct():
    """构造 GetLastInputInfo 的结构体类型；延迟到第一次调用才建。"""
    global _LAST_INPUT
    if _LAST_INPUT is None:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT),
                        ("dwTime", wintypes.DWORD)]

        _LAST_INPUT = LASTINPUTINFO
    return _LAST_INPUT


def idle_seconds():
    """用户已经多久没有键鼠输入（秒）；非 Windows 或取不到时返回 0。

    ``GetLastInputInfo`` 的 dwTime 只有 32 位，按 2^32 取模才能跨过
    49.7 天的回绕——长期不关机的机器上正好会踩到。
    """
    if os.name != "nt":
        return 0.0
    try:
        import ctypes

        info = _last_input_struct()()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount64()
        elapsed = (tick - int(info.dwTime)) & 0xFFFFFFFF
        return elapsed / 1000.0
    except Exception:
        # 拿不到空闲时长只意味着「判定不出是空闲触发」，按定时触发处理
        return 0.0


def read_state(kind, data_dir=None):
    """读取某模块上次的结果；文件缺失或损坏都当作空状态。"""
    try:
        with open(state_path(kind, data_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(kind, ok, data_dir=None, now=None):
    """记下这次运行的结果；永不抛错。

    ``ok`` 决定下一次空闲还能不能跑：只有成功才进冷却，失败保持可重试。
    控制台的失败自愈会在同一个进程里立刻重试一次，重试同样要过
    ``should_skip``；如果失败也冷却，这次重试会被自己挡掉，还会被误报成
    「已自动恢复」。
    """
    now = now or datetime.datetime.now()
    payload = {
        "kind": kind,
        "ok": bool(ok),
        "at": now.timestamp(),
        "at_text": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = state_path(kind, data_dir)
    temp = path + ".tmp"
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(temp, path)
    except OSError:
        try:
            os.remove(temp)
        except OSError:
            pass


def should_skip(kind, data_dir=None, now=None):
    """这次由空闲触发、且最近刚成功跑过 → 跳过。

    调用方应在真正做任何事之前调用它。返回 False 表示照常执行。

    **失败不跳过**：控制台的失败自愈会在同一个进程里立刻重试一次，重试
    同样要经过这道闸门；如果失败也冷却，重试会被自己挡掉，还会被误报成
    「已自动恢复」。失败后照常执行，也让下一次空闲真的能再试一次。
    """
    if idle_seconds() < DETECT_SECONDS:
        return False
    state = read_state(kind, data_dir)
    if not state.get("ok"):
        return False
    last = state.get("at")
    if not isinstance(last, (int, float)):
        return False
    now_ts = (now or datetime.datetime.now()).timestamp()
    if now_ts - float(last) >= COOLDOWN_SECONDS:
        return False
    _SKIPPED[str(kind)] = time.time()
    return True


def skipped(kind, within=300):
    """本进程里刚刚是否因为冷却窗口跳过一次。"""
    at = _SKIPPED.get(str(kind))
    return bool(at and time.time() - at <= within)


def reset_skipped():
    """测试用：清掉本进程的跳过标记。"""
    _SKIPPED.clear()


def log(text, data_dir=None):
    """把跳过 / 兜底结果写进控制台的 run.log；写不进去就丢掉。

    刻意只写这一份日志：跳过并不产生签到结果，写进 signin.log 会让
    「最后一条 JSON」的解析和「最近活动」的时间戳都变得可疑。
    """
    try:
        path = os.path.join(data_dir or default_data_dir(), "run.log")
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s [idle] %s\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def describe():
    """界面 / 日志里的一句话说明。"""
    return ("空闲 %d 分钟后自动补跑（%d 分钟内成功跑过则跳过）"
            % (IDLE_MINUTES, COOLDOWN_SECONDS // 60))
