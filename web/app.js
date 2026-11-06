/* EveryFind 前端逻辑 */
(function () {
  'use strict';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  const state = {
    view: 'chat',
    files: [],
    roots: [],
    cfg: null,
    lastQuestion: '',
    pickerMode: 'folder',
    pickerPath: '',
    pollTimer: null,
    // 多轮追问：保留最近几轮的问题，供后端做省略句扩展
    history: [],
    // 限定范围：只在某个文件里检索（null = 全部资料）
    scopeFileId: null,
    scopeName: '',
    // 限定范围：限定到某个目录（优先于文件）
    scopeDir: null,
    // 问答历史（服务端持久化，这里是界面缓存）
    historyItems: [],
    // 上传失败的条目，可一键重试
    uploadFailed: [],
  };

  /* ───────────────────────── 工具 ───────────────────────── */

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    const r = await fetch(path, init);
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail || detail; } catch (e) { /* noop */ }
      throw new Error(detail);
    }
    return r.json();
  }

  function toast(msg, ms) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.remove('hidden');
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.add('hidden'), ms || 2600);
  }

  function extClass(ext) {
    const e = (ext || '').replace('.', '').toLowerCase();
    if (['docx', 'docm', 'doc'].includes(e)) return 'ext-docx';
    if (['xlsx', 'xlsm', 'xls', 'csv'].includes(e)) return 'ext-xlsx';
    if (['pptx', 'pptm', 'ppt'].includes(e)) return 'ext-pptx';
    if (e === 'pdf') return 'ext-pdf';
    if (['jpg', 'jpeg', 'png', 'bmp', 'webp', 'tif', 'tiff', 'gif'].includes(e)) return 'ext-img';
    return 'ext-txt';
  }

  function extLabel(ext) {
    return (ext || '').replace('.', '').toUpperCase().slice(0, 4) || 'FILE';
  }

  function markText(text, spans) {
    if (!spans || !spans.length) return esc(text);
    const t = String(text || '');
    let out = '', last = 0;
    spans.forEach(function (sp) {
      const s = Math.max(0, sp[0]), e = Math.min(t.length, sp[1]);
      if (e <= s) return;
      if (s < last) return;
      out += esc(t.slice(last, s)) + '<mark>' + esc(t.slice(s, e)) + '</mark>';
      last = e;
    });
    out += esc(t.slice(last));
    return out;
  }

  /* ───────────────────────── 侧边栏 ───────────────────────── */

  function renderRoots() {
    const box = $('#roots');
    if (!state.roots.length) { box.innerHTML = ''; return; }
    box.innerHTML = state.roots.map(function (r) {
      const kind = r.kind === 'disk' ? '磁盘' : (r.kind === 'file' ? '文件' : '文件夹');
      return '<div class="root-item" title="' + esc(r.path) + '">' +
        '<span class="rk">' + kind + '</span>' +
        '<span class="rp">' + esc(r.path) + '</span>' +
        '<button class="x" data-root="' + r.id + '" title="移除">×</button></div>';
    }).join('');
  }

  function renderFiles() {
    const box = $('#filelist');
    if (!state.files.length) {
      box.innerHTML = '<div class="empty-side">还没有资料，先添加一个文件夹吧</div>';
      return;
    }
    // 按相对目录分组：同名文件在不同目录时要能分辨
    const groups = new Map();
    state.files.forEach(function (f) {
      const d = f.rel_dir || '';
      if (!groups.has(d)) groups.set(d, []);
      groups.get(d).push(f);
    });
    const dirs = Array.from(groups.keys()).sort();
    let html = '';
    dirs.forEach(function (d) {
      if (d) {
        const on = state.scopeDir === d;
        // 标题要说清限定范围含整棵子树：侧边栏显示的是本目录的文件数，
        // 而点它限定的是这个目录**加它下面所有子目录**
        const sub = state.files.filter(function (f) {
          const fd = f.rel_dir || '';
          return fd === d || fd.indexOf(d + '/') === 0;
        }).length;
        html += '<div class="dir-head' + (on ? ' on' : '') + '" data-dir="' + esc(d) + '" ' +
          'title="限定到这个目录及其子目录里的资料（共 ' + sub + ' 个文件）">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
          '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>' +
          '<span class="dn">' + esc(d) + '</span>' +
          '<span class="dc">' + groups.get(d).length + '</span></div>';
      }
      groups.get(d).forEach(function (f) {
        const st = f.status === 'ok' ? 'ok' : (f.status === 'error' ? 'err' : '');
        const stTxt = f.status === 'ok' ? (f.chunk_count + ' 块')
          : (f.status === 'error' ? '失败' : (f.status === 'indexing' ? '索引中' : '等待'));
        html += '<div class="file-item" data-file="' + f.id + '" data-path="' + esc(f.path) + '">' +
          '<span class="ext ' + extClass(f.ext) + '">' + esc(extLabel(f.ext)) + '</span>' +
          '<span class="fn" title="' + esc(f.path) + '">' + esc(f.name) + '</span>' +
          '<span class="st ' + st + '">' + stTxt + '</span></div>';
      });
    });
    box.innerHTML = html;
  }

  function renderStatus(s) {
    const c = s.counts || {};
    $('#stat-files').textContent = (c.ok || 0) + ' 个文件';
    $('#stat-chunks').textContent = (c.chunks || 0) + ' 个内容块';
    // 自动同步出错要能被看到。以前 watcher 把异常整个吞掉，
    // 同步坏了用户毫无察觉，只能以为「它就是不动」。
    if (s.sync_error && s.sync_error !== state.lastSyncError) {
      state.lastSyncError = s.sync_error;
      toast('自动同步出错：' + s.sync_error, 6000);
    }
    const p = s.progress || {};
    const box = $('#progress');
    if (p.state === 'running' || p.total > 0) {
      box.classList.remove('hidden');
      const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
      $('#progress-bar').style.width = pct + '%';
      $('#progress-text').textContent = '索引中 ' + (p.done || 0) + '/' + (p.total || 0) +
        (p.current ? ' · ' + p.current : '');
    } else {
      box.classList.add('hidden');
    }
  }

  async function refreshStatus() {
    try {
      const s = await api('/api/status');
      renderStatus(s);
      state.roots = s.roots || [];
      renderRoots();
    } catch (e) { /* noop */ }
  }

  async function refreshFiles() {
    try {
      const r = await api('/api/files');
      state.files = r.files || [];
      renderFiles();
    } catch (e) { /* noop */ }
  }

  /* ───────────────────────── 问答 ───────────────────────── */

  function pushMsg(html, cls) {
    const chat = $('#chat');
    if ($('#welcome')) $('#welcome').remove();
    const div = document.createElement('div');
    div.className = 'msg ' + cls;
    div.innerHTML = html;
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
    return div;
  }

  function renderAnswer(res, question) {
    const citations = res.citations || [];
    const cited = res.cited || [];
    const byN = {};
    citations.forEach(function (c) { byN[c.n] = c; });

    let body = esc(res.answer || '');
    body = body.replace(/\[(\d{1,3})\]/g, function (m, n) {
      const k = parseInt(n, 10);
      if (!byN[k]) return m;
      return '<button class="cite" data-cite="' + k + '" title="' +
        esc(byN[k].file_name + ' · ' + byN[k].loc_label) + '">' + k + '</button>';
    });

    const files = res.files || [];
    let html = '<div class="answer-card"><div class="answer-body">' + body + '</div>';
    if (files.length) {
      html += '<button class="evidence-toggle" data-toggle="1">▶ 查看依据 · ' + files.length + ' 个文件</button>';
      html += '<div class="evidence-list hidden">';
      cited.forEach(function (n) {
        const c = byN[n];
        if (!c) return;
        html += '<div class="evi" data-cite="' + n + '">' +
          '<span class="n">' + n + '</span><div style="min-width:0">' +
          '<div class="t">' + esc(c.file_name) + '<span>' + esc(c.loc_label) + '</span></div>' +
          '<div class="x">' + esc(c.snippet || '') + '</div>' +
          '</div></div>';
      });
      html += '</div>';
    }
    const modeTxt = res.mode === 'llm' ? '模型生成 · 已标注引用来源'
      : (res.mode === 'computed' ? '表格聚合计算 · 按原始数值精确计算'
      : (res.mode === 'empty' ? '' : (res.degraded ? '模型调用失败 · 已降级为原文摘录' : '未接入模型 · 原文摘录模式')));
    if (modeTxt) html += '<div class="answer-mode' + (res.degraded ? ' degraded' : '') + '">' + esc(modeTxt) + '</div>';
    html += '<div class="answer-actions">' +
      '<button data-export="md" title="导出为 Markdown">导出 Markdown</button>' +
      '<button data-export="docx" title="导出为 Word 文档">导出 Word</button>' +
      (res.expanded ? '<span class="expanded-hint">已结合上文扩展检索</span>' : '') +
      '</div>';
    html += '</div>';
    return html;
  }

  // 从 Content-Disposition 里取文件名：服务端用 RFC 5987 双写，
  // filename 给 ASCII 兜底名，filename* 给 UTF-8 真名
  function fileNameFrom(cd, fallback) {
    const star = /filename\*=UTF-8''([^;]+)/i.exec(cd || '');
    if (star) {
      try { return decodeURIComponent(star[1]); } catch (e) { /* 用兜底名 */ }
    }
    const m = /filename="([^"]+)"/.exec(cd || '');
    return m ? m[1] : fallback;
  }

  // 触发浏览器下载。不能用 window.location.href：
  // 服务端返回 4xx 时浏览器会直接跳到错误页，把整个应用界面弄丢。
  async function downloadFrom(url, fallbackName) {
    const r = await fetch(url);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const blob = await r.blob();
    const name = fileNameFrom(r.headers.get('Content-Disposition') || '', fallbackName);
    const a = document.createElement('a');
    const objUrl = URL.createObjectURL(blob);
    a.href = objUrl;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(objUrl); }, 5000);
    return name;
  }

  async function exportAnswer(res, question, format) {
    try {
      const cited = (res.cited || []);
      const r = await fetch('/api/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          format: format,
          question: question,
          answer: res.answer || '',
          mode: res.mode || '',
          citations: (res.citations || []).filter(function (c) {
            return cited.indexOf(c.n) >= 0;
          })
        })
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const blob = await r.blob();
      const name = fileNameFrom(r.headers.get('Content-Disposition') || '',
        ('EveryFind-问答.' + format));
      const a = document.createElement('a');
      const objUrl = URL.createObjectURL(blob);
      a.href = objUrl;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(objUrl); }, 5000);
      toast('已导出 ' + name);
    } catch (e) {
      toast('导出失败：' + e.message);
    }
  }

  async function ask(question) {
    question = (question || '').trim();
    if (!question) return;
    state.lastQuestion = question;
    pushMsg('<div class="bubble">' + esc(question) + '</div>', 'msg-user');
    const wait = pushMsg('<div class="answer-card"><div class="answer-body"><span class="typing"><i></i><i></i><i></i></span> 正在检索资料…</div></div>', 'msg-bot');
    $('#btn-send').disabled = true;
    try {
      const body = { question: question };
      // 多轮追问：把最近几轮的问题带上，后端据此扩展「那预算是多少」这类省略句
      const prev = state.history.slice(-3).map(function (h) { return h.question; });
      if (prev.length) body.prev_questions = prev;
      // 限定范围：只在选中的文件（或目录）里检索
      if (state.scopeFileId) body.file_id = state.scopeFileId;
      else if (state.scopeDir) body.dir = state.scopeDir;
      const res = await api('/api/ask', { method: 'POST', body: body });
      wait.__res = res;
      wait.__question = question;
      wait.innerHTML = renderAnswer(res, question);
      bindAnswer(wait, question);
      state.history.push({ question: question });
      if (state.history.length > 8) state.history.shift();
    } catch (e) {
      wait.innerHTML = '<div class="answer-card"><div class="answer-body">出错了：' + esc(e.message) + '</div></div>';
    } finally {
      $('#btn-send').disabled = false;
      $('#chat').scrollTop = $('#chat').scrollHeight;
    }
  }

  function bindAnswer(root, question) {
    const res = root.__res || {};
    $$('.cite', root).forEach(function (b) {
      b.addEventListener('click', function () {
        $$('.cite', root).forEach(function (x) { x.classList.remove('hit'); });
        b.classList.add('hit');
        openSource(parseInt(b.dataset.cite, 10), question, root);
      });
    });
    $$('.evi', root).forEach(function (row) {
      row.addEventListener('click', function () { openSource(parseInt(row.dataset.cite, 10), question, root); });
    });
    $$('[data-export]', root).forEach(function (b) {
      b.addEventListener('click', function () { exportAnswer(res, question, b.dataset.export); });
    });
    const tg = $('.evidence-toggle', root);
    if (tg) {
      tg.addEventListener('click', function () {
        const list = $('.evidence-list', root);
        const open = list.classList.toggle('hidden');
        tg.textContent = (open ? '▶' : '▼') + ' 查看依据 · ' +
          $$('.evi', root).length + ' 条引用';
      });
    }
  }

  /* ───────────────────────── 来源面板 ───────────────────────── */

  async function openSource(n, question, ctx) {
    let c = null;
    // 引用编号要对着「它所属的那条回答」解析。
    // 以前只留了全局的最后一次结果，点旧回答的引用就会用错问题，
    // 严重时还会重新调一次云端模型。
    const res = ctx ? ctx.__res : null;
    if (res && res.citations) {
      c = res.citations.find(function (x) { return x.n === n; });
    }
    try {
      let chunkId = c ? c.chunk_id : null;
      if (!chunkId) {
        // 兜底：只做检索、不调模型地重新解析一次
        const q = (ctx && ctx.__question) || question;
        const r = await api('/api/resolve', { method: 'POST', body: { question: q } });
        const hit = (r.citations || []).find(function (x) { return x.n === n; });
        chunkId = hit ? hit.chunk_id : null;
      }
      if (!chunkId) { toast('未找到该引用对应的原文'); return; }
      const d = await api('/api/source/' + chunkId + '?question=' + encodeURIComponent(question || ''));
      renderSource(d);
    } catch (e) {
      toast('打开来源失败：' + e.message);
    }
  }

  function renderSource(d) {
    $('#src-file').textContent = d.file_name;
    $('#src-loc').textContent = d.loc_label + '　·　' + d.file_path;

    const body = $('#src-body');
    body.innerHTML = '<div class="src-note">来自本文档的索引摘录，表格单元格与页码保留。</div>';

    let loc = {};
    try { loc = JSON.parse(d.loc_json || '{}'); } catch (e) { loc = {}; }

    if (d.loc_type === 'cell' && loc.cells && loc.cells.length) {
      let html = '<table class="src-table"><thead><tr><th>位置</th><th>字段</th><th>原值</th></tr></thead><tbody>';
      loc.cells.forEach(function (c) {
        const focus = d.focus_cell && d.focus_cell.ref === c.ref ? ' class="focus"' : '';
        html += '<tr' + focus + '><td class="ref">' + esc(c.ref) + '</td>' +
          '<td>' + esc(c.header || '') + '</td><td>' + esc(c.value) + '</td></tr>';
      });
      html += '</tbody></table>';
      html += '<div class="src-note" style="margin-top:12px">原始文本：' + markText(d.text, d.spans) + '</div>';
      body.innerHTML += html;
    } else {
      let extra = '';
      if (d.loc_type === 'slide' && loc.title) extra = '<div class="src-note">标题：' + esc(loc.title) + '</div>';
      if (d.loc_type === 'page' && loc.source === 'ocr') extra = '<div class="src-note">该页为扫描件，以上为本地 OCR 识别结果。</div>';
      if (d.loc_type === 'image') extra = '<div class="src-note">以下文字由本地 OCR 从图片中识别得到。</div>';
      body.innerHTML += extra +
        '<div class="src-block">' + markText(d.text, d.spans) + '</div>';
    }

    $('#src-open').onclick = function () { api('/api/open', { method: 'POST', body: { file_id: d.file_id } }).catch(function (e) { toast(e.message); }); };
    $('#src-reveal').onclick = function () { api('/api/reveal', { method: 'POST', body: { file_id: d.file_id } }).catch(function (e) { toast(e.message); }); };
    $('#overlay-source').classList.remove('hidden');
  }

  /* ───────────────────────── 全文搜索 ───────────────────────── */

  async function doSearch() {
    const q = $('#search-input').value.trim();
    if (!q) return;
    const box = $('#results');
    box.innerHTML = '<div class="empty-side">搜索中…</div>';
    try {
      const r = await api('/api/search?q=' + encodeURIComponent(q) + scopeQuery());
      const hits = r.hits || [];
      const scopeTxt = state.scopeFileId ? ('（仅「' + state.scopeName + '」内）')
        : (state.scopeDir ? ('（仅「' + state.scopeName + '」目录内）') : '');
      $('#search-meta').textContent = (hits.length ? '找到 ' + hits.length + ' 处相关内容' : '没有找到相关内容') + scopeTxt;
      if (!hits.length) { box.innerHTML = '<div class="empty-side">换个关键词试试</div>'; return; }
      box.innerHTML = hits.map(function (h) {
        return '<div class="result" data-chunk="' + h.chunk_id + '">' +
          '<div class="result-head"><span class="ext ' + extClass(h.file_ext) + '">' + esc(extLabel(h.file_ext)) + '</span>' +
          '<span class="fn">' + esc(h.file_name) + '</span>' +
          '<span class="loc">' + esc(h.loc_label) + '</span>' +
          '<span class="score">' + (h.score * 100).toFixed(0) + '%</span></div>' +
          '<div class="result-text">' + markText(h.snippet, h.spans) + '</div></div>';
      }).join('');
      $$('.result', box).forEach(function (el) {
        el.addEventListener('click', function () {
          const id = el.dataset.chunk;
          api('/api/source/' + id + '?question=' + encodeURIComponent(q)).then(renderSource).catch(function (e) { toast(e.message); });
        });
      });
    } catch (e) {
      box.innerHTML = '<div class="empty-side">搜索失败：' + esc(e.message) + '</div>';
    }
  }

  /* ───────────────────────── 检索范围 ───────────────────────── */

  function setScope(fileId, name) {
    state.scopeFileId = fileId || null;
    state.scopeDir = null;
    state.scopeName = name || '';
    applyScopeBar();
    refreshSearchIfActive();
  }

  function setScopeDir(dir, name) {
    state.scopeFileId = null;
    state.scopeDir = dir || null;
    state.scopeName = name || dir || '';
    applyScopeBar();
    renderFiles();          // 立刻把目录标题的高亮切过来，别等下一次轮询
    refreshSearchIfActive();
  }

  function clearScope() {
    state.scopeFileId = null;
    state.scopeDir = null;
    state.scopeName = '';
    applyScopeBar();
    renderFiles();
    refreshSearchIfActive();
  }

  // 范围变了，搜索面板里已有的结果就是过期的，重新跑一次
  function refreshSearchIfActive() {
    if (state.view !== 'search') return;
    const inp = $('#search-input');
    if (inp && inp.value.trim()) doSearch();
  }

  // 当前检索范围对应的查询串：限定到文件或目录时，搜索面板也要跟着收窄，
  // 否则会出现「已经限定到 2026 目录，搜索结果却全是 2025 的文件」这种自相矛盾。
  function scopeQuery() {
    if (state.scopeFileId) return '&file_id=' + encodeURIComponent(state.scopeFileId);
    if (state.scopeDir) return '&dir=' + encodeURIComponent(state.scopeDir);
    return '';
  }

  function applyScopeBar() {
    const bar = $('#scope-bar');
    const on = !!(state.scopeFileId || state.scopeDir);
    if (bar) {
      if (on) {
        $('#scope-name').textContent = state.scopeName;
        bar.classList.remove('hidden');
        $('#composer-tip').textContent = '仅在「' + state.scopeName + '」内检索 · 点范围条的 ✕ 可取消';
      } else {
        bar.classList.add('hidden');
        $('#composer-tip').textContent = 'Enter 发送 · Shift+Enter 换行';
      }
    }
    // 搜索视图里也挂一份：范围条在聊天区，切到搜索页就看不见、也清不掉
    const sbar = $('#scope-bar-search');
    if (sbar) {
      if (on) {
        $('#scope-name-search').textContent = state.scopeName;
        sbar.classList.remove('hidden');
      } else {
        sbar.classList.add('hidden');
      }
    }
  }

  /* ───────────────────────── 索引体检报告 ───────────────────────── */

  function rpStat(label, value, cls) {
    return '<div class="rp-stat' + (cls ? ' ' + cls : '') + '"><b>' + esc(value) +
      '</b><span>' + esc(label) + '</span></div>';
  }

  function renderReport(r) {
    const s = r.summary || {};
    const problems = r.problems || [];
    let html = '';

    html += '<div class="rp-summary">' +
      rpStat('资料总数', s.total || 0, '') +
      rpStat('索引成功', s.ok || 0, 'good') +
      rpStat('解析失败', s.error || 0, s.error ? 'bad' : '') +
      rpStat('已跳过', s.skipped || 0, s.skipped ? 'bad' : '') +
      rpStat('待索引', (s.pending || 0) + (s.indexing || 0), '') +
      rpStat('内容块', s.chunks || 0, '') +
      '</div>';

    const health = r.health || [];
    if (health.length) {
      html += '<div class="rp-health"><div class="rp-h">需要留意</div><ul style="margin:0;padding-left:18px">';
      health.forEach(function (h) { html += '<li>' + esc(h) + '</li>'; });
      html += '</ul></div>';
    }

    if (problems.length) {
      html += '<div class="rp-h">问题清单（' + problems.length + ' 条）</div>';
      problems.forEach(function (p) {
        html += '<div class="rp-issue ' + esc(p.level) + '">' +
          '<span class="lv">' + esc(p.kind) + '</span>' +
          '<div><div class="nm">' + esc(p.name) + '</div>' +
          (p.detail ? '<div class="dt">' + esc(p.detail) + '</div>' : '') +
          '<div class="dt">' + esc(p.path) + '</div></div></div>';
      });
    } else {
      html += '<div class="rp-ok">没有发现问题，所有资料都正常入库。</div>';
    }

    const exts = r.by_ext || [];
    if (exts.length) {
      html += '<div class="rp-h">按格式分布</div><table class="rp-table"><thead><tr>' +
        '<th>格式</th><th>文件数</th><th>内容块</th><th>失败</th></tr></thead><tbody>';
      exts.forEach(function (e) {
        html += '<tr><td>' + esc(e.ext) + '</td><td>' + e.files + '</td><td>' +
          e.chunks + '</td><td>' + (e.failed ? '<span style="color:var(--orange)">' + e.failed + '</span>' : '0') +
          '</td></tr>';
      });
      html += '</tbody></table>';
    }

    $('#rp-body').innerHTML = html;
    $('#rp-sub').textContent = '已入库 ' + (s.ok || 0) + ' / ' + (s.total || 0) +
      ' 个文件，共 ' + (s.chunks || 0) + ' 个内容块';
  }

  async function openReport() {
    const body = $('#rp-body');
    body.innerHTML = '<div class="src-note">正在体检…</div>';
    $('#overlay-report').classList.remove('hidden');
    try {
      const r = await api('/api/report');
      renderReport(r);
    } catch (e) {
      body.innerHTML = '<div class="src-note">体检失败：' + esc(e.message) + '</div>';
    }
  }

  /* ───────────────────────── 文件选择器 ───────────────────────── */

  async function openPicker(mode) {
    state.pickerMode = mode;
    state.pickerPath = '';
    $('#pk-title').textContent = mode === 'disk' ? '选择磁盘'
      : (mode === 'file' ? '选择文件'
        : (mode === 'scope' ? '限定检索范围' : '选择文件夹'));
    $('#pk-confirm').classList.toggle('hidden', mode !== 'folder');
    $('#overlay-picker').classList.remove('hidden');
    await loadPicker('');
  }

  async function loadPicker(path) {
    const body = $('#pk-body');
    body.innerHTML = '<div class="empty-side">读取中…</div>';
    try {
      const r = await api('/api/fs/list?path=' + encodeURIComponent(path || ''));
      state.pickerPath = r.path || '';
      $('#pk-path').textContent = r.path || '';
      let html = '';
      if (r.need_drive) {
        (r.drives || []).forEach(function (d) {
          html += '<div class="pk-row" data-path="' + esc(d.path) + '"><span class="ico">💽</span>' + esc(d.name) + '</div>';
        });
      } else {
        if (r.parent) html += '<div class="pk-row" data-up="' + esc(r.parent) + '"><span class="ico">↑</span>上一级</div>';
        (r.dirs || []).forEach(function (d) {
          html += '<div class="pk-row" data-path="' + esc(d.path) + '"><span class="ico">📁</span>' + esc(d.name) + '</div>';
        });
        (r.files || []).forEach(function (f) {
          html += '<div class="pk-row" data-file="' + esc(f.path) + '"><span class="ico f">📄</span>' + esc(f.name) + '</div>';
        });
        if (!r.dirs.length && !r.files.length) html += '<div class="empty-side">这个文件夹里没有可索引的文件</div>';
      }
      body.innerHTML = html;
      $$('.pk-row', body).forEach(function (el) {
        el.addEventListener('click', function () {
          if (el.dataset.up !== undefined && el.dataset.up !== '') { loadPicker(el.dataset.up); return; }
          if (el.dataset.file) { pickFile(el.dataset.file); return; }
          loadPicker(el.dataset.path);
        });
      });
    } catch (e) {
      body.innerHTML = '<div class="empty-side">读取失败：' + esc(e.message) + '</div>';
    }
  }

  async function pickFile(path) {
    if (state.pickerMode === 'scope') {
      // 限定检索范围：选中后只在这个文件里检索
      $('#overlay-picker').classList.add('hidden');
      const base = path.split(/[\\/]/).pop();
      const f = (state.files || []).find(function (x) {
        return x.name === base || x.path === path;
      });
      if (!f) { toast('这个文件还没进资料库，先添加它再限定'); return; }
      setScope(Number(f.id), f.name);
      toast('已限定到 ' + f.name);
      return;
    }
    // 本地文件直接登记进索引库
    const res = await api('/api/roots', { method: 'POST', body: { path: path, kind: 'file' } });
    $('#overlay-picker').classList.add('hidden');
    toast(res.files ? '已导入 ' + res.files + ' 个文件，正在建立索引' : '已导入');
    refreshFiles();
  }

  async function confirmFolder() {
    const path = state.pickerPath;
    if (!path) { toast('请选择文件夹'); return; }
    try {
      const res = await api('/api/roots', { method: 'POST', body: { path: path, kind: 'folder' } });
      $('#overlay-picker').classList.add('hidden');
      toast('已添加，正在建立索引（' + (res.files || 0) + ' 个文件）');
      refreshStatus(); refreshFiles();
    } catch (e) { toast('添加失败：' + e.message); }
  }

  /* ───────────────────────── 设置 ───────────────────────── */

  async function openSettings() {
    const r = await api('/api/config');
    state.cfg = r.config;
    const l = r.config.llm || {};
    $('#cfg-provider').value = l.provider || 'none';
    $('#cfg-base').value = l.base_url || '';
    $('#cfg-key').value = '';
    $('#cfg-key').placeholder = l.has_api_key ? '已保存，留空保持；勾选下方可移除' : '输入 API Key';
    $('#cfg-clear-key').checked = false;
    $('#cfg-model').value = l.model || '';
    $('#cfg-temp').value = l.temperature != null ? l.temperature : 0.1;
    $('#cfg-topk').value = (r.config.retrieve || {}).top_k || 8;
    $('#cfg-ocr').checked = !!(r.config.ocr || {}).enabled;
    $('#ocr-hint').textContent = r.ocr_available
      ? '本机 OCR 引擎已就绪（RapidOCR · CPU 离线运行）。'
      : '未检测到 OCR 引擎，图片与扫描件将无法识别文字。';
    const autoSec = (r.config.index || {}).auto_rescan_seconds;
    $('#cfg-autorescan').value = autoSec == null ? 60 : autoSec;
    $('#autorescan-hint').textContent =
      '已添加的文件夹会每隔这个时间自动重扫一次，新增/修改/删除的文件自动入库。填 0 关闭自动同步，只能手动点左侧「刷新」。';
    $('#set-msg').textContent = '';
    $('#overlay-settings').classList.remove('hidden');
    updateLlmBadge(r.llm);
  }

  function updateLlmBadge(info) {
    const b = $('#llm-badge');
    if (info && info.configured) {
      b.textContent = '模型：' + info.model;
      b.classList.add('on');
    } else {
      b.textContent = '未接入模型';
      b.classList.remove('on');
    }
  }

  async function saveSettings() {
    const payload = {
      clear_api_key: $('#cfg-clear-key').checked,
      llm: {
        provider: $('#cfg-provider').value,
        base_url: $('#cfg-base').value.trim(),
        ...($('#cfg-key').value.trim() ? { api_key: $('#cfg-key').value.trim() } : {}),
        model: $('#cfg-model').value.trim(),
        temperature: parseFloat($('#cfg-temp').value || '0.1'),
      },
      ocr: { enabled: $('#cfg-ocr').checked },
      index: {
        auto_rescan_seconds: parseInt($('#cfg-autorescan').value || '60', 10),
      },
      retrieve: { top_k: parseInt($('#cfg-topk').value || '8', 10) },
    };
    try {
      const r = await api('/api/config', { method: 'POST', body: payload });
      updateLlmBadge(r.llm);
      $('#set-msg').textContent = '已保存。';
      $('#cfg-key').value = '';
      $('#cfg-clear-key').checked = false;
      toast('设置已保存');
    } catch (e) { $('#set-msg').textContent = '保存失败：' + e.message; }
  }

  async function testLlm() {
    $('#set-msg').textContent = '测试中…';
    try {
      await api('/api/config', {
        method: 'POST',
        body: {
          clear_api_key: $('#cfg-clear-key').checked,
          llm: {
            provider: $('#cfg-provider').value,
            base_url: $('#cfg-base').value.trim(),
            ...($('#cfg-key').value.trim() ? { api_key: $('#cfg-key').value.trim() } : {}),
            model: $('#cfg-model').value.trim(),
            temperature: parseFloat($('#cfg-temp').value || '0.1'),
          },
        },
      });
      const r = await api('/api/llm/test', { method: 'POST' });
      $('#set-msg').textContent = (r.ok ? '✓ ' : '✗ ') + r.message;
    } catch (e) { $('#set-msg').textContent = '测试失败：' + e.message; }
  }

  /* ───────────────────────── 拖拽上传 ───────────────────────── */

  let dragDepth = 0;
  let dragIsFile = false;

  function isFileDrag(e) {
    const t = e.dataTransfer && e.dataTransfer.types;
    if (!t) return false;
    return Array.prototype.indexOf.call(t, 'Files') >= 0;
  }

  function showDrop(on) {
    const dz = $('#dropzone');
    if (dz) dz.classList.toggle('on', !!on);
  }

  // readEntries 一次最多吐 100 条，必须读到空数组为止
  function readAllEntries(reader, acc) {
    return new Promise(function (resolve) {
      reader.readEntries(function (list) {
        if (!list || !list.length) { resolve(acc); return; }
        acc = acc.concat(Array.prototype.slice.call(list));
        readAllEntries(reader, acc).then(resolve);
      }, function () { resolve(acc); });
    });
  }

  // 递归遍历拖入的目录，收集 {file, rel}
  function walkEntry(entry, prefix, out) {
    if (!entry) return Promise.resolve(out);
    if (entry.isFile) {
      return new Promise(function (resolve) {
        entry.file(function (f) {
          out.push({ file: f, rel: prefix + f.name });
          resolve(out);
        }, function () { resolve(out); });
      });
    }
    if (entry.isDirectory) {
      return new Promise(function (resolve) {
        readAllEntries(entry.createReader(), []).then(function (entries) {
          const p = prefix + entry.name + '/';
          let chain = Promise.resolve(out);
          entries.forEach(function (e) {
            chain = chain.then(function (acc) { return walkEntry(e, p, acc); });
          });
          chain.then(resolve);
        });
      });
    }
    return Promise.resolve(out);
  }

  function collectDropped(dt) {
    const items = dt.items;
    if (!items || !items.length || !items[0].webkitGetAsEntry) {
      // 降级：拿不到目录信息时按平铺文件处理
      const out = [];
      Array.prototype.forEach.call(dt.files || [], function (f) {
        out.push({ file: f, rel: f.name });
      });
      return Promise.resolve(out);
    }
    const entries = [];
    for (let i = 0; i < items.length; i++) {
      const en = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
      if (en) entries.push(en);
    }
    let chain = Promise.resolve([]);
    entries.forEach(function (en) {
      chain = chain.then(function (acc) { return walkEntry(en, '', acc); });
    });
    return chain;
  }

  /* ───────────────────────── 问答历史 ───────────────────────── */

  async function refreshHistory() {
    try {
      const r = await api('/api/history?limit=100');
      state.historyItems = r.items || [];
    } catch (e) { /* noop */ }
  }

  function fmtTime(ts) {
    const d = new Date((ts || 0) * 1000);
    const p = (n) => String(n).padStart(2, '0');
    const today = new Date();
    const same = d.toDateString() === today.toDateString();
    const hm = p(d.getHours()) + ':' + p(d.getMinutes());
    return same ? hm : (d.getMonth() + 1) + '/' + d.getDate() + ' ' + hm;
  }

  function modeLabel(m) {
    if (m === 'llm') return '模型生成';
    if (m === 'computed') return '聚合计算';
    if (m === 'empty') return '无结果';
    if (m === 'none') return '空';
    return '原文摘录';
  }

  function renderHistory() {
    const box = $('#hi-body');
    if (!state.historyItems.length) {
      box.innerHTML = '<div class="src-note">还没有问答记录。问过的问题会自动存在这里，重启工具也不会丢。</div>';
      return;
    }
    box.innerHTML = state.historyItems.map(function (h) {
      const ans = (h.answer || '').replace(/\s+/g, ' ').slice(0, 90);
      return '<div class="hi-item" data-hid="' + h.id + '">' +
        '<div class="hi-q" data-ask="' + esc(h.question) + '" title="点击填入输入框">' +
          esc(h.question) + '</div>' +
        '<div class="hi-a">' + esc(ans) + (ans.length >= 90 ? '…' : '') + '</div>' +
        '<div class="hi-meta">' +
          '<span class="hi-time">' + fmtTime(h.ts) + '</span>' +
          '<span class="hi-mode' + (h.mode === 'computed' ? ' c' : '') + '">' + modeLabel(h.mode) + '</span>' +
          (h.file_name ? '<span class="hi-file">' + esc(h.file_name) + '</span>' : '') +
          '<button class="hi-act" data-reask="' + h.id + '">再问一次</button>' +
          '<button class="hi-act danger" data-del="' + h.id + '">删除</button>' +
        '</div></div>';
    }).join('');
  }

  async function openHistory() {
    $('#overlay-history').classList.remove('hidden');
    $('#hi-body').innerHTML = '<div class="src-note">正在读取…</div>';
    await refreshHistory();
    renderHistory();
  }

  async function reask(id) {
    const h = state.historyItems.find(function (x) { return String(x.id) === String(id); });
    if (!h) return;
    $('#overlay-history').classList.add('hidden');
    switchView('chat');
    const inp = $('#ask-input');
    inp.value = h.question;
    inp.dispatchEvent(new Event('input'));
    inp.focus();
    toast('已填入：' + h.question);
  }

  /* ───────────────────────── 上传进度与重试 ───────────────────────── */

  function uploadProgressUI(text, pct) {
    const box = $('#up-progress');
    if (!box) return;
    box.classList.remove('hidden');
    $('#up-text').textContent = text;
    $('#up-bar').style.width = Math.max(0, Math.min(100, pct)) + '%';
  }

  function uploadProgressHide() {
    const box = $('#up-progress');
    if (box) box.classList.add('hidden');
  }

  function showRetry(items) {
    state.uploadFailed = items || [];
    const box = $('#up-retry');
    if (!box) return;
    if (!state.uploadFailed.length) { box.classList.add('hidden'); box.innerHTML = ''; return; }
    box.classList.remove('hidden');
    box.innerHTML = '<div class="up-r-h">有 ' + state.uploadFailed.length + ' 个文件没导入成功</div>' +
      '<div class="up-r-list">' + state.uploadFailed.slice(0, 8).map(function (it) {
        return '<div class="up-r-i">' + esc(it.name) + '<span>' + esc(it.detail || '') + '</span></div>';
      }).join('') + '</div>' +
      '<div class="up-r-a"><button id="up-retry-btn">重试失败的文件</button>' +
      '<button class="ghost" id="up-retry-clear">忽略</button></div>';
  }

  async function uploadList(list) {
    const BATCH = 20;
    let added = 0;
    const failed = [];
    for (let i = 0; i < list.length; i += BATCH) {
      const chunk = list.slice(i, i + BATCH);
      const fd = new FormData();
      const rels = [];
      chunk.forEach(function (it) {
        fd.append('files', it.file, it.file.name);
        rels.push(it.rel);
      });
      fd.append('paths', JSON.stringify(rels));
      const base = Math.round((i / list.length) * 100);
      const span = Math.max(1, Math.round((chunk.length / list.length) * 100));
      uploadProgressUI('正在导入 ' + (i + chunk.length) + '/' + list.length + ' …', base);
      let r;
      try {
        r = await fetch('/api/upload', { method: 'POST', body: fd });
      } catch (e) {
        chunk.forEach(function (it) {
          failed.push({ name: it.file.name, rel: it.rel, detail: '网络中断', file: it.file });
        });
        uploadProgressUI('正在导入 ' + (i + chunk.length) + '/' + list.length + ' …', base + span);
        continue;
      }
      if (!r.ok) {
        let detail = r.statusText;
        try { detail = (await r.json()).detail || detail; } catch (e) { /* noop */ }
        chunk.forEach(function (it) {
          failed.push({ name: it.file.name, rel: it.rel, detail: detail, file: it.file });
        });
        uploadProgressUI('正在导入 ' + (i + chunk.length) + '/' + list.length + ' …', base + span);
        continue;
      }
      const j = await r.json();
      added += (j.files || 0);
      // 服务端逐文件回报结果，才能只重试失败的那几个。
      // 按下标对应最稳：服务端 results[i] 就是 files[i]。
      // 按名字匹配不可靠 —— 服务端会清洗文件名（非法字符换成 _），
      // 两边对不上就会把「其实已保存」的文件误报成失败，用户一重试就多出一份副本。
      const results = j.results || [];
      const byRel = {};
      results.forEach(function (x) { byRel[x.rel || x.name] = x; });
      chunk.forEach(function (it, idx) {
        const rr = results[idx] || byRel[it.rel] || byRel[it.file.name];
        if (!rr || rr.status !== 'saved') {
          failed.push({
            name: it.file.name, rel: it.rel,
            detail: (rr && rr.detail) || '未知原因', file: it.file
          });
        }
      });
      uploadProgressUI('正在导入 ' + (i + chunk.length) + '/' + list.length + ' …', base + span);
    }
    uploadProgressUI('导入完成', 100);
    setTimeout(uploadProgressHide, 1200);
    showRetry(failed);
    return { added: added, failed: failed };
  }

  async function importFiles(list) {
    if (!list || !list.length) { toast('没有可导入的文件'); return; }
    toast('正在导入 ' + list.length + ' 个文件…', 60000);
    try {
      const res = await uploadList(list);
      if (res.failed.length) {
        toast('已导入 ' + res.added + ' 个，' + res.failed.length + ' 个失败（可重试）', 5000);
      } else {
        toast('已导入 ' + res.added + ' 个文件，正在建立索引');
      }
      refreshFiles(); refreshStatus();
    } catch (e) {
      toast('导入失败：' + e.message);
    }
  }

  async function importDropped(dt) {
    toast('正在读取拖入的文件…');
    let list = [];
    try {
      list = await collectDropped(dt);
    } catch (e) {
      toast('读取失败：' + e.message);
      return;
    }
    await importFiles(list);
  }

  function bindDrop() {
    const input = $('#drop-input');
    if (input) {
      input.addEventListener('change', function () {
        const files = Array.prototype.slice.call(input.files || []);
        input.value = '';
        importFiles(files.map(function (f) { return { file: f, rel: f.name }; }));
      });
    }

    document.addEventListener('dragenter', function (e) {
      if (!isFileDrag(e)) return;
      e.preventDefault();
      dragIsFile = true;
      dragDepth += 1;
      showDrop(true);
    });
    document.addEventListener('dragover', function (e) {
      if (!dragIsFile && !isFileDrag(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    });
    document.addEventListener('dragleave', function (e) {
      if (!dragIsFile) return;
      e.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) { dragIsFile = false; showDrop(false); }
    });
    document.addEventListener('drop', function (e) {
      if (!dragIsFile && !isFileDrag(e)) return;
      e.preventDefault();
      dragDepth = 0; dragIsFile = false;
      showDrop(false);
      importDropped(e.dataTransfer);
    });
  }

  /* ───────────────────────── 事件绑定 ───────────────────────── */

  function switchView(view) {
    state.view = view;
    $$('.tab').forEach(function (x) { x.classList.toggle('active', x.dataset.view === view); });
    $('#view-chat').classList.toggle('hidden', view !== 'chat');
    $('#view-search').classList.toggle('hidden', view !== 'search');
  }

  function bind() {
    $$('.tab').forEach(function (t) {
      t.addEventListener('click', function () {
        switchView(t.dataset.view);
        if (state.view === 'search') $('#search-input').focus();
      });
    });

    $('#btn-send').addEventListener('click', function () {
      const v = $('#ask-input').value;
      $('#ask-input').value = '';
      $('#ask-input').style.height = 'auto';
      ask(v);
    });
    $('#ask-input').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        $('#btn-send').click();
      }
    });
    $('#ask-input').addEventListener('input', function () {
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 160) + 'px';
    });
    $$('.example').forEach(function (b) {
      b.addEventListener('click', function () { ask(b.dataset.q); });
    });

    $('#btn-search').addEventListener('click', doSearch);
    $('#search-input').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') doSearch();
    });

    $('#btn-add-folder').addEventListener('click', function () { openPicker('folder'); });
    $('#btn-add-files').addEventListener('click', function () { openPicker('file'); });
    $('#btn-add-disk').addEventListener('click', function () { openPicker('disk'); });
    bindDrop();
    $('#pk-confirm').addEventListener('click', confirmFolder);
    $('#pk-close').addEventListener('click', function () { $('#overlay-picker').classList.add('hidden'); });

    $('#src-close').addEventListener('click', function () { $('#overlay-source').classList.add('hidden'); });
    $('#set-close').addEventListener('click', function () { $('#overlay-settings').classList.add('hidden'); });
    $('#btn-settings').addEventListener('click', openSettings);
    $('#cfg-save').addEventListener('click', saveSettings);
    $('#cfg-test').addEventListener('click', testLlm);

    $$('.overlay').forEach(function (o) {
      o.addEventListener('click', function (e) { if (e.target === o) o.classList.add('hidden'); });
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') $$('.overlay').forEach(function (o) { o.classList.add('hidden'); });
    });

    $('#btn-rescan').addEventListener('click', async function () {
      toast('正在重新扫描…');
      await api('/api/rescan', { method: 'POST' });
      refreshStatus(); refreshFiles();
    });
    $('#btn-reindex').addEventListener('click', async function () {
      const r = await api('/api/reindex', { method: 'POST' });
      toast('已排队重建 ' + r.files + ' 个文件');
      refreshStatus();
    });

    $('#roots').addEventListener('click', async function (e) {
      const x = e.target.closest('[data-root]');
      if (!x) return;
      await api('/api/roots/' + x.dataset.root, { method: 'DELETE' });
      refreshStatus();
    });

    $('#filelist').addEventListener('click', async function (e) {
      // 点目录标题 = 把检索范围限定到这个目录
      const dir = e.target.closest('[data-dir]');
      if (dir) {
        const d = dir.dataset.dir;
        if (state.scopeDir === d) {
          clearScope();
          toast('已取消范围限定');
        } else {
          setScopeDir(d, d);
          toast('已限定到目录 ' + d);
        }
        return;
      }
      const item = e.target.closest('[data-file]');
      if (!item) return;
      const id = item.dataset.file;
      const path = item.dataset.path;
      if (e.target.classList.contains('st')) return;
      const f = (state.files || []).find(function (x) { return String(x.id) === String(id); });
      const name = f ? f.name : path.split(/[\\/]/).pop();
      // 点文件 = 把检索范围限定到这个文件，并打开它在内容里的第一处命中
      setScope(Number(id), name);
      renderFiles();
      let r = await api('/api/search?q=' + encodeURIComponent(name) + '&file_id=' + encodeURIComponent(id));
      if (!r.hits || !r.hits.length) {
        // 文件名可能没出现在正文里，退回全局检索来定位原文。
        // 但必须只认属于这个文件的命中 —— 否则会打开别的文件的内容。
        r = await api('/api/search?q=' + encodeURIComponent(name) + '&limit=20');
        r.hits = (r.hits || []).filter(function (h) {
          return String(h.file_id) === String(id);
        });
      }
      if (r.hits && r.hits.length) {
        api('/api/source/' + r.hits[0].chunk_id + '?question=' + encodeURIComponent(name)).then(renderSource);
      } else {
        toast('已限定到 ' + name + '，该文件暂无内容索引');
      }
    });

    // 检索范围
    $('#scope-clear').addEventListener('click', function () {
      clearScope();
      toast('已取消范围限定');
    });
    // 搜索视图里的那份范围条
    $('#scope-clear-search').addEventListener('click', function () {
      clearScope();
      toast('已取消范围限定');
    });
    $('#scope-pick').addEventListener('click', function () { openPicker('scope'); });

    // 索引体检
    $('#btn-report').addEventListener('click', openReport);
    $('#rp-close').addEventListener('click', function () { $('#overlay-report').classList.add('hidden'); });

    // 问答历史
    $('#btn-history').addEventListener('click', openHistory);
    $('#hi-close').addEventListener('click', function () { $('#overlay-history').classList.add('hidden'); });
    $('#hi-clear').addEventListener('click', async function () {
      if (!state.historyItems.length) { toast('没有可清空的记录'); return; }
      await api('/api/history', { method: 'DELETE' });
      await refreshHistory();
      renderHistory();
      toast('已清空问答历史');
    });
    $('#hi-export').addEventListener('click', async function () {
      if (!state.historyItems.length) { toast('还没有问答记录'); return; }
      try {
        const name = await downloadFrom('/api/history/export', 'EveryFind-问答记录.md');
        toast('已导出 ' + name);
      } catch (e) {
        toast('导出失败：' + e.message);
      }
    });
    $('#hi-body').addEventListener('click', async function (e) {
      const reaskBtn = e.target.closest('[data-reask]');
      if (reaskBtn) { reask(reaskBtn.dataset.reask); return; }
      const delBtn = e.target.closest('[data-del]');
      if (delBtn) {
        await api('/api/history/' + delBtn.dataset.del, { method: 'DELETE' });
        await refreshHistory();
        renderHistory();
        toast('已删除该条记录');
        return;
      }
      const q = e.target.closest('[data-ask]');
      if (q) {
        $('#overlay-history').classList.add('hidden');
        switchView('chat');
        const inp = $('#ask-input');
        inp.value = q.dataset.ask;
        inp.dispatchEvent(new Event('input'));
        inp.focus();
      }
    });

    // 上传失败重试
    $('#up-retry').addEventListener('click', function (e) {
      if (e.target.id === 'up-retry-btn') {
        const items = state.uploadFailed.filter(function (x) { return x.file; });
        if (!items.length) { toast('没有可重试的文件'); return; }
        showRetry([]);
        importFiles(items);
        return;
      }
      if (e.target.id === 'up-retry-clear') {
        showRetry([]);
      }
    });
  }

  /* ───────────────────────── 启动 ───────────────────────── */

  async function init() {
    bind();
    await refreshStatus();
    await refreshFiles();
    await refreshHistory();
    // 把历史里的最近几轮灌进追问上下文，重启后接着问也能接上
    state.history = state.historyItems.slice(0, 8).map(function (h) {
      return { question: h.question };
    }).reverse();
    try {
      const r = await api('/api/config');
      updateLlmBadge(r.llm);
    } catch (e) { /* noop */ }
    state.pollTimer = setInterval(refreshStatus, 2000);
    setInterval(refreshFiles, 4000);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
