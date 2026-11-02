/* 侧边栏状态点回归测试。
 *
 * 背景：Qoder 卸载/没登录时，侧边栏仍可能显示绿点——因为绿点原来只看
 * 「登录态能不能读」。卸载后本地残留的凭据照样能通过 doctor，于是
 * 一个已经不存在的桌面端被画成「已连接」。
 *
 * 现在状态点必须同时看后端下发的 desktop.installed：
 *   绿 可用 / 黄 未登录 / 灰 未安装 / 红 异常
 * 唯一例外是显式配置了 QODER_TOKEN（没有桌面端也算能连）。
 *
 * 用 Node 自带 vm 加载 app.js，不引入 jsdom，也不启动 WebView2。
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
    title: '',
    innerHTML: '',
    hidden: false,
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
    querySelectorAll() { return []; },
  };
  return el;
}

function boot() {
  const elements = {};
  const el = (id) => {
    if (!elements[id]) elements[id] = makeElement(id);
    return elements[id];
  };

  const document = {
    getElementById: el,
    querySelectorAll() { return []; },
    createElement(tag) { return makeElement(tag); },
    addEventListener() {},
    documentElement: { dataset: {}, style: { setProperty() {} } },
    body: { appendChild() {}, removeChild() {} },
  };
  const localStorage = { getItem() { return null; }, setItem() {} };
  // 启动期的任何后台调用都返回永不完成的 Promise：本测试只关心状态点
  const api = new Proxy({}, {
    get: () => () => new Promise(() => {}),
  });
  const window = {
    __errs: [],
    addEventListener() {},
    pywebview: { api },
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
  return { el, context };
}

/* 未安装：即使 doctor 还能读到残留凭据，也必须显示灰点而不是绿点 */
function testNotInstalledIsNotGreen() {
  const h = boot();
  const dot = h.context.qoderDotState(
    { result: 'OK' },
    { result: 'OK', report: '登录态可读' },
    true,
    { installed: false, running: false },
  );
  assert.strictEqual(dot.kind, 'off', '未安装不能是绿点');
  assert.strictEqual(dot.missing, true);
  assert.ok(dot.title.indexOf('未检测到 Qoder CN') >= 0,
    '提示要说明本机没装桌面端');
}

/* 已安装、登录态可读、但桌面端没运行：绿点 + 说明用的是缓存登录态 */
function testInstalledNotRunningKeepsGreen() {
  const h = boot();
  const dot = h.context.qoderDotState(
    null,
    { result: 'OK', report: '登录态可读' },
    true,
    { installed: true, running: false },
  );
  assert.strictEqual(dot.kind, 'ok');
  assert.ok(dot.title.indexOf('桌面端未运行') >= 0,
    '未运行时要说明凭据来自缓存，避免误解成进程已连上');
}

/* 已安装未登录：黄点 */
function testInstalledNotLoggedInIsWarn() {
  const h = boot();
  const dot = h.context.qoderDotState(
    null,
    { result: 'NO_AUTH', report: '尚未登录' },
    false,
    { installed: true, running: false },
  );
  assert.strictEqual(dot.kind, 'warn');
  assert.ok(dot.title.indexOf('未登录') >= 0);
}

/* 显式 QODER_TOKEN：没有桌面端也算能连 */
function testEnvTokenWithoutDesktopIsGreen() {
  const h = boot();
  const dot = h.context.qoderDotState(
    null,
    { result: 'OK', token_source: 'env', report: '使用环境变量 QODER_TOKEN' },
    false,
    { installed: false, running: false },
  );
  assert.strictEqual(dot.kind, 'ok');
  assert.ok(dot.title.indexOf('QODER_TOKEN') >= 0,
    '要说明绿色来自环境变量，而不是桌面端');
}

/* 侧边栏落地：文字变「未安装」，圆点 class / title / aria-label 同步 */
function testQoderSidebarShowsUninstalled() {
  const h = boot();
  h.context.applyDesktopInfo({ qoder: { installed: false, running: false } });
  h.context.qoderSideState({ result: 'NO_AUTH' }, { result: 'NO_AUTH' }, false);

  const hint = h.el('qoderSideHint');
  assert.strictEqual(hint.textContent, '未安装');

  const dot = h.el('qoderDot');
  assert.ok(dot.className.indexOf('side-dot') === 0);
  assert.ok(dot.className.indexOf('off') > 0, '未安装要用 off 样式');
  assert.ok(dot.title.length > 0, '圆点自身要有 title');
  assert.strictEqual(dot['aria-label'], dot.title);
  assert.strictEqual(dot['data-state'], 'off');
}

/* 已安装但没登录：文字直接说「未登录」，不写含糊的「未领取」 */
function testQoderNotLoggedInShowsReason() {
  const h = boot();
  h.context.applyDesktopInfo({ qoder: { installed: true, running: false } });
  h.context.qoderSideState(null, { result: 'NO_AUTH' }, false);
  assert.strictEqual(h.el('qoderSideHint').textContent, '未登录');
  assert.strictEqual(h.el('qoderDot').className, 'side-dot warn');
}

/* Trae：卸载后不能因为本地账号缓存继续显示绿点 */
function testTraeUninstalledIsOff() {
  const h = boot();
  const data = {
    ok: true,
    accounts: [{ enabled: true, token_expired: false, has_session: true }],
  };
  const installed = h.context.traeDotState(data, { installed: true, running: true });
  assert.strictEqual(installed.kind, 'ok');

  const gone = h.context.traeDotState(data, { installed: false, running: false });
  assert.strictEqual(gone.kind, 'off');
  assert.strictEqual(gone.missing, true);
}

/* WorkBuddy 同理：没装桌面端时不能是绿点 */
function testWorkbuddyUninstalledIsOff() {
  const h = boot();
  const ok = h.context.wbDotState({ result: 'AUTH_READY' }, null,
    { installed: true, running: true });
  assert.strictEqual(ok.kind, 'ok');

  const gone = h.context.wbDotState({ result: 'AUTH_READY' }, null,
    { installed: false, running: false });
  assert.strictEqual(gone.kind, 'off');
}

/* 探测不可用（null / 非 Windows）时不能误判成未安装 */
function testUnknownDesktopFallsBackToAuth() {
  const h = boot();
  const dot = h.context.qoderDotState(
    null,
    { result: 'OK', report: '登录态可读' },
    false,
    null,
  );
  assert.strictEqual(dot.kind, 'ok');
  assert.strictEqual(dot.missing, false);
}

/* 图例和实际颜色必须对得上：
 * 旧实现把文字用的深琥珀 --warn 直接拿来画圆点，图例却写「黄」，
 * 小尺寸下看起来像褐色；后来又一度写成「橙」。这里把颜色钉死为亮黄。 */
function testLegendMatchesDotColors() {
  const css = fs.readFileSync(path.join(__dirname, 'assets', 'style.css'), 'utf8');
  const html = fs.readFileSync(path.join(__dirname, 'assets', 'index.html'), 'utf8');

  const hit = css.match(/--dot-warn:\s*#([0-9a-f]{6})/i);
  assert.ok(hit, '状态点必须有自己的黄色变量，不能复用文字色 --warn');
  const r = parseInt(hit[1].slice(0, 2), 16);
  const g = parseInt(hit[1].slice(2, 4), 16);
  const b = parseInt(hit[1].slice(4, 6), 16);
  assert.ok(r >= 230 && g >= 160 && b <= 120,
    '未登录点必须是亮黄色，当前 #' + hit[1]);

  assert.ok(/\.side-dot\.warn\s*\{[^}]*var\(--dot-warn\)/.test(css),
    'warn 圆点必须使用 --dot-warn');
  assert.ok(/\.side-dot\.off\s*\{[^}]*transparent/.test(css),
    '未安装必须是空心点，不能是实心灰');

  ['可用', '未登录', '未安装', '异常'].forEach((label) => {
    assert.ok(html.indexOf('>' + label + '<') >= 0,
      '图例缺少「' + label + '」');
  });
  assert.ok(html.indexOf('黄点') >= 0, '图例说明必须写明「黄点」');
  assert.ok(html.indexOf('橙点') < 0, '图例不能再写「橙点」，实际是亮黄');
}

function main() {
  const tests = [
    testNotInstalledIsNotGreen,
    testInstalledNotRunningKeepsGreen,
    testInstalledNotLoggedInIsWarn,
    testEnvTokenWithoutDesktopIsGreen,
    testQoderSidebarShowsUninstalled,
    testQoderNotLoggedInShowsReason,
    testTraeUninstalledIsOff,
    testWorkbuddyUninstalledIsOff,
    testUnknownDesktopFallsBackToAuth,
    testLegendMatchesDotColors,
  ];
  tests.forEach((fn) => {
    fn();
    console.log('  ok  ' + fn.name);
  });
  console.log('状态点回归测试通过（' + tests.length + ' 项）');
}

main();
