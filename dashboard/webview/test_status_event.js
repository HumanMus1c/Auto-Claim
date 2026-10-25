/* 前端事件回归测试：命令完成后不能把无关区域的已有数据清空。
 *
 * 重点场景：仅执行「成长中心 growth」时，后端 payload 是成长中心汇报
 * （report / energy / streak_days…），不是签到状态。旧逻辑把它交给
 * renderFromStatus，导致顶部积分卡和签到详情全部变成「—」。
 *
 * 用 Node 自带的 vm 加载 app.js，并用最小 DOM 桩驱动 __onBackend。
 * 不引入 jsdom，也不启动 WebView2。
 */

'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, 'assets', 'app.js');
const INDEX = path.join(__dirname, 'assets', 'index.html');

function makeElement(id) {
  const el = {
    id,
    textContent: '',
    className: '',
    innerHTML: '',
    checked: false,
    childElementCount: 0,
    style: { setProperty() {} },
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    },
    appendChild(child) { el.childElementCount += 1; return child; },
    append() {},
    removeChild() { el.childElementCount -= 1; },
    setAttribute(name, value) { el[name] = value; },
    focus() {},
    querySelector() { return makeElement('query'); },
    querySelectorAll(selector) {
      if (id === 'kv' && selector === 'dd') return el.dds || [];
      return [];
    },
  };
  return el;
}

function boot(savedTheme) {
  const elements = {};
  const el = (id) => {
    if (!elements[id]) elements[id] = makeElement(id);
    return elements[id];
  };
  const dds = Array.from({ length: 6 }, (_, i) => makeElement('dd' + i));
  el('kv').dds = dds;
  const documentElement = { dataset: {}, style: { setProperty() {} } };

  const document = {
    getElementById: el,
    querySelectorAll() { return []; },
    createElement(tag) { return makeElement(tag); },
    addEventListener() {},
    documentElement,
    body: { appendChild() {}, removeChild() {} },
  };
  const localStorage = {
    getItem(key) { return key === 'wbTheme' ? (savedTheme || null) : null; },
    setItem() {},
  };
  const window = {
    __errs: [],
    addEventListener() {},
    // 永不完成：测试只需要 done 事件，不需要真的发起成长中心刷新
    pywebview: { api: { growth: () => new Promise(() => {}) } },
    localStorage,
  };
  const context = {
    window,
    document,
    localStorage,
    navigator: {},
    console,
    setTimeout,
    clearTimeout,
    Promise,
    Date,
    Number,
    String,
    Array,
    Object,
    JSON,
    Set,
  };
  window.window = window;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(APP, 'utf8'), context, { filename: APP });
  return { el, dds, window, documentElement, context };
}

function seedStatus(h) {
  h.el('vCredit').textContent = '100';
  h.el('hCredit').textContent = '今日已签到';
  h.el('vTotal').textContent = '1234';
  h.dds[0].textContent = '是';
  h.dds[1].textContent = '9';
  h.dds[2].textContent = '1234';
}

function assertStatusKept(h) {
  assert.strictEqual(h.el('vCredit').textContent, '100');
  assert.strictEqual(h.el('hCredit').textContent, '今日已签到');
  assert.strictEqual(h.el('vTotal').textContent, '1234');
  assert.strictEqual(h.dds[0].textContent, '是');
  assert.strictEqual(h.dds[1].textContent, '9');
  assert.strictEqual(h.dds[2].textContent, '1234');
}

function testGrowthDoneKeepsCheckinData() {
  const h = boot();
  seedStatus(h);

  h.window.__onBackend('done', {
    action: 'growth',
    exit: 0,
    payload: {
      result: 'GROWTH',
      report: '成长中心无可领取项（能量 10，连签 9 天）',
      energy: 10,
      streak_days: 9,
    },
  });

  assertStatusKept(h);
}

function testFailedCommandKeepsCheckinData() {
  const h = boot();
  seedStatus(h);

  h.window.__onBackend('done', {
    action: 'auto',
    exit: 1,
    payload: { result: 'NETWORK', report: '网络不可达，签到跳过' },
  });

  assertStatusKept(h);
}

function testStatusDoneStillUpdates() {
  const h = boot();
  seedStatus(h);

  h.window.__onBackend('done', {
    action: 'status',
    exit: 0,
    payload: {
      step: 'status',
      http: 200,
      body: {
        code: 0,
        data: {
          today_checked_in: true,
          today_credit: 120,
          streak_days: 10,
          total_credits: 2000,
          is_streak_day: false,
          week_checkin_days: 3,
          activity_name: '每日签到',
        },
      },
    },
  });

  assert.strictEqual(h.el('vCredit').textContent, '120');
  assert.strictEqual(h.el('vTotal').textContent, '2000');
  assert.strictEqual(h.dds[0].textContent, '是');
  assert.strictEqual(h.dds[1].textContent, '10');
  assert.strictEqual(h.dds[2].textContent, '2000');
  assert.strictEqual(h.dds[4].textContent, '3 / 7 天');
  assert.strictEqual(h.dds[5].textContent, '每日签到');
  assert.strictEqual(h.el('wbAccounts').childElementCount, 1);
  assert.ok(h.el('wbAccountAt').textContent.startsWith('最近读取'));
}

function testThemeFollowsSystemWhenNotOverridden() {
  const h = boot();
  h.window.__onBackend('theme', { dark: true });
  assert.strictEqual(h.documentElement.dataset.theme, 'dark');
  h.window.__onBackend('theme', { dark: false });
  assert.strictEqual(h.documentElement.dataset.theme, 'light');
}

function testThemeFollowsSystemInExplicitSystemMode() {
  const h = boot('system');
  h.documentElement.dataset.theme = 'light';      // 模拟首屏内联脚本的结果
  h.window.__onBackend('theme', { dark: true });
  assert.strictEqual(h.documentElement.dataset.theme, 'dark');
}

function testThemeKeepsManualChoice() {
  const h = boot('light');
  h.documentElement.dataset.theme = 'light';      // 模拟首屏内联脚本的结果
  h.window.__onBackend('theme', { dark: true });
  assert.strictEqual(h.documentElement.dataset.theme, 'light');
}

function testAccountRegionDefaults() {
  const h = boot();
  const missing = vm.runInContext('accountRegionInfo({})', h.context);
  assert.strictEqual(missing.kind, 'cn');
  assert.strictEqual(missing.label, '国内');

  const intl = vm.runInContext("accountRegionInfo({ region: 'SG' })", h.context);
  assert.strictEqual(intl.kind, 'intl');
  assert.strictEqual(intl.label, '国际');
}

function testSideStatusUsesUnifiedWording() {
  const h = boot();

  vm.runInContext("qoderSideState({ result: 'OK', campaigns: "
    + "[{ campaign: 'daily', active: true, status: 'CLAIMABLE' }] }, "
    + "{ result: 'OK' }, true)", h.context);
  assert.strictEqual(h.el('qoderSideHint').textContent, '未领取');
  assert.strictEqual(h.el('qoderDot').className, 'side-dot ok');

  vm.runInContext("qoderSideState({ result: 'OK', campaigns: "
    + "[{ campaign: 'daily', active: true, status: 'CLAIMED' }] }, "
    + "{ result: 'OK' }, true)", h.context);
  assert.strictEqual(h.el('qoderSideHint').textContent, '已领取');
  assert.strictEqual(h.el('qoderDot').className, 'side-dot ok');

  vm.runInContext("renderFromStatus({ result: 'OK', today_checked_in: false, "
    + "today_credit: 0, streak_days: 2, total_credits: 10, "
    + "checkin_dates: ['2026-10-22'] })", h.context);
  assert.strictEqual(h.el('wbSideHint').textContent, '未签到');
  assert.strictEqual(h.el('wbDot').className, 'side-dot ok');

  vm.runInContext("renderFromStatus({ result: 'ALREADY', today_checked_in: true, "
    + "today_credit: 100, streak_days: 3, total_credits: 110, "
    + "checkin_dates: ['2026-10-22'] })", h.context);
  assert.strictEqual(h.el('wbSideHint').textContent, '已签到');

  vm.runInContext("TRAE_DATA = { ok: true, active_id: 'a', "
    + "history: { today: '2026-10-22' }, accounts: "
    + "[{ id: 'a', enabled: true, last_checkin: '2026-10-22' }] }; "
    + "TRAE_BUSY = false;", h.context);
  vm.runInContext('traeSideState()', h.context);
  assert.strictEqual(h.el('traeSideHint').textContent, '已签到');
  assert.strictEqual(h.el('traeDot').className, 'side-dot ok');

  vm.runInContext("TRAE_DATA = { ok: true, active_id: 'a', "
    + "history: { today: '2026-10-22' }, accounts: "
    + "[{ id: 'a', enabled: true, last_checkin: '' }] };", h.context);
  vm.runInContext('traeSideState()', h.context);
  assert.strictEqual(h.el('traeSideHint').textContent, '未签到');
  assert.strictEqual(h.el('traeDot').className, 'side-dot ok');
}

function testStartupPrefetchQueuesBothPages() {
  const h = boot();
  const calls = [];
  h.context.ensureQoder = () => calls.push('qoder');
  h.context.ensureTrae = () => calls.push('trae');
  h.window.requestIdleCallback = (fn) => fn();
  h.context.setTimeout = (fn) => fn();       // 第二个页面按错开延时入队

  vm.runInContext(
    'scheduleIntegrationPrefetch({ qoder_exists: true, trae_exists: true })',
    h.context);
  assert.deepStrictEqual(calls, ['qoder', 'trae']);
}

function testEnsureRefreshesStalePageData() {
  const h = boot();
  const calls = [];
  h.context.refreshQoder = () => { calls.push('monitor'); return Promise.resolve(); };
  h.context.refreshQoderService = () => { calls.push('service'); return Promise.resolve(); };
  h.context.scheduleQoderServicePoll = () => {};
  // QODER_* 是 let 绑定，必须通过 vm 赋值，不能挂到 sandbox 对象上
  vm.runInContext(
    'QODER_LOADED = true; QODER_LOADING = false; QODER_AT = Date.now();',
    h.context);

  vm.runInContext('ensureQoder()', h.context);
  assert.deepStrictEqual(calls, [], '新数据不应在切页时重复查询');

  vm.runInContext('QODER_AT = Date.now() - 61 * 1000;', h.context);
  vm.runInContext('ensureQoder()', h.context);
  assert.deepStrictEqual(calls, ['monitor', 'service'], '过期数据应静默补拉');
}

function testPlanTaskHeadingAndTraeOrder() {
  const html = fs.readFileSync(INDEX, 'utf8');
  const headings = Array.from(html.matchAll(/<h2>([^<]+)<\/h2>/g), (m) => m[1]);
  assert.strictEqual(headings.filter((x) => x === '计划任务').length, 3);
  assert.ok(!headings.includes('脚本服务'));
  assert.ok(!headings.includes('Qoder 计划任务'));
  assert.ok(!headings.includes('Trae 计划任务'));

  const start = html.indexOf('id="page-trae"');
  const end = html.indexOf('</main>', start);
  const page = html.slice(start, end);
  const accounts = page.indexOf('id="traeAccounts"');
  const service = page.indexOf('id="tsvcState"');
  const status = page.indexOf('id="traeKv"');
  assert.ok(accounts >= 0 && service > accounts && status > service,
    'Trae 计划任务应位于账号卡片下方、签到状态上方');
}

const tests = [
  ['growth 完成不覆盖签到数据', testGrowthDoneKeepsCheckinData],
  ['失败结果不覆盖签到数据', testFailedCommandKeepsCheckinData],
  ['status 完成仍正常更新', testStatusDoneStillUpdates],
  ['系统主题变化时跟随', testThemeFollowsSystemWhenNotOverridden],
  ['显式跟随系统时响应变化', testThemeFollowsSystemInExplicitSystemMode],
  ['手动主题不被系统覆盖', testThemeKeepsManualChoice],
  ['账号区域缺省为国内、SG 为国际', testAccountRegionDefaults],
  ['侧边栏状态统一为已签到/未签到、已领取/未领取', testSideStatusUsesUnifiedWording],
  ['启动后自动预取 Qoder 与 Trae', testStartupPrefetchQueuesBothPages],
  ['页面数据过期后切页静默补拉', testEnsureRefreshesStalePageData],
  ['三个计划任务标题一致且 Trae 卡片位于账号下方', testPlanTaskHeadingAndTraeOrder],
];

for (const [name, fn] of tests) {
  fn();
  console.log('  ✓ ' + name);
}
console.log('\n[OK] 前端事件回归测试通过');
