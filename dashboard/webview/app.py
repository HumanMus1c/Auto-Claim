"""pywebview 后端 API。

JS 通过 window.pywebview.api.* 调用这里的函数。

设计约束：
  - 只暴露非 silent 命令（status / auto / growth / claim / all），
    写操作由前端二次确认
  - 命令统一走进程内 _run()（不用 subprocess，避免打包后递归启动）
  - 每个耗时调用都走后台线程 + 完成事件，界面不会被长超时请求卡住
  - 路径与超时集中在这里，前端只管渲染
"""

import io
import json
import os
import subprocess
import sys
import threading
import time

# _locate_signin() 走过的候选路径，失败时供界面展示排查线索
SEARCHED = []


def _base_dir():
    """资源根目录。

    PyInstaller 单文件模式下，sys.frozen 为真、__file__ 指向解包后的临时目录
    （sys._MEIPASS），资源被放在那里；源码运行时则用脚本所在目录。
    打包与直跑必须走不同分支，否则 exe 起来就是白屏。
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = _base_dir()
ASSETS = os.path.join(HERE, "assets")
DASHBOARD = os.path.dirname(HERE)

# win_theme / win_title 在上一级目录（dashboard/），本方案继续复用
if DASHBOARD not in sys.path:
    sys.path.insert(0, DASHBOARD)


def _locate_signin():
    """定位 signin.py。

    搜索起点分两种情况，这里踩过坑：
      - 源码运行：从脚本所在目录（dashboard/webview）上溯
      - 打包运行：sys._MEIPASS 是 %TEMP% 下的临时解包目录，从那里上溯只会
        一路搜到 C:\\Users\\，**永远碰不到 exe 所在目录**——而 signin.py 正是
        跟 exe 放在一起的。所以冻结时必须优先从 dirname(sys.executable) 找。

    不写死上溯层数，目录挪动后仍能命中；环境变量优先级最高。
    """
    override = os.environ.get("WORKBUDDY_DEMO_SIGNIN")
    if override:
        return override

    roots = []
    if getattr(sys, "frozen", False):
        # exe 同级 -> 用户的预期用法（两个文件放一起）
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
        roots.append(HERE)                 # 解包目录，仅作兜底
    roots.append(HERE)
    roots.append(os.getcwd())              # 从命令行在别处启动时的最后兜底

    global SEARCHED
    SEARCHED = []
    seen = set()
    for start in roots:
        path = os.path.abspath(start)
        for _ in range(6):
            if path in seen:
                break
            seen.add(path)
            candidate = os.path.join(path, "signin.py")
            SEARCHED.append(candidate)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    # 兜底：明确报缺失，不静默。SIGNIN_SEARCHED 供界面把搜索路径显示出来，
    # 否则用户只看到"未找到"却不知道程序到底去哪儿找过。
    return os.path.join(HERE, "signin.py")


SIGNIN = _locate_signin()

# 仓库根目录也放进 sys.path：tasks() 要复用 signin.py 的认证链路
# （凭据解密、请求头、重试），必须能 import 到它。这里直接由
# _locate_signin() 的结果推导，免得另写一套上溯层数又走偏。
_REPO = os.path.dirname(SIGNIN)
if os.path.isdir(_REPO) and _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def _signin_log_path():
    """signin.py 的默认日志位置；环境变量优先，规则与 emit() 一致。"""
    override = os.environ.get("WORKBUDDY_SIGNIN_LOG")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(SIGNIN)), "signin.log")


def _service_runner():
    """计划任务要启动的命令：可执行文件 + 固定前置参数。

    打包后直接运行本 exe 的 --service 模式，任务不依赖系统 Python；
    源码运行时则用 pythonw.exe 跑 app.py，避免弹出控制台窗口。
    """
    if getattr(sys, "frozen", False):
        return sys.executable, ["--service"]

    exe = os.path.abspath(sys.executable)
    folder, name = os.path.split(exe)
    if name.lower() == "python.exe":
        pythonw = os.path.join(folder, "pythonw.exe")
        if os.path.isfile(pythonw):
            exe = pythonw
    return exe, [os.path.abspath(__file__), "--service"]


def icon_path():
    """窗口图标。打包后随资源一起分发，路径走 ASSETS。"""
    path = os.path.join(ASSETS, "icon.ico")
    return path if os.path.isfile(path) else None

# 可在控制台里执行的命令。
#
# 只列非 silent 的：signin.py 的 emit() 对 silent* 前缀走「写日志文件」分支，
# stdout 是空的——把 silent-poll 接到控制台上会一条输出都收不到。
#
# write=True 的会真实改动服务端状态（领积分、抽奖、消耗补登卡），
# 前端对这类按钮强制二次确认。
ACTIONS = {
    "status":  dict(label="查签到状态", write=False, timeout=120,
                    desc="只读查询，不改动任何状态"),
    "auto":    dict(label="签到 + 成长中心", write=True, timeout=660,
                    desc="先查签到状态，未签才领取今日积分，再跑完成长中心："
                         "Buddy 旅行（到达收礼、空闲派发）、接任务与领任务奖、"
                         "补登卡补当月断签、连登 7/14/28 天奖励兑换、开盲盒、能量开 Buddy"),
    "growth":  dict(label="仅成长中心", write=True, timeout=660,
                    desc="不碰签到接口，只跑成长中心：领旅行礼物与派 Buddy、"
                         "接任务并领任务奖、补登卡补当月断签（每轮最多 1 张）、"
                         "连登奖励兑换、开盲盒、能量开 Buddy"),
    "claim":   dict(label="仅领取今日签到", write=True, timeout=120,
                    desc="直接调领取接口拿今日积分，不查状态、不跑成长中心；"
                         "接口幂等，已签时重复点击不会重复发积分"),
    "all":     dict(label="查询 + 领取", write=True, timeout=180,
                    desc="调试用：先查一次签到状态，再无条件调一次领取接口，"
                         "两步的原始返回都打进日志"),
    # doctor 只保留后端能力，启动时自动跑一次填充「凭据格式」卡片，
    # 不在界面上暴露按钮——它对使用者没有可操作性。
    "doctor":  dict(label="离线体检", write=False, timeout=120,
                    desc="本地诊断凭据格式与运行时能力", hidden=True),
}

READONLY_ACTIONS = tuple(k for k, v in ACTIONS.items() if not v["write"])

# signin.py 用模块级全局变量记录时间预算（_started_at / _budget_seconds），
# 并发进入会互相干扰。所有调用路径共用这一把锁。
_SIGNIN_LOCK = threading.RLock()

# 只加载一次的 signin 模块，见 _load_signin()
_SIGNIN_MODULE = None
_SIGNIN_LOAD_LOCK = threading.Lock()


def _load_signin():
    """按文件路径加载 exe 旁边的 signin.py，且只加载一次。

    不能用普通 import，两个坑都真踩过：

      1) 并发半成品：tasks() 和 run() 同时 import 时，后到的线程会从
         sys.modules 直接拿到尚未执行完的模块对象，报
         "module 'signin' has no attribute '_run'" —— 而真实原因
         可能是它内部某个依赖缺失，错误信息指错了方向。
      2) 同名顶替：PyInstaller 的 FrozenImporter 在 sys.meta_path 里
         排在路径查找器之前，PYZ 里若有同名模块，sys.path.insert 也
         救不回来。

    显式按路径加载可以同时避开这两点。
    """
    global _SIGNIN_MODULE
    if _SIGNIN_MODULE is not None:
        return _SIGNIN_MODULE

    with _SIGNIN_LOAD_LOCK:
        if _SIGNIN_MODULE is not None:
            return _SIGNIN_MODULE

        import importlib.util

        alias = "_workbuddy_signin"
        spec = importlib.util.spec_from_file_location(alias, SIGNIN)
        if spec is None or spec.loader is None:
            raise ImportError("无法读取 %s" % SIGNIN)

        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            # 失败必须把半成品摘掉，否则下次会拿到不完整的模块
            sys.modules.pop(alias, None)
            raise

        _SIGNIN_MODULE = module
        return module


def _diag_dir():
    """诊断与历史日志的落盘目录。

    不能用 ASSETS：打包后它是 %TEMP% 下的解包目录，进程一退整个目录
    就被删掉，写进去的崩溃日志会跟着一起消失——于是 exe 里的异常
    彻底查无实据。必须落在用户目录这种真正持久的地方。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "WorkBuddyDashboard")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return HERE
    return folder


def _log_path():
    return os.path.join(_diag_dir(), "run.log")


def _crash_path():
    return os.path.join(_diag_dir(), "crash.log")


def _trace(msg):
    """按需落盘的运行期追踪。

    只在 WORKBUDDY_DEBUG=1 时生效，正常使用不产生任何文件。
    存在的理由：窗口能打开、进程也正常，但界面就是不干活——这种情况下
    唯一的线索是「页面到底加载了没有、JS 到底跑了没有」，只能主动去问。
    """
    if os.environ.get("WORKBUDDY_DEBUG") != "1":
        return
    try:
        with open(os.path.join(_diag_dir(), "trace.log"),
                  "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _probe(window):
    """窗口起来后，把页面的真实状态写进 trace.log。

    document.readyState / 标题 / body 长度 / bridge 是否就绪，
    这几项足以区分「页面没加载」「页面加载了但 JS 报错」「bridge 没通」。
    """
    if os.environ.get("WORKBUDDY_DEBUG") != "1":
        return
    js = ("JSON.stringify({url:location.href,ready:document.readyState,"
          "title:document.title,"
          "badge:(document.getElementById('badge')||{}).textContent||'',"
          "env:(document.getElementById('env')||{}).textContent||'',"
          "envSignin:(document.getElementById('envSignin')||{}).textContent||'',"
          "credit:(document.getElementById('vCredit')||{}).textContent||'',"
          "streak:(document.getElementById('vStreak')||{}).textContent||'',"
          "total:(document.getElementById('vTotal')||{}).textContent||'',"
          "credType:(document.getElementById('vCred')||{}).textContent||'',"
          "todo:(document.getElementById('cntTodo')||{}).textContent||'',"
          "done:(document.getElementById('cntDone')||{}).textContent||'',"
          "svc:(document.getElementById('svcState')||{}).textContent||'',"
          "svcInstalled:(document.getElementById('svcSummary')||{}).textContent||'',"
          "tasks:(document.getElementById('tasklist')||{}).childElementCount||0,"
          "calSigned:document.querySelectorAll('#cal .on').length,"
          "logLines:(document.getElementById('log')||{}).childElementCount||0,"
          "jsErrors:window.__errs||[]})")
    for i in range(3):
        time.sleep(5)
        try:
            raw = window.evaluate_js(js)
            _trace("probe#%d %s" % (i + 1, raw))
            if raw and '"ready":"complete"' in raw and '"api":true' in raw:
                break
        except Exception as exc:
            _trace("probe#%d 失败: %r" % (i + 1, exc))


def _store_path():
    """上次查询结果的持久化位置。目录见 _diag_dir()。"""
    return os.path.join(_diag_dir(), "last_result.json")


def _load_store():
    try:
        with open(_store_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_store(data):
    try:
        with open(_store_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _read_local_log(limit=200):
    """读取最近一次历史日志，供界面初始化时回填。"""
    path = _log_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-limit:]
    except OSError:
        return []


def _append_log(line):
    try:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class Api:
    """暴露给 JS 的接口集合。"""

    def __init__(self):
        self._window = None
        self._lock = threading.Lock()
        self._busy = False

    # ---------- 生命周期 ----------

    def bootstrap(self):
        """界面加载完成后调用：环境信息 + 上次查询的缓存结果。

        cached 让界面在网络查询返回前就能先把卡片填上，
        避免每次启动都是一片「—」。
        """
        _trace("bootstrap 被调用")
        return {
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "platform": sys.platform,
            "signin": SIGNIN,
            "signin_exists": os.path.exists(SIGNIN),
            "signin_searched": list(SEARCHED),
            "readonly": list(READONLY_ACTIONS),
            "actions": [
                {"key": k, "label": v["label"], "write": v["write"],
                 "desc": v["desc"], "hidden": v.get("hidden", False)}
                for k, v in ACTIONS.items() if not v.get("hidden")
            ],
            "history": _read_local_log(),
            "cached": _load_store(),
        }

    def attach(self, window_id):
        """pywebview 的 on_loaded 回调里保存窗口句柄。"""
        self._window = window_id

    @staticmethod
    def _invoke(action):
        """在当前进程里跑 signin.py 的 _run()，捕获它打印的 JSON。

        signin.py 的输出约定是「向 stdout 打一行 JSON」，emit() 内部就是
        print(payload)。这里把 stdout 临时换成 StringIO 即可拿到，
        不必改上游一行代码。
        """
        try:
            signin = _load_signin()
        except Exception as exc:
            _trace("_invoke: 加载 signin.py 失败: %r" % (exc,))
            return 1, "", "无法加载 signin.py（%s）：%s" % (SIGNIN, exc)

        # import 成功不等于模块可用：冻结后 sys.path 的顺序和源码运行时
        # 不同，可能命中别处的同名模块。出错时把来源一起带上，
        # 否则只报 "no attribute '_run'" 会完全无从判断。
        where = getattr(signin, "__file__", "<无 __file__>")
        _trace("_invoke: signin=%r has_run=%s sys.path[:4]=%r"
               % (where, hasattr(signin, "_run"), sys.path[:4]))
        if not hasattr(signin, "_run"):
            return 1, "", ("signin 模块缺少 _run()，来源：%s；"
                           "sys.path 前 3 项：%s" % (where, sys.path[:3]))

        buf = io.StringIO()
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = buf
        try:
            with _SIGNIN_LOCK:
                code = signin._run(action)
        except SystemExit as exc:                 # _run 不该 exit，兜一下
            code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:
            code, _ = 1, buf.write("\n异常：%s: %s" % (type(exc).__name__, exc))
        finally:
            sys.stdout, sys.stderr = stdout, stderr
        return code, buf.getvalue(), ""

    # ---------- 命令执行 ----------

    def run(self, action):
        """执行一个 signin.py 命令。立即返回，结果通过事件异步推给前端。"""
        spec = ACTIONS.get(action)
        if spec is None:
            return {"ok": False, "error": "未知命令：%s" % action}
        _trace("run 被调用: %s" % action)
        with self._lock:
            if self._busy:
                return {"ok": False, "error": "有命令正在执行"}
            self._busy = True

        threading.Thread(target=self._safe_worker, args=(action, spec),
                         daemon=True).start()
        return {"ok": True, "action": action}

    def _safe_worker(self, action, spec):
        """_worker 的包装：任何异常都要推给前端。

        之前是裸起线程，线程里的 TypeError 只进 stderr，而打包后
        没有控制台——异常彻底消失，界面永远停在"执行中…"，
        _busy 也永远是 True，后续所有命令都被挡掉。
        """
        try:
            self._worker(action, spec)
        except Exception as exc:
            import traceback
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self._push("log", "内部错误：%s" % detail)
            try:
                _append_log("内部错误: %s" % detail)
            except Exception:
                pass
            with self._lock:
                self._busy = False
            self._push("done", {"action": action, "exit": 2, "payload": None})

    def _worker(self, action, spec):
        stamp = time.strftime("%H:%M:%S")
        self._push("log", "[%s] %s" % (stamp, spec["desc"]))
        if spec["write"]:
            self._push("log", "  写操作，正在执行……")

        # 在本进程内调用 signin.py，而不是 subprocess 起子进程。
        #
        # 原因有二：
        #   1) 打包后 sys.executable 就是 exe 自己，用它跑 signin.py 等于
        #      又启动一份完整程序——而新实例启动后又会自动跑 status，
        #      于是形成 fork bomb（实测每 3 秒生成一个进程，十几秒堆到 10+）。
        #   2) 进程内调用省掉每次启动解释器的开销，status 从秒级降到毫秒级。
        code, stdout, stderr = self._invoke(action)

        parsed = self._parse_payload(stdout)

        # 只推摘要，不推原始 JSON：那坨东西含整月签到日期数组，直接刷屏没法看
        if parsed is not None:
            for line in self._summarize(parsed):
                self._push("log", line)
                _append_log(line)
        else:
            for line in stdout.splitlines():
                self._push("log", line)
                _append_log(line)
        if stderr:
            for line in stderr.splitlines():
                self._push("log", "[stderr] " + line)
                # stderr 也要落盘：_invoke 的失败信息只走这里，
                # 不写文件的话退出后完全查不到原因
                _append_log("[stderr] " + line)
        self._push("log", "[%s] exit=%s" % (time.strftime("%H:%M:%S"), code))

        # 持久化：下次启动先渲染缓存，再刷新，避免开场就是一片「—」
        if parsed is not None and code == 0:
            store = _load_store()
            store[action] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "exit": code, "payload": parsed}
            _save_store(store)

        with self._lock:
            self._busy = False
        self._push("done", {"action": action, "exit": code, "payload": parsed})

    # ---------- 事件推送 ----------

    @staticmethod
    def _parse_payload(stdout):
        """从 stdout 里挑出那行 JSON 结果。"""
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _summarize(payload):
        """把套娃响应拍平成一行一个字段的摘要。

        status 的结构是 status -> body -> data 三层，且 checkin_dates 里塞了
        整月日期数组，直接 dumps 会刷屏，所以只挑有信息量的字段。
        """
        data = payload.get("body", payload)
        while isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]

        lines = []
        for key in ("result", "report", "growth", "growth_result",
                    "credits_gained", "energy", "failures", "idle",
                    "credential_format", "runtime",
                    "electron_version", "active", "today_checked_in",
                    "today_credit", "daily_credit", "credit", "streak_days",
                    "total_credits", "is_streak_day", "next_streak_day",
                    "week_checkin_days", "season", "activity_name",
                    "theme_name", "start_time", "end_time", "http",
                    "needs_attention"):
            if key not in data:
                continue
            value = data[key]
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                shown = "是" if value else "否"
            elif isinstance(value, (list, dict)):
                shown = "（%d 项）" % len(value)
            else:
                shown = str(value)
            # report / growth 是长句，单独处理
            if key in ("report", "growth"):
                lines.append("  %s" % shown)
            else:
                lines.append("  %-18s %s" % (key, shown))
        return lines

    # ---------- 主题 ----------

    def tasks(self):
        """拉取云端任务列表。

        直接复用 signin.py 自己的认证链路（凭据解密 / 请求头 / 重试），
        不另写一份，也就不用改动上游脚本。
        """
        try:
            signin = _load_signin()
        except Exception as exc:
            return {"ok": False, "error": "无法加载 signin.py：%s" % exc}

        # 宁可快速失败也不要阻塞：auto 可能跑几分钟，
        # 拿任务列表时干等着会让界面像卡死
        if not _SIGNIN_LOCK.acquire(blocking=False):
            return {"ok": False, "error": "有命令正在执行，稍后再刷新任务"}
        try:
            auth_file, _ = signin.find_auth_file()
            if not auth_file or not os.path.exists(auth_file):
                return {"ok": False, "error": "未找到登录凭据"}
            session = signin.resolve_session(signin.load_session_retry(auth_file))
            headers = signin.build_headers(session)
            endpoint = ((session.get("auth") or {}).get("endpoint")
                        or signin.DEFAULT_ENDPOINT).rstrip("/")

            code, body = signin.get(endpoint + "/v2/activity/growth/tasks", headers)
            if code != 200:
                return {"ok": False, "error": "HTTP %s" % code}

            raw = signin.dig(body, "tasks") or []
            items = []
            for t in raw:
                if not isinstance(t, dict):
                    continue
                prog = t.get("progress") or {}
                items.append({
                    "code": t.get("task_code"),
                    "title": t.get("title") or t.get("task_code"),
                    "desc": t.get("task_desc") or t.get("description") or "",
                    "status": t.get("accept_status") or "unknown",
                    "locked": bool(t.get("locked")),
                    "credit": t.get("reward_credit") or 0,
                    "energy": t.get("reward_energy") or 0,
                    "buddy": bool(t.get("reward_buddy")),
                    "current": prog.get("current"),
                    "target": prog.get("target"),
                    "badge": t.get("badge_name") or "",
                    "new": bool(t.get("is_new")),
                })
            _trace("tasks: 拉到 %d 项" % len(items))
            return {"ok": True, "items": items}
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            _SIGNIN_LOCK.release()

    # ---------- 脚本服务（Windows 计划任务） ----------

    def service_status(self):
        """读取后台计划任务的安装、启用、运行与最近执行状态。"""
        try:
            import win_service
            return win_service.status(_signin_log_path())
        except Exception as exc:
            return {
                "supported": os.name == "nt",
                "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "tasks": [],
                "installed": False,
                "installed_count": 0,
                "enabled": False,
                "running": False,
                "next_run": None,
                "last_run": None,
                "log": None,
            }

    def service_action(self, action):
        """安装/卸载/启用/停用/立即运行/停止后台计划任务。

        只接受固定动作名；具体任务名、触发器与命令全部在 win_service 里定义，
        前端无法借这个入口执行任意命令。
        """
        allowed = ("install", "uninstall", "enable", "disable", "run", "stop")
        if action not in allowed:
            return {"ok": False, "error": "未知服务操作：%s" % action}

        try:
            import win_service
            if action == "install":
                if not os.path.isfile(SIGNIN):
                    result = {"ok": False, "error": "未找到 signin.py，无法安装计划任务"}
                else:
                    runner, prefix = _service_runner()
                    result = win_service.install(
                        runner, prefix, workdir=os.path.dirname(SIGNIN))
            else:
                result = win_service.action(action)
        except Exception as exc:
            result = {"ok": False,
                      "error": "%s: %s" % (type(exc).__name__, exc)}

        result["status"] = self.service_status()
        text = result.get("message") or result.get("error") or "操作完成"
        _append_log("脚本服务 %s: %s" % (action, text))
        return result

    def system_theme(self):
        """把注册表里读到的亮/暗与强调色交给前端做初始配色。"""
        try:
            import win_theme
            dark, accent = win_theme.detect()
        except Exception:
            dark, accent = False, (0, 120, 212)
        return {"dark": bool(dark), "accent": list(accent)}

    def native_title(self, text):
        """用 Win32 API 修正窗口标题。

        pywebview 走的是系统 WebView2，标题理论上已是正确 Unicode；
        这里保留一次兜底修正，成本极低。
        """
        try:
            import win_title
            return win_title.set_native_title(text)
        except Exception:
            return 0

    # ---------- 事件推送 ----------

    def log_error(self, text):
        """前端把 JS 异常送回来落盘。

        打包后没有控制台、stderr 不可见，前端报错除了显示在界面上
        没有任何留存，事后无从追溯。
        """
        try:
            with open(_crash_path(), "a", encoding="utf-8") as fh:
                fh.write("[js] %s\n" % text)
        except Exception:
            pass
        return True

    def _push(self, kind, payload):
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                "window.__onBackend && window.__onBackend(%s, %s)"
                % (json.dumps(kind), json.dumps(payload, ensure_ascii=False))
            )
        except Exception:
            pass  # 窗口已关闭


def _service_main(argv):
    """计划任务入口：``--service silent`` / ``silent-poll``。

    由 win_service 注册的计划任务调用。直接复用 Api._invoke()，不创建窗口，
    也不需要 pywebview；silent 类命令的结果由 signin.emit() 写进 signin.log。
    """
    action = argv[0] if argv else ""
    allowed = ("silent", "silent-poll", "silent-growth")
    if action not in allowed:
        _append_log("[service] 未知或缺少动作：%r（可用：%s）"
                    % (action, " / ".join(allowed)))
        return 2

    try:
        code, stdout, stderr = Api._invoke(action)
    except Exception as exc:
        import traceback
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        _append_log("[service] %s 启动失败：%s" % (action, detail))
        try:
            with open(_crash_path(), "a", encoding="utf-8") as fh:
                fh.write("[service %s] %s\n" % (action, detail))
        except Exception:
            pass
        return 1

    # silent 正常时 stdout 为空；异常或加载失败才会有内容，落盘便于事后追溯。
    for line in (stderr or "").splitlines() + (stdout or "").splitlines():
        _append_log("[service] " + line)
    return code if isinstance(code, int) else 1


def main():
    import webview

    _trace("main 进入: HERE=%s ASSETS=%s index=%s SIGNIN=%s"
           % (HERE, ASSETS,
              os.path.isfile(os.path.join(ASSETS, "index.html")), SIGNIN))

    title = "WorkBuddy 控制台"
    guard_handle = None
    watchdog = None
    try:
        import win_guard
    except Exception as exc:
        # 单实例/看门狗是保护能力，不应因为它们导致整个控制台起不来。
        # 打包校验会检查 win_guard.py 是否真的进了 exe；这里只做 fail-open。
        _trace("win_guard 不可用: %r" % (exc,))
        win_guard = None

    if win_guard is not None:
        try:
            guard_handle, already_running = win_guard.acquire_single_instance()
        except Exception as exc:
            _trace("单实例锁异常: %r" % (exc,))
            guard_handle, already_running = None, False

        if already_running:
            _trace("检测到已有实例，尝试激活已有窗口")
            try:
                state = win_guard.activate_existing_window()
            except Exception as exc:
                _trace("激活已有实例失败: %r" % (exc,))
                state = "missing"

            message = None
            warning = False
            if state == "hung":
                message = ("WorkBuddy 控制台已在运行，但现有窗口无响应。\n\n"
                           "看门狗（默认开启）会自动结束它；也可以稍后重试，"
                           "或在任务管理器中结束 Auto-claim-dashboard.exe。")
                warning = True
            elif state == "missing":
                message = ("WorkBuddy 控制台已在运行，但暂时找不到窗口。\n"
                           "请稍等片刻后再试。")
            if message:
                win_guard.show_message(message, title, warning=warning)
            return

    api = Api()
    window = webview.create_window(
        title,
        os.path.join(ASSETS, "index.html"),
        js_api=api,
        width=1180,
        height=800,
        min_size=(940, 640),
        background_color="#f3f3f3",
    )
    # 立刻绑定：bootstrap / system_theme 等调用发生在 start() 之前，
    # _push 要靠它把结果送回前端
    api._window = window

    # UI 看门狗：窗口创建后开始等主窗口，连续几次 WM_NULL 超时就退出，
    # 避免卡死的 WebView2 窗口把 DWM/任务栏一起拖住。
    if (win_guard is not None
            and os.environ.get("WORKBUDDY_WATCHDOG", "1") != "0"):
        try:
            watchdog = win_guard.UIWatchdog(
                os.path.join(_diag_dir(), "watchdog.log"))
            watchdog.start()
        except Exception as exc:
            _trace("UI 看门狗启动失败: %r" % (exc,))
            # 构造失败的场景 watchdog 仍是 None；构造成功但 start 失败的
            # 场景由 finally 里的 stop() 兜底，不必在这里重复赋值。

    # 仅调试模式：5 秒后主动问 DOM 要状态（见 _probe）
    threading.Thread(target=_probe, args=(window,), daemon=True).start()

    # gui="edgechromium" 是必须的，不能省。
    # pywebview 6.2.1 在这台机器上自动后端探测会失败（webview.guilib 一直是 None），
    # start() 表现为静默挂起、不建窗口也不报错，只能靠显式指定后端绕开。
    try:
        webview.start(gui="edgechromium", debug=False, icon=icon_path())
    finally:
        if watchdog is not None:
            watchdog.stop()
        if guard_handle is not None and win_guard is not None:
            win_guard.release_single_instance(guard_handle)
    _trace("webview.start 返回（窗口已关闭）")


if __name__ == "__main__":
    # 计划任务走无窗口服务分支，不创建 WebView2，也不触发单实例锁。
    if len(sys.argv) > 1 and sys.argv[1] == "--service":
        sys.exit(_service_main(sys.argv[2:]))

    # edgechromium 后端会接管 stdout/stderr，异常信息经常整个丢失。
    # 自建一份文件日志，出问题时至少还有痕迹。路径见 _crash_path()：
    # 绝不能写进 ASSETS（打包后那是临时解包目录，退出即删）。
    try:
        main()
    except Exception:
        import traceback
        try:
            with open(_crash_path(), "a", encoding="utf-8") as fh:
                fh.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        raise