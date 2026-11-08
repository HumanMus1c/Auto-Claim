"""空闲补跑：计划任务「空闲触发」的本地闸门与睡眠前随机点。

每个签到任务除了每天的随机时刻，还挂了一条空闲触发器
（``MSFT_TaskIdleTrigger``）：机器空闲下来就跑一次，专门兜住「睡了一整天」
「断电重启后整组时刻全错过」这类情况。

**「空闲」是谁判的、按什么判**：判定不在本模块，也不是看内存或电源功率。
Windows 8 以后由系统电源子系统的「用户在场」检测决定，默认连续 **4 分钟**
没有键鼠输入即视为用户离开；任务计划程序同时要求所有处理器和磁盘在检测
区间内至少 80% 的时间处于空闲（Windows 7 是 90%）。内存占用不参与判定；
电源功率也不参与，但 AC/DC 切换和电量变化会触发一次重新检测。另外任何设置
了 ``ES_DISPLAY_REQUIRED`` 的程序（全屏视频、演示）会强制系统「不空闲」。

注意 ``IdleSettings/Duration`` 自 Windows 8 起**已弃用**（文档明确说明不再
使用，只是任务计划程序 UI 里还留着这个输入框）。所以注册时写的
``IDLE_MINUTES`` 只是让任务 XML 与真实行为对得上，真正生效的是系统那 4 分钟。

**睡眠前随机点**：进入空闲只是开始，机器还会在「睡眠/休眠倒计时」到点时
睡过去，睡过去之后当天的机会就全没了。所以空闲触发那一次跑完以后，会读取
当前电源方案（``powercfg``）算出睡眠截止时间，在「现在」到「截止前 M 分钟」
之间随机再排 2 个一次性触发器：当次 + 2 次 = 3 次，全部落在倒计时区间内，
且最后一个点距离睡眠至少 10 分钟——严格不靠近、更不越过截止点。窗口太窄
（例如电源方案设成 10 分钟就睡）时自动减为 2 次或 1 次，绝不硬塞。

**为什么还要闸门**：空闲触发是按「进入空闲」点火的，用户每离开工位一次就会
响一次……现在一次空闲会带来 3 次执行，更需要去重。闸门规则：

  - 普通空闲触发：最近 30 分钟内成功跑过就跳过（失败不冷却）
  - 睡眠前随机点：只要人还没回来就一定执行（这是排好的 3 次之一）；
    人回来了就跳过剩余的点——窗口已经结束

30 分钟冷却比同一条任务里最密的定时间隔（Trae / Qoder 45 分钟）还短，不会
误伤正常的定时执行；失败不进冷却，否则控制台的失败自愈重试会被自己挡掉。

判断「这次是不是空闲触发」用 ``GetLastInputInfo``：空闲触发点火时用户至少
已经离开 4 分钟，进程启动时若读到空闲时长超过 ``DETECT_SECONDS``，几乎只
可能来自它。计划点则用 ``in_plan`` 按时间比对，避免把「用户回来后的定时
任务」误当成计划点。

状态按模块各存两份小 JSON：``idle-guard-<kind>.json``（上次结果）和
``idle-plan-<kind>.json``（本窗口的随机点）。三个脚本可能在同一秒里各写
各的，分开存最省事。

读写失败一律按「不跳过 / 不排点」处理——宁可多跑一次，也不能因为状态文件
损坏把签到彻底挡住。
"""

import datetime
import json
import os
import random
import re
import subprocess
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

# 睡眠前随机点：
#   PLAN_COUNT      —— 除了空闲触发当次，再排几个点（当次 + 2 = 3 次）
#   PLAN_MAX_IDLE   —— 只有「刚进入空闲」才排点；空闲超过 10 分钟说明多半是
#                      定时任务撞上长时间无人，不该再排一轮
#   PLAN_EARLIEST   —— 第一个点至少等当次运行结束 4 分钟，避免撞上正在跑的任务
#   PLAN_MARGIN     —— 最后一个点距离睡眠截止至少 10 分钟：签到任务自身的预算
#                      上限是 9 分钟（PT10M - 60s），留够它跑完再睡
#   PLAN_MIN_GAP    —— 相邻点最小间隔
#   PLAN_TOLERANCE  —— 判断「这次是计划点」的时间容差。任务计划程序在忙时
#                      可能晚几十秒到几分钟才启动，5 分钟足够覆盖，又不会
#                      把正常的每日点误认成计划点
PLAN_COUNT = 2
PLAN_MAX_IDLE = 10 * 60
PLAN_EARLIEST = 4 * 60
PLAN_MARGIN = 10 * 60
PLAN_MIN_GAP = 5 * 60
PLAN_TOLERANCE = 300

# powercfg 里「睡眠」与「休眠」的设置 GUID；只认 GUID 与 0x 数值，
# 不依赖本地化标签，中英文系统都能解析。
SLEEP_SETTING_GUID = "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"
HIBERNATE_SETTING_GUID = "9d7815a6-7ee4-497e-8888-515a05f02364"

STATE_PREFIX = "idle-guard-"
PLAN_PREFIX = "idle-plan-"
KINDS = ("workbuddy", "qoder", "trae")

CREATE_NO_WINDOW = 0x08000000

# 本次进程里刚刚跳过的模块。app.py 用它来决定「跳过时不要重排触发时刻」——
# 否则每次空闲被跳过都会把当天已过的点重新抽一遍，白写一次任务。
_SKIPPED = {}

# 进程启动时测到的空闲时长、空闲起点与「这次是不是计划点」。重排发生在
# 运行结束，那时候用户可能已经回来了，必须用开始时的事实，不能现测。
_IDLE_AT_START = {}
_IDLE_SINCE = {}
_IN_PLAN = {}

_LAST_INPUT = None
_POWER_STRUCT = None


def default_data_dir():
    """状态落盘目录，与控制台 _diag_dir() / win_service 保持同一路径。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Auto-Claim", "dashboard")


def state_path(kind, data_dir=None):
    return os.path.join(data_dir or default_data_dir(),
                        STATE_PREFIX + str(kind) + ".json")


def plan_path(kind, data_dir=None):
    return os.path.join(data_dir or default_data_dir(),
                        PLAN_PREFIX + str(kind) + ".json")


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


def _power_status_struct():
    global _POWER_STRUCT
    if _POWER_STRUCT is None:
        import ctypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte),
                        ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte),
                        ("Reserved1", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong),
                        ("BatteryFullLifeTime", ctypes.c_ulong)]

        _POWER_STRUCT = SYSTEM_POWER_STATUS
    return _POWER_STRUCT


def on_battery():
    """当前是否由电池供电；拿不到就按交流电处理（多数台式机没有电池）。"""
    if os.name != "nt":
        return False
    try:
        import ctypes

        status = _power_status_struct()()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return status.ACLineStatus == 0
    except Exception:
        pass
    return False


def parse_power_settings(text):
    """从 ``powercfg /query`` 输出里取睡眠/休眠的 (交流, 直流) 秒数。

    按设置 GUID 找块，再取块内最后两个 ``0x`` 数值——前面的几个是
    「最小 / 最大 / 增量」，最后两个才是当前 AC/DC 值。这样不依赖
    「当前交流电源设置索引」这类本地化标签。
    """
    matches = list(re.finditer(
        r"GUID:\s*([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
        text))
    result = {}
    for index, match in enumerate(matches):
        guid = match.group(1).lower()
        if guid not in (SLEEP_SETTING_GUID, HIBERNATE_SETTING_GUID):
            continue
        end = (matches[index + 1].start() if index + 1 < len(matches)
               else len(text))
        values = [int(raw, 16) for raw in
                  re.findall(r"0x([0-9a-fA-F]+)", text[match.end():end])]
        if len(values) >= 2:
            result[guid] = (values[-2], values[-1])
    return result


def sleep_timeout_seconds():
    """当前电源方案下先到的那一个睡眠/休眠超时（秒）；0 表示永不。"""
    if os.name != "nt":
        return 0
    try:
        proc = subprocess.run(
            ["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP"],
            capture_output=True, timeout=10,
            creationflags=CREATE_NO_WINDOW)
        text = (proc.stdout or b"").decode("latin-1", "replace")
    except Exception:
        return 0
    settings = parse_power_settings(text)
    battery = on_battery()
    candidates = []
    for guid in (SLEEP_SETTING_GUID, HIBERNATE_SETTING_GUID):
        pair = settings.get(guid)
        if not pair:
            continue
        seconds = pair[1] if battery else pair[0]
        if seconds > 0:
            candidates.append(seconds)
    return min(candidates) if candidates else 0


def plan_window(idle_since):
    """给定空闲起点（epoch 秒），返回 ``(起点, 睡眠截止)``；无倒计时则 None。"""
    timeout = sleep_timeout_seconds()
    if timeout <= 0:
        return None
    idle_since = float(idle_since)
    return idle_since, idle_since + timeout


def pick_points(deadline, now_ts, count=PLAN_COUNT, earliest=PLAN_EARLIEST,
                margin=PLAN_MARGIN, min_gap=PLAN_MIN_GAP):
    """在 ``[now+earliest, deadline-margin]`` 里挑 count 个点（升序）。

    用「先给每段留足 min_gap，再在剩余空白里随机」的做法，保证点不会
    挤在一起，也不会落到截止点附近。窗口塞不下就自动少排，绝不越界。
    """
    start = float(now_ts) + earliest
    end = float(deadline) - margin
    if end <= start:
        return []
    span = end - start
    count = min(int(count), int(span // min_gap) + 1)
    if count <= 0:
        return []
    usable = span - (count - 1) * min_gap
    rng = random.SystemRandom()
    offsets = sorted(rng.uniform(0, usable) for _ in range(count))
    return [int(round(start + offsets[i] + i * min_gap))
            for i in range(count)]


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
    _write_json(state_path(kind, data_dir), payload)


def read_plan(kind, data_dir=None):
    """读取本窗口的随机点；没有或已过期都返回空。"""
    try:
        with open(plan_path(kind, data_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    points = data.get("points")
    if not isinstance(points, list):
        return {}
    return data


def write_plan(kind, points, idle_since, deadline, data_dir=None):
    """记下本窗口排好的随机点；永不抛错。"""
    payload = {
        "kind": kind,
        "points": [float(p) for p in points],
        "idle_since": float(idle_since),
        "deadline": float(deadline),
        "created": time.time(),
    }
    _write_json(plan_path(kind, data_dir), payload)


def clear_plan(kind, data_dir=None):
    try:
        os.remove(plan_path(kind, data_dir))
    except OSError:
        pass


def in_plan(kind, now=None, data_dir=None):
    """这次启动是否正好落在本窗口的某个计划点上。"""
    plan = read_plan(kind, data_dir)
    points = plan.get("points") or []
    if not points:
        return False
    now_ts = (now or datetime.datetime.now()).timestamp()
    deadline = plan.get("deadline") or 0
    # 超过截止点一小时的计划已经没有意义，别让它继续影响判断
    if deadline and now_ts > float(deadline) + 3600:
        return False
    for point in points:
        try:
            if abs(now_ts - float(point)) <= PLAN_TOLERANCE:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _write_json(path, payload):
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


def idle_at_start(kind):
    """本进程启动那一刻的空闲时长（秒）；没测过返回 0。"""
    return float(_IDLE_AT_START.get(str(kind)) or 0)


def idle_since_at_start(kind):
    """本进程启动那一刻推算出的空闲起点（epoch 秒）；没测过返回 0。"""
    return float(_IDLE_SINCE.get(str(kind)) or 0)


def was_planned(kind):
    """本进程启动时是否落在计划点上（用启动时的事实，不现测）。"""
    return bool(_IN_PLAN.get(str(kind)))


def should_skip(kind, data_dir=None, now=None):
    """这次该不该直接返回。

    调用方应在真正做任何事之前调用它。返回 False 表示照常执行。

    - 计划点 + 人还在空闲 → 一定执行（这是排好的 3 次之一）
    - 计划点 + 人已回来   → 跳过（窗口结束）
    - 普通空闲触发        → 最近 30 分钟内成功过就跳过
    - 失败不进冷却：控制台的失败自愈会在同一个进程里立刻重试一次，重试
      同样要经过这道闸门；如果失败也冷却，重试会被自己挡掉，还会被误报成
      「已自动恢复」。
    """
    now = now or datetime.datetime.now()
    idle = idle_seconds()
    planned = in_plan(kind, now, data_dir)
    _IDLE_AT_START[str(kind)] = idle
    _IDLE_SINCE[str(kind)] = now.timestamp() - idle
    _IN_PLAN[str(kind)] = planned

    if planned:
        if idle < DETECT_SECONDS:
            log("%s 空闲窗口已结束，跳过剩余的计划点" % kind, data_dir)
            _SKIPPED[str(kind)] = time.time()
            return True
        return False

    if idle < DETECT_SECONDS:
        return False
    state = read_state(kind, data_dir)
    if not state.get("ok"):
        return False
    last = state.get("at")
    if not isinstance(last, (int, float)):
        return False
    if now.timestamp() - float(last) >= COOLDOWN_SECONDS:
        return False
    _SKIPPED[str(kind)] = time.time()
    return True


def skipped(kind, within=300):
    """本进程里刚刚是否被闸门挡下过。"""
    at = _SKIPPED.get(str(kind))
    return bool(at and time.time() - at <= within)


def reset_skipped():
    """测试用：清掉本进程的跳过与启动快照。"""
    _SKIPPED.clear()
    _IDLE_AT_START.clear()
    _IDLE_SINCE.clear()
    _IN_PLAN.clear()


def log(text, data_dir=None):
    """把跳过 / 计划结果写进控制台的 run.log；写不进去就丢掉。

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
    return ("空闲 %d 分钟后补跑，睡眠倒计时内随机补 %d 次"
            % (IDLE_MINUTES, PLAN_COUNT))
