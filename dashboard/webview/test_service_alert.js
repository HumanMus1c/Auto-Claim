/* 计划任务卡片失败提示回归测试。
 *
 * 背景：Trae / Qoder 的计划任务安装失败时，卡片只是退回「未安装」，
 * 真实原因（例如本机没有 pythonw.exe）只留在日志面板一行——用户看到的就是
 * 「点了没反应」。现在失败必须在卡片上给出「是什么问题 + 怎么办」。
 *
 * 这里用 Node 自带 vm + 最小 DOM 桩直接驱动 app.js 里的三个提示函数，
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
    hidden: false,
    title: '',
    children: [],
    childElementCount: 0,
    style: { setProperty() {} },
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    },
    appendChild(child) {
      el.children.push(child);
      el.childElementCount = el.children.length;
      return child;
    },
    removeChild(child) {
      el.children = el.children.filter((item) => item !== child);
      el.childElementCount = el.children.length;
    },
    append() {},
    setAttribute(name, value) { el[name] = value; },
    focus() {},
    querySelector() { return makeElement('query'); },
    querySelectorAll() { return []; },
  };
  // 真实 DOM 里给 textContent 赋值会清空子节点，paintServiceAlert 依赖这一点；
  // 桩里不模拟的话「收起提示」会残留旧节点。
  Object.defineProperty(el, 'textContent', {
    get() { return el._text || ''; },
    set(value) {
      el._text = value;
      el.children = [];
      el.childElementCount = 0;
    },
  });
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
  // 任何后台调用都返回永不完成的 Promise：本测试只关心提示渲染
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

function testInstallFailureShowsReadableHint() {
  const h = boot();
  h.context.setServiceAlert('tsvcAlert', {
    ok: false,
    code: 'python-missing',
    error: '缺失 Python 3 运行环境，计划任务无法启动',
    hint: '安装 Python 3 后重试（winget install -e --id Python.Python.3.12）。',
    detail: 'PowerShell 退出码 1：未找到 pythonw.exe',
  });

  const el = h.el('tsvcAlert');
  assert.strictEqual(el.hidden, false, '失败后提示必须可见');
  assert.strictEqual(el.children.length, 3, '应有标题、解决办法、详情三块');

  const title = el.children[0];
  assert.strictEqual(title.textContent, '缺失 Python 3 运行环境，计划任务无法启动');

  const hint = el.children[1];
  assert.strictEqual(hint.className, 'svc-alert-hint');
  assert.ok(hint.textContent.indexOf('Python 3') >= 0, '解决办法必须给出可执行动作');

  const details = el.children[2];
  assert.strictEqual(details.children[0].textContent, '技术详情');
  assert.ok(details.children[1].textContent.indexOf('pythonw') >= 0,
    '原始报错要保留在详情里');
}

function testHintFallsBackToCode() {
  const h = boot();
  // 后端没给 hint 时，按 code 查表兜底
  h.context.setServiceAlert('svcAlert', {
    ok: false, code: 'access-denied', error: 'Windows 拒绝写入计划任务（拒绝访问）',
  });
  const hint = h.el('svcAlert').children[1];
  assert.ok(hint.textContent.indexOf('管理员') >= 0,
    'access-denied 应给出提权建议');

  h.context.setServiceAlert('qsvcAlert', {
    ok: false, code: 'no-such-code', error: '未知故障',
  });
  const fallback = h.el('qsvcAlert').children[1];
  assert.ok(fallback.textContent.length > 0, '未知 code 也要有兜底话术');
}

function testSuccessClearsAlert() {
  const h = boot();
  h.context.setServiceAlert('qsvcAlert', { ok: false, error: '安装失败' });
  assert.strictEqual(h.el('qsvcAlert').hidden, false);

  h.context.setServiceAlert('qsvcAlert', null);
  assert.strictEqual(h.el('qsvcAlert').hidden, true, '成功后提示必须收起');
  assert.strictEqual(h.el('qsvcAlert').children.length, 0);
}

function testStatusFailureLabeledAndRecovers() {
  const h = boot();
  h.context.syncServiceAlert('svcAlert',
    { ok: false, code: 'ps-failed', error: 'PowerShell 执行失败' },
    '读取计划任务状态');

  const el = h.el('svcAlert');
  assert.strictEqual(el.hidden, false);
  assert.ok(el.children[0].textContent.indexOf('读取计划任务状态失败') === 0,
    '读状态失败要带上前缀，说明是查询而非安装失败');

  // 状态恢复：来自状态的提示应自动消失
  h.context.syncServiceAlert('svcAlert', { ok: true }, '读取计划任务状态');
  assert.strictEqual(el.hidden, true);
}

function testUnresolvedInstallAlertSurvivesRefresh() {
  const h = boot();
  h.context.setServiceAlert('tsvcAlert', {
    ok: false, code: 'python-missing', error: '缺失 Python 3 运行环境',
  });
  // 后台刷新会带着 ok 的状态回来，但不能顺手把「安装失败」也抹掉
  h.context.syncServiceAlert('tsvcAlert', { ok: true }, '读取 Trae 计划任务状态');

  const el = h.el('tsvcAlert');
  assert.strictEqual(el.hidden, false, '安装失败提示要留到用户解决为止');
  assert.ok(el.children[0].textContent.indexOf('缺失 Python 3') >= 0);
}

function testStatusRenderShowsAlert() {
  const h = boot();
  h.context.renderTraeService({
    supported: true,
    ok: false,
    code: 'ps-failed',
    error: 'PowerShell 执行失败',
    hint: '请展开技术详情查看原始报错。',
    tasks: [],
  });

  const el = h.el('tsvcAlert');
  assert.strictEqual(el.hidden, false, '读状态失败也要在卡片上提示');
  assert.ok(el.children[0].textContent.indexOf('读取 Trae 计划任务状态失败') === 0);
  assert.strictEqual(h.el('tsvcState').textContent, '读取失败');
}

const TESTS = [
  ['安装失败给出可读提示与解决办法', testInstallFailureShowsReadableHint],
  ['缺少 hint 时按 code 兜底', testHintFallsBackToCode],
  ['安装成功后收起提示', testSuccessClearsAlert],
  ['读状态失败带前缀并在恢复后消失', testStatusFailureLabeledAndRecovers],
  ['未解决的安装失败不被刷新覆盖', testUnresolvedInstallAlertSurvivesRefresh],
  ['渲染状态失败时卡片显示提示', testStatusRenderShowsAlert],
];

let failed = 0;
for (const [name, fn] of TESTS) {
  try {
    fn();
    console.log('  ok   ' + name);
  } catch (err) {
    failed += 1;
    console.log('  FAIL ' + name);
    console.log('       ' + (err && err.message));
  }
}

if (failed) {
  console.log('\n' + failed + ' 项失败');
  process.exit(1);
}
console.log('\n全部通过（' + TESTS.length + ' 项）');
