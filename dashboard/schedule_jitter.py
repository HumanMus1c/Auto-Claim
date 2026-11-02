"""计划任务触发时间的随机化。

固定整点最容易被判定成「脚本行为」：每天 00:05:00 准时醒来、每 4 小时整
分一次，模式比内容更显眼。这里把每个触发点打散到一个 ±15 分钟的窗口里。

**为什么不用 ``random``**：默认的伪随机序列在启动时只用时间戳（秒级）做
种子，同一秒内起两个进程会拿到同一串数，而且序列本身可预测——项目里三个
任务恰好会在同一分钟内被连续注册，用 ``random`` 反而会生成一组互相雷同的
偏移。改成把三层真实系统熵源揉在一起再做 BLAKE2b 摘要：

  ① **系统时钟**：``time.time_ns()`` 的纳秒位。RTC 读数抖动、NTP 微调、
     调度延迟都会落在这一位上，是唯一每天都不同的源头。

  ② **分钟级系统事件**：开机至今的分钟数（Windows 用 ``GetTickCount64``，
     POSIX 用 ``CLOCK_BOOTTIME``）。它由内核 tick 推进，和「整分钟」这个
     人为边界天然错位；同一台机器每次开机都会落在不同的相位上。

  ③ **10 分钟倍数的其他系统事件**：epoch 的 10 分钟序号（Windows 时间
     服务、任务计划心跳、遥测上报都按这个粒度走）和 uptime 的 10 分钟
     序号各取一个。这两层与 ② 的周期互质（分钟 / 10 分钟），叠加后不会
     出现「每 10 分钟重复一次」的短周期。

三层相乘后的状态空间远大于一个窗口内的秒数，所以相邻两次、相邻两天拿到
相同偏移的概率可以忽略；同时把任务名和日期也拌进摘要，同一轮里不同的
触发点（``任务名#序号``）会得到互不相同的偏移。

本模块只做纯计算，不碰计划任务、不起进程，方便单测。
"""

import datetime
import hashlib
import os
import time

# 抖动幅度：±15 分钟。相邻触发点最小间隔是 1 小时（Qoder/Trae 的 +1h），
# 15 分钟既能打散整点特征，又不会让两个点靠在一起或跨过午夜。
JITTER_WINDOW = 15 * 60

# 重排后的时刻之间至少留 30 分钟，避免两个触发点挤在同一时刻重复跑。
MIN_GAP = 30 * 60

# 一天 1440 分钟 / 86400 秒，用于把时刻折回并处理跨午夜。
DAY_MINUTES = 24 * 60
DAY_SECONDS = DAY_MINUTES * 60

# 摘要的个性化参数，让这份哈希和进程里其它 blake2b 用途区分开
_PERSON = b"auto-claim-jit"

_kernel32 = None


def _boot_millis():
    """开机至今的毫秒数——第二、三层熵源的载体。

    Windows 的 ``GetTickCount64`` 必须显式声明 ``restype``，否则 ctypes 会
    按 32 位 int 截断返回值：机器开机 24.8 天之后它会翻回负数，正好是最
    需要随机性的长期在线的机器上失效。
    """
    global _kernel32
    if os.name == "nt":
        try:
            if _kernel32 is None:
                import ctypes
                _kernel32 = ctypes.windll.kernel32
                _kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return int(_kernel32.GetTickCount64())
        except Exception:
            pass
    try:
        # Linux/macOS 的等价物；Windows 上没有这个常量，会走上面的分支
        return int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000)
    except (AttributeError, OSError):
        return int(time.monotonic() * 1000)


def entropy(name, day=None):
    """返回这一次抽样的四层熵源，便于测试时替换。"""
    clock_ns = time.time_ns()
    boot_minute = _boot_millis() // 60_000
    return {
        "clock_ns": clock_ns % 1_000_000_000,      # ① 纳秒位
        "boot_minute": boot_minute,                # ② 分钟级事件
        "epoch_slot": clock_ns // 600_000_000_000,  # ③ epoch 的 10 分钟序号
        "uptime_slot": boot_minute // 10,          # ③ uptime 的 10 分钟序号
        "name": name,
        "day": day or datetime.date.today().isoformat(),
    }


def _digest(parts):
    """把各层熵源拼成一行再过 BLAKE2b，取 64 位整数。"""
    hasher = hashlib.blake2b(digest_size=8, person=_PERSON)
    hasher.update("|".join(str(part) for part in parts).encode("utf-8"))
    return int.from_bytes(hasher.digest(), "big")


def offset_seconds(name, day=None, window=JITTER_WINDOW, forward=False):
    """抽一个落在 ``[-window, +window]``（或 ``[0, +window]``）内的偏移。

    ``forward=True`` 用于有下界的任务：Qoder 必须晚于 UTC+8 刷新时刻才能
    领到 Credits，签到的第一个点也不能往前跨过午夜（否则会被算进前一天）。

    以**分钟**为粒度且永不返回 0：偏移 0 等于没打散，而计划任务的触发器
    本来也只精确到分钟，秒级抖动的结果会被格式化吃掉。撞上原时刻的概率
    虽然只有 1/1800，但在「每天 6 个点、连跑一年」的量级上必然发生。
    """
    window = max(0, min(int(window), DAY_MINUTES * 30))
    span = max(1, window // 60)
    data = entropy(name, day)
    raw = _digest((
        data["clock_ns"], data["boot_minute"], data["epoch_slot"],
        data["uptime_slot"], data["name"], data["day"],
    ))
    if forward:
        minutes = raw % span + 1                 # 1..span
    else:
        slot = raw % (2 * span)                  # 0..2span-1
        # 前一半落在负区间、后一半落在正区间，正好跳开 0
        minutes = slot - span if slot < span else slot - span + 1
    return minutes * 60


def parse_hhmm(text):
    """``HH:mm`` / ``HH:mm:ss`` → 当天第几分钟；不合法返回 None。"""
    try:
        parts = str(text).strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
    except (IndexError, TypeError, ValueError):
        hour = minute = -1
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


def format_minutes(total):
    """当天第几分钟 → ``HH:mm``，自动折回 0~1439。"""
    total = int(total) % DAY_MINUTES
    return "%02d:%02d" % (total // 60, total % 60)


def jitter_times(times, name, day=None, window=JITTER_WINDOW, forward=False):
    """给一组 ``HH:mm`` 各加一个独立偏移，返回新的 ``HH:mm`` 列表。"""
    day = day or datetime.date.today().isoformat()
    result = []
    for index, raw in enumerate(times):
        minute = parse_hhmm(raw)
        if minute is None:
            result.append(str(raw))
            continue
        shift = offset_seconds("%s#%d" % (name, index), day, window, forward)
        result.append(format_minutes((minute * 60 + shift) // 60))
    return result


def _circular_gap(left, right):
    """当天两个秒数之间的环形距离（23:55 与 00:05 只差 10 分钟）。"""
    gap = abs(left - right) % DAY_SECONDS
    return min(gap, DAY_SECONDS - gap)


def _avoid_clash(target, kept, min_gap=MIN_GAP):
    """把落点推开，直到和已保留的时刻都不小于 ``min_gap``。"""
    for _ in range(8):
        if all(_circular_gap(target, other) >= min_gap for other in kept):
            break
        target = (target + min_gap // 3) % DAY_SECONDS
    return target


def _pair_points(base_minutes, current_minutes):
    """把任务里的当前时刻配到最近的基准点上。

    配对比按索引更稳：任务触发器从 PowerShell 读回来是按时间排过序的，
    跨午夜的组（``22:30 / 00:30 / 03:30``）排序后顺序会和配置相反。
    按环形距离贪心配对，两边都能对上。
    """
    remaining = list(range(len(current_minutes)))
    pairs = []
    for base in base_minutes:
        best, best_gap = None, None
        if base is not None:
            for slot in remaining:
                if current_minutes[slot] is None:
                    continue
                gap = _circular_gap(base * 60, current_minutes[slot] * 60)
                if best_gap is None or gap < best_gap:
                    best, best_gap = slot, gap
        if best is None:
            pairs.append(None)
            continue
        remaining.remove(best)
        pairs.append(current_minutes[best])
    return pairs


def reshuffle(times, name, base=None, now=None, window=JITTER_WINDOW,
              forward=False, min_gap=MIN_GAP):
    """签到跑完后调用：只重抽「今天已经过去」的点，未到的点原样保留。

    **偏移永远以 ``base`` 为锚点，不是以当前值为锚点。** 这一点是必须的：
    早期版本拿任务里的当前时刻当基准再抽 ±15 分钟，误差会随天数滚雪球
    （00:20 → 00:25 → 00:40 → 00:57 …），一个月就能漂到白天，签到时间
    彻底失控。``base`` 是配置里的基准点，每天抽的都是「基准 + 一次新偏移」。

    为什么不是整组重抽：poll 任务一天有 6 个点，凌晨那次跑完就把 05/09/13
    全部换掉的话，白天那几个点会被推到已经过去的时段，当天直接少跑 5 次。
    每个点在它自己跑完的当天被换掉、第二天起生效，所以到第二天零点时，
    整组 6 个点已经全部是新的时刻。

    跨午夜的组（Trae 的 ``22:30 / 00:30 / 03:30``）按 ``base`` 顺序推进日期
    偏移，后半段的 00:30 会被正确认成「明天凌晨」而不是「今天已过」。
    """
    now = now or datetime.datetime.now()
    now_minutes = now.hour * 60 + now.minute
    day = now.date().isoformat()

    base_minutes = [parse_hhmm(t) for t in (base if base is not None else times)]
    current_minutes = [parse_hhmm(t) for t in times]
    paired = _pair_points(base_minutes, current_minutes)

    # 日偏移按 base 顺序推进：base 才是稳定的日程，当前值可能已经漂过
    day_offsets = []
    offset_days = 0
    prev = None
    for minute in base_minutes:
        if minute is not None:
            if prev is not None and minute < prev:
                offset_days += 1
            prev = minute
        day_offsets.append(offset_days)

    result = [str(raw) for raw in times]
    kept_seconds = []
    expired = []
    for index, base_minute in enumerate(base_minutes):
        current = paired[index] if index < len(paired) else None
        if base_minute is None or current is None:
            continue
        # 当天还没到的点保持不动，避免白天那几个点被推到已过去的时段
        if day_offsets[index] * DAY_MINUTES + current > now_minutes + 1:
            kept_seconds.append(current * 60)
            result[index] = format_minutes(current)
        else:
            expired.append((index, base_minute))

    for index, base_minute in expired:
        shift = offset_seconds("%s#%d" % (name, index), day, window, forward)
        target = _avoid_clash((base_minute * 60 + shift) % DAY_SECONDS,
                              kept_seconds, min_gap)
        kept_seconds.append(target)
        result[index] = format_minutes(target // 60)
    return result


def describe(window=JITTER_WINDOW, forward=False):
    """界面上的一句话说明，避免用户看到时间跳来跳去以为出了 bug。"""
    minutes = max(1, int(round(window / 60.0)))
    if forward:
        return "整点后 1~%d 分钟内随机" % minutes
    return "整点前后 %d 分钟内随机（不为 0）" % minutes
