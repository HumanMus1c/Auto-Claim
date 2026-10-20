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

/* ---------- 侧边栏页面切换 ---------- */

const PAGE_KEY = 'wbPage';
let CURRENT_PAGE = 'workbuddy';
let QODER_LOADED = false;
let QODER_LOADING = false;

function savedPage() {
  try {
    const saved = localStorage.getItem(PAGE_KEY);
    if (saved === 'qoder') return 'qoder';
  } catch (e) { /* 忽略 */ }
  return 'workbuddy';
}

function setPage(page) {
  CURRENT_PAGE = page === 'qoder' ? 'qoder' : 'workbuddy';
  document.querySelectorAll('.side-tab').forEach((tab) => {
    const active = tab.dataset.page === CURRENT_PAGE;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-current', active ? 'page' : 'false');
  });
  const wb = $('page-workbuddy');
  const qoder = $('page-qoder');
  if (wb) wb.hidden = CURRENT_PAGE !== 'workbuddy';
  if (qoder) qoder.hidden = CURRENT_PAGE !== 'qoder';
  try { localStorage.setItem(PAGE_KEY, CURRENT_PAGE); } catch (e) { /* 忽略 */ }
  if (CURRENT_PAGE === 'qoder') ensureQoder();
}

function ensureQoder() {
  if (QODER_LOADED || QODER_LOADING) return;
  refreshQoder(false);
  refreshQoderService(true);
  scheduleQoderServicePoll();
}

/* ---------- 主题 ---------- */

// 首屏配色已由 index.html 里的内联脚本在样式表之前完成，这里只负责
// 运行期切换。本地记住的是「模式」而不是最终亮暗：system / light / dark。
const THEME_KEY = 'wbTheme';
const THEME_LABEL = { system: '跟随系统', light: '浅色', dark: '深色' };
const THEME_GLYPH = { system: '◐', light: '☀', dark: '☾' };

function systemPrefersDark() {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  } catch (e) {
    return false;
  }
}

function savedThemeMode() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark' || saved === 'system') return saved;
  } catch (e) { /* 忽略 */ }
  return 'system';               // 没有本地选择时默认跟随系统
}

function applyTheme(mode) {
  state.theme = mode;
  const root = document.documentElement;
  root.dataset.theme = mode;
  root.style.colorScheme = mode;
}

function syncThemeMenu() {
  const menu = $('themeMenu');
  if (!menu) return;
  menu.querySelectorAll('[data-theme-mode]').forEach((item) => {
    const active = item.dataset.themeMode === state.themeMode;
    item.classList.toggle('active', active);
    item.setAttribute('aria-checked', active ? 'true' : 'false');
  });
  const note = $('themeSystemState');
  if (note) note.textContent = state.systemDark ? '当前深色' : '当前浅色';
}

function updateThemeButton() {
  const btn = $('themeToggle');
  if (!btn) return;
  const mode = state.themeMode;
  btn.textContent = THEME_GLYPH[mode] || THEME_GLYPH.system;
  const text = mode === 'system'
    ? '主题：跟随系统（当前' + THEME_LABEL[state.theme] + '）'
    : '主题：' + THEME_LABEL[mode];
  btn.title = text + '，点击选择';
  btn.setAttribute('aria-label', btn.title);
}

/** 切换主题模式；system 模式使用最近一次读到的系统亮暗。 */
function applyThemeMode(mode, options) {
  options = options || {};
  if (mode !== 'light' && mode !== 'dark') mode = 'system';
  if (typeof options.systemDark === 'boolean') state.systemDark = options.systemDark;
  state.themeMode = mode;
  applyTheme(mode === 'system'
    ? (state.systemDark ? 'dark' : 'light')
    : mode);
  document.documentElement.dataset.themeMode = mode;
  if (options.persist) {
    try { localStorage.setItem(THEME_KEY, mode); } catch (e) { /* 忽略 */ }
  }
  updateThemeButton();
  syncThemeMenu();
}

/** 系统主题变化：只有「跟随系统」模式才改页面，手动选择保持不动。 */
function updateSystemTheme(dark) {
  state.systemDark = !!dark;
  if (state.themeMode === 'system') {
    applyThemeMode('system', { systemDark: state.systemDark });
  } else {
    syncThemeMenu();
  }
}

function setThemeMenu(open) {
  const menu = $('themeMenu');
  const btn = $('themeToggle');
  if (!menu || !btn) return;
  menu.hidden = !open;
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function chooseThemeMode(mode) {
  applyThemeMode(mode, { persist: true });
  setThemeMenu(false);
  const btn = $('themeToggle');
  if (btn) btn.focus();
}

function toggleThemeMenu() {
  const menu = $('themeMenu');
  if (menu) setThemeMenu(menu.hidden);
}

state.themeMode = savedThemeMode();
state.systemDark = systemPrefersDark();

// 浏览器媒体查询是系统主题最及时的信号；win_icon 的原生推送作为补充。
const systemThemeMedia = window.matchMedia
  ? window.matchMedia('(prefers-color-scheme: dark)')
  : null;
if (systemThemeMedia) {
  const onSystemThemeChange = (event) => updateSystemTheme(event.matches);
  if (systemThemeMedia.addEventListener) {
    systemThemeMedia.addEventListener('change', onSystemThemeChange);
  } else if (systemThemeMedia.addListener) {
    systemThemeMedia.addListener(onSystemThemeChange);   // 旧版 WebView2
  }
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

  // 失败/异常结果通常只有 result/report，没有签到字段。此时保留上一次
  // 渲染的数据，不要把指标卡和签到详情清成「—」。streak_days 不算：
  // 成长中心汇报里也有这个字段，正是它让 growth 结果被误当成签到状态。
  const hasStatus = ['today_checked_in', 'today_credit', 'daily_credit',
                     'credit', 'total_credits', 'checkin_dates',
                     'week_progress'].some((k) => k in d);
  if (!hasStatus) return;

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

/* ---------- 成长中心任务 ---------- */

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
let GROWTH = null;
let GROWTH_LOADING = false;

function renderTasks() {
  const box = $('tasklist');
  const want = TASK_TABS[TASK_TAB];
  const list = TASKS.filter((t) => want.indexOf(t.status) >= 0);

  $('cntTodo').textContent = TASKS.filter((t) => TASK_TABS.todo.indexOf(t.status) >= 0).length;
  $('cntDone').textContent = TASKS.filter((t) => TASK_TABS.done.indexOf(t.status) >= 0).length;

  box.innerHTML = '';
  if (!TASKS.length) {
    box.innerHTML = '<p class="empty">点击「刷新成长中心」拉取</p>';
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

/* ---------- 成长中心概览 ---------- */

const TRAVEL_LABEL = {
  idle: '待命',
  traveling: '旅行中',
  arrived: '已到达',
  unknown: '未知',
};
const REDEEM_LABEL = {
  available: '可兑换',
  claimed: '已兑换',
  locked: '未解锁',
  unknown: '未知',
};

function growthEta(arriveAt, serverNow) {
  if (arriveAt == null || serverNow == null) return '';
  const left = Number(arriveAt) - Number(serverNow);
  if (!Number.isFinite(left)) return '';
  if (left <= 0) return '已到达待领取';
  const minutes = Math.max(1, Math.round(left / 60));
  if (minutes < 60) return '约 ' + minutes + ' 分钟后回';
  return '约 ' + (left / 3600).toFixed(1) + ' 小时后回';
}

function growthTierLabel(tier) {
  const text = String(tier || '');
  return /^\d+d$/.test(text) ? text.slice(0, -1) + ' 天' : text;
}

function makeGrowthCard(label) {
  const el = document.createElement('div');
  el.className = 'growth-card';

  const head = document.createElement('div');
  head.className = 'growth-card-head';
  const name = document.createElement('span');
  name.className = 'growth-card-label';
  name.textContent = label;
  head.appendChild(name);
  el.appendChild(head);

  const value = document.createElement('div');
  value.className = 'growth-card-value';
  el.appendChild(value);

  const lines = document.createElement('div');
  lines.className = 'growth-card-lines';
  el.appendChild(lines);

  return { el, value, lines };
}

function addGrowthLine(card, text, cls) {
  if (!text) return;
  const p = document.createElement('p');
  p.className = 'growth-card-line' + (cls ? ' ' + cls : '');
  p.textContent = text;
  p.title = text;
  card.lines.appendChild(p);
}

function addGrowthBar(card, pct, left, right) {
  const safe = Math.max(0, Math.min(100, Number(pct) || 0));
  const bar = document.createElement('div');
  bar.className = 'bar' + (safe >= 100 ? ' full' : '');
  const fill = document.createElement('i');
  fill.style.width = safe + '%';
  bar.appendChild(fill);
  card.lines.appendChild(bar);

  const label = document.createElement('div');
  label.className = 'bar-label';
  const a = document.createElement('span');
  a.textContent = left || '';
  const b = document.createElement('span');
  b.textContent = right || '';
  label.append(a, b);
  card.lines.appendChild(label);
}

function renderGrowth(data, at) {
  const box = $('growthOverview');
  if (!box) return;
  data = (data && typeof data === 'object') ? data : {};
  GROWTH = data;
  box.innerHTML = '';

  const updated = $('growthUpdated');
  if (updated) updated.textContent = at ? '更新于 ' + at : '刚刚更新';

  const errors = Array.isArray(data.errors) ? data.errors : [];
  if (errors.length) {
    const err = document.createElement('p');
    err.className = 'growth-errors';
    err.textContent = '部分数据读取失败：' + errors.join('；');
    box.appendChild(err);
  }

  // 任务接口失败时保留上一次的任务列表，避免一次网络抖动把整栏清空。
  const tasksFailed = errors.some((e) => String(e).indexOf('/tasks') === 0);
  if (Array.isArray(data.tasks) && !tasksFailed) {
    TASKS = data.tasks;
    renderTasks();
  }

  // Buddy 旅行
  const travelCard = makeGrowthCard('Buddy 旅行');
  if (!data.travel) {
    travelCard.value.textContent = '—';
    addGrowthLine(travelCard, '未取到数据');
  } else {
    const travel = data.travel;
    const state = travel.state || 'unknown';
    travelCard.value.textContent = TRAVEL_LABEL[state] || state;
    if (state === 'traveling') {
      const eta = growthEta(travel.arrive_at, travel.server_now);
      addGrowthLine(travelCard, (travel.location ? '前往 ' + travel.location : '旅行中')
        + (eta ? ' · ' + eta : ''));
    } else if (state === 'arrived') {
      addGrowthLine(travelCard, '旅行礼物待领取');
    } else if (state === 'idle') {
      addGrowthLine(travelCard, travel.daily_limit_reached ? '今日派出名额已用完' : '今日可派出');
    } else {
      addGrowthLine(travelCard, '状态未知');
    }
    if (state !== 'idle') {
      addGrowthLine(travelCard, travel.daily_limit_reached ? '今日名额：已用完' : '今日名额：可用');
    }
    if (travel.reward_credit) addGrowthLine(travelCard, '待领礼物：+' + travel.reward_credit + ' 积分');
  }
  box.appendChild(travelCard.el);

  // 补登
  const streak = data.streak || {};
  const makeup = streak.makeup_cards || {};
  const makeupCard = makeGrowthCard('补登');
  if (!data.streak) {
    makeupCard.value.textContent = '—';
    addGrowthLine(makeupCard, '未取到数据');
  } else {
    const balance = firstNum(makeup.balance);
    const max = firstNum(makeup.max) || 4;
    makeupCard.value.textContent = (balance != null ? balance : '—') + ' / ' + max + ' 张';
    if (balance != null && balance > 0) {
      const dates = Array.isArray(streak.makeup_candidates) ? streak.makeup_candidates : [];
      if (dates.length) {
        addGrowthLine(makeupCard, '可补 ' + dates.length + ' 天 · 最近 ' + dates[0]);
      } else if (streak.makeup_error) {
        addGrowthLine(makeupCard, '可补日期读取失败', 'warn');
      } else {
        addGrowthLine(makeupCard, '暂无可补日期');
      }
      if (streak.makeup_error) addGrowthLine(makeupCard, streak.makeup_error, 'warn');
    } else if (balance === 0) {
      addGrowthLine(makeupCard, '暂无补登卡');
    } else {
      addGrowthLine(makeupCard, '未取到补登卡余额');
    }
    if (balance != null) {
      addGrowthBar(makeupCard, max ? (balance / max) * 100 : 0,
        balance + ' / ' + max, '上限 ' + max);
    }
  }
  box.appendChild(makeupCard.el);

  // 连登兑换
  const redeem = data.redeem || {};
  const redeemCard = makeGrowthCard('连登兑换');
  if (!data.streak && !data.redeem) {
    redeemCard.value.textContent = '—';
    addGrowthLine(redeemCard, '未取到数据');
  } else {
    const days = firstNum(streak.days);
    redeemCard.value.textContent = days != null ? '连签 ' + days + ' 天' : '—';
    const next = streak.next_tier;
    const remain = firstNum(streak.next_tier_remaining);
    if (next && remain != null) {
      addGrowthLine(redeemCard, '下一档 ' + growthTierLabel(next) + '，还差 ' + remain + ' 天');
      const target = parseInt(String(next), 10);
      if (days != null && target > 0) {
        addGrowthBar(redeemCard, (days / target) * 100,
          days + ' / ' + target, remain + ' 天后解锁');
      }
    } else if (days != null) {
      addGrowthLine(redeemCard, '已达最高档');
    }
    const tiers = Array.isArray(redeem.tiers) ? redeem.tiers : [];
    if (tiers.length) {
      addGrowthLine(redeemCard, tiers.map((t) =>
        (t.label || growthTierLabel(t.tier)) + ' '
        + (REDEEM_LABEL[t.status] || t.status || '未知')).join(' · '));
      const available = tiers.find((t) => t.status === 'available');
      if (available) {
        const bits = [];
        if (available.credit) bits.push('+' + available.credit + ' 积分');
        if (available.energy) bits.push('+' + available.energy + ' 能量');
        if (available.cards) bits.push('+' + available.cards + ' 补登卡');
        if (available.chances) bits.push('+' + available.chances + ' 抽奖');
        if (bits.length) addGrowthLine(redeemCard, '可兑奖励：' + bits.join(' · '));
      }
    } else {
      addGrowthLine(redeemCard, '未取到兑换档位');
    }
  }
  box.appendChild(redeemCard.el);

  // 抽奖
  const lotteryCard = makeGrowthCard('抽奖');
  if (!data.lottery) {
    lotteryCard.value.textContent = '—';
    addGrowthLine(lotteryCard, '未取到数据');
  } else {
    const chances = firstNum(data.lottery.balance);
    lotteryCard.value.textContent = chances != null ? chances + ' 次' : '—';
    addGrowthLine(lotteryCard, chances == null ? '未取到抽奖次数'
      : (chances > 0 ? '有 ' + chances + ' 次机会待使用' : '暂无抽奖机会'));
    addGrowthLine(lotteryCard, '每轮最多开 1 次 · 实物奖需手动填地址');
  }
  box.appendChild(lotteryCard.el);

  // Buddy 盲盒
  const buddyCard = makeGrowthCard('Buddy 盲盒');
  if (!data.buddy && !data.energy) {
    buddyCard.value.textContent = '—';
    addGrowthLine(buddyCard, '未取到数据');
  } else {
    const buddy = data.buddy || {};
    const energy = data.energy || {};
    const affordable = firstNum(buddy.affordable);
    const balance = firstNum(buddy.energy, energy.balance);
    const cost = firstNum(buddy.cost_per_open);
    const maxOpen = firstNum(buddy.max_open_count);
    buddyCard.value.textContent = affordable != null ? '可开 ' + affordable + ' 次' : '—';
    if (balance != null || cost != null) {
      addGrowthLine(buddyCard, '能量 ' + (balance != null ? balance : '—')
        + (cost != null ? ' / ' + cost : '')
        + (maxOpen != null ? ' · 单次最多 ' + maxOpen + ' 个' : ''));
    }
    if (balance != null && cost) {
      addGrowthBar(buddyCard, (balance / cost) * 100, balance + ' 能量', '单价 ' + cost);
    }
    if (affordable === 0) addGrowthLine(buddyCard, '能量不足，继续攒能量');
  }
  box.appendChild(buddyCard.el);
}

function loadGrowth(silent) {
  if (GROWTH_LOADING) return;
  GROWTH_LOADING = true;
  const btn = $('btnGrowth');
  const paneBtn = $('btnGrowthRefresh');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '刷新中…';
  }
  if (paneBtn) paneBtn.disabled = true;
  call('growth')
    .then((r) => {
      if (!r || !r.ok) {
        appendLog('刷新成长中心失败：' + ((r && r.error) || '未知错误'), 'err');
        return;
      }
      renderGrowth(r.data || {}, r.at);
      const errors = (r.data && r.data.errors) || [];
      errors.forEach((e) => appendLog('成长中心部分数据读取失败：' + e, 'warn'));
      const changes = Array.isArray(r.changes) ? r.changes : [];
      if (changes.length) {
        appendLog('成长中心数据变化（' + (r.at || '') + '）：', 'meta');
        changes.forEach((line) => appendLog('  ' + line, 'info'));
      } else if (!silent && !errors.length) {
        appendLog('成长中心已刷新（' + (r.at || '')
          + (r.baseline ? '，首次建立基线' : '，数据无变化') + '）', 'meta');
      }
    })
    .catch((e) => appendLog('刷新成长中心异常：' + e, 'err'))
    .finally(() => {
      GROWTH_LOADING = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = '刷新成长中心';
      }
      if (paneBtn) paneBtn.disabled = BUSY;
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

// 只有这些命令的输出是签到状态结构。growth 的输出是成长中心汇报
// （report / energy / streak_days…），字段并不完整；若交给 renderFromStatus，
// 顶部指标卡和签到详情会被覆盖成「—」。
const STATUS_ACTIONS = ['status', 'auto', 'claim', 'all'];

window.__onBackend = function (kind, data) {
  if (kind === 'log') {
    appendLog(data);

  } else if (kind === 'theme') {
    // 系统主题变化：只有「跟随系统」模式才改页面；手动选过亮/暗时保持不动。
    updateSystemTheme(!!data.dark);

  } else if (kind === 'qoder_log') {
    appendQoderLog(data);

  } else if (kind === 'qoder_done') {
    onQoderDone(data);

  } else if (kind === 'done') {
    const p = data.payload;
    if (data.action === 'doctor') {
      renderFromDoctor(p);
    } else if (p && STATUS_ACTIONS.indexOf(data.action) >= 0) {
      // status / auto / claim / all 的签到字段结构一致，统一走同一个渲染。
      // 成长中心的细项由 loadGrowth() 单独渲染。
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
    // 成长中心命令可能改变旅行、补登卡、连登、抽奖或能量；跑完立即重拉一次，
    // 让概览与日志都反映最新状态。
    if (data.action === 'auto' || data.action === 'growth') {
      loadGrowth(true);
    }
  }
};

/* ---------- 交互 ---------- */

let BUSY = false;
let PENDING = null;      // 等待二次确认的动作 {key, btn}

// 各面板标题栏上的刷新按钮；命令执行期间统一禁用，避免重复触发。
const PANE_REFRESH_BUTTONS = ['btnGrowthRefresh', 'btnStatusRefresh', 'btnCalRefresh'];

function setPaneRefreshDisabled(on) {
  PANE_REFRESH_BUTTONS.forEach((id) => {
    const b = $(id);
    if (b) b.disabled = on || (id === 'btnGrowthRefresh' && GROWTH_LOADING);
  });
}

function setBusy(on) {
  BUSY = on;
  document.querySelectorAll('#actGroup .btn').forEach((b) => {
    if (!b.classList.contains('confirm')) b.disabled = on;
  });
  setPaneRefreshDisabled(on);
  setBadge(on ? '执行中…' : '就绪', on ? 'busy' : 'ok');
}

/** 标题栏刷新：重新查询签到状态（同时更新签到状态与本月签到两栏）。 */
function refreshStatus() {
  if (BUSY) return;
  run('status');
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
function runSeries(actions, done) {
  const queue = actions.slice();
  const step = () => {
    if (!queue.length) {
      if (typeof done === 'function') done();
      return;
    }
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

/* ---------- 脚本服务（Windows 计划任务） ---------- */

const SVC_STATE_LABEL = {
  Ready: '就绪',
  Running: '运行中',
  Disabled: '已停用',
  Queued: '排队中',
  Unknown: '未知',
};

const SVC_BUTTONS = {
  install: 'btnSvcInstall',
  uninstall: 'btnSvcUninstall',
  enable: 'btnSvcEnable',
  disable: 'btnSvcDisable',
  run: 'btnSvcRun',
  stop: 'btnSvcStop',
};

const SVC_ACTION_LABEL = {
  install: '安装 / 修复',
  uninstall: '卸载',
  enable: '启用',
  disable: '停用',
  run: '立即运行',
  stop: '停止',
};

// 只有会改变任务注册状态的操作需要二次确认；启用/停用/运行/停止可随时恢复
const SVC_CONFIRM = { install: true, uninstall: true };

let SERVICE = null;
let SVC_BUSY = false;
let SVC_PENDING = null;
let SVC_TIMER = null;
let SVC_REFRESHING = false;
let SVC_REFRESH_SEQ = 0;

function svcResultText(code) {
  if (code === null || code === undefined || code === '') return '—';
  if (code === 0) return '成功';
  const known = {
    267009: '正在运行',
    267010: '已禁用',
    267011: '尚未运行',
    267014: '已终止',
  };
  if (known[code]) return known[code];
  let n = Number(code);
  if (!Number.isFinite(n)) return String(code);
  if (n < 0) n = n >>> 0;
  return '0x' + n.toString(16).toUpperCase();
}

function makeServiceNode(t) {
  const el = document.createElement('div');
  el.className = 'svc-task';

  const top = document.createElement('div');
  top.className = 'svc-task-top';

  const name = document.createElement('span');
  name.className = 'svc-name';
  name.textContent = t.name;
  name.title = t.action || t.name;

  const tag = document.createElement('span');
  let stateText = '未安装';
  let stateCls = 'locked';
  if (t.installed) {
    stateText = SVC_STATE_LABEL[t.state] || t.state || '未知';
    stateCls = t.state === 'Running' ? 'buddy' : (t.enabled ? 'claimed' : 'locked');
  }
  tag.className = 'tag ' + stateCls;
  tag.textContent = stateText;

  top.append(name, tag);
  el.appendChild(top);

  const line1 = document.createElement('p');
  line1.className = 'svc-line';
  line1.textContent = (t.when || '—') + ' · ' + (t.what || '');
  el.appendChild(line1);

  const line2 = document.createElement('p');
  line2.className = 'svc-line';
  if (t.installed) {
    const parts = ['上次 ' + (t.last_run || '尚未运行'),
                   '结果 ' + svcResultText(t.last_result)];
    if (t.next_run) parts.push('下次 ' + t.next_run);
    line2.textContent = parts.join(' · ');
    line2.title = [t.action, t.last_run, t.next_run].filter(Boolean).join('\n');
  } else {
    line2.textContent = '点击「安装 / 修复」创建此任务';
  }
  el.appendChild(line2);
  return el;
}

function renderService(s) {
  SERVICE = s || {};
  const supported = SERVICE.supported !== false;
  const ok = SERVICE.ok !== false;
  const tasks = SERVICE.tasks || [];

  const pill = $('svcState');
  let pillText = '未检测';
  let pillKind = '';
  if (!supported) {
    pillText = '仅 Windows';
    pillKind = 'warn';
  } else if (!ok) {
    pillText = '读取失败';
    pillKind = 'err';
  } else if (SERVICE.running) {
    pillText = '运行中';
    pillKind = 'busy';
  } else if (SERVICE.installed && SERVICE.enabled) {
    pillText = '已启用';
    pillKind = 'ok';
  } else if (SERVICE.installed) {
    const enabledCount = tasks.filter((t) => t.installed && t.enabled).length;
    pillText = enabledCount ? '部分停用' : '已停用';
    pillKind = 'warn';
  } else if (SERVICE.installed_count) {
    pillText = '部分安装';
    pillKind = 'warn';
  } else {
    pillText = '未安装';
  }
  pill.textContent = pillText;
  pill.className = 'svc-pill ' + pillKind;

  const summary = $('svcSummary');
  if (!supported) {
    summary.textContent = '脚本服务使用 Windows 计划任务，当前平台不支持。';
  } else if (!ok) {
    summary.textContent = '状态读取失败：' + (SERVICE.error || '未知错误');
  } else if (!SERVICE.installed_count) {
    summary.textContent = '尚未安装后台任务。点击「安装 / 修复」创建两个计划任务；'
      + '任务由 Windows 托管，不需要保持控制台打开。';
  } else if (SERVICE.installed && SERVICE.enabled) {
    summary.textContent = '两个计划任务均已安装并启用'
      + (SERVICE.next_run ? '，下次运行 ' + SERVICE.next_run : '')
      + '。';
  } else if (SERVICE.installed) {
    const enabledCount = tasks.filter((t) => t.installed && t.enabled).length;
    summary.textContent = enabledCount
      ? '两个计划任务均已安装，但有任务被停用；点击「启用」恢复按计划运行。'
      : '两个计划任务均已安装，但当前被停用；启用后会按计划自动运行。';
  } else {
    summary.textContent = '仅安装了 ' + SERVICE.installed_count
      + ' / 2 个计划任务，点击「安装 / 修复」补齐。';
  }

  const box = $('svcTasks');
  box.innerHTML = '';
  tasks.forEach((t) => box.appendChild(makeServiceNode(t)));

  const log = $('svcLog');
  if (SERVICE.log && SERVICE.log.line) {
    log.textContent = '最近日志 ' + (SERVICE.log.at || '') + ' · ' + SERVICE.log.line;
    log.title = (SERVICE.log.path || '') + '\n' + SERVICE.log.line;
  } else {
    log.textContent = '';
    log.title = '';
  }

  const installed = !!SERVICE.installed_count;
  const allEnabled = !!SERVICE.installed && !!SERVICE.enabled;
  const anyEnabled = tasks.some((t) => t.installed && t.enabled);
  const canUse = supported && ok && !SVC_BUSY;

  $('btnSvcInstall').disabled = !canUse;
  $('btnSvcEnable').disabled = !canUse || !installed || allEnabled;
  $('btnSvcDisable').disabled = !canUse || !installed || !anyEnabled;
  $('btnSvcRun').disabled = !canUse || !installed || !anyEnabled;
  $('btnSvcStop').disabled = !canUse || !SERVICE.running;
  $('btnSvcUninstall').disabled = !canUse || !installed;
  $('btnSvcRefresh').disabled = SVC_BUSY;
}

function setServiceBusy(on) {
  SVC_BUSY = on;
  const pill = $('svcState');
  if (on) {
    pill.textContent = '处理中…';
    pill.className = 'svc-pill busy';
  }
  Object.keys(SVC_BUTTONS).forEach((key) => {
    if (on) $(SVC_BUTTONS[key]).disabled = true;
  });
  $('btnSvcRefresh').disabled = on;
  if (!on) renderService(SERVICE || {});
}

function resetSvcPending() {
  if (!SVC_PENDING) return;
  const btn = $(SVC_BUTTONS[SVC_PENDING]);
  if (btn) {
    btn.classList.remove('confirm');
    if (btn.dataset.label) {
      btn.textContent = btn.dataset.label;
      delete btn.dataset.label;
    }
  }
  SVC_PENDING = null;
}

function scheduleServicePoll() {
  if (SVC_TIMER) clearTimeout(SVC_TIMER);
  const delay = (SERVICE && SERVICE.running) ? 20000 : 180000;
  SVC_TIMER = setTimeout(() => {
    if (document.hidden) {
      scheduleServicePoll();
      return;
    }
    refreshService(true).finally(scheduleServicePoll);
  }, delay);
}

function refreshService(silent) {
  if (SVC_BUSY || SVC_REFRESHING) return Promise.resolve();
  SVC_REFRESHING = true;
  const seq = ++SVC_REFRESH_SEQ;
  $('btnSvcRefresh').disabled = true;
  return call('service_status')
    .then((s) => {
      if (seq !== SVC_REFRESH_SEQ) return;
      renderService(s);
      if (!silent && s && s.ok === false) {
        appendLog('读取脚本服务状态失败：' + (s.error || '未知错误'), 'err');
      }
    })
    .catch((e) => {
      if (seq !== SVC_REFRESH_SEQ) return;
      renderService({ supported: true, ok: false, error: String(e), tasks: [] });
      appendLog('读取脚本服务状态异常：' + e, 'err');
    })
    .finally(() => {
      SVC_REFRESHING = false;
      if (seq === SVC_REFRESH_SEQ) $('btnSvcRefresh').disabled = SVC_BUSY;
    });
}

function serviceAction(action) {
  if (SVC_BUSY) return;
  const btn = $(SVC_BUTTONS[action]);
  const label = SVC_ACTION_LABEL[action] || action;

  if (SVC_CONFIRM[action] && SVC_PENDING !== action) {
    resetSvcPending();
    SVC_PENDING = action;
    btn.dataset.label = btn.textContent;
    btn.classList.add('confirm');
    btn.textContent = '确认' + label + '？';
    appendLog('待确认：' + label + '脚本服务（再次点击按钮执行）', 'warn');
    setTimeout(() => { if (SVC_PENDING) resetSvcPending(); }, 8000);
    return;
  }

  resetSvcPending();
  setServiceBusy(true);
  appendLog('脚本服务：' + label + '…', 'cmd');

  call('service_action', action)
    .then((r) => {
      if (r && r.ok) {
        appendLog('脚本服务：' + (r.message || (label + '完成')), 'ok');
      } else {
        appendLog('脚本服务：' + ((r && (r.error || r.message)) || '操作失败'), 'err');
      }
      if (r && r.status) {
        SVC_REFRESH_SEQ++;          // 丢弃操作前发出的旧状态响应
        renderService(r.status);
      } else {
        return refreshService(true);
      }
    })
    .catch((e) => appendLog('脚本服务调用失败：' + e, 'err'))
    .finally(() => {
      SVC_BUSY = false;
      refreshService(true).finally(() => setServiceBusy(false));
    });
}

/* ---------- Qoder CN 监控 ---------- */

const QODER_RESULT_LABEL = {
  OK: '已连接',
  CLAIMED: '已领取',
  ALREADY: '本周期已领取',
  INACTIVE: '当前无活动',
  PENDING: '活动未开放',
  DRY_RUN: '仅预览',
  NO_AUTH: '未登录',
  AUTH_ERROR: '登录态异常',
  AUTH_REJECTED: '需重新登录',
  FORBIDDEN: '无资格',
  NETWORK: '网络异常',
  TIMEOUT: '请求超时',
  ERROR: '查询失败',
};

const QODER_SVC_BUTTONS = {
  install: 'btnQSvcInstall',
  uninstall: 'btnQSvcUninstall',
  enable: 'btnQSvcEnable',
  disable: 'btnQSvcDisable',
  run: 'btnQSvcRun',
  stop: 'btnQSvcStop',
};

let QODER_DATA = null;
let QODER_SERVICE = null;
let QODER_BUSY = false;
let QODER_PENDING = false;
let QODER_SVC_BUSY = false;
let QODER_SVC_PENDING = null;
let QODER_SVC_REFRESHING = false;
let QODER_SVC_TIMER = null;

function qoderNum(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function qoderSum(list, key) {
  let total = null;
  list.forEach((item) => {
    const n = qoderNum(item[key]);
    if (n !== null) total = (total || 0) + n;
  });
  return total;
}

function qoderExpiry(value) {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString('zh-CN', { hour12: false });
}

function setQoderDd(index, value, title) {
  const dds = $('qKv') ? $('qKv').querySelectorAll('dd') : [];
  const dd = dds[index];
  if (!dd) return;
  dd.textContent = value == null || value === '' ? '—' : String(value);
  dd.title = title || '';
}

function qoderSetDot(kind) {
  const dot = $('qoderDot');
  if (!dot) return;
  dot.className = 'side-dot' + (kind ? ' ' + kind : '');
}

function renderQoderCards(status, doctor) {
  const campaigns = (status && status.campaigns) || [];
  const todo = campaigns.filter((c) => c.active && c.status === 'CLAIMABLE');
  const claimed = campaigns.filter((c) => c.active && c.status === 'CLAIMED');
  const todoCredits = qoderSum(todo, 'credits');
  const claimedCredits = qoderSum(claimed, 'credits');
  const keys = (list) => list.map((c) => c.campaign || '?').join('、');

  let credits = null;
  let creditsHint = '当前没有可领取活动';
  if (todo.length) {
    credits = todoCredits;
    creditsHint = '本周期可领取 · ' + keys(todo);
  } else if (claimed.length) {
    credits = claimedCredits;
    creditsHint = '本周期已领取 · ' + keys(claimed);
  } else if (status && status.report) {
    creditsHint = status.report;
  }
  setCard('qCredits', 'qCreditsHint', credits, creditsHint);

  const result = status && status.result;
  const stateText = result ? (QODER_RESULT_LABEL[result] || result) : '—';
  let stateHint = (status && status.report) || '等待查询';
  if (todo.length) stateHint = '可领取 ' + todo.length + ' 项';
  else if (claimed.length) stateHint = '本周期已领取 ' + claimed.length + ' 项';
  setCard('qState', 'qStateHint',
    stateText === '已连接' && (todo.length || claimed.length)
      ? (todo.length ? '可领取' : '已领取') : stateText,
    stateHint);

  let tokenText = '—';
  let tokenHint = '等待体检';
  if (doctor) {
    if (doctor.result === 'OK') {
      tokenText = doctor.token_expires_at ? '有效' : '可读取';
      tokenHint = doctor.token_expires_at
        ? '到期 ' + qoderExpiry(doctor.token_expires_at)
        : (doctor.report || '桌面端登录态');
    } else {
      tokenText = QODER_RESULT_LABEL[doctor.result] || doctor.result || '异常';
      tokenHint = doctor.report || '';
    }
  }
  setCard('qToken', 'qTokenHint', tokenText, tokenHint);

  setQoderDd(0, (doctor && doctor.auth_dir) || '—',
    (doctor && doctor.auth_dir) || '');
  setQoderDd(1, doctor && doctor.token_source === 'env'
    ? '环境变量 QODER_TOKEN' : '桌面端登录态');
  setQoderDd(2, doctor ? qoderExpiry(doctor.token_expires_at) : '—');
  setQoderDd(3, doctor && doctor.device_identity ? '已获取' : '未获取');
  setQoderDd(4, doctor && doctor.device_component
    ? String(doctor.device_component).split(/[\\/]/).pop() : '未找到',
    (doctor && doctor.device_component) || '');
  setQoderDd(5, QODER_DATA && QODER_DATA.at ? QODER_DATA.at : '—');
}

function qoderCampaignTag(campaign) {
  if (!campaign.active) return ['非活动期', 'locked'];
  if (campaign.status === 'CLAIMABLE') return ['可领取', 'buddy'];
  if (campaign.status === 'CLAIMED') return ['已领取', 'claimed'];
  return [campaign.status || '未知', 'locked'];
}

function renderQoderCampaigns(status) {
  const box = $('qCampaigns');
  if (!box) return;
  const campaigns = (status && status.campaigns) || [];
  box.innerHTML = '';
  if (!campaigns.length) {
    box.innerHTML = '<p class="empty">当前没有活动记录（活动可能已结束，或登录态需要刷新）</p>';
    return;
  }

  campaigns.forEach((c) => {
    const el = document.createElement('div');
    el.className = 'qcamp' + (c.status === 'CLAIMED' ? ' claimed' : '');

    const top = document.createElement('div');
    top.className = 'qcamp-top';

    const key = document.createElement('span');
    key.className = 'qcamp-key';
    key.textContent = c.campaign || '?';

    const reward = document.createElement('span');
    reward.className = 'qcamp-reward';
    reward.textContent = c.credits == null ? '—' : c.credits + ' Credits';

    top.append(key, reward);
    el.appendChild(top);

    const tagRow = document.createElement('div');
    tagRow.className = 'task-top';
    const tagInfo = qoderCampaignTag(c);
    const tag = document.createElement('span');
    tag.className = 'tag ' + tagInfo[1];
    tag.textContent = tagInfo[0];
    tagRow.appendChild(tag);
    el.appendChild(tagRow);

    const meta = document.createElement('p');
    meta.className = 'qcamp-meta';
    const parts = [];
    if (c.action) parts.push(c.action);
    if (c.window) parts.push(c.window);
    parts.push(c.active ? '活动进行中' : '不在领取窗口');
    meta.textContent = parts.join(' · ');
    el.appendChild(meta);

    box.appendChild(el);
  });
}

function appendQoderLog(text, kind) {
  const box = $('qoderLog');
  if (!box) return;
  kind = kind || classify(text);
  const spec = LOG_KINDS[kind] || LOG_KINDS.info;
  const row = document.createElement('div');
  row.className = 'line t-' + kind;

  const ico = document.createElement('span');
  ico.className = 'ico';
  ico.textContent = spec.ico;

  const txt = document.createElement('span');
  txt.className = 'txt';
  txt.textContent = text;

  row.append(ico, txt);
  box.appendChild(row);
  while (box.childElementCount > 400) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

function renderQoderLog(log) {
  const box = $('qoderLog');
  if (!box) return;
  box.innerHTML = '';
  const lines = (log && log.lines) || [];
  lines.forEach((line) => appendQoderLog(line));
  const at = $('qoderLogAt');
  if (at) at.textContent = log && log.at ? '最近写入 ' + log.at : '尚无日志';
}

function qoderSideState(status, doctor, healthy) {
  const hint = $('qoderSideHint');
  if (!hint) return;
  let text = '未检测';
  let kind = '';
  if (healthy) {
    text = '已连接';
    kind = 'ok';
  } else if (doctor && doctor.result === 'NO_AUTH') {
    text = '未登录';
    kind = 'warn';
  } else if (doctor && doctor.result === 'AUTH_REJECTED') {
    text = '需重新登录';
    kind = 'err';
  } else if (status && status.result) {
    text = QODER_RESULT_LABEL[status.result] || status.result;
    kind = status.result === 'OK' ? 'ok' : 'warn';
  }
  hint.textContent = text;
  qoderSetDot(kind);
}

function renderQoder(data) {
  QODER_DATA = data || {};
  const ok = QODER_DATA.ok !== false;
  const status = QODER_DATA.status || null;
  const doctor = QODER_DATA.doctor || null;

  if (!ok) {
    setCard('qCredits', 'qCreditsHint', '—', '无法读取');
    setCard('qState', 'qStateHint', '不可用',
      QODER_DATA.error || '请检查 qoder_signin.py 是否存在');
    setCard('qToken', 'qTokenHint', '—', '—');
    renderQoderCampaigns(null);
    renderQoderLog(QODER_DATA.log);
    const hint = $('qoderSideHint');
    if (hint) hint.textContent = QODER_DATA.busy ? '领取中' : '不可用';
    qoderSetDot(QODER_DATA.busy ? 'busy' : 'err');
    if (!QODER_DATA.busy) {
      appendQoderLog('Qoder 监控不可用：' + (QODER_DATA.error || '未知错误'), 'err');
    }
    return;
  }

  renderQoderCards(status, doctor);
  renderQoderCampaigns(status);
  renderQoderLog(QODER_DATA.log);
  qoderSideState(status, doctor, !!QODER_DATA.healthy);
  if (QODER_DATA.errors && QODER_DATA.errors.length) {
    QODER_DATA.errors.forEach((line) => appendQoderLog(line, 'warn'));
  }
}

function refreshQoder(silent) {
  if (QODER_LOADING) return Promise.resolve();
  QODER_LOADING = true;
  const btn = $('btnQoderRefresh');
  if (btn) btn.disabled = true;
  if (!silent) appendQoderLog('正在读取 Qoder 账号、活动与日志…', 'meta');
  return call('qoder_monitor')
    .then((data) => {
      QODER_LOADED = true;
      renderQoder(data);
    })
    .catch((e) => {
      QODER_LOADED = true;
      renderQoder({ ok: false, error: String(e) });
      appendQoderLog('刷新 Qoder 监控失败：' + e, 'err');
    })
    .finally(() => {
      QODER_LOADING = false;
      if (btn) btn.disabled = false;
    });
}

function qoderResetClaim() {
  if (!QODER_PENDING) return;
  const btn = $('btnQoderClaim');
  if (btn) {
    btn.classList.remove('confirm');
    btn.textContent = '领取今日 Credits';
  }
  QODER_PENDING = false;
}

function qoderClaim() {
  if (QODER_BUSY) return;
  const btn = $('btnQoderClaim');
  if (!QODER_PENDING) {
    QODER_PENDING = true;
    if (btn) {
      btn.classList.add('confirm');
      btn.textContent = '确认领取？点此执行';
    }
    appendQoderLog('待确认：再次点击「确认领取」会真实调用 Qoder 领取接口', 'warn');
    setTimeout(qoderResetClaim, 8000);
    return;
  }

  qoderResetClaim();
  QODER_BUSY = true;
  if (btn) btn.disabled = true;
  qoderSetDot('busy');
  appendQoderLog('正在领取 Qoder 今日 Credits…', 'cmd');
  call('qoder_claim')
    .then((r) => {
      if (r && !r.ok) {
        QODER_BUSY = false;
        if (btn) btn.disabled = false;
        appendQoderLog('领取未启动：' + (r.error || '未知错误'), 'err');
        refreshQoder(true);
      }
    })
    .catch((e) => {
      QODER_BUSY = false;
      if (btn) btn.disabled = false;
      appendQoderLog('领取调用失败：' + e, 'err');
    });
}

function onQoderDone(data) {
  QODER_BUSY = false;
  const btn = $('btnQoderClaim');
  if (btn) {
    btn.disabled = false;
    btn.textContent = '领取今日 Credits';
    btn.classList.remove('confirm');
  }
  if (data && data.payload) {
    const result = data.payload.result || '';
    const report = data.payload.report || result;
    appendQoderLog((QODER_RESULT_LABEL[result] || result || '完成') + '：' + report,
      data.exit === 0 ? 'ok' : 'warn');
  } else if (data && data.error) {
    appendQoderLog('领取异常：' + data.error, 'err');
  }
  if (data && data.log) renderQoderLog(data.log);
  refreshQoder(true);
  refreshQoderService(true);
}

function renderQoderService(s) {
  QODER_SERVICE = s || {};
  const supported = QODER_SERVICE.supported !== false;
  const ok = QODER_SERVICE.ok !== false;
  const tasks = QODER_SERVICE.tasks || [];
  const task = tasks[0] || {};

  const pill = $('qsvcState');
  let pillText = '未检测';
  let pillKind = '';
  if (!supported) {
    pillText = '仅 Windows';
    pillKind = 'warn';
  } else if (!ok) {
    pillText = '读取失败';
    pillKind = 'err';
  } else if (QODER_SERVICE.running) {
    pillText = '运行中';
    pillKind = 'busy';
  } else if (QODER_SERVICE.installed && QODER_SERVICE.enabled) {
    pillText = '已启用';
    pillKind = 'ok';
  } else if (QODER_SERVICE.installed) {
    pillText = '已停用';
    pillKind = 'warn';
  } else {
    pillText = '未安装';
  }
  pill.textContent = pillText;
  pill.className = 'svc-pill ' + pillKind;

  const summary = $('qsvcSummary');
  if (!supported) {
    summary.textContent = 'Qoder 计划任务使用 Windows 任务计划程序，当前平台不支持。';
  } else if (!ok) {
    summary.textContent = '状态读取失败：' + (QODER_SERVICE.error || '未知错误');
  } else if (!QODER_SERVICE.installed) {
    summary.textContent = '尚未安装 QoderDailyCredits。点击「安装 / 修复」创建独立任务；'
      + '它不会改动 WorkBuddy 的两个任务。';
  } else if (QODER_SERVICE.enabled) {
    summary.textContent = 'QoderDailyCredits 已启用'
      + (QODER_SERVICE.next_run ? '，下次运行 ' + QODER_SERVICE.next_run : '') + '。';
  } else {
    summary.textContent = 'QoderDailyCredits 已安装但被停用；点击「启用」恢复自动领取。';
  }

  const box = $('qsvcTasks');
  box.innerHTML = '';
  tasks.forEach((t) => box.appendChild(makeServiceNode(t)));

  const log = $('qsvcLog');
  if (QODER_SERVICE.log && QODER_SERVICE.log.line) {
    log.textContent = '最近日志 ' + (QODER_SERVICE.log.at || '') + ' · '
      + QODER_SERVICE.log.line;
    log.title = (QODER_SERVICE.log.path || '') + '\n' + QODER_SERVICE.log.line;
  } else {
    log.textContent = '';
    log.title = '';
  }

  let taskText = '—';
  let taskHint = '未检测';
  if (!supported) {
    taskText = '仅 Windows';
    taskHint = '计划任务管理不可用';
  } else if (!ok) {
    taskText = '读取失败';
    taskHint = QODER_SERVICE.error || '';
  } else if (!task.installed) {
    taskText = '未安装';
    taskHint = '点击「安装 / 修复」创建 QoderDailyCredits';
  } else if (task.state === 'Running') {
    taskText = '运行中';
    taskHint = '上次 ' + (task.last_run || '尚未运行');
  } else if (task.enabled) {
    taskText = '已启用';
    taskHint = task.next_run ? '下次 ' + task.next_run : '等待调度';
  } else {
    taskText = '已停用';
    taskHint = '点击「启用」恢复自动领取';
  }
  setCard('qTask', 'qTaskHint', taskText, taskHint);

  const installed = !!QODER_SERVICE.installed_count;
  const anyEnabled = tasks.some((t) => t.installed && t.enabled);
  const canUse = supported && ok && !QODER_SVC_BUSY;

  $('btnQSvcInstall').disabled = !canUse;
  $('btnQSvcEnable').disabled = !canUse || !installed || !!QODER_SERVICE.enabled;
  $('btnQSvcDisable').disabled = !canUse || !installed || !anyEnabled;
  $('btnQSvcRun').disabled = !canUse || !installed || !anyEnabled;
  $('btnQSvcStop').disabled = !canUse || !QODER_SERVICE.running;
  $('btnQSvcUninstall').disabled = !canUse || !installed;
  $('btnQSvcRefresh').disabled = QODER_SVC_BUSY;
}

function setQoderServiceBusy(on) {
  QODER_SVC_BUSY = on;
  const pill = $('qsvcState');
  if (on) {
    pill.textContent = '处理中…';
    pill.className = 'svc-pill busy';
  }
  Object.keys(QODER_SVC_BUTTONS).forEach((key) => {
    if (on) $(QODER_SVC_BUTTONS[key]).disabled = true;
  });
  $('btnQSvcRefresh').disabled = on;
  if (!on) renderQoderService(QODER_SERVICE || {});
}

function resetQoderSvcPending() {
  if (!QODER_SVC_PENDING) return;
  const btn = $(QODER_SVC_BUTTONS[QODER_SVC_PENDING]);
  if (btn) {
    btn.classList.remove('confirm');
    if (btn.dataset.label) {
      btn.textContent = btn.dataset.label;
      delete btn.dataset.label;
    }
  }
  QODER_SVC_PENDING = null;
}

function qoderLabel(action) {
  return {
    install: '安装 / 修复',
    uninstall: '卸载',
    enable: '启用',
    disable: '停用',
    run: '立即运行',
    stop: '停止',
  }[action] || action;
}

function qoderServiceAction(action) {
  if (QODER_SVC_BUSY) return;
  const btn = $(QODER_SVC_BUTTONS[action]);
  const label = qoderLabel(action);

  if ((action === 'install' || action === 'uninstall')
      && QODER_SVC_PENDING !== action) {
    resetQoderSvcPending();
    QODER_SVC_PENDING = action;
    btn.dataset.label = btn.textContent;
    btn.classList.add('confirm');
    btn.textContent = '确认' + label + '？';
    appendQoderLog('待确认：' + label + ' Qoder 计划任务（再次点击按钮执行）', 'warn');
    setTimeout(() => { if (QODER_SVC_PENDING) resetQoderSvcPending(); }, 8000);
    return;
  }

  resetQoderSvcPending();
  setQoderServiceBusy(true);
  appendQoderLog('Qoder 计划任务：' + label + '…', 'cmd');
  call('qoder_service_action', action)
    .then((r) => {
      if (r && r.ok) {
        appendQoderLog('Qoder 计划任务：' + (r.message || (label + '完成')), 'ok');
      } else {
        appendQoderLog('Qoder 计划任务：'
          + ((r && (r.error || r.message)) || '操作失败'), 'err');
      }
      if (r && r.status) renderQoderService(r.status);
      else return refreshQoderService(true);
    })
    .catch((e) => appendQoderLog('Qoder 计划任务调用失败：' + e, 'err'))
    .finally(() => {
      QODER_SVC_BUSY = false;
      refreshQoderService(true).finally(() => setQoderServiceBusy(false));
    });
}

function refreshQoderService(silent) {
  if (QODER_SVC_BUSY || QODER_SVC_REFRESHING) return Promise.resolve();
  QODER_SVC_REFRESHING = true;
  const btn = $('btnQSvcRefresh');
  if (btn) btn.disabled = true;
  return call('qoder_service_status')
    .then((s) => {
      renderQoderService(s);
      if (!silent && s && s.ok === false) {
        appendQoderLog('读取 Qoder 计划任务失败：' + (s.error || '未知错误'), 'err');
      }
    })
    .catch((e) => {
      renderQoderService({ supported: true, ok: false, error: String(e), tasks: [] });
      appendQoderLog('读取 Qoder 计划任务异常：' + e, 'err');
    })
    .finally(() => {
      QODER_SVC_REFRESHING = false;
      if (btn) btn.disabled = QODER_SVC_BUSY;
    });
}

function scheduleQoderServicePoll() {
  if (QODER_SVC_TIMER) clearTimeout(QODER_SVC_TIMER);
  const delay = (QODER_SERVICE && QODER_SERVICE.running) ? 20000 : 180000;
  QODER_SVC_TIMER = setTimeout(() => {
    if (document.hidden || CURRENT_PAGE !== 'qoder') {
      scheduleQoderServicePoll();
      return;
    }
    refreshQoderService(true).finally(scheduleQoderServicePoll);
  }, delay);
}

/* ---------- 存活心跳 ---------- */

// 每 5 秒一次，等上一次 Promise 落地后才排下一次。UI 线程或 WebView2
// 卡住时这个 Promise 不会返回，心跳自然停止；后端看门狗据此判定界面
// 已死并强制退出，避免卡死窗口把整个桌面的窗口切换一起拖住。
let BEAT_STOPPED = false;

function startHeartbeat(api) {
  const tick = async () => {
    if (BEAT_STOPPED) return;
    try { await api.heartbeat(); } catch (e) { /* 看门狗负责收尾 */ }
    if (!BEAT_STOPPED) setTimeout(tick, 5000);
  };
  setTimeout(tick, 3000);
}

window.addEventListener('beforeunload', () => { BEAT_STOPPED = true; });

/* ---------- 启动 ---------- */

async function boot(api) {
  const info = await api.bootstrap();

  // 主题：首屏已由 index.html 的内联脚本上色；这里用创建窗口前探测到的
  // 系统主题校准「跟随系统」模式，并同步三态菜单与按钮状态。
  const initialDark = typeof info.dark === 'boolean'
    ? info.dark : systemPrefersDark();
  applyThemeMode(state.themeMode, { systemDark: initialDark });
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
  if (info.growth && info.growth.data) {
    renderGrowth(info.growth.data, info.growth.at);
    appendLog('已载入上次成长中心数据（' + (info.growth.at || '') + '）', 'meta');
  }

  if (info.history && info.history.length) {
    info.history.forEach((l) => appendLog(l));
  }

  // 动作按钮由后端下发的命令表生成
  state.actions = info.actions || [];
  buildActions(state.actions);
  appendLog('可用命令：' + state.actions.map((a) => a.key).join(' / '), 'meta');
  setBadge('就绪', 'ok');

  // 脚本服务状态独立刷新：计划任务可能被系统或其他工具改动。
  // 空闲时 3 分钟一次，任务运行中 20 秒一次；操作后也会立即刷新。
  // 频次不必太高：每次刷新都要起一次 PowerShell，纯属浪费。
  refreshService(true);
  scheduleServicePoll();

  setPage(savedPage());
  if (!info.qoder_exists) {
    const hint = $('qoderSideHint');
    if (hint) hint.textContent = '缺少 qoder_signin.py';
    qoderSetDot('err');
    appendLog('未找到 qoder_signin.py，Qoder 监控页不可用。', 'warn');
  }

  // 启动即刷新：缓存只是"先有个数看"，真实数据以这次查询为准。
  // doctor 只跑后端、不暴露按钮，用来填充「凭据格式」那张卡。
  if (info.signin_exists) {
    // status/doctor 占用 signin 锁；等它们跑完再拉成长中心，避免启动时
    // 非阻塞加锁失败，在日志里留下一条无意义的“有命令正在执行”。
    runSeries(['status', 'doctor'], () => loadGrowth(true));
  } else {
    setBadge('缺少 signin.py', 'err');
  }

  // 用后端读到的系统主题/标题再校正一次
  try {
    const t = await api.system_theme();
    updateSystemTheme(!!t.dark);
    if (t.accent) applyAccent(t.accent);
    await api.native_title('WorkBuddy 控制台');
  } catch (e) {
    appendLog('主题同步失败（不影响使用）：' + (e && e.message || e), 'warn');
  }
}

document.addEventListener('DOMContentLoaded', function () {
  // 首屏内联脚本已经上色，这里只同步菜单选中项
  applyThemeMode(state.themeMode);

  $('btnClear').addEventListener('click', clearLog);
  $('btnCopy').addEventListener('click', copyLog);
  $('themeToggle').addEventListener('click', toggleThemeMenu);
  $('themeMenu').addEventListener('click', (e) => {
    const option = e.target.closest('[data-theme-mode]');
    if (option) chooseThemeMode(option.dataset.themeMode);
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#themePicker')) setThemeMenu(false);
  });
  document.addEventListener('keydown', (e) => {
    const menu = $('themeMenu');
    if (e.key !== 'Escape' || !menu || menu.hidden) return;
    setThemeMenu(false);
    $('themeToggle').focus();
  });
  $('btnGrowth').addEventListener('click', () => loadGrowth(false));
  $('btnGrowthRefresh').addEventListener('click', () => loadGrowth(false));
  $('btnStatusRefresh').addEventListener('click', refreshStatus);
  $('btnCalRefresh').addEventListener('click', refreshStatus);

  $('btnSvcRefresh').addEventListener('click', () => refreshService(false));
  Object.keys(SVC_BUTTONS).forEach((action) => {
    $(SVC_BUTTONS[action]).addEventListener('click', () => serviceAction(action));
  });

  document.querySelectorAll('.side-tab').forEach((tab) => {
    tab.addEventListener('click', () => setPage(tab.dataset.page));
  });

  $('btnQoderRefresh').addEventListener('click', () => refreshQoder(false));
  $('btnQoderClaim').addEventListener('click', qoderClaim);
  $('btnQoderLogClear').addEventListener('click', () => {
    $('qoderLog').innerHTML = '';
    $('qoderLogAt').textContent = '显示已清空';
  });
  $('btnQSvcRefresh').addEventListener('click', () => refreshQoderService(false));
  Object.keys(QODER_SVC_BUTTONS).forEach((action) => {
    $(QODER_SVC_BUTTONS[action]).addEventListener('click', () => qoderServiceAction(action));
  });

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
    startHeartbeat(window.pywebview.api);
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