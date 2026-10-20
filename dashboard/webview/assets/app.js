/* WorkBuddy 控制台前端
 *
 * 与 Python 后端通过 pywebview 的 JS bridge 通信：
 *   window.pywebview.api.<method>()  —— 调用
 *   window.__onBackend(kind, data)   —— 后端事件推送（app.py 里 _push 触发）
 */

'use strict';

// 调试模式下的错误留痕：后端探针会把它读走并落盘。
// 放在最前面，尽量保证后续任何一步出错都有地方可记。
window.__errs = window.__errs || [];

// 强调色需按比例调整亮度：完全依赖 CSS 的 filter 会在嵌套元素上出问题
const state = {
  theme: 'light',
  accent: [0, 120, 212],
  history: [],
  ready: false,
};

const $ = (id) => document.getElementById(id);

/* ---------- 工具 ---------- */

function cls(el, name, on) {
  if (el) el.classList.toggle(name, !!on);
}

function setBadge(text, kind) {
  const b = $('badge');
  b.textContent = text;
  b.className = 'badge' + (kind ? ' ' + kind : '');
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------- 主题 ---------- */

function applyTheme(mode) {
  state.theme = mode;
  document.documentElement.dataset.theme = mode;
}

function applyAccent(rgb) {
  state.accent = rgb;
  const root = document.documentElement;
  const hex = '#' + rgb.map((v) => v.toString(16).padStart(2, '0')).join('');
  root.style.setProperty('--accent', hex);

  // 依据亮度决定强调色上的前景色，保证按钮文字可读
  const lum = (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255;
  root.style.setProperty('--accent-fg', lum > 0.62 ? '#1c1c1c' : '#ffffff');
}

function toggleTheme() {
  const next = state.theme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem('wbTheme', next); } catch (e) { /* 忽略 */ }
}

/* ---------- 日志 ---------- */

// 每种日志类型配一个图标 + 中文语义
const LOG_KINDS = {
  cmd:   { ico: '▸', name: '命令' },
  ok:    { ico: '✔', name: '成功' },
  warn:  { ico: '▲', name: '警告' },
  err:   { ico: '✖', name: '错误' },
  meta:  { ico: '·', name: '信息' },
  info:  { ico: 'ℹ', name: '提示' },
  field: { ico: ' ', name: '字段' },
};

function classify(text) {
  if (/^\[.*\]\s*\$/.test(text)) return 'cmd';
  if (/\[stderr\]|Traceback|^\s*File "|Error|Exception/i.test(text)) return 'err';
  if (/exit=0|成功|已就绪|就绪/.test(text)) return 'ok';
  if (/exit=[1-9]|-1|-2|失败|未找到|拒绝|警告/.test(text)) return 'warn';
  if (/^\[/.test(text)) return 'meta';
  if (/^\s{2,}\S+\s{2,}/.test(text)) return 'field';   // 摘要里的字段行
  return 'info';
}

function appendLog(text, kind) {
  kind = kind || classify(text);
  const spec = LOG_KINDS[kind] || LOG_KINDS.info;

  const box = $('log');
  const row = document.createElement('div');
  row.className = 'line t-' + kind;

  const ico = document.createElement('span');
  ico.className = 'ico';
  ico.textContent = spec.ico;

  const txt = document.createElement('span');
  txt.className = 'txt';

  // 字段行渲染成 "key  value"，值按类型着色
  const m = text.match(/^(\s*)([A-Za-z_]+)(\s{2,})(.*)$/);
  if (kind === 'field' && m) {
    const k = document.createElement('span');
    k.className = 'k';
    k.textContent = m[2];
    const v = document.createElement('span');
    v.className = 'v';
    const val = m[4];
    if (/^-?\d[\d,]*(\.\d+)?$/.test(val)) v.classList.add('num');
    else if (val === '是') v.classList.add('bool-yes');
    else if (val === '否') v.classList.add('bool-no');
    v.textContent = val;
    txt.append(k, '  ', v);
  } else {
    txt.textContent = text;
  }

  row.append(ico, txt);
  box.appendChild(row);

  while (box.childElementCount > 800) box.removeChild(box.firstChild);
  if ($('autoScroll').checked) box.scrollTop = box.scrollHeight;
}

function clearLog() {
  $('log').innerHTML = '';
}

function logText() {
  return Array.from($('log').children)
    .map((row) => {
      const ico = row.querySelector('.ico').textContent;
      const txt = row.querySelector('.txt').textContent;
      return ico === ' ' ? txt : ico + ' ' + txt;
    })
    .join('\n');
}

function copyLog() {
  const btn = $('btnCopy');
  const text = logText();
  const done = () => {
    btn.textContent = '已复制';
    btn.classList.add('done');
    setTimeout(() => { btn.textContent = '复制'; btn.classList.remove('done'); }, 1600);
  };
  const fail = (e) => appendLog('复制失败：' + e, 'err');

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallback(text, done, fail));
  } else {
    fallback(text, done, fail);
  }
}

// WebView2 里 clipboard API 在非安全上下文可能被拒，退回 execCommand
function fallback(text, done, fail) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand('copy') ? done() : fail('execCommand 返回 false');
  } catch (e) {
    fail(e);
  } finally {
    document.body.removeChild(ta);
  }
}

/* ---------- 指标卡 ---------- */

function setCard(valueId, hintId, value, hint) {
  $(valueId).textContent = value == null || value === '' ? '—' : String(value);
  $(hintId).textContent = hint || '';
}

/**
 * signin.py 的响应是套娃的，而且不止一层：
 *   status  -> {step, http, body:{code,msg,requestId, data:{...}}}
 *   doctor  -> {result, credential_format, runtime, ...}   ← 顶层就是平的
 *   auto    -> {result, credit, streak_days, ...}          ← 也是平的
 * 所以按「谁先命中谁」逐层下钻，而不是假定某个固定层级。
 */
function unwrap(payload) {
  if (!payload || typeof payload !== 'object') return null;
  let cur = payload;
  // 最多下钻 4 层，每层都在 body / data / result 里找更像业务负载的那个
  for (let i = 0; i < 4; i++) {
    const hasDirect = 'today_credit' in cur || 'streak_days' in cur
                   || 'credential_format' in cur || 'credit' in cur;
    if (hasDirect) return cur;

    let next = null;
    for (const key of ['data', 'body', 'result']) {
      const v = cur[key];
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        // data 里带业务字段时优先，否则记下 body 兜底
        const score = ('today_credit' in v) + ('streak_days' in v)
                    + ('credential_format' in v) + ('total_credits' in v);
        if (!next || score > 0) next = v;
        if (score > 0) break;
      }
    }
    if (!next) return cur;
    cur = next;
  }
  return cur;
}

function firstNum(...vals) {
  for (const v of vals) {
    if (v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v))) {
      return Number(v);
    }
  }
  return null;
}

function renderFromStatus(raw) {
  const d = unwrap(raw);
  if (!d) return;

  const checked = d.today_checked_in === true || d.result === 'ALREADY'
               || d.result === 'CLAIMED';
  const credit = firstNum(d.credit, d.today_credit, d.daily_credit);
  const streak = firstNum(d.streak_days);
  const total = firstNum(d.total_credits);

  setCard('vCredit', 'hCredit', credit,
    credit != null ? (checked ? '今日已签到' : '今日未领取') : '未取到数据');
  setCard('vStreak', 'hStreak', streak, streak != null ? '连续签到' : '');
  setCard('vTotal', 'hTotal', total, total != null ? '历史累计' : '');

  const week = firstNum(d.week_checkin_days);
  const rows = [
    ['今日已签', checked ? '是' : '否'],
    ['连签天数', streak],
    ['累计积分', total],
    ['连签奖励日', d.is_streak_day === true ? '是' : '否'],
    ['本周签到', week != null ? week + ' / 7 天' : null],
    ['活动', d.activity_name || d.theme_name],
  ];
  const dds = $('kv').querySelectorAll('dd');
  rows.forEach((r, i) => {
    if (dds[i]) dds[i].textContent = r[1] == null || r[1] === '' ? '—' : String(r[1]);
  });

  // 缓存渲染不写趋势图，否则每次启动都会多出一根重复柱子
  renderCalendar(d);
}

function renderFromDoctor(raw) {
  const d = unwrap(raw);
  if (!d) return;
  const fmt = d.credential_format;
  setCard('vCred', 'hCred',
    fmt === 'plaintext' ? '明文' : fmt === 'sym-v1' ? 'sym-v1 加密' : (fmt || '—'),
    d.electron_version ? 'Electron ' + d.electron_version
      : (d.runtime ? '运行时已就绪' : (d.report || '')));
}

/* ---------- 云端任务 ---------- */

const TASK_TABS = {
  // accept_status 的服务端取值，completed 保留兼容
  todo: ['not_accepted', 'accepted', 'in_progress', 'completed'],
  done: ['claimed'],
};
const TASK_LABEL = {
  not_accepted: '待接单',
  accepted: '进行中',
  in_progress: '进行中',
  completed: '可领奖',
  claimed: '已领取',
};

let TASKS = [];
let TASK_TAB = 'todo';

function renderTasks() {
  const box = $('tasklist');
  const want = TASK_TABS[TASK_TAB];
  const list = TASKS.filter((t) => want.indexOf(t.status) >= 0);

  $('cntTodo').textContent = TASKS.filter((t) => TASK_TABS.todo.indexOf(t.status) >= 0).length;
  $('cntDone').textContent = TASKS.filter((t) => TASK_TABS.done.indexOf(t.status) >= 0).length;

  box.innerHTML = '';
  if (!TASKS.length) {
    box.innerHTML = '<p class="empty">点击「刷新任务」拉取</p>';
    return;
  }
  if (!list.length) {
    box.innerHTML = '<p class="empty">这一类暂时没有任务</p>';
    return;
  }

  // 未完成按进度排前面，已完成按时间无所谓
  list.forEach((t) => box.appendChild(makeTaskNode(t)));
}

function makeTaskNode(t) {
  const el = document.createElement('div');
  el.className = 'task' + (TASK_TAB === 'done' ? ' done' : '');

  const top = document.createElement('div');
  top.className = 'task-top';

  const title = document.createElement('div');
  title.className = 'task-title';
  const name = document.createElement('span');
  name.textContent = t.title;
  name.title = t.title;
  title.appendChild(name);

  if (t.new) title.appendChild(makeTag('新', 'new'));
  if (t.buddy) title.appendChild(makeTag('Buddy', 'buddy'));
  if (t.locked) title.appendChild(makeTag('未解锁', 'locked'));

  const reward = document.createElement('div');
  reward.className = 'task-reward';
  const bits = [];
  if (t.credit) bits.push(t.credit + ' 积分');
  if (t.energy) bits.push(t.energy + ' 能量');
  reward.textContent = bits.join(' · ') || '—';

  top.append(title, reward);
  el.appendChild(top);

  if (t.desc) {
    const d = document.createElement('p');
    d.className = 'task-desc';
    d.textContent = t.desc;
    d.title = t.desc;
    el.appendChild(d);
  }

  // 进度条：有 current/target 才画
  if (t.current != null && t.target) {
    const pct = Math.min(100, Math.round((t.current / t.target) * 100));
    const bar = document.createElement('div');
    bar.className = 'bar' + (pct >= 100 ? ' full' : '');
    const fill = document.createElement('i');
    fill.style.width = pct + '%';
    bar.appendChild(fill);
    el.appendChild(bar);

    const label = document.createElement('div');
    label.className = 'bar-label';
    label.innerHTML = '<span>' + t.current + ' / ' + t.target + '</span>'
                    + '<span>' + pct + '%</span>';
    el.appendChild(label);
  }

  const tag = makeTag(TASK_LABEL[t.status] || t.status,
                      t.status === 'claimed' ? 'claimed' : '');
  title.appendChild(tag);
  return el;
}

function makeTag(text, cls) {
  const s = document.createElement('span');
  s.className = 'tag ' + (cls || '');
  s.textContent = text;
  return s;
}

function loadTasks(silent) {
  const btn = $('btnTasks');
  btn.disabled = true;
  btn.textContent = '拉取中…';
  call('tasks')
    .then((r) => {
      if (r && r.ok) {
        TASKS = r.items || [];
        renderTasks();
        if (!silent) appendLog('已拉取云端任务：' + TASKS.length + ' 个', 'ok');
      } else {
        appendLog('拉取任务失败：' + (r && r.error || '未知错误'), 'err');
      }
    })
    .catch((e) => appendLog('拉取任务异常：' + e, 'err'))
    .finally(() => {
      btn.disabled = false;
      btn.textContent = '刷新任务';
    });
}

/* ---------- 成长中心结果 ---------- */

/* ---------- 本月签到日历 ---------- */

/**
 * 用 checkin_dates 画当月日历。
 * 之前这里放的是"近 14 次查询"的柱状图，但每天的 daily_credit 是固定的，
 * 画出来就是 14 根一模一样的柱子，没有任何信息量——换成日历才说得通：
 * 哪天签了、哪天断了，一眼可见。
 */
function renderCalendar(d) {
  const box = $('cal');
  const dates = Array.isArray(d.checkin_dates) ? d.checkin_dates : [];
  if (!dates.length) return;

  // 以服务端返回的活动区间为准，避免本机时区把月初算错
  const sample = dates[0];                       // 形如 2026-10-19
  const [y, m] = sample.split('-').map(Number);
  const first = new Date(y, m - 1, 1);
  const days = new Date(y, m, 0).getDate();
  const lead = first.getDay();                  // 周日为 0
  const rows = Math.ceil((lead + days) / 7);    // 当月实际占用的周数
  const todayStr = null;                        // 稍后用今天（服务端无该字段）

  const set = new Set(dates.map((s) => String(s).slice(8, 10)));

  box.innerHTML = '';

  const head = document.createElement('div');
  head.className = 'cal-head';
  ['日', '一', '二', '三', '四', '五', '六'].forEach((w) => {
    const s = document.createElement('span');
    s.textContent = w;
    head.appendChild(s);
  });
  box.appendChild(head);

  const grid = document.createElement('div');
  grid.className = 'cal-grid';
  grid.style.setProperty('--cal-rows', rows);
  for (let i = 0; i < lead; i++) {
    const ph = document.createElement('i');
    grid.appendChild(ph);
  }
  for (let dnum = 1; dnum <= days; dnum++) {
    const cell = document.createElement('div');
    cell.className = 'cal-day';
    cell.textContent = dnum;
    if (set.has(String(dnum).padStart(2, '0'))) cell.classList.add('on');
    grid.appendChild(cell);
  }
  box.appendChild(grid);

  // 页脚：本月签到天数 + 本周 7 点进度
  const foot = document.createElement('div');
  foot.className = 'cal-foot';

  const lg = document.createElement('span');
  lg.className = 'cal-legend';
  lg.innerHTML = '<b class="on"></b>已签到';
  foot.appendChild(lg);

  const monthCount = dates.filter((s) => String(s).startsWith(sample.slice(0, 7))).length;
  const cnt = document.createElement('span');
  cnt.textContent = '本月 ' + monthCount + ' 天';
  foot.appendChild(cnt);

  const week = Array.isArray(d.week_progress) ? d.week_progress : [];
  const dots = document.createElement('span');
  dots.className = 'weekdots';
  for (let i = 0; i < 7; i++) {
    const b = document.createElement('b');
    if (week[i]) b.classList.add('on');
    b.title = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][i];
    dots.appendChild(b);
  }
  foot.appendChild(dots);
  box.appendChild(foot);
}

/* ---------- 后端事件 ---------- */

window.__onBackend = function (kind, data) {
  if (kind === 'log') {
    appendLog(data);

  } else if (kind === 'done') {
    const p = data.payload;
    if (data.action === 'doctor') {
      renderFromDoctor(p);
    } else if (p) {
      // status / auto / claim / all 的签到字段结构一致，统一走同一个渲染。
      // 成长中心的细项不再单独出卡片，后端已把它们写进日志的摘要行。
      renderFromStatus(p);
    }
    setBusy(false);
    if (data.exit === 0) {
      setBadge('就绪', 'ok');
      appendLog('执行成功（' + data.action + '）', 'ok');
    } else {
      setBadge('exit=' + data.exit, 'err');
      appendLog('执行失败，退出码 ' + data.exit, 'err');
    }
  }
};

/* ---------- 交互 ---------- */

let BUSY = false;
let PENDING = null;      // 等待二次确认的动作 {key, btn}

function setBusy(on) {
  BUSY = on;
  document.querySelectorAll('#actGroup .btn').forEach((b) => {
    if (!b.classList.contains('confirm')) b.disabled = on;
  });
  setBadge(on ? '执行中…' : '就绪', on ? 'busy' : 'ok');
}

function run(action) {
  if (BUSY) return;
  setBusy(true);
  call('run', action)
    .then((r) => {
      if (r && !r.ok) {
        appendLog('[' + new Date().toLocaleTimeString('zh-CN') + '] ' + r.error, 'err');
        setBusy(false);
      }
    })
    .catch((e) => {
      appendLog('调用后端失败：' + e, 'err');
      setBusy(false);
    });
}

/** 串行执行：后端同一时刻只允许一个任务。 */
function runSeries(actions) {
  const queue = actions.slice();
  const step = () => {
    if (!queue.length) return;
    run(queue.shift());
    const wait = () => {
      if (!BUSY) step();
      else setTimeout(wait, 150);
    };
    setTimeout(wait, 250);
  };
  step();
}

/* ---------- 动作按钮 ---------- */

// 按钮区下方的提示行：默认文案来自 HTML；悬停/聚焦某个命令按钮时改成
// 展示它的完整功能描述，移开（或失焦）后还原。原生 tooltip（title）保留
// 作为兜底，但描述较长，只有这行能一眼看全。
const DEFAULT_HINT = $('actionHint').textContent;

function showHint(text) {
  const h = $('actionHint');
  h.textContent = text || DEFAULT_HINT;
  h.classList.toggle('show', !!text);
}

function buildActions(list) {
  const box = $('actGroup');
  box.innerHTML = '';

  const readonly = list.filter((a) => !a.write);
  const writing = list.filter((a) => a.write);

  const add = (a, cls) => {
    const b = document.createElement('button');
    b.className = 'btn ' + cls;
    b.id = 'act-' + a.key;
    b.title = a.desc;
    b.innerHTML = esc(a.label) + ' <code>' + esc(a.key) + '</code>';
    b.addEventListener('mouseenter', () => showHint(a.desc));
    b.addEventListener('mouseleave', () => showHint());
    b.addEventListener('focus', () => showHint(a.desc));
    b.addEventListener('blur', () => showHint());
    b.addEventListener('click', () => onActionClick(a, b));
    box.appendChild(b);
    return b;
  };

  readonly.forEach((a) => add(a, 'primary'));
  if (writing.length) {
    const sep = document.createElement('span');
    sep.className = 'act-sep';
    sep.textContent = '写操作';
    box.appendChild(sep);
    writing.forEach((a) => add(a, 'act'));
  }
}

function onActionClick(action, btn) {
  // 已在等待确认且点的是同一个按钮 -> 确认执行
  if (PENDING && PENDING.key === action.key) {
    btn.classList.remove('confirm');
    const key = PENDING.key;
    PENDING = null;
    btn.innerHTML = esc(action.label) + ' <code>' + esc(key) + '</code>';
    appendLog('已确认执行：' + action.desc, 'warn');
    run(key);
    return;
  }
  // 点别的按钮：取消上一个待确认
  resetPending();

  if (!action.write) {
    run(action.key);
    return;
  }

  // 写操作二次确认
  PENDING = { key: action.key, btn: btn };
  btn.classList.add('confirm');
  btn.innerHTML = '确认执行？ <code>点此</code>';
  appendLog('待确认：' + action.desc + '（再次点击该按钮以执行）', 'warn');
  setTimeout(() => { if (PENDING) resetPending(); }, 8000);
}

function resetPending() {
  if (!PENDING) return;
  const btn = PENDING.btn;
  btn.classList.remove('confirm');
  const a = state.actions.find((x) => x.key === PENDING.key);
  if (a) btn.innerHTML = esc(a.label) + ' <code>' + esc(a.key) + '</code>';
  PENDING = null;
}

/* ---------- 启动 ---------- */

async function boot(api) {
  const info = await api.bootstrap();

  // 主题：优先用本地记忆，其次跟随系统
  let saved = null;
  try { saved = localStorage.getItem('wbTheme'); } catch (e) { /* 忽略 */ }
  applyTheme(saved || (info.dark ? 'dark' : 'light'));
  if (info.accent) applyAccent(info.accent);

  $('env').textContent = 'Python ' + info.python + ' · ' + info.platform;
  $('env').title = info.executable;

  const sp = $('envSignin');
  sp.textContent = info.signin_exists ? 'signin.py 已找到' : 'signin.py 未找到';
  sp.classList.toggle('bad', !info.signin_exists);
  sp.title = info.signin;
  if (!info.signin_exists) {
    appendLog('未找到 signin.py。', 'err');
    appendLog('程序按以下顺序找过（都不存在）：', 'meta');
    (info.signin_searched || []).forEach((p) => appendLog('  ' + p, 'meta'));
    appendLog('解决办法：把 signin.py 放到 exe 同目录，'
      + '或设环境变量 WORKBUDDY_DEMO_SIGNIN 指向它的完整路径。', 'info');
  }

  // 先渲染上次的结果，界面立刻有数据
  const cached = info.cached || {};
  if (cached.doctor) {
    renderFromDoctor(cached.doctor.payload);
    appendLog('已载入上次体检结果（' + cached.doctor.at + '）', 'meta');
  }
  if (cached.status) {
    renderFromStatus(cached.status.payload);
    appendLog('已载入上次签到状态（' + cached.status.at + '）', 'meta');
  }

  if (info.history && info.history.length) {
    info.history.forEach((l) => appendLog(l));
  }

  // 动作按钮由后端下发的命令表生成
  state.actions = info.actions || [];
  buildActions(state.actions);
  appendLog('可用命令：' + state.actions.map((a) => a.key).join(' / '), 'meta');
  setBadge('就绪', 'ok');

  // 启动即刷新：缓存只是"先有个数看"，真实数据以这次查询为准。
  // doctor 只跑后端、不暴露按钮，用来填充「凭据格式」那张卡。
  if (info.signin_exists) {
    runSeries(['status', 'doctor']);
    loadTasks(true);              // 后台静默拉一次，不打断启动
  } else {
    setBadge('缺少 signin.py', 'err');
  }

  // 用后端读到的系统主题/标题再校正一次
  try {
    const t = await api.system_theme();
    if (!localStorage.getItem('wbTheme')) applyTheme(t.dark ? 'dark' : 'light');
    if (t.accent) applyAccent(t.accent);
    await api.native_title('WorkBuddy 控制台');
  } catch (e) {
    appendLog('主题同步失败（不影响使用）：' + (e && e.message || e), 'warn');
  }
}

document.addEventListener('DOMContentLoaded', function () {
  $('btnClear').addEventListener('click', clearLog);
  $('btnCopy').addEventListener('click', copyLog);
  $('themeToggle').addEventListener('click', toggleTheme);
  $('btnTasks').addEventListener('click', () => loadTasks(false));

  $('taskTabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.tab');
    if (!btn) return;
    TASK_TAB = btn.dataset.tab;
    $('taskTabs').querySelectorAll('.tab').forEach((t) => {
      t.classList.toggle('active', t === btn);
    });
    renderTasks();
  });

  // 全局兜底：任何未捕获异常都写到界面上，并让后端落盘。
  // 打包后没有控制台，不落盘的话这些异常事后完全无从追溯。
  function reportError(text) {
    try { window.__errs.push(text); } catch (e) {}
    appendLog(text, 'err');
    try {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.log_error) {
        window.pywebview.api.log_error(text);
      }
    } catch (e) { /* 落盘失败就算了，界面已经显示过了 */ }
  }

  window.addEventListener('error', (e) => {
    reportError('前端异常：' + (e.message || e)
      + ' @' + (e.filename || '') + ':' + (e.lineno || ''));
  });
  window.addEventListener('unhandledrejection', (e) => {
    reportError('未处理的 Promise 异常：'
      + ((e.reason && e.reason.message) || e.reason));
  });

  // pywebview 可能在 app.js 挂上监听之前就已经派发过 pywebviewready，
  // 那样就永远等不到、界面一直卡在「连接中…」。
  // 所以先探测现成的 api，探测不到再等事件——两条路都要覆盖。
  function ready() {
    if (state.ready) return;
    if (!window.pywebview || !window.pywebview.api) return;
    state.ready = true;                    // 防止并发重复初始化
    boot(window.pywebview.api)
      .catch((e) => {
        appendLog('初始化失败：' + (e && e.message || e), 'err');
        setBadge('初始化失败', 'err');
      });
  }

  window.addEventListener('pywebviewready', ready);
  if (window.pywebview && window.pywebview.api) ready();
  else setTimeout(ready, 250);             // 兜底轮询一次
});

/** 调后端接口的薄封装：pywebview 的 api 返回 Promise。 */
function call(method) {
  const args = Array.prototype.slice.call(arguments, 1);
  return Promise.resolve(window.pywebview.api[method].apply(null, args));
}