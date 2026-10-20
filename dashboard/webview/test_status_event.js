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
  return { el, dds, window, documentElement };
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

const tests = [
  ['growth 完成不覆盖签到数据', testGrowthDoneKeepsCheckinData],
  ['失败结果不覆盖签到数据', testFailedCommandKeepsCheckinData],
  ['status 完成仍正常更新', testStatusDoneStillUpdates],
  ['系统主题变化时跟随', testThemeFollowsSystemWhenNotOverridden],
  ['显式跟随系统时响应变化', testThemeFollowsSystemInExplicitSystemMode],
  ['手动主题不被系统覆盖', testThemeKeepsManualChoice],
];

for (const [name, fn] of tests) {
  fn();
  console.log('  ✓ ' + name);
}
console.log('\n[OK] 前端事件回归测试通过');
