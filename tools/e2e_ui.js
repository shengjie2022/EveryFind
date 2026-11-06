/**
 * EveryFind 浏览器端到端验收：Chrome + CDP 发真实鼠标事件。
 *
 * 为什么不用内置 Browser：它点不动（点全部无效），只能自己驱动 CDP。
 * 三个坑（都踩过）：
 *   1. 调试端口必须随机（9400+rand(400)）—— 固定端口会让残留 Chrome 实例抢端口，
 *      CDP 连到上一个实例，表现为「元素莫名其妙不存在」。
 *   2. 点击前必须 scrollIntoView 并等一帧 —— 目标在视口外时鼠标事件被浏览器丢弃。
 *   3. 要点最后一个匹配元素 —— 聊天区会堆好几条回答，querySelector 只拿第一条。
 *
 * 用法：
 *   node tools\e2e_ui.js <应用地址>
 * 默认地址 http://127.0.0.1:8765/
 */
const { spawn } = require('child_process');
const os = require('os');
const path = require('path');
const fs = require('fs');

const CHROME = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
].find((p) => fs.existsSync(p));

const URL_APP = process.argv[2] || 'http://127.0.0.1:8765/';
const PORT = 9400 + Math.floor(Math.random() * 400);
const profile = path.join(os.tmpdir(), 'ef_cdp_' + Date.now() + '_' + PORT);

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok: !!ok, detail });
  console.log((ok ? 'PASS  ' : 'FAIL  ') + name + (detail ? '   ' + detail : ''));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  if (!CHROME) { console.log('找不到 Chrome/Edge'); process.exit(1); }
  console.log('浏览器:', CHROME, ' 端口:', PORT);

  const proc = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + profile,
    '--window-size=1400,1000', 'about:blank',
  ], { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 60; i++) {
    await sleep(500);
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const page = list.find((t) => t.type === 'page');
      if (page) { target = page; break; }
    } catch (e) { /* retry */ }
  }
  if (!target) { console.log('CDP 未就绪'); proc.kill(); process.exit(1); }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const consoleErrors = [];
  const navs = [];
  const searchReqs = [];   // 应用自己发出的 /api/search 请求，用来验「范围真的带上了」

  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    } else if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
      consoleErrors.push(msg.params.args.map((a) => a.value || a.description || '').join(' '));
    } else if (msg.method === 'Runtime.exceptionThrown') {
      consoleErrors.push('EXC: ' + (msg.params.exceptionDetails.exception?.description || ''));
    } else if (msg.method === 'Page.frameNavigated') {
      navs.push(msg.params.frame.url || '');
    } else if (msg.method === 'Network.requestWillBeSent') {
      const u = msg.params.request.url || '';
      if (u.includes('/api/search')) searchReqs.push(u);
    }
  });
  await new Promise((r) => ws.addEventListener('open', r));

  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });

  const evaluate = async (expr) => {
    const r = await send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise: true,
    });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'eval error');
    return r.result.value;
  };

  const clickAt = async (x, y) => {
    await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', buttons: 0 });
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 });
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 });
    await sleep(450);
  };

  const realClick = async (sel) => {
    await evaluate(`(() => {
      const el = document.querySelector(${JSON.stringify(sel)});
      if (el) el.scrollIntoView({block: 'center'});
      return !!el;
    })()`).catch(() => {});
    await sleep(500);
    const box = await evaluate(`(() => {
      const el = document.querySelector(${JSON.stringify(sel)});
      if (!el) return null;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return null;   // 祖先被隐藏时矩形全 0
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()`);
    if (!box) return false;
    await clickAt(box.x, box.y);
    return true;
  };

  const realClickLast = async (sel) => {
    const ok = await evaluate(`(() => {
      const arr = Array.from(document.querySelectorAll(${JSON.stringify(sel)}));
      const el = arr[arr.length - 1];
      if (!el) return false;
      el.scrollIntoView({block: 'center'});
      return true;
    })()`);
    if (!ok) return false;
    await sleep(500);
    const pos = await evaluate(`(() => {
      const arr = Array.from(document.querySelectorAll(${JSON.stringify(sel)}));
      const el = arr[arr.length - 1];
      if (!el) return null;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return null;
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()`);
    if (!pos) return false;
    await clickAt(pos.x, pos.y);
    return true;
  };

  const realType = async (sel, text) => {
    await realClick(sel);
    // 先清空：前面的步骤（点历史问题）可能往输入框里塞了内容
    await evaluate(`(() => {
      const el = document.querySelector(${JSON.stringify(sel)});
      if (el) { el.value = ''; el.dispatchEvent(new Event('input')); }
      return true;
    })()`);
    await send('Input.insertText', { text });
    await sleep(400);
  };

  const lastAnswer = async (sel) => evaluate(
    `(() => { const a = Array.from(document.querySelectorAll('#chat ${sel}')); const e = a[a.length - 1]; return e ? e.innerText : ''; })()`);

  const nav = async () => {
    await send('Page.navigate', { url: URL_APP });
    for (let i = 0; i < 40; i++) {
      await sleep(250);
      const ok = await evaluate(
        `!!document.querySelector('#overlay-source') && !!document.querySelector('#btn-report') && !!document.querySelector('#ask-input')`
      ).catch(() => false);
      if (ok) { await sleep(700); return true; }
    }
    return false;
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');

  console.log('\n=== 0. 打开应用 ===');
  check('应用 DOM 加载完整', await nav());

  console.log('\n=== 1. 目录分组与目录限定 ===');
  const dirHeads = await evaluate(`Array.from(document.querySelectorAll('#filelist .dir-head')).map(e => e.dataset.dir)`);
  check('侧栏按目录分组', Array.isArray(dirHeads) && dirHeads.length > 0, JSON.stringify(dirHeads));
  check('目录标题带文件数', await evaluate(`!!document.querySelector('#filelist .dir-head .dc')`));
  if (dirHeads && dirHeads.length) {
    const d = dirHeads[0];
    await realClick('#filelist .dir-head');
    await sleep(700);
    check('点目录出现范围条', await evaluate(`!document.querySelector('#scope-bar').classList.contains('hidden')`));
    const nm = await evaluate(`document.querySelector('#scope-name').textContent`);
    check('范围条显示目录名', nm === d, nm + ' vs ' + d);
    const tip = await evaluate(`document.querySelector('#composer-tip').textContent`);
    check('提示文案说明限定目录', /仅在/.test(tip), tip);
    await realType('#ask-input', '预算是多少？');
    await realClick('#btn-send');
    await sleep(3000);
    const cites = await evaluate(`Array.from(document.querySelectorAll('#chat .answer-body .cite')).length`);
    check('目录限定下能正常作答', cites > 0, cites + ' 个引用');
    await realClick('#src-close');
    await sleep(300);
    await realClick('#scope-clear');
    await sleep(300);
    check('✕ 取消目录限定', await evaluate(`document.querySelector('#scope-bar').classList.contains('hidden')`));
  }

  console.log('\n=== 2. 搜索面板跟随检索范围 ===');
  await realClick('.tab[data-view="search"]');
  await sleep(400);
  await realType('#search-input', '预算');
  await realClick('#btn-search');
  await sleep(2000);
  const allHits = await evaluate(`document.querySelectorAll('#results .result').length`);
  check('全文搜索有结果', allHits > 0, allHits + ' 条');
  if (dirHeads && dirHeads.length) {
    const d = dirHeads[0];
    await realClick('#filelist .dir-head');
    await sleep(2200);
    const meta = await evaluate(`document.querySelector('#search-meta').textContent`);
    check('搜索说明里标出范围', /目录内/.test(meta), meta);
    check('搜索视图里显示范围条', await evaluate(`!document.querySelector('#scope-bar-search').classList.contains('hidden')`));

    // 弱断言的坑：限定前后的条数都被「最多 N 条」截断，条数相等 ≠ 没生效。
    // 真正要验三件事：① 应用自己发的请求带了 dir ② 服务端返回的命中全在该目录里
    // ③ 界面上显示的每一条都属于限定后的结果。
    const lastReq = searchReqs[searchReqs.length - 1] || '';
    check('应用发出的搜索请求带上了目录范围', /[?&]dir=/.test(lastReq), lastReq);
    const cmp = await evaluate(`(async () => {
      const D = ${JSON.stringify(d)};
      const q = encodeURIComponent('预算');
      const inDir = (p) => (p || '').indexOf('/' + D + '/') >= 0 || (p || '').indexOf('\\\\' + D + '\\\\') >= 0;
      const un = await (await fetch('/api/search?q=' + q + '&limit=300')).json();
      const sc = await (await fetch('/api/search?q=' + q + '&limit=300&dir=' + encodeURIComponent(D))).json();
      const dom = Array.from(document.querySelectorAll('#results .result')).map(e => String(e.dataset.chunk));
      const scSet = new Set((sc.hits || []).map(h => String(h.chunk_id)));
      const unHits = un.hits || [], scHits = sc.hits || [];
      return {
        domN: dom.length,
        domAllInScoped: dom.length > 0 && dom.every(i => scSet.has(i)),
        scopedAllInDir: scHits.length > 0 && scHits.every(h => inDir(h.file_path)),
        unscopedHasOutside: unHits.some(h => !inDir(h.file_path)),
        scopedN: scHits.length, unscopedN: unHits.length,
      };
    })()`);
    check('限定后命中的文件全在该目录（含子目录）里', cmp.scopedAllInDir, JSON.stringify(cmp));
    check('限定确实收窄了结果（未限定时有目录外的命中）', cmp.unscopedHasOutside, JSON.stringify(cmp));
    check('界面上每条命中都属于限定后的结果', cmp.domAllInScoped, JSON.stringify(cmp));

    await realClick('#scope-clear-search');
    await sleep(2200);
    const backHits = await evaluate(`document.querySelectorAll('#results .result').length`);
    check('取消限定后搜索结果恢复', backHits === allHits, `${cmp.domN} -> ${backHits}`);
    check('取消后搜索视图的范围条也隐藏了', await evaluate(`document.querySelector('#scope-bar-search').classList.contains('hidden')`));
  }
  await realClick('.tab[data-view="chat"]');
  await sleep(400);

  console.log('\n=== 3. 聚合计算问答 ===');
  await realType('#ask-input', '预算合计是多少？');
  await realClick('#btn-send');
  await sleep(3500);
  const calcMode = await evaluate(`(() => { const a = Array.from(document.querySelectorAll('#chat .answer-mode')); const e = a[a.length-1]; return e ? e.textContent : ''; })()`);
  check('聚合计算有专属模式标注', /聚合计算/.test(calcMode), calcMode);
  const calcText = await lastAnswer('.answer-body');
  check('聚合结果含具体金额', /\d/.test(calcText), JSON.stringify(calcText.slice(0, 140)));
  check('聚合结果带引用', await evaluate(`(() => { const a = Array.from(document.querySelectorAll('#chat .answer-body')); const e = a[a.length-1]; return e ? e.querySelectorAll('.cite').length > 0 : false; })()`));

  console.log('\n=== 4. 问答历史 ===');
  check('点击「历史」按钮', await realClick('#btn-history'));
  check('历史面板已打开', await evaluate(`!document.querySelector('#overlay-history').classList.contains('hidden')`));
  const histCount = await evaluate(`document.querySelectorAll('#hi-body .hi-item').length`);
  check('历史里有记录', histCount > 0, histCount + ' 条');
  check('历史显示模式徽章', await evaluate(`!!document.querySelector('#hi-body .hi-mode')`));
  await realClick('#hi-body .hi-q');
  await sleep(500);
  const filled = await evaluate(`document.querySelector('#ask-input').value`);
  check('点历史问题填入输入框', filled.length > 0, JSON.stringify(filled));
  check('填入后历史面板关闭', await evaluate(`document.querySelector('#overlay-history').classList.contains('hidden')`));
  await realClick('#btn-history');
  await sleep(600);
  const before = await evaluate(`document.querySelectorAll('#hi-body .hi-item').length`);
  await realClick('#hi-body .hi-act.danger');
  await sleep(900);
  const after = await evaluate(`document.querySelectorAll('#hi-body .hi-item').length`);
  check('可删除单条历史', after === before - 1, `${before} -> ${after}`);

  // 导出历史：以前用 window.location.href，服务端 4xx 时浏览器会跳到错误页，
  // 把整个应用界面弄丢。现在走 blob 下载，点完必须还停在应用里。
  const navBefore = navs.length;
  await realClick('#hi-export');
  await sleep(2000);
  check('导出历史后仍停留在应用里（没跳到错误页）', navs.length === navBefore,
    navs.slice(navBefore).join(' | '));
  check('导出后应用 DOM 还在', await evaluate(`!!document.querySelector('#ask-input')`));
  await realClick('#hi-close');
  await sleep(300);

  console.log('\n=== 5. 范围限定（文件级）===');
  const fileItem = await evaluate(`(() => {
    const el = document.querySelector('#filelist .file-item');
    if (!el) return null;
    return { id: el.dataset.file, name: el.querySelector('.fn').textContent };
  })()`);
  check('资料库有文件可点', !!fileItem, fileItem ? fileItem.name : '没有');
  if (fileItem) {
    await realClick('#filelist .file-item');
    await sleep(900);
    check('点文件出现范围条', await evaluate(`!document.querySelector('#scope-bar').classList.contains('hidden')`));
    const nm = await evaluate(`document.querySelector('#scope-name').textContent`);
    check('范围条显示文件名', nm === fileItem.name, nm);
    await realClick('#src-close');
    await sleep(300);
    await realClick('#scope-clear');
    await sleep(300);
    check('✕ 取消文件限定', await evaluate(`document.querySelector('#scope-bar').classList.contains('hidden')`));
  }

  console.log('\n=== 6. 体检报告 / 导出 / 追问 ===');
  await realClick('#btn-report');
  check('体检面板已打开', await evaluate(`!document.querySelector('#overlay-report').classList.contains('hidden')`));
  check('体检有统计块', await evaluate(`document.querySelectorAll('#rp-body .rp-stat').length > 0`));
  await realClick('#rp-close');

  await realType('#ask-input', '差旅费预算是多少？');
  await realClick('#btn-send');
  await sleep(3000);
  check('普通问答有回答', (await lastAnswer('.answer-body')).length > 10);
  check('出现「导出 Markdown」按钮', await evaluate(`!!document.querySelector('[data-export="md"]')`));
  check('出现「导出 Word」按钮', await evaluate(`!!document.querySelector('[data-export="docx"]')`));
  check('引用徽章可点', await evaluate(`(() => { const a = Array.from(document.querySelectorAll('#chat .answer-body')); const e = a[a.length-1]; return e ? e.querySelectorAll('.cite').length > 0 : false; })()`));

  await realClickLast('#chat .answer-body .cite');
  check('来源面板已打开', await evaluate(`!document.querySelector('#overlay-source').classList.contains('hidden')`));
  await realClick('#src-close');

  await realType('#ask-input', '那预算是多少？');
  await realClick('#btn-send');
  await sleep(3000);
  check('省略句触发上文扩展提示', await evaluate(`!!document.querySelector('#chat .expanded-hint')`));

  console.log('\n=== 7. 上传进度 / 重试面板存在 ===');
  check('存在上传进度条容器', await evaluate(`!!document.querySelector('#up-progress')`));
  check('存在上传失败重试容器', await evaluate(`!!document.querySelector('#up-retry')`));

  console.log('\n=== 8. 控制台 ===');
  check('无 JS 报错', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '));
  check('页面未发生意外跳转', navs.every((u) => u.startsWith(URL_APP.replace(/\/$/, ''))), navs.join(' | '));

  ws.close();
  proc.kill();
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) { /* ignore */ }

  const bad = results.filter((r) => !r.ok);
  console.log('\n========================================');
  console.log(`  ${results.length - bad.length}/${results.length} 通过`);
  if (bad.length) { console.log('失败项：'); bad.forEach((b) => console.log('  · ' + b.name + (b.detail ? '  ' + b.detail : ''))); }
  console.log('========================================');
  process.exit(bad.length ? 1 : 0);
})().catch((e) => { console.error('E2E 异常:', e); process.exit(1); });
