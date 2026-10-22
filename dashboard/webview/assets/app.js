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
let TRAE_LOADED = false;
let TRAE_LOADING = false;
let WB_STATUS = null;
let WB_DOCTOR = null;
let WB_ACCOUNT_PENDING = null;

function savedPage() {
  try {
    const saved = localStorage.getItem(PAGE_KEY);
    if (saved === 'qoder' || saved === 'trae') return saved;
  } catch (e) { /* 忽略 */ }
  return 'workbuddy';
}

function setPage(page) {
  CURRENT_PAGE = (page === 'qoder' || page === 'trae') ? page : 'workbuddy';
  document.querySelectorAll('.side-tab').forEach((tab) => {
    const active = tab.dataset.page === CURRENT_PAGE;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-current', active ? 'page' : 'false');
  });
  const wb = $('page-workbuddy');
  const qoder = $('page-qoder');
  const trae = $('page-trae');
  if (wb) wb.hidden = CURRENT_PAGE !== 'workbuddy';
  if (qoder) qoder.hidden = CURRENT_PAGE !== 'qoder';
  if (trae) trae.hidden = CURRENT_PAGE !== 'trae';
  try { localStorage.setItem(PAGE_KEY, CURRENT_PAGE); } catch (e) { /* 忽略 */ }
  if (CURRENT_PAGE === 'qoder') ensureQoder();
  else if (CURRENT_PAGE === 'trae') ensureTrae();
  else lazyKick();   // 切回监控页时，未加载区块按新的可见性重新判定
}

function ensureQoder() {
  if (QODER_LOADED || QODER_LOADING) return;
  refreshQoder(false);
  refreshQoderService(true);
  scheduleQoderServicePoll();
}

function ensureTrae() {
  if (TRAE_LOADED || TRAE_LOADING) return;
  refreshTrae(false);
  refreshTraeService(true);
  scheduleTraeServicePoll();
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
  WB_STATUS = d;
  renderWorkbuddyAccount();
  wbSideState();
}

function renderFromDoctor(raw) {
  const d = unwrap(raw);
  if (!d) return;
  WB_DOCTOR = d;
  const fmt = d.credential_format;
  setCard('vCred', 'hCred',
    fmt === 'plaintext' ? '明文' : fmt === 'sym-v1' ? 'sym-v1 加密' : (fmt || '—'),
    d.electron_version ? 'Electron ' + d.electron_version
      : (d.runtime ? '运行时已就绪' : (d.report || '')));
  renderWorkbuddyAccount();
  wbSideState();
}

function wbCredentialLabel(fmt) {
  return fmt === 'plaintext' ? '明文' : fmt === 'sym-v1' ? 'sym-v1 加密'
    : (fmt || '—');
}

function wbSetDot(kind) {
  const dot = $('wbDot');
  if (!dot) return;
  dot.className = 'side-dot' + (kind ? ' ' + kind : '');
}

/** 侧边栏 WorkBuddy 状态：绿点表示登录态，文字只表示今日签到结果。 */
function wbSideState() {
  const hint = $('wbSideHint');
  if (!hint) return;

  const doctor = WB_DOCTOR;
  const status = WB_STATUS;
  let text = '未检测';
  let kind = '';
  let title = '';

  if (doctor) {
    if (doctor.result === 'AUTH_READY') {
      kind = 'ok';
      title = 'WorkBuddy 登录态已就绪';
    } else if (doctor.result === 'NO_AUTH') {
      kind = 'warn';
      title = '未找到 WorkBuddy 登录态';
    } else if (doctor.result === 'AUTH_REJECTED') {
      kind = 'err';
      title = 'WorkBuddy 登录态已失效，请重新登录';
    } else {
      kind = 'err';
      title = doctor.report || doctor.result || 'WorkBuddy 登录态异常';
    }
  } else if (status) {
    if (status.result === 'AUTH_REJECTED') {
      kind = 'err';
      title = 'WorkBuddy 登录态已失效，请重新登录';
    } else if (status.result === 'NO_AUTH') {
      kind = 'warn';
      title = '未找到 WorkBuddy 登录态';
    } else {
      kind = 'ok';
      title = status.report || '签到状态已更新';
    }
  }

  if (status) {
    const checked = status.today_checked_in === true
      || status.result === 'ALREADY' || status.result === 'CLAIMED';
    text = checked ? '已签到' : '未签到';
  } else if (doctor) {
    // 登录态已读出但还没查询签到：先按未签到展示，查询完成后自动更正。
    text = '未签到';
  }

  hint.textContent = text;
  hint.title = title;
  wbSetDot(kind);
}

function renderWorkbuddyAccount() {
  const box = $('wbAccounts');
  if (!box) return;
  const doctor = WB_DOCTOR;
  const status = WB_STATUS;
  box.innerHTML = '';

  if (!doctor && !status) {
    traeEmpty(box, '正在读取 WorkBuddy 本机登录态…');
    const at = $('wbAccountAt');
    if (at) at.textContent = '尚未刷新';
    return;
  }

  const account = (doctor && doctor.account) || {};
  const fmt = (doctor && doctor.credential_format) || account.credential_format || '';
  const subParts = [];
  if (account.uin_masked) subParts.push('UIN ' + account.uin_masked);
  if (account.uid_masked) subParts.push('UID ' + account.uid_masked);
  if (account.domain) subParts.push(account.domain);

  let stateText = '待检测';
  let stateKind = 'warn';
  if (doctor) {
    if (doctor.result === 'AUTH_READY') {
      stateText = '已登录';
      stateKind = 'ok';
    } else if (doctor.result === 'NO_AUTH') {
      stateText = '未登录';
    } else if (doctor.result === 'AUTH_REJECTED') {
      stateText = '需重新登录';
      stateKind = 'err';
    } else {
      stateText = doctor.result || '登录态异常';
      stateKind = 'err';
    }
  } else if (status && status.result) {
    stateText = status.result === 'OK' || status.result === 'ALREADY'
      || status.result === 'CLAIMED' ? '已连接' : status.result;
    stateKind = stateText === '已连接' ? 'ok' : 'warn';
  }
  if (status && status.result === 'AUTH_REJECTED') {
    stateText = '需重新登录';
    stateKind = 'err';
  }

  const card = document.createElement('article');
  card.className = 'account-card' + (stateKind === 'ok' ? ' active' : '');

  const head = document.createElement('div');
  head.className = 'ta-head';
  const identity = document.createElement('div');
  identity.className = 'ta-identity';
  const name = document.createElement('div');
  name.className = 'ta-name';
  name.textContent = 'WorkBuddy 本机账号';
  name.title = name.textContent;
  const sub = document.createElement('div');
  sub.className = 'ta-sub';
  sub.textContent = subParts.join(' · ') || '等待读取账号摘要';
  sub.title = sub.textContent;
  identity.append(name, sub);
  head.append(accountAvatarElement('', 'WorkBuddy'), identity,
    accountStateElement(stateText, stateKind, doctor && doctor.report));
  card.appendChild(head);

  const credit = status ? firstNum(status.credit, status.today_credit,
    status.daily_credit) : null;
  const streak = status ? firstNum(status.streak_days) : null;
  const checked = !!(status && (status.today_checked_in === true
    || status.result === 'ALREADY' || status.result === 'CLAIMED'));

  const badges = document.createElement('div');
  badges.className = 'ta-badges';
  badges.appendChild(accountRegionBadge(Object.assign({ region: 'CN' }, account)));
  badges.appendChild(traeBadge('WorkBuddy', 'member'));
  if (fmt) badges.appendChild(traeBadge(wbCredentialLabel(fmt), ''));
  card.appendChild(badges);

  const metrics = document.createElement('div');
  metrics.className = 'ta-metrics';
  metrics.appendChild(accountMetricElement('今日积分', credit,
    credit == null ? '尚未查询签到状态' : (checked ? '今日已签到' : '今日未领取')));
  metrics.appendChild(accountMetricElement('连签天数', streak,
    streak == null ? '尚未查询签到状态' : '连续签到'));
  metrics.appendChild(accountMetricElement('凭据', wbCredentialLabel(fmt),
    doctor && doctor.report ? doctor.report : '本地凭据格式', 'narrow'));
  card.appendChild(metrics);

  const meta = document.createElement('div');
  meta.className = 'ta-meta';
  const endpoint = account.endpoint || '';
  meta.textContent = '来源 本机 WorkBuddy 登录态'
    + (endpoint ? ' · ' + endpoint : '');
  meta.title = endpoint;
  card.appendChild(meta);

  const actions = document.createElement('div');
  actions.className = 'ta-actions';
  actions.appendChild(accountActionButton('签到', '领取今日 WorkBuddy 签到积分（真实写操作，需二次确认）',
    (e) => wbAccountClaim(e.currentTarget)));
  actions.appendChild(accountActionButton('刷新', '重新读取 WorkBuddy 本机登录态与签到状态',
    refreshWorkbuddyAccount));
  actions.appendChild(accountActionButton('查询签到', '只重新查询 WorkBuddy 签到状态',
    refreshStatus));
  card.appendChild(actions);

  box.appendChild(card);
  const at = $('wbAccountAt');
  if (at) {
    at.textContent = '最近读取 '
      + new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }
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

// status/doctor 与 growth 在后端共用 signin 锁：两者同时可见时，先等签到
// 查询跑完再拉成长中心，否则非阻塞加锁会失败，日志里多一条无意义的报错。
let CHECKIN_STARTED = false;
let CHECKIN_DONE = false;
let GROWTH_WANTED = false;

function maybeLoadGrowth() {
  if (!GROWTH_WANTED) return;
  if (CHECKIN_STARTED && !CHECKIN_DONE) return;
  loadGrowth(true);
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

  } else if (kind === 'trae_log') {
    appendTraeLog(data);

  } else if (kind === 'trae_done') {
    onTraeDone(data);

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
const PANE_REFRESH_BUTTONS = ['btnGrowthRefresh', 'btnStatusRefresh', 'btnCalRefresh', 'btnWbAccountRefresh'];

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
  if (on) wbSetDot('busy');
  else wbSideState();
  setBadge(on ? '执行中…' : '就绪', on ? 'busy' : 'ok');
}

/** 标题栏刷新：重新查询签到状态（同时更新签到状态与本月签到两栏）。 */
function refreshStatus() {
  if (BUSY) return;
  run('status');
}

/** WorkBuddy 账号刷新：先重读登录态，再查签到状态，卡片与指标一起更新。 */
function refreshWorkbuddyAccount() {
  if (BUSY) return;
  runSeries(['doctor', 'status']);
}

function resetWbAccountPending() {
  if (!WB_ACCOUNT_PENDING) return;
  const btn = WB_ACCOUNT_PENDING;
  btn.classList.remove('confirm');
  btn.textContent = '签到';
  WB_ACCOUNT_PENDING = null;
}

/** 账号卡片签到：claim 为真实写操作，沿用二次确认；成功后补一次 status。 */
function wbAccountClaim(btn) {
  if (!btn || BUSY) return;
  if (WB_ACCOUNT_PENDING !== btn) {
    resetWbAccountPending();
    WB_ACCOUNT_PENDING = btn;
    btn.classList.add('confirm');
    btn.textContent = '确认签到？';
    appendLog('待确认：再次点击「确认签到」会真实领取今日 WorkBuddy 积分', 'warn');
    setTimeout(() => {
      if (WB_ACCOUNT_PENDING === btn) resetWbAccountPending();
    }, 8000);
    return;
  }
  resetWbAccountPending();
  appendLog('已确认执行：领取今日 WorkBuddy 签到积分', 'warn');
  runSeries(['claim', 'status']);
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
    // 页面不可见、或停在 Qoder 页时不做无用查询：每次刷新都要起 PowerShell
    if (document.hidden || CURRENT_PAGE !== 'workbuddy') {
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
let QODER_PENDING = null;
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

function renderQoderAccount(status, doctor) {
  const box = $('qoderAccounts');
  if (!box) return;
  const at = $('qoderAccountAt');
  box.innerHTML = '';

  if (!status && !doctor) {
    traeEmpty(box, QODER_DATA && QODER_DATA.error
      ? 'Qoder 账号不可用：' + QODER_DATA.error
      : '打开此页或点击「刷新监控」后显示 Qoder CN 账号');
    if (at) at.textContent = '尚未刷新';
    return;
  }

  const account = (doctor && doctor.account) || {};
  const isEnv = !!(doctor && doctor.token_source === 'env');
  const display = account.name
    || (isEnv ? 'Qoder CN 环境变量' : 'Qoder CN 本机账号');
  const subParts = [];
  if (account.id) subParts.push('UID ' + account.id);
  if (account.email_masked) subParts.push(account.email_masked);
  else if (account.phone_masked) subParts.push(account.phone_masked);
  if (isEnv) subParts.push('QODER_TOKEN');
  else if (doctor) subParts.push('桌面端登录态');

  let stateText = '待检测';
  let stateKind = 'warn';
  if (doctor) {
    if (doctor.result === 'OK') {
      stateText = '已连接';
      stateKind = 'ok';
    } else if (doctor.result === 'NO_AUTH') {
      stateText = '未登录';
    } else if (doctor.result === 'AUTH_REJECTED') {
      stateText = '需重新登录';
      stateKind = 'err';
    } else {
      stateText = QODER_RESULT_LABEL[doctor.result] || doctor.result || '登录态异常';
      stateKind = 'err';
    }
  } else if (status && status.result === 'OK') {
    stateText = '已连接';
    stateKind = 'ok';
  }
  if (status && status.result === 'AUTH_REJECTED') {
    stateText = '需重新登录';
    stateKind = 'err';
  }

  const card = document.createElement('article');
  card.className = 'account-card' + (stateKind === 'ok' ? ' active' : '');

  const head = document.createElement('div');
  head.className = 'ta-head';
  const identity = document.createElement('div');
  identity.className = 'ta-identity';
  const name = document.createElement('div');
  name.className = 'ta-name';
  name.textContent = display;
  name.title = display;
  const sub = document.createElement('div');
  sub.className = 'ta-sub';
  sub.textContent = subParts.join(' · ') || 'Qoder CN 本机账号';
  sub.title = sub.textContent;
  identity.append(name, sub);
  head.append(accountAvatarElement(account.avatar_url, display), identity,
    accountStateElement(stateText, stateKind, doctor && doctor.report));
  card.appendChild(head);

  const campaigns = (status && status.campaigns) || [];
  const todo = campaigns.filter((c) => c.active && c.status === 'CLAIMABLE');
  const claimed = campaigns.filter((c) => c.active && c.status === 'CLAIMED');
  const credits = todo.length ? qoderSum(todo, 'credits')
    : (claimed.length ? qoderSum(claimed, 'credits') : null);
  let creditsHint = status ? '当前没有可领取活动' : '尚未查询活动';
  if (todo.length) creditsHint = '本周期可领取 · ' + todo.length + ' 项';
  else if (claimed.length) creditsHint = '本周期已领取 · ' + claimed.length + ' 项';
  else if (status && status.report) creditsHint = status.report;

  const badges = document.createElement('div');
  badges.className = 'ta-badges';
  badges.appendChild(accountRegionBadge(doctor || { region: 'CN' }));
  badges.appendChild(traeBadge(isEnv ? '环境变量' : 'Qoder CN',
    isEnv ? 'warn' : 'member'));
  card.appendChild(badges);

  const metrics = document.createElement('div');
  metrics.className = 'ta-metrics';
  metrics.appendChild(accountMetricElement('可领取 Credits', credits, creditsHint));
  metrics.appendChild(accountMetricElement('令牌到期',
    doctor && doctor.token_expires_at
      ? qoderExpiry(doctor.token_expires_at).split(' ')[0] : '未记录',
    doctor && doctor.token_expires_at
      ? '到期 ' + qoderExpiry(doctor.token_expires_at) : '未读取到令牌到期时间',
    'narrow'));
  metrics.appendChild(accountMetricElement('设备',
    doctor ? (doctor.device_identity ? '已获取' : '未获取') : '—',
    doctor && doctor.device_component
      ? String(doctor.device_component).split(/[\\/]/).pop() : '设备组件未找到',
    'narrow'));
  card.appendChild(metrics);

  const meta = document.createElement('div');
  meta.className = 'ta-meta';
  const source = isEnv ? '环境变量 QODER_TOKEN'
    : (doctor && doctor.auth_dir
      ? String(doctor.auth_dir).split(/[\\/]/).filter(Boolean).pop()
      : '桌面端登录态');
  meta.textContent = '来源 ' + source
    + (QODER_DATA && QODER_DATA.at ? ' · 最近检测 ' + QODER_DATA.at : '');
  meta.title = (doctor && doctor.auth_dir) || source;
  card.appendChild(meta);

  const actions = document.createElement('div');
  actions.className = 'ta-actions';
  actions.appendChild(accountActionButton('签到', '领取今日 Qoder Credits（真实写操作，需二次确认）',
    qoderClaim, '', QODER_BUSY));
  actions.appendChild(accountActionButton('刷新', '重新查询 Qoder 账号与活动',
    () => refreshQoder(false), '', QODER_BUSY));
  card.appendChild(actions);

  box.appendChild(card);
  if (at) at.textContent = QODER_DATA && QODER_DATA.at
    ? '最近检测 ' + QODER_DATA.at : '尚未刷新';
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
  let kind = '';
  let title = '';

  if (healthy) {
    kind = 'ok';
  } else if (doctor && doctor.result === 'OK') {
    kind = 'ok';
    title = doctor.report || '登录态可读';
  } else if (doctor && doctor.result === 'NO_AUTH') {
    kind = 'warn';
    title = '未登录 Qoder CN';
  } else if (doctor && doctor.result === 'AUTH_REJECTED') {
    kind = 'err';
    title = 'Qoder CN 登录态已失效，请重新登录';
  } else if (status && status.result) {
    kind = status.result === 'OK' ? 'ok' : 'warn';
    title = status.report || QODER_RESULT_LABEL[status.result] || status.result;
  }

  // 文字只表示本周期领取结果；绿点单独表示登录 / 连接状态。
  let text = '未检测';
  const campaigns = (status && status.campaigns) || [];
  const todo = campaigns.filter((c) => c.active && c.status === 'CLAIMABLE');
  const claimed = campaigns.filter((c) => c.active && c.status === 'CLAIMED');
  if (status) {
    if (todo.length) {
      text = '未领取';
      title = '可领取：' + todo.map((c) => c.campaign || '?').join('、');
    } else if (claimed.length) {
      text = '已领取';
      title = '本周期已领取：' + claimed.map((c) => c.campaign || '?').join('、');
    } else if (status.result === 'OK') {
      text = '已领取';
      title = status.report || '当前没有可领取活动';
    } else {
      text = '未领取';
      title = title || QODER_RESULT_LABEL[status.result] || status.result;
    }
  } else if (doctor) {
    text = '未领取';
  }

  hint.textContent = text;
  hint.title = title;
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
    renderQoderAccount(null, null);
    renderQoderCampaigns(null);
    renderQoderLog(QODER_DATA.log);
    const hint = $('qoderSideHint');
    if (hint) {
      hint.textContent = '未领取';
      hint.title = QODER_DATA.busy
        ? '领取正在执行'
        : (QODER_DATA.error || 'Qoder 监控不可用');
    }
    qoderSetDot(QODER_DATA.busy ? 'busy' : 'err');
    if (!QODER_DATA.busy) {
      appendQoderLog('Qoder 监控不可用：' + (QODER_DATA.error || '未知错误'), 'err');
    }
    return;
  }

  renderQoderCards(status, doctor);
  renderQoderAccount(status, doctor);
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
  const btns = [$('btnQoderRefresh'), $('btnQoderAccountRefresh')];
  btns.forEach((b) => { if (b) b.disabled = true; });
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
      btns.forEach((b) => { if (b) b.disabled = false; });
    });
}

function qoderResetClaim() {
  if (!QODER_PENDING) return;
  const { btn, label } = QODER_PENDING;
  if (btn) {
    btn.classList.remove('confirm');
    btn.textContent = label;
  }
  QODER_PENDING = null;
}

function qoderSetClaimBusy(on) {
  QODER_BUSY = on;
  const top = $('btnQoderClaim');
  if (top) top.disabled = on;
  document.querySelectorAll('#qoderAccounts .mini-btn').forEach((b) => {
    b.disabled = on;
  });
}

function qoderClaim(eventOrBtn) {
  if (QODER_BUSY) return;
  let btn = eventOrBtn;
  if (btn && btn.currentTarget) btn = btn.currentTarget;   // 顶栏按钮的事件对象
  if (!btn) btn = $('btnQoderClaim');
  if (!btn) return;
  const label = btn.id === 'btnQoderClaim' ? '领取今日 Credits' : '签到';
  const confirming = btn.id === 'btnQoderClaim';
  const confirmText = confirming ? '确认领取？点此执行' : '确认签到？点此执行';
  const confirmLog = confirming
    ? '待确认：再次点击「确认领取」会真实调用 Qoder 领取接口'
    : '待确认：再次点击「确认签到」会真实调用 Qoder 领取接口';

  if (!QODER_PENDING || QODER_PENDING.btn !== btn) {
    qoderResetClaim();
    QODER_PENDING = { btn, label };
    btn.classList.add('confirm');
    btn.textContent = confirmText;
    appendQoderLog(confirmLog, 'warn');
    setTimeout(qoderResetClaim, 8000);
    return;
  }

  qoderResetClaim();
  qoderSetClaimBusy(true);
  qoderSetDot('busy');
  appendQoderLog('正在领取 Qoder 今日 Credits…', 'cmd');
  call('qoder_claim')
    .then((r) => {
      if (r && !r.ok) {
        qoderSetClaimBusy(false);
        appendQoderLog('领取未启动：' + (r.error || '未知错误'), 'err');
        refreshQoder(true);
      }
    })
    .catch((e) => {
      qoderSetClaimBusy(false);
      appendQoderLog('领取调用失败：' + e, 'err');
    });
}

function onQoderDone(data) {
  qoderResetClaim();
  qoderSetClaimBusy(false);
  const btn = $('btnQoderClaim');
  if (btn) {
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

/* ---------- Trae 多账号监控 ---------- */

const TRAE_RESULT_LABEL = {
  OK: '已连接',
  ALREADY: '今日已签到',
  CLAIMED: '签到成功',
  PARTIAL: '部分成功',
  NO_ACCOUNT: '没有账号',
  NO_AUTH: '登录态失效',
  NO_CLIENT: '未找到客户端',
  NO_CHANGE: '没有变化',
  AUTH_ERROR: '登录态异常',
  ERROR: '失败',
};

const TRAE_SVC_BUTTONS = {
  install: 'btnTSvcInstall',
  uninstall: 'btnTSvcUninstall',
  enable: 'btnTSvcEnable',
  disable: 'btnTSvcDisable',
  run: 'btnTSvcRun',
  stop: 'btnTSvcStop',
};

const TRAE_SVC_ACTION_LABEL = {
  install: '安装 / 修复',
  uninstall: '卸载',
  enable: '启用',
  disable: '停用',
  run: '立即运行',
  stop: '停止',
};

const TRAE_SVC_CONFIRM = { install: true, uninstall: true };
const TRAE_OFFSETS = [0, 2, 5, 9, 13];

let TRAE_DATA = null;
let TRAE_SERVICE = null;
let TRAE_BUSY = false;
let TRAE_BUSY_LABEL = '';
let TRAE_PENDING = false;
let TRAE_EDIT_ID = null;
let TRAE_ACCOUNT_PENDING = null;
let TRAE_SVC_BUSY = false;
let TRAE_SVC_PENDING = null;
let TRAE_SVC_REFRESHING = false;
let TRAE_SVC_TIMER = null;
let TRAE_SVC_SEQ = 0;
let TRAE_USAGE_DAYS_SET = false;
let TRAE_EXPIRING = [];
let TRAE_EXPIRING_ACCOUNT = null;

function traeNum(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function traeFmt(value, digits) {
  const n = traeNum(value);
  if (n === null) return '—';
  const places = typeof digits === 'number' ? digits : 2;
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: places,
  });
}

function traeLocalDate() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

function traeTimeText(ms) {
  const n = traeNum(ms);
  if (n === null || n <= 0) return '—';
  const d = new Date(n);
  if (Number.isNaN(d.getTime())) return String(ms);
  return d.toLocaleString('zh-CN', { hour12: false });
}

function traeSetDot(kind) {
  const dot = $('traeDot');
  if (!dot) return;
  dot.className = 'side-dot' + (kind ? ' ' + kind : '');
}

function traeActive(data) {
  const source = data || TRAE_DATA || {};
  const accounts = source.accounts || [];
  return accounts.find((a) => a.id === source.active_id) || accounts[0] || null;
}

function traeAccountById(id) {
  const accounts = (TRAE_DATA && TRAE_DATA.accounts) || [];
  return accounts.find((a) => a.id === id) || null;
}

function traeToday() {
  return (TRAE_DATA && TRAE_DATA.history && TRAE_DATA.history.today)
    || traeLocalDate();
}

function traeIsChecked(account, today) {
  if (!account) return false;
  if (account.last_checkin === today) return true;
  const status = account.last_status || {};
  return status.checked_in === true
    && String(status.at || '').slice(0, 10) === today;
}

function traeAccountGain(account, today) {
  if (!account) return 0;
  if (TRAE_DATA && TRAE_DATA.active_id === account.id
      && TRAE_DATA.history
      && TRAE_DATA.history.today === today) {
    return traeNum(TRAE_DATA.history.today_credits) || 0;
  }
  const status = account.last_status || {};
  if (!status.checked_in) return 0;
  let gained = traeNum(status.credits) || 0;
  if (account.is_member) gained += traeNum(status.extra_credits) || 0;
  return gained;
}

function traeSourceLabel(source) {
  if (!source) return '手动添加';
  if (/solo/i.test(source)) return 'TRAE SOLO 客户端';
  if (/trae cn/i.test(source)) return 'Trae CN 客户端';
  if (/trae/i.test(source)) return 'Trae 客户端';
  return source;
}

function traeResultLabel(result) {
  return TRAE_RESULT_LABEL[result] || result || '完成';
}

function traeEmpty(box, text) {
  if (!box) return;
  box.innerHTML = '';
  const p = document.createElement('p');
  p.className = 'empty';
  p.textContent = text;
  box.appendChild(p);
}

function traeBadge(text, kind) {
  const el = document.createElement('span');
  el.className = 'ta-badge' + (kind ? ' ' + kind : '');
  el.textContent = text;
  return el;
}

function accountInitial(value) {
  const text = String(value == null ? '' : value).trim();
  return text ? Array.from(text)[0].toUpperCase() : 'A';
}

function accountAvatarElement(url, display) {
  const el = document.createElement('span');
  el.className = 'ta-avatar';
  const initial = accountInitial(display);
  if (url) {
    const img = document.createElement('img');
    img.alt = '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    img.src = url;
    img.addEventListener('error', () => {
      img.remove();
      el.textContent = initial;
    });
    el.appendChild(img);
  } else {
    el.textContent = initial;
  }
  return el;
}

function accountStateElement(text, kind, title) {
  const el = document.createElement('span');
  el.className = 'ta-state' + (kind ? ' ' + kind : '');
  el.textContent = text;
  el.title = title || '';
  return el;
}

function accountMetricElement(label, value, hint, cls) {
  const item = document.createElement('div');
  item.className = 'ta-metric' + (cls ? ' ' + cls : '');
  const key = document.createElement('span');
  key.textContent = label;
  const val = document.createElement('b');
  val.textContent = value == null || value === '' ? '—' : String(value);
  val.title = hint || '';
  item.append(key, val);
  return item;
}

function accountRegionInfo(account) {
  const region = String((account && account.region) || '').toUpperCase();
  // 旧缓存可能只有 label / 没有 region；默认按国内展示，避免空区域被
  // 误判成国际后出现「橙色国内」这种自相矛盾的徽标。
  const intl = !!region && region !== 'CN';
  const kind = (account && account.region_kind) || (intl ? 'intl' : 'cn');
  const label = (account && account.region_label) || (intl ? '国际' : '国内');
  return { region, kind, label, host: (account && account.host) || '' };
}

function accountRegionBadge(account) {
  const info = accountRegionInfo(account);
  const el = traeBadge(info.label, 'region-' + info.kind);
  el.title = info.host ? info.label + ' · ' + info.host : info.label;
  return el;
}

function accountActionButton(label, title, onClick, extraClass, disabled) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'mini-btn' + (extraClass ? ' ' + extraClass : '');
  btn.textContent = label;
  btn.title = title || label;
  btn.disabled = !!disabled;
  if (typeof onClick === 'function' && typeof btn.addEventListener === 'function') {
    btn.addEventListener('click', onClick);
  }
  return btn;
}

function traeSideState() {
  const hint = $('traeSideHint');
  if (!hint) return;

  // 绿点只表示登录 / 连接状态；文字统一为今日签到结果。
  let text = '未检测';
  let kind = '';
  let title = '';

  if (TRAE_DATA && TRAE_DATA.ok === false) {
    text = '未签到';
    kind = 'err';
    title = TRAE_DATA.error || 'Trae 监控不可用';
  } else if (TRAE_DATA) {
    const accounts = TRAE_DATA.accounts || [];
    const enabled = accounts.filter((a) => a.enabled);
    if (!accounts.length) {
      text = '未签到';
      kind = 'warn';
      title = '未添加 Trae 账号';
    } else if (!enabled.length) {
      text = '未签到';
      kind = 'warn';
      title = 'Trae 账号均已停用';
    } else {
      const today = traeToday();
      const checked = enabled.filter((a) => traeIsChecked(a, today)).length;
      const broken = enabled.filter((a) => a.token_expired && !a.has_session);
      text = checked >= enabled.length ? '已签到' : '未签到';
      title = '今日已签 ' + checked + '/' + enabled.length;
      if (broken.length === enabled.length) {
        kind = 'err';
        title = '登录态已过期 · ' + title;
      } else if (broken.length) {
        kind = 'warn';
        title = broken.length + ' 个账号登录态已过期 · ' + title;
      } else {
        kind = 'ok';
      }
    }
  }

  if (TRAE_BUSY) kind = 'busy';
  hint.textContent = text;
  hint.title = title;
  traeSetDot(kind);
}

function renderTraeCards(data) {
  const active = traeActive(data);
  const history = data.history || null;
  const accounts = data.accounts || [];
  const enabled = accounts.filter((a) => a.enabled);
  const today = (history && history.today) || traeToday();
  const checked = enabled.filter((a) => traeIsChecked(a, today)).length;

  if (!active) {
    setCard('tToday', 'tTodayHint', '—', '添加账号后显示');
    setCard('tRemaining', 'tRemainingHint', '—', '添加账号后显示');
  } else {
    const todayCredits = history ? history.today_credits : 0;
    setCard('tToday', 'tTodayHint', traeFmt(todayCredits),
      traeIsChecked(active, today) ? '主号今日已签到' : '主号今日未签到');
    setCard('tRemaining', 'tRemainingHint', traeFmt(active.remaining),
      active.display + (accounts.length > 1 ? ' · 共 ' + accounts.length + ' 个账号' : ''));
  }

  setCard('tAccounts', 'tAccountsHint', enabled.length,
    '共 ' + accounts.length + ' 个账号 · 今日已签 ' + checked + ' 个');
}

function renderTraeAccounts(data) {
  const box = $('traeAccounts');
  if (!box) return;
  const accounts = data.accounts || [];
  box.innerHTML = '';

  if (!accounts.length) {
    const clients = data.clients || [];
    if (clients.length) {
      const names = clients.map((c) => c.name).join('、');
      traeEmpty(box, '检测到本机客户端（' + names + '），但尚未导入登录态。'
        + '点击「导入本机登录态」，或「添加账号」粘贴 Token / 会话 Cookie。');
    } else {
      traeEmpty(box, '还没有 Trae 账号。安装并登录 Trae 桌面端后可导入本机登录态，'
        + '也可以直接粘贴 Cloud-IDE-JWT Token 或 X-Cloudide-Session。');
    }
    return;
  }

  const today = traeToday();
  accounts.forEach((account) => {
    const checked = traeIsChecked(account, today);
    const status = account.last_status || {};
    const card = document.createElement('article');
    card.className = 'account-card trae-account'
      + (account.active ? ' active' : '')
      + (account.enabled ? '' : ' disabled');

    const head = document.createElement('div');
    head.className = 'ta-head';

    const display = account.display || account.name || 'Trae';
    const avatar = accountAvatarElement(account.avatar_url, display);

    const identity = document.createElement('div');
    identity.className = 'ta-identity';
    const name = document.createElement('div');
    name.className = 'ta-name';
    name.textContent = display;
    name.title = display;
    const sub = document.createElement('div');
    sub.className = 'ta-sub';
    sub.textContent = traeSourceLabel(account.source)
      + (account.mobile_masked ? ' · ' + account.mobile_masked : '');
    identity.append(name, sub);

    let stateText = checked ? '今日已签' : '待签到';
    let stateKind = checked ? 'ok' : 'warn';
    if (!account.enabled) {
      stateText = '已停用';
      stateKind = 'off';
    } else if (status.code !== undefined && status.code !== null && status.code !== 0
               && status.code !== '' && Number(status.code) !== 0) {
      stateText = '登录态异常';
      stateKind = 'err';
    }
    const state = accountStateElement(stateText, stateKind, status.message || '');

    head.append(avatar, identity, state);
    card.appendChild(head);

    const badges = document.createElement('div');
    badges.className = 'ta-badges';
    badges.appendChild(accountRegionBadge(account));
    if (account.active) badges.appendChild(traeBadge('主号', 'main'));
    if (account.is_member) badges.appendChild(traeBadge('会员', 'member'));
    if (account.is_student) badges.appendChild(traeBadge('学生', 'student'));
    if (account.token_expired) {
      badges.appendChild(traeBadge(account.has_session ? 'Token 可续期' : '需重新登录',
        account.has_session ? 'warn' : 'err'));
    }
    if (badges.childElementCount) card.appendChild(badges);

    const metrics = document.createElement('div');
    metrics.className = 'ta-metrics';
    metrics.appendChild(accountMetricElement('剩余积分', traeFmt(account.remaining),
      account.remaining == null ? '尚未刷新剩余积分' : '最近一次查询结果'));
    metrics.appendChild(accountMetricElement('今日获得', '+' + traeFmt(traeAccountGain(account, today)),
      checked ? '来自今日签到记录' : '今日尚未签到'));
    metrics.appendChild(accountMetricElement('Token 到期',
      account.token_expiry ? String(account.token_expiry).slice(0, 10) : '未记录',
      account.token_expiry || 'Token 中没有到期时间', 'narrow'));
    card.appendChild(metrics);

    const meta = document.createElement('div');
    meta.className = 'ta-meta';
    const metaParts = [
      '设备 ' + (account.device_id_masked || '—'),
      '最近签到 ' + (account.last_checkin || '—'),
    ];
    meta.textContent = metaParts.join(' · ');
    card.appendChild(meta);

    const actions = document.createElement('div');
    actions.className = 'ta-actions';
    const addAction = (action, label, title, extraClass, disabled) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'mini-btn' + (extraClass ? ' ' + extraClass : '');
      btn.dataset.traeAction = action;
      btn.dataset.traeId = account.id;
      const armed = TRAE_ACCOUNT_PENDING
        && TRAE_ACCOUNT_PENDING.action === action
        && TRAE_ACCOUNT_PENDING.id === account.id;
      btn.textContent = armed
        ? (action === 'remove' ? '确认删除？'
          : action === 'renew_device' ? '确认换设备？' : '确认签到？')
        : label;
      if (armed) btn.classList.add('confirm');
      btn.title = title || label;
      btn.disabled = !!disabled || TRAE_BUSY;
      actions.appendChild(btn);
      return btn;
    };

    addAction('checkin', checked ? '已签到' : '签到',
      checked ? '今天已经签到' : '立即签到这个账号',
      '', !account.enabled || checked || !account.has_token && !account.has_session);
    addAction('refresh', '刷新', '查询状态、资料和剩余积分');
    if (!account.active) addAction('activate', '设为主号', '切换主号，用于日历与用量统计');
    addAction('edit', '编辑', '修改备注名、会员标记或启用状态');
    addAction('renew_device', '换设备', '重新生成 16 位数字设备号，可能影响服务端风控');
    addAction('toggle', account.enabled ? '停用' : '启用',
      account.enabled ? '保留账号但不再自动签到' : '恢复自动签到');
    addAction('remove', '删除', '从本机删除这个账号（需要二次确认）', 'danger');

    card.appendChild(actions);
    box.appendChild(card);
  });
}

function renderTraeKv(data) {
  const kv = $('traeKv');
  if (!kv) return;
  const active = traeActive(data);
  const history = data.history || null;
  const today = (history && history.today) || traeToday();
  const checked = history ? history.today_checked : (active ? traeIsChecked(active, today) : false);
  const rows = [
    checked ? '是' : '否',
    traeFmt(history ? history.today_credits : 0),
    history ? history.streak + ' 天' : '—',
    history ? history.month_signed_days + ' 天' : '—',
    active ? traeFmt(active.remaining) : '—',
    active ? active.display : '—',
  ];
  const dds = kv.querySelectorAll('dd');
  dds.forEach((dd, i) => {
    dd.textContent = rows[i] == null || rows[i] === '' ? '—' : String(rows[i]);
    dd.title = rows[i] == null ? '' : String(rows[i]);
  });
  const at = $('traeCheckinAt');
  if (at) at.textContent = data.at ? '本地记录 · ' + data.at : '尚未查询';
}

function renderTraeCalendar(data) {
  const box = $('traeCal');
  if (!box) return;
  const active = traeActive(data);
  const history = data.history || null;
  const at = $('traeCalAt');
  if (!active || !history) {
    traeEmpty(box, '添加账号并签到后显示本月日历');
    if (at) at.textContent = '尚未查询';
    return;
  }

  const today = history.today || traeLocalDate();
  const parts = today.split('-').map(Number);
  const year = parts[0];
  const month = parts[1];
  const first = new Date(year, month - 1, 1);
  const days = new Date(year, month, 0).getDate();
  const lead = first.getDay();
  const rows = Math.ceil((lead + days) / 7);
  const signed = new Set((history.signed_dates || []).map(String));
  const prefix = today.slice(0, 7);

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
  for (let i = 0; i < lead; i++) grid.appendChild(document.createElement('i'));
  const pad = (n) => String(n).padStart(2, '0');
  for (let d = 1; d <= days; d++) {
    const iso = year + '-' + pad(month) + '-' + pad(d);
    const cell = document.createElement('div');
    cell.className = 'cal-day';
    cell.textContent = d;
    if (signed.has(iso)) cell.classList.add('on');
    if (iso === today) cell.classList.add('today');
    if (iso > today) cell.classList.add('future');
    cell.title = iso + (signed.has(iso) ? ' · 已签到' : ' · 未签到');
    grid.appendChild(cell);
  }
  box.appendChild(grid);

  const foot = document.createElement('div');
  foot.className = 'cal-foot';
  const legend = document.createElement('span');
  legend.className = 'cal-legend';
  const dot = document.createElement('b');
  dot.className = 'on';
  legend.append(dot, '已签到');
  const count = document.createElement('span');
  const monthCount = (history.signed_dates || [])
    .filter((s) => String(s).startsWith(prefix)).length;
  count.textContent = '本月 ' + monthCount + ' 天 · 连签 ' + (history.streak || 0) + ' 天';
  const week = document.createElement('span');
  week.className = 'weekdots';
  (history.week_progress || []).forEach((on, i) => {
    const b = document.createElement('b');
    if (on) b.classList.add('on');
    b.title = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][i];
    week.appendChild(b);
  });
  foot.append(legend, count, week);
  box.appendChild(foot);
  if (at) at.textContent = active.display + ' · ' + today;
}

function traeTable(headers, rows, emptyText) {
  const wrap = document.createElement('div');
  wrap.className = 'trae-table-wrap';
  if (!rows.length) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = emptyText;
    wrap.appendChild(p);
    return wrap;
  }

  const table = document.createElement('table');
  table.className = 'trae-table';
  const thead = document.createElement('thead');
  const tr = document.createElement('tr');
  headers.forEach((text) => {
    const th = document.createElement('th');
    th.textContent = text;
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  rows.forEach((row) => {
    const bodyRow = document.createElement('tr');
    row.forEach((value, index) => {
      const td = document.createElement('td');
      const cell = value && typeof value === 'object' ? value : { text: value };
      td.textContent = cell.text == null || cell.text === '' ? '—' : String(cell.text);
      if (cell.title) td.title = cell.title;
      if (cell.cls) td.className = cell.cls;
      if (index === row.length - 1 && cell.ellipsis) td.classList.add('ellipsis');
      bodyRow.appendChild(td);
    });
    tbody.appendChild(bodyRow);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function renderTraeHistory(data) {
  const box = $('traeHistory');
  if (!box) return;
  const active = traeActive(data);
  const history = data.history || null;
  const records = (history && history.records) || [];
  const at = $('traeHistoryAt');

  if (!active) {
    traeEmpty(box, '签到成功后会自动写入 SQLite 历史');
    if (at) at.textContent = '尚无记录';
    return;
  }
  if (!records.length) {
    traeEmpty(box, '主号还没有签到记录。第一次签到成功后会写入本地 SQLite。');
    if (at) at.textContent = active.display + ' · 尚无记录';
    return;
  }

  box.innerHTML = '';
  records.forEach((record) => {
    const row = document.createElement('div');
    row.className = 'trae-hist-row';
    const date = document.createElement('span');
    date.className = 'trae-hist-date';
    date.textContent = (record.date || '—') + ' ' + (record.time || '');
    const name = document.createElement('span');
    name.className = 'trae-hist-name';
    name.textContent = record.account_name || active.display;
    name.title = name.textContent;
    if (record.is_member) {
      const member = document.createElement('small');
      member.textContent = '会员';
      name.appendChild(member);
    }
    const credit = document.createElement('span');
    credit.className = 'trae-hist-credit';
    credit.textContent = '+' + traeFmt(record.credits);
    row.append(date, name, credit);
    box.appendChild(row);
  });
  if (at) at.textContent = active.display + ' · ' + records.length + ' 条';
}

function renderTraeUsage(data) {
  const active = traeActive(data);
  const usage = data.usage || null;
  const summary = $('traeUsageSummary');
  const models = $('traeModels');
  const sessions = $('traeSessions');
  const at = $('traeUsageAt');
  const expiring = $('traeExpiring');
  if (!summary || !models || !sessions) return;

  if (!active) {
    TRAE_EXPIRING = [];
    TRAE_EXPIRING_ACCOUNT = null;
  } else if (TRAE_EXPIRING_ACCOUNT && TRAE_EXPIRING_ACCOUNT !== active.id) {
    TRAE_EXPIRING = [];
    TRAE_EXPIRING_ACCOUNT = null;
  }
  if (active && usage && Array.isArray(usage.expiring)) {
    TRAE_EXPIRING = usage.expiring;
    TRAE_EXPIRING_ACCOUNT = active.id;
  }

  if (!active) {
    traeEmpty(summary, '添加 Trae 账号后显示用量');
    models.innerHTML = '';
    sessions.innerHTML = '';
    if (expiring) {
      expiring.textContent = '';
      expiring.className = 'trae-expiring';
    }
    if (at) at.textContent = '尚未同步';
    return;
  }

  if (!TRAE_USAGE_DAYS_SET && usage && usage.days) {
    const select = $('traeUsageDays');
    if (select) select.value = String(usage.days);
  }

  summary.innerHTML = '';
  if (!usage || !usage.has_data) {
    traeEmpty(summary, '本地还没有用量记录。选择天数后点击「同步用量」，'
      + '会从 Trae 拉取主号的会话、Token 与缓存命中数据。');
    models.innerHTML = '';
    sessions.innerHTML = '';
  } else {
    const stats = [
      ['会话数', traeFmt(usage.sessions_count, 0), '近 ' + usage.days + ' 天'],
      ['消耗积分', traeFmt(usage.credits), 'Credits'],
      ['输入 Token', traeFmt(usage.input_token, 0), '含缓存读取'],
      ['输出 Token', traeFmt(usage.output_token, 0), '模型生成'],
      ['缓存命中', traeFmt(usage.cache_rate, 1) + '%', '只读缓存占比'],
    ];
    stats.forEach(([label, value, hint]) => {
      const item = document.createElement('div');
      item.className = 'trae-stat';
      const key = document.createElement('span');
      key.textContent = label;
      const val = document.createElement('b');
      val.textContent = value;
      const note = document.createElement('small');
      note.textContent = hint;
      item.append(key, val, note);
      summary.appendChild(item);
    });

    const modelRows = (usage.models || []).map((m) => ([
      m.model || '未知模型',
      traeFmt(m.sessions, 0),
      traeFmt(m.credits),
      traeFmt(m.input_token, 0),
      traeFmt(m.output_token, 0),
      traeFmt(m.cache_read_token, 0),
    ]));
    models.innerHTML = '';
    const modelsTitle = document.createElement('h3');
    modelsTitle.className = 'trae-table-title';
    modelsTitle.textContent = '按模型';
    models.appendChild(modelsTitle);
    models.appendChild(traeTable(
      ['模型', '会话', '积分', '输入', '输出', '缓存读'],
      modelRows, '近 ' + usage.days + ' 天没有模型用量'));

    const sessionRows = (usage.sessions || []).map((s) => ([
      traeTimeText(s.usage_time),
      s.model || '—',
      traeFmt(s.credits),
      traeFmt(s.input_token, 0) + ' / ' + traeFmt(s.output_token, 0),
      {
        text: s.preview || '—',
        title: s.preview || '',
        ellipsis: true,
      },
    ]));
    sessions.innerHTML = '';
    const sessionsTitle = document.createElement('h3');
    sessionsTitle.className = 'trae-table-title';
    sessionsTitle.textContent = '最近会话';
    sessions.appendChild(sessionsTitle);
    sessions.appendChild(traeTable(
      ['时间', '模型', '积分', '输入 / 输出', '摘要'],
      sessionRows, '近 ' + usage.days + ' 天没有会话记录'));
  }

  if (at) {
    at.textContent = usage && usage.at
      ? (usage.account && usage.account.name ? usage.account.name + ' · ' : '')
        + usage.at
      : (active.last_usage_at ? '本地缓存 · 最近同步 ' + active.last_usage_at : '尚未同步');
  }

  if (expiring) {
    expiring.className = 'trae-expiring';
    expiring.innerHTML = '';
    if (!TRAE_EXPIRING.length) {
      expiring.textContent = '近期没有即将过期的积分包。';
    } else {
      const label = document.createElement('b');
      label.textContent = '即将过期：';
      expiring.appendChild(label);
      TRAE_EXPIRING.forEach((item, index) => {
        if (index) expiring.appendChild(document.createTextNode('；'));
        const span = document.createElement('span');
        const when = traeTimeText(item.expire_time);
        span.textContent = (item.name || '积分包') + ' '
          + traeFmt(item.credits) + ' 分 · ' + when
          + (item.expired ? '（已过期）' : '');
        span.className = item.expired ? 'expired' : 'soon';
        expiring.appendChild(span);
      });
    }
  }
}

function appendTraeLog(text, kind) {
  const box = $('traeLog');
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

function renderTraeLog(log) {
  const box = $('traeLog');
  if (!box) return;
  box.innerHTML = '';
  ((log && log.lines) || []).forEach((line) => appendTraeLog(line));
  const at = $('traeLogAt');
  if (at) at.textContent = log && log.at ? '最近写入 ' + log.at : '尚无日志';
}

function renderTrae(data) {
  TRAE_DATA = data || {};
  const ok = TRAE_DATA.ok !== false;

  if (!ok) {
    setCard('tToday', 'tTodayHint', '—', '无法读取');
    setCard('tRemaining', 'tRemainingHint', '—', '无法读取');
    setCard('tAccounts', 'tAccountsHint', '—', '无法读取');
    traeSideState();
    traeEmpty($('traeAccounts'),
      'Trae 监控不可用：' + (TRAE_DATA.error || '未知错误'));
    traeEmpty($('traeCal'), 'Trae 监控不可用');
    traeEmpty($('traeUsageSummary'), 'Trae 监控不可用');
    traeEmpty($('traeHistory'), 'Trae 监控不可用');
    $('traeModels').innerHTML = '';
    $('traeSessions').innerHTML = '';
    $('traeExpiring').textContent = '';
    renderTraeLog(TRAE_DATA.log);
    if (!TRAE_DATA.busy && TRAE_DATA.error) {
      appendTraeLog('Trae 监控不可用：' + TRAE_DATA.error, 'err');
    }
    setTraeAvailability(false);
    return;
  }

  const active = traeActive(TRAE_DATA);
  const history = TRAE_DATA.history || null;
  renderTraeCards(TRAE_DATA);
  renderTraeAccounts(TRAE_DATA);
  renderTraeKv(TRAE_DATA);
  renderTraeCalendar(TRAE_DATA);
  renderTraeHistory(TRAE_DATA);
  renderTraeUsage(TRAE_DATA);
  renderTraeLog(TRAE_DATA.log);

  const select = $('traeUsageDays');
  const settings = TRAE_DATA.settings || {};
  if (select && !TRAE_USAGE_DAYS_SET) {
    select.value = String((history && history.days) || (TRAE_DATA.usage && TRAE_DATA.usage.days)
      || settings.usage_days || 7);
  }
  const accountsAt = $('traeAccountsAt');
  if (accountsAt) {
    accountsAt.textContent = TRAE_DATA.at
      ? '刷新于 ' + TRAE_DATA.at
      : '尚未刷新';
  }
  const hint = $('traeHint');
  if (hint) {
    hint.textContent = '刷新监控只读；一键签到与账号操作会真实改动本地账号或服务端签到状态。'
      + (active ? ' 当前主号：' + active.display : '');
    hint.title = TRAE_DATA.data_dir ? '数据目录：' + TRAE_DATA.data_dir : '';
  }

  setTraeAvailability(true);
  if (TRAE_DATA.busy) {
    TRAE_BUSY = true;
    TRAE_BUSY_LABEL = '任务执行中';
    setTraeBusy(true, TRAE_BUSY_LABEL);
  }
  traeSideState();
}

function refreshTrae(silent) {
  if (TRAE_LOADING) return Promise.resolve();
  TRAE_LOADING = true;
  const btn = $('btnTraeRefresh');
  if (btn) btn.disabled = true;
  if (!silent) appendTraeLog('正在读取 Trae 账号、签到历史、用量与日志…', 'meta');
  return call('trae_monitor')
    .then((data) => {
      if (data && data.busy && TRAE_DATA && !(data.accounts || []).length) {
        TRAE_DATA.log = data.log || TRAE_DATA.log;
        TRAE_DATA.busy = true;
        renderTraeLog(TRAE_DATA.log);
        traeSideState();
        return;
      }
      TRAE_LOADED = true;
      renderTrae(data);
    })
    .catch((e) => {
      TRAE_LOADED = true;
      renderTrae({ ok: false, error: String(e), log: null });
      appendTraeLog('刷新 Trae 监控失败：' + e, 'err');
    })
    .finally(() => {
      TRAE_LOADING = false;
      if (btn) btn.disabled = false;
    });
}

function setTraeAvailability(on) {
  const ids = ['btnTraeCheckin', 'btnTraeImport', 'btnTraeAdd',
    'btnTraeSettings', 'btnTraeAccountsRefresh', 'btnTraeUsage'];
  ids.forEach((id) => {
    const btn = $(id);
    if (btn) btn.disabled = !on;
  });
  if (!on) {
    document.querySelectorAll('#traeAccounts [data-trae-action]').forEach((b) => {
      b.disabled = true;
    });
  }
}

function setTraeBusy(on, label) {
  TRAE_BUSY = !!on;
  TRAE_BUSY_LABEL = on ? (label || '处理中') : '';
  const ids = ['btnTraeCheckin', 'btnTraeImport', 'btnTraeAdd',
    'btnTraeSettings', 'btnTraeAccountsRefresh', 'btnTraeUsage'];
  const available = !!(TRAE_DATA && TRAE_DATA.ok !== false);
  ids.forEach((id) => {
    const btn = $(id);
    if (btn) btn.disabled = !available || on;
  });
  document.querySelectorAll('#traeAccounts [data-trae-action]').forEach((b) => {
    b.disabled = on;
  });
  if (on) traeSetDot('busy');
  else traeSideState();
}

function traeResetCheckin() {
  if (!TRAE_PENDING) return;
  const btn = $('btnTraeCheckin');
  if (btn) {
    btn.classList.remove('confirm');
    btn.textContent = '一键签到';
  }
  TRAE_PENDING = false;
}

function traeCheckin() {
  if (TRAE_BUSY) return;
  const btn = $('btnTraeCheckin');
  if (!TRAE_PENDING) {
    TRAE_PENDING = true;
    if (btn) {
      btn.classList.add('confirm');
      btn.textContent = '确认签到？点此执行';
    }
    appendTraeLog('待确认：再次点击会真实调用 Trae 签到接口，逐个账号签到', 'warn');
    setTimeout(traeResetCheckin, 8000);
    return;
  }
  traeResetCheckin();
  startTraeTask('checkin', null);
}

function startTraeTask(kind, accountId, days) {
  if (TRAE_BUSY) return;
  const labels = {
    checkin: '正在签到',
    refresh: '正在刷新账号状态',
    usage: '正在同步用量',
    import: '正在导入本机登录态',
  };
  const label = labels[kind] || kind;
  setTraeBusy(true, label + '…');
  appendTraeLog(label + '…', 'cmd');

  let request;
  if (kind === 'checkin') request = call('trae_checkin', accountId || null);
  else if (kind === 'refresh') request = call('trae_refresh', accountId || null);
  else if (kind === 'usage') request = call('trae_usage', accountId || null, days || null);
  else if (kind === 'import') request = call('trae_import');
  else request = Promise.reject(new Error('未知操作：' + kind));

  return request
    .then((r) => {
      if (r && r.ok === false) {
        setTraeBusy(false);
        appendTraeLog('未能启动：' + (r.error || '未知错误'), 'err');
      }
    })
    .catch((e) => {
      setTraeBusy(false);
      appendTraeLog(label + '调用失败：' + e, 'err');
    });
}

function traeRefreshAccounts() {
  if (!(TRAE_DATA && (TRAE_DATA.accounts || []).length)) {
    appendTraeLog('还没有 Trae 账号，请先导入本机登录态或添加账号', 'warn');
    return;
  }
  startTraeTask('refresh', null);
}

function traeImport() {
  startTraeTask('import', null);
}

function traeSyncUsage() {
  if (!(TRAE_DATA && (TRAE_DATA.accounts || []).length)) {
    appendTraeLog('还没有 Trae 账号，无法同步用量', 'warn');
    return;
  }
  const select = $('traeUsageDays');
  const days = select ? Number(select.value) : 7;
  const active = traeActive(TRAE_DATA);
  if (!active) return;
  startTraeTask('usage', active.id, Number.isFinite(days) ? days : null);
}

function resetTraeAccountPending() {
  if (!TRAE_ACCOUNT_PENDING) return;
  TRAE_ACCOUNT_PENDING = null;
  if (TRAE_DATA) renderTraeAccounts(TRAE_DATA);
}

function traeAccountAction(action, id) {
  if (TRAE_BUSY) return;
  const account = traeAccountById(id);
  if (!account) return;

  if (action === 'edit') {
    openTraeAccountDialog(account);
    return;
  }
  if (action === 'refresh') {
    startTraeTask('refresh', id);
    return;
  }
  if (action === 'checkin' || action === 'remove'
      || action === 'renew_device') {
    if (!TRAE_ACCOUNT_PENDING
        || TRAE_ACCOUNT_PENDING.action !== action
        || TRAE_ACCOUNT_PENDING.id !== id) {
      TRAE_ACCOUNT_PENDING = { action, id };
      renderTraeAccounts(TRAE_DATA);
      appendTraeLog('待确认：再次点击「'
        + (action === 'remove' ? '确认删除'
          : action === 'renew_device' ? '确认换设备' : '确认签到')
        + '」会修改本地 Trae 账号或服务端签到状态', 'warn');
      setTimeout(resetTraeAccountPending, 8000);
      return;
    }
    TRAE_ACCOUNT_PENDING = null;
  }
  if (action === 'checkin') {
    startTraeTask('checkin', id);
    return;
  }

  let actionName = action;
  let payload = null;
  if (action === 'toggle') {
    actionName = 'update';
    payload = { enabled: !account.enabled };
  }
  if (action === 'activate') actionName = 'activate';

  setTraeBusy(true, '正在保存账号设置…');
  call('trae_account_action', actionName, id, payload)
    .then((r) => {
      if (r && r.ok) {
        appendTraeLog('账号「' + (account.display || '') + '」：'
          + (r.report || '操作完成'), 'ok');
      } else {
        appendTraeLog('账号操作失败：' + ((r && (r.error || r.report)) || '未知错误'), 'err');
      }
    })
    .catch((e) => appendTraeLog('账号操作调用失败：' + e, 'err'))
    .finally(() => {
      setTraeBusy(false);
      refreshTrae(true);
    });
}

function onTraeDone(data) {
  setTraeBusy(false);
  traeResetCheckin();
  const kind = data && data.kind ? data.kind : '';
  const payload = data && data.payload ? data.payload : null;

  if (payload && kind === 'usage' && Array.isArray(payload.expiring)) {
    TRAE_EXPIRING = payload.expiring;
    TRAE_EXPIRING_ACCOUNT = payload.account && payload.account.id
      ? payload.account.id : null;
  }

  if (payload) {
    const result = payload.result || payload.report || '完成';
    const text = (TRAE_RESULT_LABEL[result] || result) + '：'
      + (payload.report || payload.message || '');
    appendTraeLog(text, data.exit === 0 ? 'ok' : 'warn');
    if (kind === 'import' && (payload.added !== undefined || payload.updated !== undefined)) {
      appendTraeLog('本机导入：新增 ' + (payload.added || 0)
        + ' 个，更新 ' + (payload.updated || 0) + ' 个', 'meta');
    }
    (payload.accounts || []).forEach((row) => {
      if (!row || typeof row !== 'object') return;
      const mark = row.ok ? '✔' : '✖';
      const gained = row.gained ? ' +' + row.gained : '';
      const remaining = row.remaining !== undefined && row.remaining !== null
        ? '（剩余 ' + row.remaining + '）' : '';
      appendTraeLog('  ' + mark + ' ' + (row.name || '账号') + gained
        + remaining + ' ' + (row.message || ''), row.ok ? 'ok' : 'warn');
    });
  } else if (data && data.error) {
    appendTraeLog('Trae 操作异常：' + data.error, 'err');
  }

  if (data && data.log) renderTraeLog(data.log);
  refreshTrae(true);
  refreshTraeService(true).finally(scheduleTraeServicePoll);
}

function renderTraeService(s) {
  TRAE_SERVICE = s || {};
  const supported = TRAE_SERVICE.supported !== false;
  const ok = TRAE_SERVICE.ok !== false;
  const tasks = TRAE_SERVICE.tasks || [];
  const task = tasks[0] || {};

  const pill = $('tsvcState');
  let pillText = '未检测';
  let pillKind = '';
  if (!supported) {
    pillText = '仅 Windows';
    pillKind = 'warn';
  } else if (!ok) {
    pillText = '读取失败';
    pillKind = 'err';
  } else if (TRAE_SERVICE.running) {
    pillText = '运行中';
    pillKind = 'busy';
  } else if (TRAE_SERVICE.installed && TRAE_SERVICE.enabled) {
    pillText = '已启用';
    pillKind = 'ok';
  } else if (TRAE_SERVICE.installed) {
    pillText = '已停用';
    pillKind = 'warn';
  } else {
    pillText = '未安装';
  }
  pill.textContent = pillText;
  pill.className = 'svc-pill ' + pillKind;

  const summary = $('tsvcSummary');
  const autoTime = (TRAE_DATA && TRAE_DATA.settings && TRAE_DATA.settings.auto_time) || '08:05';
  if (!supported) {
    summary.textContent = 'Trae 计划任务使用 Windows 任务计划程序，当前平台不支持。';
  } else if (!ok) {
    summary.textContent = '状态读取失败：' + (TRAE_SERVICE.error || '未知错误');
  } else if (!TRAE_SERVICE.installed) {
    summary.textContent = '尚未安装 TraeDailyCheckin。点击「安装 / 修复」创建独立任务，'
      + '默认从 ' + autoTime + ' 起每天运行 5 次；不会改动 WorkBuddy / Qoder 的任务。';
  } else if (TRAE_SERVICE.enabled) {
    summary.textContent = 'TraeDailyCheckin 已启用'
      + (TRAE_SERVICE.next_run ? '，下次运行 ' + TRAE_SERVICE.next_run : '')
      + '。';
  } else {
    summary.textContent = 'TraeDailyCheckin 已安装但被停用；点击「启用」恢复自动签到。';
  }

  const box = $('tsvcTasks');
  box.innerHTML = '';
  tasks.forEach((t) => box.appendChild(makeServiceNode(t)));

  const log = $('tsvcLog');
  if (TRAE_SERVICE.log && TRAE_SERVICE.log.line) {
    log.textContent = '最近日志 ' + (TRAE_SERVICE.log.at || '') + ' · '
      + TRAE_SERVICE.log.line;
    log.title = (TRAE_SERVICE.log.path || '') + '\n' + TRAE_SERVICE.log.line;
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
    taskHint = TRAE_SERVICE.error || '';
  } else if (!task.installed) {
    taskText = '未安装';
    taskHint = '点击「安装 / 修复」创建 TraeDailyCheckin';
  } else if (task.state === 'Running') {
    taskText = '运行中';
    taskHint = '上次 ' + (task.last_run || '尚未运行');
  } else if (task.enabled) {
    taskText = '已启用';
    taskHint = task.next_run ? '下次 ' + task.next_run : '等待调度';
  } else {
    taskText = '已停用';
    taskHint = '点击「启用」恢复自动签到';
  }
  setCard('tTask', 'tTaskHint', taskText, taskHint);
  if (TRAE_DATA) traeSideState();

  const installed = !!TRAE_SERVICE.installed_count;
  const anyEnabled = tasks.some((t) => t.installed && t.enabled);
  const canUse = supported && ok && !TRAE_SVC_BUSY;

  $('btnTSvcInstall').disabled = !canUse;
  $('btnTSvcEnable').disabled = !canUse || !installed || !!TRAE_SERVICE.enabled;
  $('btnTSvcDisable').disabled = !canUse || !installed || !anyEnabled;
  $('btnTSvcRun').disabled = !canUse || !installed || !anyEnabled;
  $('btnTSvcStop').disabled = !canUse || !TRAE_SERVICE.running;
  $('btnTSvcUninstall').disabled = !canUse || !installed;
  $('btnTSvcRefresh').disabled = TRAE_SVC_BUSY;
}

function setTraeServiceBusy(on) {
  TRAE_SVC_BUSY = on;
  const pill = $('tsvcState');
  if (on) {
    pill.textContent = '处理中…';
    pill.className = 'svc-pill busy';
  }
  Object.keys(TRAE_SVC_BUTTONS).forEach((key) => {
    if (on) $(TRAE_SVC_BUTTONS[key]).disabled = true;
  });
  $('btnTSvcRefresh').disabled = on;
  if (!on) renderTraeService(TRAE_SERVICE || {});
}

function resetTraeSvcPending() {
  if (!TRAE_SVC_PENDING) return;
  const btn = $(TRAE_SVC_BUTTONS[TRAE_SVC_PENDING]);
  if (btn) {
    btn.classList.remove('confirm');
    if (btn.dataset.label) {
      btn.textContent = btn.dataset.label;
      delete btn.dataset.label;
    }
  }
  TRAE_SVC_PENDING = null;
}

function traeServiceAction(action) {
  if (TRAE_SVC_BUSY) return;
  const btn = $(TRAE_SVC_BUTTONS[action]);
  const label = TRAE_SVC_ACTION_LABEL[action] || action;

  if (TRAE_SVC_CONFIRM[action] && TRAE_SVC_PENDING !== action) {
    resetTraeSvcPending();
    TRAE_SVC_PENDING = action;
    btn.dataset.label = btn.textContent;
    btn.classList.add('confirm');
    btn.textContent = '确认' + label + '？';
    appendTraeLog('待确认：' + label + ' Trae 计划任务（再次点击按钮执行）', 'warn');
    setTimeout(() => { if (TRAE_SVC_PENDING) resetTraeSvcPending(); }, 8000);
    return;
  }

  resetTraeSvcPending();
  setTraeServiceBusy(true);
  appendTraeLog('Trae 计划任务：' + label + '…', 'cmd');
  call('trae_service_action', action)
    .then((r) => {
      if (r && r.ok) {
        appendTraeLog('Trae 计划任务：' + (r.message || (label + '完成')), 'ok');
      } else {
        appendTraeLog('Trae 计划任务：'
          + ((r && (r.error || r.message)) || '操作失败'), 'err');
      }
      if (r && r.status) renderTraeService(r.status);
      else return refreshTraeService(true);
    })
    .catch((e) => appendTraeLog('Trae 计划任务调用失败：' + e, 'err'))
    .finally(() => {
      TRAE_SVC_BUSY = false;
      refreshTraeService(true).finally(() => setTraeServiceBusy(false));
    });
}

function refreshTraeService(silent) {
  if (TRAE_SVC_BUSY || TRAE_SVC_REFRESHING) return Promise.resolve();
  TRAE_SVC_REFRESHING = true;
  const seq = ++TRAE_SVC_SEQ;
  const btn = $('btnTSvcRefresh');
  if (btn) btn.disabled = true;
  return call('trae_service_status')
    .then((s) => {
      if (seq !== TRAE_SVC_SEQ) return;
      renderTraeService(s);
      if (!silent && s && s.ok === false) {
        appendTraeLog('读取 Trae 计划任务失败：' + (s.error || '未知错误'), 'err');
      }
    })
    .catch((e) => {
      if (seq !== TRAE_SVC_SEQ) return;
      renderTraeService({ supported: true, ok: false, error: String(e), tasks: [] });
      appendTraeLog('读取 Trae 计划任务异常：' + e, 'err');
    })
    .finally(() => {
      TRAE_SVC_REFRESHING = false;
      if (seq === TRAE_SVC_SEQ) {
        const refresh = $('btnTSvcRefresh');
        if (refresh) refresh.disabled = TRAE_SVC_BUSY;
      }
    });
}

function scheduleTraeServicePoll() {
  if (TRAE_SVC_TIMER) clearTimeout(TRAE_SVC_TIMER);
  const delay = (TRAE_SERVICE && TRAE_SERVICE.running) ? 20000 : 180000;
  TRAE_SVC_TIMER = setTimeout(() => {
    if (document.hidden || CURRENT_PAGE !== 'trae') {
      scheduleTraeServicePoll();
      return;
    }
    refreshTraeService(true).finally(scheduleTraeServicePoll);
  }, delay);
}

function openTraeAccountDialog(account) {
  TRAE_EDIT_ID = account ? account.id : null;
  const dialog = $('traeAccountDialog');
  if (!dialog) return;
  $('traeAccountTitle').textContent = account ? '编辑 Trae 账号' : '添加 Trae 账号';
  $('traeAccountHint').textContent = account
    ? '修改备注名、会员标记或启用状态。登录凭证不在此处回显。'
    : '支持 Cloud-IDE-JWT Token，或 X-Cloudide-Session 会话 Cookie；'
      + '两者同时粘贴时优先使用 Token。';
  $('traeCredentialField').hidden = !!account;
  $('traeCredential').value = '';
  $('traeAccountName').value = account ? (account.name || '') : '';
  $('traeAccountMember').checked = account ? !!account.is_member : false;
  $('traeAccountEnabled').checked = account ? !!account.enabled : true;
  $('traeAccountError').textContent = '';
  $('btnTraeAccountSave').disabled = false;
  dialog.showModal();
  setTimeout(() => {
    if (account) $('traeAccountName').focus();
    else $('traeCredential').focus();
  }, 30);
}

function closeTraeAccountDialog() {
  const dialog = $('traeAccountDialog');
  if (dialog && dialog.open) dialog.close();
  TRAE_EDIT_ID = null;
}

function setTraeFormBusy(formId, saveId, on, label) {
  const form = $(formId);
  const save = $(saveId);
  if (form) {
    form.querySelectorAll('input, textarea, button').forEach((el) => {
      if (el !== save) el.disabled = on;
    });
  }
  if (save) {
    save.disabled = on;
    if (on) {
      save.dataset.label = save.textContent;
      save.textContent = label || '保存中…';
    } else if (save.dataset.label) {
      save.textContent = save.dataset.label;
      delete save.dataset.label;
    }
  }
}

async function submitTraeAccount(event) {
  event.preventDefault();
  const error = $('traeAccountError');
  error.textContent = '';
  const name = $('traeAccountName').value.trim();
  const member = $('traeAccountMember').checked;
  const enabled = $('traeAccountEnabled').checked;

  if (TRAE_EDIT_ID) {
    setTraeFormBusy('traeAccountForm', 'btnTraeAccountSave', true);
    try {
      const r = await call('trae_account_action', 'update', TRAE_EDIT_ID,
        { name, enabled, is_member: member });
      if (!r || !r.ok) {
        error.textContent = (r && (r.error || r.report)) || '保存失败';
        return;
      }
      appendTraeLog('账号「' + (name || '未命名') + '」设置已保存', 'ok');
      closeTraeAccountDialog();
      refreshTrae(true);
    } catch (e) {
      error.textContent = '调用失败：' + e;
    } finally {
      setTraeFormBusy('traeAccountForm', 'btnTraeAccountSave', false);
    }
    return;
  }

  const credential = $('traeCredential').value.trim();
  if (!credential) {
    error.textContent = '请粘贴 Cloud-IDE-JWT Token 或 X-Cloudide-Session 会话 Cookie';
    $('traeCredential').focus();
    return;
  }

  setTraeFormBusy('traeAccountForm', 'btnTraeAccountSave', true, '验证中…');
  try {
    let r = await call('trae_account_action', 'add', null,
      { credential, name });
    if (!r || !r.ok) {
      error.textContent = (r && (r.error || r.report)) || '添加失败';
      return;
    }
    const account = r.account || {};
    if (account.id && (!!account.is_member !== member || !!account.enabled !== enabled)) {
      const update = await call('trae_account_action', 'update', account.id,
        { name, enabled, is_member: member });
      if (!update || !update.ok) {
        error.textContent = (update && (update.error || update.report))
          || '账号已添加，但保存会员 / 启用状态失败';
        return;
      }
    }
    appendTraeLog('账号「' + (name || account.display || '新账号') + '」已添加', 'ok');
    closeTraeAccountDialog();
    refreshTrae(true);
  } catch (e) {
    error.textContent = '调用失败：' + e;
  } finally {
    setTraeFormBusy('traeAccountForm', 'btnTraeAccountSave', false);
  }
}

function openTraeSettings() {
  const dialog = $('traeSettingsDialog');
  if (!dialog) return;
  const settings = (TRAE_DATA && TRAE_DATA.settings) || {};
  $('traeAutoTime').value = settings.auto_time || '08:05';
  $('traeInterval').value = settings.interval_seconds == null
    ? 5 : settings.interval_seconds;
  $('traeUsageDefaultDays').value = settings.usage_days || 7;
  $('traeFeishu').value = '';
  $('traeFeishuState').textContent = settings.feishu_configured
    ? '已配置飞书 Webhook（不会回显）。留空保持不变。'
    : '尚未配置飞书推送；填写后，签到结果会推送到群机器人。';
  $('traeSettingsError').textContent = '';
  $('btnTraeSettingsSave').disabled = false;
  dialog.showModal();
  setTimeout(() => $('traeAutoTime').focus(), 30);
}

function closeTraeSettings() {
  const dialog = $('traeSettingsDialog');
  if (dialog && dialog.open) dialog.close();
}

async function submitTraeSettings(event) {
  event.preventDefault();
  const error = $('traeSettingsError');
  error.textContent = '';
  const autoTime = $('traeAutoTime').value;
  if (!autoTime) {
    error.textContent = '请选择自动签到时间';
    return;
  }
  const payload = {
    auto_time: autoTime,
    interval_seconds: Number($('traeInterval').value),
    usage_days: Number($('traeUsageDefaultDays').value),
  };
  const feishu = $('traeFeishu').value.trim();
  if (feishu) payload.feishu_webhook = feishu;

  setTraeFormBusy('traeSettingsForm', 'btnTraeSettingsSave', true);
  try {
    const r = await call('trae_update_settings', payload);
    if (!r || !r.ok) {
      error.textContent = (r && (r.error || r.report)) || '保存失败';
      return;
    }
    if (TRAE_DATA && r.settings) TRAE_DATA.settings = r.settings;
    appendTraeLog('Trae 设置已保存'
      + (payload.feishu_webhook ? '，飞书 Webhook 已更新' : ''), 'ok');
    closeTraeSettings();
    refreshTrae(true);
  } catch (e) {
    error.textContent = '调用失败：' + e;
  } finally {
    setTraeFormBusy('traeSettingsForm', 'btnTraeSettingsSave', false);
  }
}

async function clearTraeFeishu() {
  const state = $('traeFeishuState');
  const error = $('traeSettingsError');
  error.textContent = '';
  setTraeFormBusy('traeSettingsForm', 'btnTraeSettingsSave', true, '清除中…');
  try {
    const r = await call('trae_update_settings', { feishu_webhook: '' });
    if (!r || !r.ok) {
      error.textContent = (r && (r.error || r.report)) || '清除失败';
      return;
    }
    if (TRAE_DATA && r.settings) TRAE_DATA.settings = r.settings;
    if (state) state.textContent = '已清除飞书 Webhook。';
    appendTraeLog('飞书 Webhook 已清除', 'ok');
  } catch (e) {
    error.textContent = '调用失败：' + e;
  } finally {
    setTraeFormBusy('traeSettingsForm', 'btnTraeSettingsSave', false);
  }
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

/* ---------- 分区懒加载 ---------- */

/* 启动时只渲染 bootstrap 带回来的缓存数据；网络查询与 PowerShell 调用
 * 都推迟到对应区块进入视口时。原因：这些动作全发生在首帧之后，缩短不了
 * WebView2 初始化那段固定等待，但它们在窗口很小、或停在 Qoder 页时
 * 完全是白干的——尤其脚本服务每次刷新都要起一次 PowerShell。
 *
 * 按数据源分组而不是按卡片一张一张拉：顶部四张卡共用同一次 status/doctor
 * 查询，拆开只会增加往返和后端锁竞争，并不会更快。
 */
const LAZY_SECTIONS = [];
let LAZY_OBSERVER = null;
let LAZY_RESIZE_BOUND = false;

function lazyKick() {
  if (!LAZY_SECTIONS.length) return;
  // 预取边距：滚动到附近就提前加载，避免用户看到空面板
  const margin = 240;
  const viewport = window.innerHeight || 800;
  LAZY_SECTIONS.forEach((section) => {
    if (section.done) return;
    const el = section.el;
    // 找不到元素时按可见处理（fail-open），避免改版后区块永远不加载
    let visible = true;
    if (el) {
      const rect = el.getBoundingClientRect();
      // 区块所在页面被切走（hidden）时 rect 全为 0，等切回来再判定
      const hasBox = rect.width > 0 || rect.height > 0;
      visible = hasBox && rect.top < viewport + margin && rect.bottom > -margin;
    }
    if (!visible) return;
    section.done = true;
    if (LAZY_OBSERVER && el) LAZY_OBSERVER.unobserve(el);
    try {
      section.run();
    } catch (e) {
      appendLog('懒加载区块失败（' + section.key + '）：' + e, 'err');
      try { window.__errs.push('懒加载失败：' + section.key + '：' + e); } catch (err) {}
    }
  });
}

function whenVisible(key, el, run) {
  const section = { key, el, run, done: false };
  LAZY_SECTIONS.push(section);

  if (el && typeof IntersectionObserver === 'function') {
    if (!LAZY_OBSERVER) {
      LAZY_OBSERVER = new IntersectionObserver(() => lazyKick(),
        { rootMargin: '240px 0px' });
    }
    LAZY_OBSERVER.observe(el);
  }
  if (!LAZY_RESIZE_BOUND && typeof window.addEventListener === 'function') {
    // 窗口从很小拉到很大、或从 Qoder 页切回来时，观察器之外的兜底判定
    window.addEventListener('resize', lazyKick);
    // 窗口被显示出来（visibilityState 变化）时再判一次：隐藏期间
    // IntersectionObserver 可能不派发回调，rect 判定不受影响但这里保险
    if (typeof document.addEventListener === 'function') {
      document.addEventListener('visibilitychange', lazyKick);
    }
    LAZY_RESIZE_BOUND = true;
  }
  setTimeout(lazyKick, 0);            // 首帧后再判一次，可见区块立即开跑
  return section;
}

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
    const hint = $('wbSideHint');
    if (hint) hint.textContent = '缺少 signin.py';
    wbSetDot('err');
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
  // 首次查询同样等区块可见再发起，见下方 whenVisible。

  // 各区块进入视口（或所在页面被切到前台）才发起自己的查询：
  //   指标卡     -> status + doctor（同一次数据源，含签到详情与本月日历）
  //   成长中心   -> growth
  //   脚本服务   -> 计划任务状态（每次都要起 PowerShell，最贵的一项）
  whenVisible('cards', $('cards'), () => {
    if (!info.signin_exists) return;
    CHECKIN_STARTED = true;
    runSeries(['status', 'doctor'], () => {
      CHECKIN_DONE = true;
      maybeLoadGrowth();
    });
  });
  // 这两个区块只有 class 没有 id（$ 只查 id），必须用 querySelector。
  whenVisible('growth', document.querySelector('.pane-tasks'), () => {
    if (!info.signin_exists) return;
    GROWTH_WANTED = true;
    maybeLoadGrowth();
  });
  whenVisible('service', document.querySelector('.pane-service'), () => {
    refreshService(true);
    scheduleServicePoll();
  });

  setPage(savedPage());
  if (!info.qoder_exists) {
    const hint = $('qoderSideHint');
    if (hint) hint.textContent = '缺少 qoder_signin.py';
    qoderSetDot('err');
    appendLog('未找到 qoder_signin.py，Qoder 监控页不可用。', 'warn');
  }
  if (!info.trae_exists) {
    const hint = $('traeSideHint');
    if (hint) hint.textContent = '缺少 trae_signin.py';
    traeSetDot('err');
    setTraeAvailability(false);
    appendLog('未找到 trae_signin.py，Trae 监控页不可用。'
      + '请把脚本放到 exe 同目录。', 'warn');
  }

  // 缓存只是"先有个数看"，真实数据以各区块自己的查询为准（见上）。
  // 只有 Qoder 页不在启动时联网：切到该页或点刷新才查询。
  if (!info.signin_exists) {
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
  $('btnWbAccountRefresh').addEventListener('click', refreshWorkbuddyAccount);

  $('btnSvcRefresh').addEventListener('click', () => refreshService(false));
  Object.keys(SVC_BUTTONS).forEach((action) => {
    $(SVC_BUTTONS[action]).addEventListener('click', () => serviceAction(action));
  });

  document.querySelectorAll('.side-tab').forEach((tab) => {
    tab.addEventListener('click', () => setPage(tab.dataset.page));
  });

  $('btnQoderRefresh').addEventListener('click', () => refreshQoder(false));
  $('btnQoderAccountRefresh').addEventListener('click', () => refreshQoder(false));
  $('btnQoderClaim').addEventListener('click', qoderClaim);
  $('btnQoderLogClear').addEventListener('click', () => {
    $('qoderLog').innerHTML = '';
    $('qoderLogAt').textContent = '显示已清空';
  });
  $('btnQSvcRefresh').addEventListener('click', () => refreshQoderService(false));
  Object.keys(QODER_SVC_BUTTONS).forEach((action) => {
    $(QODER_SVC_BUTTONS[action]).addEventListener('click', () => qoderServiceAction(action));
  });

  $('btnTraeRefresh').addEventListener('click', () => refreshTrae(false));
  $('btnTraeCheckin').addEventListener('click', traeCheckin);
  $('btnTraeImport').addEventListener('click', traeImport);
  $('btnTraeAdd').addEventListener('click', () => openTraeAccountDialog(null));
  $('btnTraeSettings').addEventListener('click', openTraeSettings);
  $('btnTraeAccountsRefresh').addEventListener('click', traeRefreshAccounts);
  $('btnTraeUsage').addEventListener('click', traeSyncUsage);
  $('btnTraeLogClear').addEventListener('click', () => {
    $('traeLog').innerHTML = '';
    $('traeLogAt').textContent = '显示已清空';
  });
  $('traeUsageDays').addEventListener('change', () => {
    TRAE_USAGE_DAYS_SET = true;
  });
  $('traeAccounts').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-trae-action]');
    if (!btn || btn.disabled) return;
    traeAccountAction(btn.dataset.traeAction, btn.dataset.traeId);
  });
  $('btnTSvcRefresh').addEventListener('click', () => refreshTraeService(false));
  Object.keys(TRAE_SVC_BUTTONS).forEach((action) => {
    $(TRAE_SVC_BUTTONS[action]).addEventListener('click', () => traeServiceAction(action));
  });

  $('traeAccountForm').addEventListener('submit', submitTraeAccount);
  $('btnTraeAccountClose').addEventListener('click', closeTraeAccountDialog);
  $('btnTraeAccountCancel').addEventListener('click', closeTraeAccountDialog);
  $('traeAccountDialog').addEventListener('click', (e) => {
    if (e.target === $('traeAccountDialog')) closeTraeAccountDialog();
  });
  $('traeSettingsForm').addEventListener('submit', submitTraeSettings);
  $('btnTraeSettingsClose').addEventListener('click', closeTraeSettings);
  $('btnTraeSettingsCancel').addEventListener('click', closeTraeSettings);
  $('btnTraeFeishuClear').addEventListener('click', clearTraeFeishu);
  $('traeSettingsDialog').addEventListener('click', (e) => {
    if (e.target === $('traeSettingsDialog')) closeTraeSettings();
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