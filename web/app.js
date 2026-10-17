/* L4D2 Mod Studio 前端 —— 原生 JS，无依赖 */
'use strict';

const S = {
  boot: null, scan: null, audit: null, gf: null,
  tab: 'overview', auditTab: 'conflicts', gfTab: 'startup',
  q: '', cats: new Set(), tag: '', onlyEnabled: false,
  sel: new Set(), refineKey: '', components: null, drop: new Set(),
  busy: false, scanning: false, staging: [],
  // AI 复核：cfg 是服务端配置（不含密钥），job 是当前分析任务的状态
  ai: { cfg: null, presets: [], job: null, timer: null, limit: 12, modelList: [] },
  // 模型归属：key=选中的 mod，d=detect 结果，plan=预演，target=目标角色
  re: { key: '', d: null, plan: null, target: '', fixInternal: true, busy: false },
  // 获取 mod：src=待入库文件路径，plan=预演，sub=子页签（ingest/ws），
  // job=创意工坊下载任务状态，scmd=SteamCMD 探测结果
  ac: { src: '', plan: null, sub: 'ingest', job: null, scmd: null, timer: null, mode: 'move' },
};

/* ---------------- 基础工具 ---------------- */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
/* 属性值转义：只用于 data-* / value= 这类属性。
   注意 —— 路径**不再**拼进内联 onclick（那才是被 &#39; 还原后截断 JS 字符串的坑），
   而是走 data-p="..."，由事件委托读 dataset，值原样取回，反斜杠/引号都安全。 */
const escAttr = esc;
const cssEsc = s => (window.CSS && CSS.escape) ? CSS.escape(String(s)) : String(s).replace(/["\\]/g, '\\$&');

/* 建 DOM 的小工具：文本一律走 textContent，绝不解析 HTML —— 外部数据（文件名/路径/标签）
   从这里进来就不可能变成标记。attr 里 'text'/'class'/'style' 特殊处理，其余优先按属性赋值。 */
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  const a = attrs || {};
  for (const k in a) {
    const v = a[k];
    if (v == null || v === false) continue;
    if (k === 'text') n.textContent = String(v);
    else if (k === 'class') n.className = v;
    else if (k === 'style') n.setAttribute('style', v);
    else if (k in n) n[k] = v;
    else n.setAttribute(k, v);
  }
  for (const kid of kids) {
    for (const x of (Array.isArray(kid) ? kid : [kid])) if (x != null) n.append(x);
  }
  return n;
}
/* ---- 视图代际 ----
   6 个视图都是「先画 spinner → await → 整页 innerHTML」。快速连点导航时旧请求晚回来
   会把新页面覆盖掉（点 A 再点 B，A 的结果最后落地）。每个视图领一个序号，写 DOM 前对一次。 */
let viewSeq = 0;
const newView = () => ++viewSeq;
const stale = seq => seq !== viewSeq;

/* 同一接口的并发请求合并成一次：切页/连点不会重复跑重活（/api/audit 要比对 206 个 mod 的内部资源表） */
const inflight = new Map();
function once(key, fn) {
  if (inflight.has(key)) return inflight.get(key);
  const p = fn().finally(() => inflight.delete(key));
  inflight.set(key, p);
  return p;
}

const GB = 1073741824, MB = 1048576;   // 体积换算：别再散落魔法数字

/* 分类色兜底可能是 3 位（core/scan.py 用的是 "#888"），直接拼 `${c}44` 会得到无效的 #88844 */
function normColor(c) {
  const s = String(c || '').trim();
  if (/^#[0-9a-f]{3}$/i.test(s)) return '#' + s[1] + s[1] + s[2] + s[2] + s[3] + s[3];
  return /^#[0-9a-f]{6}$/i.test(s) ? s : '';
}
/* 分类 pill 的描边/文字色：先归一成 6 位再拼 44 透明度后缀，3 位色也不会拼出无效 CSS */
function pillStyle(c) {
  const s = normColor(c);
  return s ? `border-color:${s}44;color:${s}` : '';
}

/* 搜索用的小写索引串：206 个 mod 每按一次键都重新拼接 + toLowerCase 是纯浪费。
   用 WeakMap 缓存，不给服务端返回的对象加字段（免得污染 payload）。 */
const hayCache = new WeakMap();
function haystack(m) {
  let s = hayCache.get(m);
  if (s === undefined) {
    s = (String(m.title ?? '') + ' ' + String(m.key ?? '') + ' ' +
         (m.tags || []).join(' ') + ' ' + (m.content_tags || []).join(' ')).toLowerCase();
    hayCache.set(m, s);
  }
  return s;
}

/* 委托控件里的 .chip 这类 span/label 默认键盘够不着；统一补 tabindex/role，
   配合 document 上的 keydown（Enter/空格）就能纯键盘操作。原生控件跳过。 */
function a11yActs(root) {
  (root || document).querySelectorAll('[data-act]').forEach(n => {
    const tag = n.tagName;
    if (tag === 'BUTTON' || tag === 'A' || tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    if (tag === 'LABEL' && n.querySelector('input,select,textarea')) return;
    if (!n.hasAttribute('tabindex')) n.setAttribute('tabindex', '0');
    if (!n.hasAttribute('role')) n.setAttribute('role', 'button');
  });
}

function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg; t.className = 'on ' + kind;
  clearTimeout(t._h); t._h = setTimeout(() => { t.className = kind; }, 3600);
}

/* ---- 统一 fetch 封装 ----
   ① 网络失败（后端没起来 / 连接断了）不再抛出未捕获的 rejection：一律转成 {ok:false,error}
      返回，调用点原有 `if (!r.ok)` 语义完全不变，UI 不会永久卡在 spinner；
   ② 超时用 AbortController。慢接口（真扫盘 10 GB、重写 VPK、调模型、弹系统文件框）
      在 API_SLOW 里不设客户端超时 —— 靠并发守卫 + 取消，绝不把正常的长任务掐断；
   ③ HTTP 非 2xx 时，后端返回的仍是 {ok:false,error,detail}（_err），能解析就用它，
      解析不出才补一个「HTTP xxx」；不改任何字段名，不因 !r.ok 抛错（否则读不到 error）。 */
const API_TIMEOUT = 60000;
const API_SLOW = ['/api/scan', '/api/rescan', '/api/audit', '/api/vpk/', '/api/ai/', '/api/isolate',
                  '/api/staging/', '/api/gamefiles/replace', '/api/gamefiles/restore',
                  '/api/gamefiles/write', '/api/pick-file', '/api/open-folder', '/api/config'];
async function api(path, body, method = 'GET') {
  const opt = { method };
  // GET 不带 Content-Type：没有请求体时它没有任何意义，只是白白多一个头。
  if (method !== 'GET') opt.headers = { 'Content-Type': 'application/json' };
  if (body && method !== 'GET') opt.body = JSON.stringify(body);
  const ms = API_SLOW.some(p => path.startsWith(p)) ? 0 : API_TIMEOUT;
  let timer = null;
  if (ms) {
    const ctl = new AbortController();
    opt.signal = ctl.signal;
    timer = setTimeout(() => ctl.abort(), ms);
  }
  try {
    const r = await fetch(path, opt);
    let j = null;
    try { j = await r.json(); } catch (e) {
      j = { ok: false, error: '返回内容不是 JSON（HTTP ' + r.status + '）' };
    }
    if (!j || typeof j !== 'object') j = { ok: false, error: '返回内容为空（HTTP ' + r.status + '）' };
    else if (!r.ok && j.ok === undefined) j.ok = false;
    return j;
  } catch (e) {
    if (e && e.name === 'AbortError') {
      return { ok: false, error: '请求超时（超过 ' + Math.round(ms / 1000) + ' 秒）：' + path };
    }
    return { ok: false, error: '无法连接后端：' + ((e && e.message) || '连接被中断') };
  } finally {
    if (timer) clearTimeout(timer);
  }
}
function spinner(text) { return `<div class="empty"><span class="spin"></span> ${esc(text)}</div>`; }

/* busy() 只切自己这一层禁用态：
   —— 原来 busy(false) 会把**所有** .btn 无条件启用，把 plan.blocked 时故意 disabled
      的「另存为新 VPK / 原地替换」重新点亮，等于把危险按钮递到手上。
   —— 现在显式禁用的按钮带 data-base-disabled="1"，busy() 永不解锁它。 */
function applyBusy() {
  $$('button.btn').forEach(b => {
    if (b.dataset.keep === '1') return;
    b.disabled = S.busy || b.dataset.baseDisabled === '1';
  });
}
function busy(on, text) {
  S.busy = !!on;
  applyBusy();
  if (on) toast(text || '处理中…');
}
const h = (n) => n == null ? '-' : n;

/* ---------------- 事件委托 ----------------
   全站只在 document 上挂 4 个监听（click / change / input / visibilitychange）。
   重绘 innerHTML 不会丢事件、也不会重复挂载；所有控件只带 data-act，数据走 data-*，
   没有任何内联 onclick —— 文件名/路径这类外部数据再也不可能拼成可执行标记。 */
const ACT = {
  /* 导航 / 顶栏 / 抽屉 */
  tab: b => switchTab(b.dataset.tab),
  scan: () => loadScan(true),
  settings: () => openSettings(),
  drawerClose: () => closeDrawer(),
  /* 总览 */
  jumpCat: b => { S.cats = new Set([b.dataset.k]); switchTab('browse'); },
  /* 分类与标签 */
  toggleOnly: () => { S.onlyEnabled = !S.onlyEnabled; refreshBrowse(); },
  toggleCat: b => { const k = b.dataset.k; S.cats.has(k) ? S.cats.delete(k) : S.cats.add(k); refreshBrowse(); },
  clearCats: () => { S.cats.clear(); refreshBrowse(); },
  setTag: b => { S.tag = b.dataset.tag || ''; refreshBrowse(); },
  selAll: () => toggleSelAll(true),
  selNone: () => { S.sel.clear(); syncSelState(); },
  batchOn: () => doBatch(Object.fromEntries([...S.sel].map(k => [k, true])), '批量启用'),
  batchOff: () => doBatch(Object.fromEntries([...S.sel].map(k => [k, false])), '批量停用'),
  batchIsolate: () => batchIsolate(),
  openMod: b => openMod(b.dataset.key),
  refine: b => { S.refineKey = b.dataset.key; S.drop.clear(); switchTab('refine'); },
  /* 组件精修 */
  loadComp: () => { S.refineKey = ($('#modSel') || {}).value || S.refineKey; S.drop.clear(); loadComponents(); },
  clearDrop: () => { S.drop.clear(); loadComponents(); },
  planStrip: () => planStrip(),
  applyNew: () => applyStrip('new'),
  applyReplace: () => applyStrip('replace'),
  toggleGroup: b => toggleGroup(+b.dataset.i, b.dataset.on === '1'),
  /* 模型归属 */
  reDetect: () => reDetect(),
  reTarget: b => rePickTarget(b.dataset.id),
  rePlan: () => rePlan(),
  reApplyNew: () => reApply('new'),
  reApplyReplace: () => reApply('replace'),
  reassignFrom: b => { S.re.key = b.dataset.key; S.re.d = null; S.re.plan = null; S.re.target = ''; switchTab('reassign'); },
  /* 获取 mod */
  acSub: b => { S.ac.sub = b.dataset.sub; rAcquire(); },
  acPick: () => acPick(),
  acPlan: () => acPlan(),
  acIngest: () => acIngest(),
  acDownload: () => acDownload(),
  acRescanScmd: () => acRescanScmd(),
  acInstall: () => acInstall(),
  acBrowser: () => acBrowser(),
  acProbeLink: () => acProbeLink(),
  acCancel: () => acCancel(),
  /* 分组标题行折叠：委托用 closest() 取「最近的」[data-act]，
     所以点行内「选中 / 取消」按钮时命中的是按钮本身，不会连带折叠。 */
  fold: b => {
    const gb = b.nextElementSibling;
    if (!gb) return;
    gb.classList.toggle('hidden');
    b.setAttribute('aria-expanded', gb.classList.contains('hidden') ? 'false' : 'true');
  },
  /* 抽屉里的操作 */
  extractVpk: b => extractVpk(b.dataset.key),
  isolateOne: b => isolateOne(b.dataset.key),
  openFolderOf: b => openFolderOf(b.dataset.p),
  /* 审计 */
  setAuditTab: b => { S.auditTab = b.dataset.t; rAudit(); },
  auditReset: () => { S.audit = null; rAudit(); },
  aiStart: () => aiStart(),
  aiCancel: () => aiCancel(),
  /* 游戏文件 */
  setGfTab: b => { S.gfTab = b.dataset.t; rGameFiles(); },
  gfPick: b => gfPick(b.dataset.rel),
  gfRestore: b => gfRestore(b.dataset.rel),
  gfRestoreAll: () => gfRestoreAll(),
  gfEdit: b => gfEdit(b.dataset.rel),
  gfSave: b => gfSave(b.dataset.rel),
  /* 隔离区 */
  restoreStaging: b => restoreStagingIdx(+b.dataset.i),
  recycleStaging: () => recycleStaging(),
  /* 备份与日志 */
  openDir: b => openDir(b.dataset.p),
  /* 设置 */
  saveCfg: () => saveCfg(),
  aiClearKey: () => aiClearKey(),
  aiSave: () => aiSave(),
  aiTest: () => aiTest(),
  /* 确认框：执行一次后立刻摘掉回调，连点不会重复提交危险操作 */
  cfmOk: () => {
    if (!cfmCb) return;
    const opt = $('#cfmRepo') ? $('#cfmRepo').checked : undefined;
    const fn = cfmCb;
    closeDrawer();
    fn(opt);
  },
};
const CHG = {
  toggleSel: (t) => toggleSelKey(t.dataset.key, t.checked),
  oneEnable: (t) => oneEnable(t.dataset.key, t.checked, t),
  selAllBox: (t) => toggleSelAll(t.checked),
  togglePart: (t) => togglePartIdx(+t.dataset.i, t.checked),
  pickMod: (t) => { S.refineKey = t.value; S.drop.clear(); loadComponents(); },
  pickReMod: (t) => { S.re.key = t.value; S.re.d = null; S.re.plan = null; S.re.target = ''; reDetect(); },
  reFixInternal: (t) => { S.re.fixInternal = t.checked; if (S.re.target) rePlan(); },
  acMode: (t) => { S.ac.mode = t.value; },
  aiPickPreset: () => aiPickPreset(),
};
const INP = { aiRefreshPriv: () => aiRefreshPriv() };

document.addEventListener('click', e => {
  const t = e.target;
  if (!(t instanceof Element)) return;
  const b = t.closest('[data-act]');
  if (!b || b.disabled) return;
  const fn = ACT[b.dataset.act];
  if (!fn) return;
  if (b.tagName === 'A' || b.tagName === 'BUTTON') e.preventDefault();
  fn(b, e);
});
document.addEventListener('change', e => {
  const t = e.target;
  if (!(t instanceof Element)) return;
  const fn = CHG[t.dataset && t.dataset.act];
  if (fn) fn(t);
});
document.addEventListener('input', e => {
  const t = e.target;
  if (!(t instanceof Element)) return;
  const fn = INP[t.dataset && t.dataset.act];
  if (fn) fn(t);
});
/* 非按钮的委托控件（目前只有分组标题行）带 tabindex，回车/空格要能激活 —— 
   否则它可聚焦却按不动，比不给 tabindex 更糟。原生 button/input 由浏览器自己处理，这里跳过。 */
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const t = e.target;
  if (!(t instanceof Element)) return;
  const tag = t.tagName;
  if (tag === 'BUTTON' || tag === 'A' || tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  const b = t.closest('[data-act]');
  if (!b) return;
  const fn = ACT[b.dataset.act];
  if (!fn) return;
  e.preventDefault();
  fn(b, e);
});
/* 页面隐藏（最小化/切走）时停掉轮询，回来再接上 —— 原来 600ms 的定时器会一直空转 */
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { stopAiPoll(); return; }
  if (S.ai.job && S.ai.job.state === 'running' && $('#aiWrap')) aiPoll(S.ai.job.id);
});

/* ---------------- 启动 ---------------- */
async function boot() {
  busy(true, '正在探测游戏目录…');
  try {
    const bs = await api('/api/bootstrap');
    S.boot = bs || {};
    /* 🔴 api() 现在把网络/超时/非 JSON 全部转成 {ok:false,error} 返回，**不抛异常**。
       所以下面那个 catch 对「后端没起来」这类最常见故障其实永远不会触发 ——
       只判异常的写法会漏掉它：S.boot 变成 {ok:false,error:'无法连接后端…'}，
       config 为空 ⇒ 顶部写「未找到游戏目录，请点设置手动指定」，一句误导。
       这不是没找到游戏目录，是后端根本连不上。必须先把这种「请求本身失败」认出来。 */
    if (bs && bs.ok === false) {
      const hp0 = $('#headPath');
      if (hp0) hp0.replaceChildren(el('span', { text: '启动失败：' + (bs.error || '未知错误'), style: 'color:var(--bad)' }));
      $('#view').innerHTML = `<div class="note bad">启动失败：${esc(bs.error || '未知错误')}
        <div class="muted" style="margin-top:6px">后端没在跑、或端口被占用时会出现这条。确认服务已启动后刷新页面。</div></div>`;
      return;
    }
    const cfg = S.boot.config || {};
    const hp = $('#headPath');
    hp.replaceChildren();
    if (cfg.game_dir) {
      hp.append('游戏目录 ', el('b', { text: cfg.game_dir }));
    } else {
      hp.append(el('span', { text: '未找到游戏目录，请点「设置」手动指定', style: 'color:var(--bad)' }));
    }
    if (S.boot.error) toast('探测失败：' + S.boot.error, 'bad');
    // AI 配置在这里先取一次：冲突页要用它决定复核按钮可不可点
    const r = await api('/api/ai/config');
    if (r && r.config) {
      S.ai.cfg = r.config; S.ai.presets = r.presets || [];
      S.ai.limit = r.config.max_cases || 12;
    }
  } catch (e) {
    // 兜底：boot 里任何意外都不许把整页留成空白（后端没起来时用户至少要知道发生了什么）
    const hp = $('#headPath');
    if (hp) hp.replaceChildren(el('span', { text: '启动失败：' + (e && e.message ? e.message : e), style: 'color:var(--bad)' }));
    const v = $('#view');
    if (v) v.innerHTML = `<div class="note bad">启动失败：${esc(e && e.message ? e.message : e)}</div>`;
    return;
  } finally {
    busy(false);
  }
  await loadScan();
}

async function loadScan(force = false) {
  // 并发守卫：连点两次「重新扫描」不会再并发两次真扫盘、互相覆盖结果、提前解锁按钮
  if (S.scanning) { toast('扫描已在进行中，请等它跑完', ''); return; }
  S.scanning = true;
  const v = $('#view');
  const prev = S.scan;
  const seq = newView();
  const n = prev && prev.summary ? ` ${prev.summary.total} 个 mod` : '';
  v.innerHTML = spinner(force ? `正在重新扫描${n}…（只读解析 VPK 目录树）` : '正在扫描…');
  busy(true);
  let r = null;
  try {
    r = await api(force ? '/api/rescan' : '/api/scan');
  } finally {
    S.scanning = false;
    busy(false);
  }
  if (stale(seq)) return;   // 扫描期间用户切走了，别再动 DOM
  if (!r || !r.summary) {
    // 扫描失败绝不能把上一次的好数据丢掉，否则每个页面都会读 undefined 而整站白屏
    S.scan = prev;
    const msg = (r && r.error) || '未知错误';
    if (prev) { toast('扫描失败：' + msg, 'bad'); render(); return; }
    v.innerHTML = `<div class="note bad">扫描失败：${esc(msg)}</div>`;
    return;
  }
  S.scan = r;
  // 重扫后旧结论一律作废：审计是「改写前」的比对结果，游戏文件状态也可能变了
  S.audit = null; S.gf = null;
  // 已选里可能有这次没扫到的 key（mod 被删/被隔离）—— 留着会把死条目重新写回 addonlist.txt
  if (S.sel.size) {
    const live = new Set(r.mods.map(m => m.key));
    [...S.sel].forEach(k => { if (!live.has(k)) S.sel.delete(k); });
  }
  $('#navMods').textContent = r.summary.total;
  render();
}

/* ---------------- 路由 ---------------- */
const NEED_SCAN = { overview: 1, browse: 1, refine: 1, audit: 1, reassign: 1 };
function render() {
  if (!S.scan && NEED_SCAN[S.tab]) { $('#view').innerHTML = spinner('正在扫描…'); return; }
  const map = { overview: rOverview, browse: rBrowse, refine: rRefine, reassign: rReassign,
                acquire: rAcquire, audit: rAudit, gamefiles: rGameFiles,
                staging: rStaging, safety: rSafety };
  (map[S.tab] || rOverview)();
  applyBusy();
  a11yActs($('#view'));
}
function switchTab(t) {
  S.tab = t;
  // index.html 里的 nav 是完整 tab 模式（role=tablist/tab + aria-controls），
  // 切页必须同步 aria-selected / tabindex / #view 的 aria-labelledby，否则读屏软件念的还是旧页。
  const v = $('#view');
  $$('nav button[data-tab]').forEach(x => {
    const on = x.dataset.tab === t;
    x.classList.toggle('on', on);
    x.setAttribute('aria-selected', on ? 'true' : 'false');
    x.tabIndex = on ? 0 : -1;
    if (on) x.setAttribute('aria-current', 'page'); else x.removeAttribute('aria-current');
  });
  if (v) v.setAttribute('aria-labelledby', 'tab-' + t);
  if (t !== 'audit') stopAiPoll();   // 离开冲突页就清掉轮询定时器（任务在服务端继续跑，切回来会接上）
  render();
}

/* ================= 总览 ================= */
function rOverview() {
  const s = S.scan.summary, cats = S.scan.categories;
  const order = Object.keys(cats).sort((a, b) => cats[a].order - cats[b].order);
  const max = Math.max(1, ...order.map(k => s.by_category[k] || 0));
  const mism = S.scan.mods.filter(m => m.mismatch);
  const issues = S.scan.mods.filter(m => !m.link_ok || m.parse_error);
  const tl = S.boot && S.boot.taglib;
  $('#view').innerHTML = `
    <h2>总览</h2>
    <div class="lead">扫描 ${s.total} 个 mod，总占用 ${esc(s.total_size_h)}，耗时 ${S.scan.elapsed}s。
      以下数据全部来自 VPK 目录树与 addoninfo.txt 的实测解析，可作为复核依据。</div>

    <div class="grid g4">
      <div class="stat"><div class="n">${s.total}</div><div class="l">mod 总数</div></div>
      <div class="stat ok"><div class="n">${s.enabled}</div><div class="l">已启用</div></div>
      <div class="stat ${s.broken ? 'bad' : ''}"><div class="n">${s.broken}</div><div class="l">链接断链</div></div>
      <div class="stat ${s.parse_error ? 'warn' : ''}"><div class="n">${s.parse_error}</div><div class="l">VPK 解析异常</div></div>
      <div class="stat ${s.mismatch ? 'warn' : ''}"><div class="n">${s.mismatch}</div><div class="l">归类冲突（强证据）</div></div>
      <div class="stat"><div class="n">${esc(s.total_size_h)}</div><div class="l">总占用</div></div>
    </div>

    <div class="card" style="margin-top:14px">
      <h3>分类分布 <span class="tagline">按仓库归类；括号内为纯内容判定结果</span></h3>
      <table>
        <tr><th style="width:130px">分类</th><th style="width:52px" class="num">数量</th>
            <th style="width:52px" class="num">内容判定</th><th>占比</th><th style="width:70px"></th></tr>
        ${order.map(k => {
          const n = s.by_category[k] || 0, a = s.by_auto_category[k] || 0;
          if (!n && !a) return '';
          return `<tr>
            <td><span class="dot" style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${escAttr(cats[k].color)};margin-right:7px"></span>${esc(cats[k].zh)}</td>
            <td class="num">${n}</td><td class="num">${a}</td>
            <td><div class="bar"><i style="width:${(n / max * 100).toFixed(1)}%"></i></div></td>
            <td><button class="btn sm ghost" data-act="jumpCat" data-k="${escAttr(k)}">查看</button></td></tr>`;
        }).join('')}
      </table>
    </div>

    ${mism.length ? `<div class="card">
      <h3>⚠ 归类冲突 <span class="tagline">仓库归类与内容强证据不符，建议人工复核</span></h3>
      <div class="tablewrap tblwrap"><table>
        <tr><th>mod</th><th style="width:110px">仓库归类</th><th style="width:110px">内容判定</th><th>标题</th></tr>
        ${mism.slice(0, 40).map(m => `<tr>
          <td class="mono small">${esc(m.key)}</td>
          <td><span class="pill">${esc(m.category_zh)}</span></td>
          <td><span class="pill acc">${esc(S.scan.categories[m.auto_category]?.zh || m.auto_category)}</span></td>
          <td>${esc(m.title)}</td></tr>`).join('')}
      </table></div></div>` : ''}

    ${issues.length ? `<div class="card">
      <h3>异常条目（${issues.length}）</h3>
      ${issues.map(m => `<div class="ev bad"><b>${esc(m.key)}</b> — ${esc(m.title)}<br>${esc(m.parse_error || '链接目标不存在')}</div>`).join('')}
    </div>` : ''}

    <div class="card">
      <h3>探测依据 <span class="tagline">为什么是这个目录 —— 每条都可复核</span></h3>
      ${(S.scan.evidence || []).map(e => `<div class="ev mono small">${esc(e)}</div>`).join('')}
      ${tl ? `<div class="ev mono small">标签库：${esc(tl.source)}（${tl.tags} 个标签 / ${tl.rules} 条路径规则）</div>` : ''}
    </div>

    <div class="card">
      <h3>仓库里没接进 addons 的 mod（${(S.scan.unlinked || []).length}）<span class="tagline">仓库有素材，但游戏加载不到</span></h3>
      ${(S.scan.unlinked || []).length ? `<div class="tblwrap"><table>
        <tr><th>分类目录</th><th>标题</th><th>原因</th></tr>
        ${S.scan.unlinked.slice(0, 60).map(u => `<tr><td>${esc(u.category || '')}</td>
          <td>${esc(u.title || u.dir || '')}</td><td class="muted">${esc(u.reason || '')}</td></tr>`).join('')}
      </table></div>` : '<div class="muted">没有。仓库与 addons 完全一致。</div>'}
    </div>`;
}

/* ================= 分类与标签 ================= */
function filtered() {
  const q = S.q.trim().toLowerCase();
  return S.scan.mods.filter(m => {
    if (S.onlyEnabled && !m.enabled) return false;
    if (S.cats.size && !S.cats.has(m.category)) return false;
    if (S.tag && !(m.tags || []).includes(S.tag) && !(m.content_tags || []).includes(S.tag)) return false;
    if (q && !haystack(m).includes(q)) return false;
    return true;
  });
}

function rBrowse() {
  const s = S.scan.summary;
  $('#view').innerHTML = `
    <h2>分类与标签</h2>
    <div class="lead">分类来自三层证据：FireAxe 仓库目录（人工整理）→ addoninfo.txt 官方内容标签 → NekoVpk 素材级路径规则。
      点任意 mod 看它的完整判定依据。</div>

    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <input type="search" id="qbox" placeholder="搜索标题 / 文件名 / 标签…" value="${escAttr(S.q)}" style="width:300px">
        <label class="chip" id="chipOnly" data-act="toggleOnly">只看已启用</label>
        <div class="spacer"></div>
        <span class="muted">匹配 <b id="browseCount">0</b> / ${s.total}</span>
      </div>
      <div id="browseFilters"></div>
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:9px">
        <b>已选 <span id="browseSel">0</span> 项</b>
        <button class="btn sm" data-act="selAll">全选当前列表</button>
        <button class="btn sm ghost" data-act="selNone">清空选择</button>
        <div class="spacer"></div>
        <button class="btn sm primary" data-act="batchOn">批量启用</button>
        <button class="btn sm" data-act="batchOff">批量停用</button>
        <button class="btn sm danger" data-act="batchIsolate">移入隔离区…</button>
      </div>
      <div class="tblwrap">
        <table>
          <thead><tr>
            <th style="width:30px"><input type="checkbox" id="selAll" data-act="selAllBox"></th>
            <th style="width:56px">启用</th>
            <th>标题 / 文件名</th>
            <th style="width:96px">分类</th>
            <th style="width:78px">标签</th>
            <th style="width:74px" class="num">体积</th>
            <th style="width:64px" class="num">文件数</th>
            <th style="width:118px">更新</th>
            <th style="width:120px">操作</th>
          </tr></thead>
          <tbody id="browseRows"></tbody>
        </table>
      </div>
      <div class="muted hidden" id="browseMore" style="margin-top:8px"></div>
    </div>`;
  bindQbox();
  refreshBrowse();
}

/* 搜索框只在建页面时绑一次；输入法组合期间不重绘，组合结束再筛 —— 
   原来每次按键都整页重绘 + 重建 input + setSelectionRange 硬撑光标，中文候选会被打断。
   再加 120ms debounce：连打一串字只筛一次，不再每键重建 400 行表格。 */
function bindQbox() {
  const qb = $('#qbox');
  if (!qb) return;
  let t = null;
  const apply = (now) => {
    S.q = qb.value;
    clearTimeout(t);
    if (now) refreshBrowse(); else t = setTimeout(refreshBrowse, 120);
  };
  qb.oninput = () => { if (qb.dataset.composing === '1') return; apply(false); };
  qb.oncompositionstart = () => { qb.dataset.composing = '1'; };
  qb.oncompositionend = () => { delete qb.dataset.composing; apply(true); };
}

/* 只重画「会变的那部分」：筛选 chip + 计数 + 表格行。
   搜索/勾选/切 chip 都不再重建输入框，光标与输入法状态自然保持。 */
function refreshBrowse() {
  const tbody = $('#browseRows');
  if (!tbody || !S.scan) return;
  const cats = S.scan.categories, s = S.scan.summary;
  const order = Object.keys(cats).sort((a, b) => cats[a].order - cats[b].order);
  const tagCount = {};
  S.scan.mods.forEach(m => (m.tags || []).forEach(t => tagCount[t] = (tagCount[t] || 0) + 1));
  const topTags = Object.entries(tagCount).sort((a, b) => b[1] - a[1]).slice(0, 26);
  const list = filtered();
  const shown = list.slice(0, 400);

  const f = $('#browseFilters');
  if (f) {
    f.innerHTML = `
      <div class="row" style="margin-bottom:8px">
        <span class="muted" style="min-width:44px">分类</span>
        ${order.map(k => `<span class="chip ${S.cats.has(k) ? 'on' : ''}" data-act="toggleCat" data-k="${escAttr(k)}">
          <i class="dot" style="background:${escAttr(cats[k].color)}"></i>${esc(cats[k].zh)}<span class="c">${s.by_category[k] || 0}</span></span>`).join('')}
        ${S.cats.size ? '<button class="btn sm ghost" data-act="clearCats">清空</button>' : ''}
      </div>
      <div class="row">
        <span class="muted" style="min-width:44px">标签</span>
        <span class="chip ${!S.tag ? 'on' : ''}" data-act="setTag" data-tag="">全部</span>
        ${topTags.map(([t, n]) => `<span class="chip ${S.tag === t ? 'on' : ''}" data-act="setTag" data-tag="${escAttr(t)}">${esc(t)}<span class="c">${n}</span></span>`).join('')}
      </div>`;
  }
  const cnt = $('#browseCount'); if (cnt) cnt.textContent = list.length;
  const co = $('#chipOnly'); if (co) co.classList.toggle('on', S.onlyEnabled);
  const selSum = $('#browseSel'); if (selSum) selSum.textContent = S.sel.size;

  // 行用 DocumentFragment 一次插入：不再拼几百行的大 HTML 字符串
  const active = document.activeElement;
  const keep = (active && tbody.contains(active) && active.dataset && active.dataset.key)
    ? { act: active.dataset.act, key: active.dataset.key } : null;
  const frag = document.createDocumentFragment();
  shown.forEach(m => frag.appendChild(modRow(m)));
  tbody.replaceChildren(frag);
  if (keep) {
    const n = tbody.querySelector(`[data-act="${keep.act}"][data-key="${cssEsc(keep.key)}"]`);
    if (n) n.focus();
  }
  const sa = $('#selAll');
  if (sa) {
    const all = shown.length > 0 && shown.every(m => S.sel.has(m.key));
    sa.checked = all;
    sa.indeterminate = !all && shown.some(m => S.sel.has(m.key));
  }
  const more = $('#browseMore');
  if (more) {
    if (list.length > 400) { more.textContent = '仅显示前 400 条，请用搜索或分类筛选缩小范围。'; more.classList.remove('hidden'); }
    else more.classList.add('hidden');
  }
}

/* 行用 DOM API 建：mod 标题 / 文件名 / 标签 / 分类名全部来自文件系统与 VPK，
   一律走 textContent —— 不再有「拼进 onclick 的裸字符串」这条 XSS 通路。 */
function modRow(m) {
  const bad = !m.link_ok || m.parse_error;
  const tr = el('tr');
  tr.append(el('td', null, el('input', {
    type: 'checkbox', checked: S.sel.has(m.key), 'data-act': 'toggleSel', 'data-key': m.key,
  })));
  tr.append(el('td', null, el('label', { class: 'sw' },
    el('input', { type: 'checkbox', checked: !!m.enabled, 'data-act': 'oneEnable', 'data-key': m.key }),
    el('i'))));
  const title = el('div');
  title.append(el('a', { href: '#', 'data-act': 'openMod', 'data-key': m.key, text: m.title }));
  if (bad) title.append(' ', el('span', { class: 'pill bad', text: '异常' }));
  if (m.mismatch) title.append(' ', el('span', { class: 'pill warn', text: '归类冲突' }));
  const sub = el('div', { class: 'mono small' });
  sub.append(String(m.key ?? ''));
  if (m.published_id) sub.append(' · ID ' + m.published_id);
  tr.append(el('td', null, title, sub));
  tr.append(el('td', null, el('span', {
    class: 'pill', text: m.category_zh, style: pillStyle(m.color),
  })));
  const tags = el('td', { class: 'muted', style: 'font-size:11.5px' });
  const tg = (m.tags || []).slice(0, 2);
  if (tg.length) tg.forEach((t, i) => { if (i) tags.append(el('br')); tags.append(String(t)); });
  else tags.append('-');
  tr.append(tags);
  tr.append(el('td', { class: 'num', text: m.size_h }));
  tr.append(el('td', { class: 'num', text: h(m.entry_count) }));
  tr.append(el('td', { class: 'muted', style: 'font-size:11.5px', text: m.updated_h || m.mtime_h || '' }));
  tr.append(el('td', null,
    el('button', { class: 'btn sm ghost', 'data-act': 'openMod', 'data-key': m.key, text: '详情' }),
    ' ',
    el('button', { class: 'btn sm ghost', 'data-act': 'refine', 'data-key': m.key, text: '精修' })));
  return tr;
}
/* 勾选一行不再整表重建 —— 原来每点一个复选框都重建 400 行，焦点、滚动位置、
   键盘操作位置全丢，键盘基本没法用。现在只原地同步勾选态与计数。 */
function shownKeys() { return filtered().slice(0, 400).map(m => m.key); }
function syncSelState() {
  const tbody = $('#browseRows');
  if (tbody) {
    tbody.querySelectorAll('input[data-act="toggleSel"]').forEach(cb => {
      const want = S.sel.has(cb.dataset.key);
      if (cb.checked !== want) cb.checked = want;
    });
  }
  const selSum = $('#browseSel'); if (selSum) selSum.textContent = S.sel.size;
  const sa = $('#selAll');
  if (sa) {
    const shown = shownKeys();
    const all = shown.length > 0 && shown.every(k => S.sel.has(k));
    sa.checked = all;
    sa.indeterminate = !all && shown.some(k => S.sel.has(k));
  }
}
function toggleSelKey(key, on) {
  if (on === undefined) { S.sel.has(key) ? S.sel.delete(key) : S.sel.add(key); }
  else if (on) S.sel.add(key); else S.sel.delete(key);
  syncSelState();
}
function toggleSelAll(on) {
  shownKeys().forEach(k => on ? S.sel.add(k) : S.sel.delete(k));
  syncSelState();
}

/* ---- 开关 / 批量 ----
   P0：服务端 set_enabled 是「整文件读-改-写」。每行的开关是 <label class="sw"><input>，
   不在 busy() 管的 button.btn 里，所以连点两行时两个请求会并发发出、各基于同一份旧
   addonlist.txt 计算，后写的覆盖前写的 —— 前一个开关静默丢失（文件被写坏）。
   修法：所有单点开关走同一条 promise 链串行执行，请求期间该 input 禁用，失败回滚。 */
let enableChain = Promise.resolve();
async function oneEnable(key, on, input) {
  if (input) input.disabled = true;   // 单点操作也要有进行中的反馈
  const prevChain = enableChain;
  let release;
  enableChain = new Promise(res => { release = res; });
  try {
    await prevChain;                  // 排队：绝不与另一个开关的读-改-写重叠
    const r = await api('/api/enable', { changes: { [key]: on } }, 'POST');
    if (!r.ok) { toast(r.error, 'bad'); if (input) input.checked = !on; return; }
    const m = S.scan.mods.find(x => x.key === key); if (m) m.enabled = on;
    toast(`${key} 已${on ? '启用' : '停用'}（已备份 addonlist.txt）`, 'ok');
  } finally {
    if (input) input.disabled = false;
    release();
  }
}
async function doBatch(changes, label) {
  const keys = Object.keys(changes);
  if (!keys.length) { toast('没有选中任何 mod', 'bad'); return; }
  if (S.busy) { toast('上一个操作还没完成', 'bad'); return; }
  const per = (S.boot && S.boot.policy && S.boot.policy.batch_limit) || 10;
  const applied = {};                 // 只累计真正写进去的，失败批次不许标成已生效
  let done = 0, batch = 0, failed = '';
  busy(true, `正在${label}…`);
  try {
    for (let i = 0; i < keys.length; i += per) {
      const chunk = {}; keys.slice(i, i + per).forEach(k => chunk[k] = changes[k]);
      const r = await api('/api/enable', { changes: chunk }, 'POST');
      if (!r.ok) { failed = '第 ' + (batch + 1) + ' 批失败：' + r.error; break; }
      Object.assign(applied, chunk);
      done += Object.keys(chunk).length; batch++;
    }
  } finally {
    busy(false);
  }
  S.scan.mods.forEach(m => { if (m.key in applied) m.enabled = applied[m.key]; });
  // 成功的那些从已选里摘掉；失败的留着，用户可以直接重试
  Object.keys(applied).forEach(k => S.sel.delete(k));
  syncSelState();
  /* 收尾文案必须带上失败原因：原来先打一条红色「第 k 批失败」，紧接着第 705 行又无条件打
     「已完成 N 项 · ok」把它盖掉 —— 用户最后看到的是一句绿色成功，实际只写了一部分。
     批量开关会直接改写 addonlist.txt，写没写全必须一眼看得出来。 */
  if (failed) {
    toast(`${label}：已完成 ${done} / ${keys.length} 项，${failed}`, 'bad');
  } else if (done) {
    toast(`${label}：${done} 项已完成${batch > 1 ? `，分 ${batch} 批` : ''}`, 'ok');
  }
  if (batch === 0) return;   // 一批都没写进去，界面没变化，不用重画列表
  if (S.tab === 'browse') refreshBrowse();
}

async function batchIsolate() {
  const keys = [...S.sel];
  if (!keys.length) { toast('没有选中任何 mod', 'bad'); return; }
  if (S.busy) { toast('上一个操作还没完成', 'bad'); return; }
  const pv = await api('/api/isolate', { keys }, 'POST');
  // 后端「超限」错误里也带 need_confirm:True（app/api.py 的 api_isolate），
  // 所以必须先看 error —— 否则会拿一个失败响应当确认框弹出来，用户点下去还是失败。
  if (pv.error) { toast(pv.error, 'bad'); return; }
  if (!pv.need_confirm) { toast('无法执行', 'bad'); return; }
  openConfirm('移入隔离区', pv, async (opts) => {
    busy(true, '正在移入隔离区…');
    const r = await api('/api/isolate', { keys, confirm: true, isolate_repo: opts }, 'POST');
    busy(false);
    if (!r.ok) { toast(r.error, 'bad'); return; }
    S.sel.clear();
    toast(`${r.moved.length} 项已移入隔离区（可随时还原）`, 'ok');
    closeDrawer(); await loadScan(true);
  });
}

/* ================= 组件精修 ================= */
function rRefine() {
  const mods = S.scan.mods.filter(m => m.entry_count > 0);
  if (!S.refineKey && mods.length) S.refineKey = mods[0].key;
  $('#view').innerHTML = `
    <h2>组件精修</h2>
    <div class="lead">把一个 mod 的 VPK 拆成「部件」，只保留你要的那部分 —— 这就是 NekoVpk 那种「单独功能启动」。
      剔除前自动备份原件，默认另存新文件；写入后还会重读成品校验（目标必须消失、其余文件一个不少）。</div>
    <div class="card">
      <div class="row">
        <span class="muted">选择 mod</span>
        <select id="modSel" data-act="pickMod" style="width:460px">
          ${mods.map(m => `<option value="${escAttr(m.key)}" ${m.key === S.refineKey ? 'selected' : ''}>${esc(m.title)}  · ${esc(m.key)}  · ${m.entry_count} 文件 / ${esc(m.size_h)}</option>`).join('')}
        </select>
        <button class="btn" data-act="loadComp">载入部件</button>
      </div>
    </div>
    <div id="compBox">${spinner('载入中…')}</div>`;
  loadComponents();
}

/* ================= 模型归属 ================= */
/* 把「替换 A 角色」的模型改成「替换 B 角色」—— 本质是给槽位文件改名。
   贴图目录一律不动，所以不会出现改完找不到贴图的情况。 */
function reCandidates() {
  // 角色替换类必然带 models/ 目录，用它把下拉框从 200+ 缩到几十条。
  // 判据不精确（有些 mod 既带模型又带别的），detect 会给出准确结论兜底。
  const all = S.scan.mods.filter(m => m.entry_count > 0);
  const hit = all.filter(m => (m.top_dirs || []).includes('models'));
  return hit.length ? hit : all;
}

function rReassign() {
  const mods = reCandidates();
  if (!S.re.key && mods.length) S.re.key = mods[0].key;
  $('#view').innerHTML = `
    <h2>模型归属</h2>
    <div class="lead">把一个「替换某位生还者」的模型，改到另一位生还者身上。
      本质是把槽位文件改个名字 —— 贴图目录一律不动，所以不会出现改完找不到贴图的情况。</div>
    <div class="card">
      <div class="row">
        <span class="muted">选择 mod</span>
        <select id="reModSel" data-act="pickReMod" style="width:440px">
          ${mods.map(m => `<option value="${escAttr(m.key)}" ${m.key === S.re.key ? 'selected' : ''}>${esc(m.title)} · ${esc(m.key)}</option>`).join('')}
        </select>
        <button class="btn" data-act="reDetect">重新检测</button>
      </div>
      <div class="muted" style="margin-top:6px">这里只列带 <span class="mono">models/</span> 目录的 mod。
        也可以从任意 mod 详情里点「改模型归属」直接过来。</div>
    </div>
    <div id="reBox">${spinner('检测中…')}</div>`;
  reDetect();
}

async function reDetect() {
  const box = $('#reBox');
  if (!box) return;
  S.re.d = null; S.re.plan = null; S.re.target = '';
  box.innerHTML = spinner('正在解析 VPK…');
  const d = await api('/api/reassign/detect?key=' + encodeURIComponent(S.re.key));
  S.re.d = d;
  if (!d.ok) {
    box.innerHTML = `<div class="note">${esc(d.error || d.reason || '无法识别')}</div>
      <div class="muted" style="margin-top:8px">这个 mod 没有覆盖 <span class="mono">models/survivors/</span>
      下的任何生还者槽位，不是角色替换类 —— 换一个试试。</div>`;
    return;
  }
  box.innerHTML = reBody();
  applyBusy();
}

function reBody() {
  const d = S.re.d;
  const warn = d.mismatch ? `<div class="note warn">
      <b>⚠ 外部与内部不一致</b>：文件名挂在 <b>${esc(d.current_zh)}</b> 槽位上，
      但模型内部引用的却是 <b>${esc(d.internal_zh)}</b> 的动画文件。
      这说明它的归属被改过（改了名字、没改内脏）—— 不影响使用，
      但请看下面「内部动画引用」那一栏。</div>` : '';
  const targets = (d.targets || []).filter(t => t.id !== d.current);
  return `
    <div class="card">
      <h3>${esc((d.mod && d.mod.title) || '')}
        <span class="tagline">${esc(d.current_zh)} 版 · VPK v${d.version} · ${d.total_files} 个文件 · ${esc(d.total_size_h)}</span></h3>
      <div class="kv" style="grid-template-columns:104px 1fr">
        <div class="k">当前槽位</div><div><span class="pill ok">${esc(d.current_zh)}</span>
          <span class="mono small">survivor_${esc(d.current)}</span></div>
        <div class="k">内部指向</div><div>${d.internal
          ? `<span class="pill ${d.mismatch ? 'warn' : 'ok'}">${esc(d.internal_zh)}</span>
             <span class="mono small">anim_${esc(d.internal)}</span>`
          : '<span class="muted">未能判断</span>'}</div>
      </div>
      ${warn}
      ${(d.internal_refs && d.internal_refs.length)
        ? `<div class="muted" style="margin-top:8px">内部引用的动画文件：${d.internal_refs.map(esc).join('　')}</div>` : ''}
    </div>
    <div class="card">
      <h3>改到哪个角色</h3>
      <div class="row">
        ${targets.map(t => `<span class="chip ${S.re.target === t.id ? 'on' : ''}" data-act="reTarget"
          data-id="${escAttr(t.id)}" role="button" tabindex="0">${esc(t.zh)}
          <span class="c">${esc(t.game)}</span></span>`).join('')}
      </div>
      <div class="row" style="margin-top:10px">
        <label class="row" style="gap:6px">
          <input type="checkbox" id="reFix" data-act="reFixInternal" ${S.re.fixInternal ? 'checked' : ''}>
          尝试一并修正模型内部的动画引用（只能等长替换，长度不同的会跳过并说明）
        </label>
      </div>
      <div id="rePlanBox" style="margin-top:10px"></div>
    </div>`;
}

async function rePickTarget(id) {
  S.re.target = id;
  const box = $('#reBox');
  if (box && S.re.d) box.innerHTML = reBody();   // 重画一次让选中的 chip 高亮
  await rePlan();
}

async function rePlan() {
  const box = $('#rePlanBox');
  if (!box) return;
  const fixEl = $('#reFix');
  if (fixEl) S.re.fixInternal = fixEl.checked;
  box.innerHTML = spinner('正在预演…');
  const p = await api('/api/reassign/plan',
    { key: S.re.key, target: S.re.target, fix_internal: S.re.fixInternal }, 'POST');
  S.re.plan = p;
  if (!p.ok) { box.innerHTML = `<div class="note bad">${esc(p.error)}</div>`; applyBusy(); return; }
  const ip = p.internal_plan || [];
  const canFix = ip.filter(x => x.applicable);
  box.innerHTML = `
    <div class="note">
      <div class="kv" style="grid-template-columns:80px 1fr">
        <div class="k">改名</div><div><b>${p.rename_count}</b> 个文件</div>
        <div class="k">丢弃</div><div>${p.drop_count} 个</div>
        <div class="k">保留</div><div>${p.keep_count} 个</div>
        <div class="k">原始</div><div>${esc(p.src_size_h)}</div>
      </div>
    </div>
    ${p.drops.length ? `<div class="note warn"><b>会被丢弃（${p.drop_count} 项）</b>
      ${p.drops.map(x => `<div class="f mono small">${esc(x.path)}</div>
        <div class="muted small">${esc(x.why)}</div>`).join('')}</div>` : ''}
    ${ip.length ? `<div class="note ${canFix.length ? '' : 'warn'}"><b>内部动画引用</b>：
      共 ${ip.length} 处，可等长替换 ${canFix.length} 处${canFix.length ? '' : '（其余长度不同，改不了也不硬改）'}
      ${ip.map(x => `<div class="f mono small">${esc(x.from)} → ${esc(x.to)}
        <span class="pill ${x.applicable ? 'ok' : 'gray'}">${x.applicable ? '可改' : '跳过'}</span>
        ${x.why ? `<span class="muted small">${esc(x.why)}</span>` : ''}</div>`).join('')}</div>` : ''}
    <div class="grp">
      <div class="gh" data-act="fold" role="button" tabindex="0" aria-expanded="false">
        <b>改名清单（${p.rename_count}）</b><div class="spacer" style="flex:1"></div>
        <span class="muted" style="font-size:11.5px">点开看逐条对照</span>
      </div>
      <div class="gb hidden">
        ${p.renames.map(x => `<div class="f">${esc(x.from)}<br>&nbsp;&nbsp;→ ${esc(x.to)}</div>`).join('')}
      </div>
    </div>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" data-act="reApplyNew">另存为新 VPK</button>
      <button class="btn danger" data-act="reApplyReplace">原地替换（先备份）</button>
    </div>
    <div class="note warn" style="margin-top:10px">${esc(p.warning)}</div>`;
  applyBusy();
}

async function reApply(mode) {
  if (S.busy) { toast('上一个操作还没完成', 'bad'); return; }
  const payload = { key: S.re.key, target: S.re.target, mode,
                    fix_internal: S.re.fixInternal };
  const pv = await api('/api/reassign/apply', payload, 'POST');
  if (!pv.need_confirm) { toast(pv.error || '无法执行', 'bad'); return; }
  openConfirm('确认改写 VPK', pv, async () => {
    busy(true, '正在备份并改写 VPK…');
    const r = await api('/api/reassign/apply',
      Object.assign({}, payload, { confirm: true }), 'POST');
    busy(false);
    if (!r.ok) { toast(r.error, 'bad'); return; }
    const res = r.result || {}, ver = r.verify || {};
    toast(`完成：改名 ${res.改名 || 0} 项，校验${r.verify_passed ? '通过' : '未通过'}`,
          r.verify_passed ? 'ok' : 'bad');
    closeDrawer();
    openResult('改归属结果', `
      <div class="note ${r.verify_passed ? '' : 'bad'}">
        <b>成品：</b><span class="mono small">${esc(r.out)}</span><br>
        <b>体积：</b>${esc(r.out_size_h)}　<b>模式：</b>${r.mode === 'replace' ? '原地替换' : '另存新文件'}<br>
        <b>原件备份：</b><span class="mono small">${esc(r.backup)}</span>
      </div>
      <div class="note ${(ver.缺失 && ver.缺失.length) || (ver.多余 && ver.多余.length) ? 'bad' : ''}">
        <b>完整性校验：</b>${r.verify_passed ? '通过 —— 改名后的条目都在，旧名字全部消失' : '未通过'}<br>
        重读条目 ${ver.重读条目数 ?? '–'} / 期望 ${ver.期望条目数 ?? '–'}　
        缺失 ${(ver.缺失 || []).length}　多余 ${(ver.多余 || []).length}　
        残留旧名 ${(ver.残留旧名 || []).length}
        ${(ver.缺失 && ver.缺失.length)
          ? `<div class="mono small" style="margin-top:6px">缺失：${ver.缺失.slice(0, 20).map(esc).join('<br>')}</div>` : ''}
      </div>
      ${(res.内部引用已改 && res.内部引用已改.length)
        ? `<div class="note">内部动画引用已改：${res.内部引用已改.map(esc).join('　')}</div>` : ''}`);
    // 原地替换改的是实体文件，重新检测一次让界面显示新的槽位
    if (r.mode === 'replace') { S.re.d = null; await reDetect(); }
  }, mode === 'replace'
      ? '将**原地覆盖**原 VPK 文件（原件已备份到 data/backup/），addons 里的符号链接会自动跟着生效。'
      : '将另存为新文件，原 VPK 不动 —— 但新文件不会自动生效，需要手动接入 addons。');
}

async function loadComponents() {
  const box = $('#compBox');
  if (!box) return;
  box.innerHTML = spinner('正在解析 VPK 目录树…');
  const r = await api('/api/mod?key=' + encodeURIComponent(S.refineKey));
  if (!r.ok) { box.innerHTML = `<div class="note bad">${esc(r.error)}</div>`; return; }
  const m = r.mod, c = r.components;
  S.components = c;
  const nav = $('#navRefine'); if (nav) nav.textContent = c.total_files ?? '–';
  if (c.error) { box.innerHTML = `<div class="note bad">${esc(c.error)}</div>`; return; }

  box.innerHTML = `
    <div class="card">
      <h3>${esc(m.title)} <span class="tagline">${esc(m.key)} · VPK v${c.version} · ${c.total_files} 个文件 · ${esc(c.total_size_h)}</span></h3>
      ${c.safe_parts.length ? `<div class="note">
        <b>可安全移除的附加部件（实测认定，删掉不影响主体）：</b><br>
        ${c.safe_parts.map((sp, j) => `<label class="chip" style="margin:5px 6px 0 0">
          <input type="checkbox" data-act="togglePart" data-i="${j}">
          ${esc(sp.zh)} <span class="c">${sp.count} 文件</span></label>`).join('')}
        <div class="muted" style="margin-top:6px">${c.safe_parts.map(sp => esc(sp.zh) + '：' + esc(sp.why)).join('　|　')}</div>
      </div>` : ''}
      <div class="note warn">${esc(c.caution)}</div>

      <div class="row" style="margin:10px 0">
        <b>已勾选剔除 <span id="dropCount">${S.drop.size}</span> 个文件</b>
        <button class="btn sm ghost" data-act="clearDrop">清空勾选</button>
        <div class="spacer"></div>
        <button class="btn sm primary" data-act="planStrip">预演（只读，不写文件）</button>
      </div>
      <div id="planBox"></div>

      <h3 style="margin-top:16px">部件分组</h3>
      ${c.groups.map((g, i) => `
        <div class="grp">
          <div class="gh" data-act="fold" role="button" tabindex="0" aria-expanded="false">
            <span class="pill ${g.removable ? 'ok' : 'acc'}">${esc(g.kind)}</span>
            <b>${esc(g.name)}</b>
            <span class="muted">${g.count} 个文件 · ${esc(g.size_h)}</span>
            <div class="spacer" style="flex:1"></div>
            <span class="muted" style="font-size:11.5px">${esc(g.note)}</span>
            <button class="btn sm ghost" data-act="toggleGroup" data-i="${i}" data-on="1">选中</button>
            <button class="btn sm ghost" data-act="toggleGroup" data-i="${i}" data-on="0">取消</button>
          </div>
          <div class="gb hidden">
            ${g.files.slice(0, 400).map(f => `<div class="f">${esc(f)}</div>`).join('')}
            ${g.files.length > 400 ? `<div class="muted">… 其余 ${g.files.length - 400} 个已省略</div>` : ''}
          </div>
        </div>`).join('')}
    </div>`;
  applyBusy();
  syncDropState();   // 勾选态在 S.drop 里，重建面板后要把计数/复选框同步回来
}
function toggleGroup(i, on) {
  const g = (S.components && S.components.groups || [])[i];
  if (!g) return;
  g.files.forEach(f => on ? S.drop.add(f) : S.drop.delete(f));
  syncDropState();
}
function togglePartIdx(j, on) {
  const sp = (S.components && S.components.safe_parts || [])[j];
  if (!sp) return;
  sp.files.forEach(f => on ? S.drop.add(f) : S.drop.delete(f));
  syncDropState();
}
/* 勾选部件不再重新请求服务器 —— 原来每点一个分组/部件都走 loadKeepScroll →
   loadComponents → /api/mod，服务端会把整个 VPK 目录树重解析一遍（1GB 级 mod 要数秒磁盘 I/O），
   而数据早在 S.components 里。现在只原地同步计数与勾选态，并把已过期的预演结果清掉。 */
function syncDropState() {
  const n = $('#dropCount'); if (n) n.textContent = S.drop.size;
  $$('#compBox input[data-act="togglePart"]').forEach(cb => {
    const sp = (S.components && S.components.safe_parts || [])[+cb.dataset.i];
    if (!sp || !sp.files.length) return;
    const hit = sp.files.filter(f => S.drop.has(f)).length;
    cb.checked = hit === sp.files.length;
    cb.indeterminate = hit > 0 && hit < sp.files.length;
  });
  const pb = $('#planBox'); if (pb) pb.innerHTML = '';   // 预演结果对应的是旧勾选，留着会误导
}

async function planStrip() {
  if (!S.drop.size) { toast('先勾选要剔除的部件', 'bad'); return; }
  const box = $('#planBox');
  if (!box) return;
  box.innerHTML = spinner('正在预演…');
  const r = await api('/api/vpk/plan', { key: S.refineKey, drop: [...S.drop] }, 'POST');
  if (!r.ok) { box.innerHTML = `<div class="note bad">${esc(r.error)}</div>`; return; }
  const p = r.plan;
  box.innerHTML = `
    <div class="note ${p.blocked ? 'bad' : ''}">
      <div class="kv">
        <div class="k">原始</div><div>${p.total} 个文件 · ${esc(p.src_size_h)}</div>
        <div class="k">将剔除</div><div><b>${p.will_drop}</b> 个文件 · ${esc(p.drop_size_h)}</div>
        <div class="k">保留</div><div>${p.will_keep} 个文件</div>
        <div class="k">预计成品</div><div>${esc(p.estimated_size_h)}</div>
      </div>
      ${p.blocked ? `<div style="color:var(--bad);margin-top:8px">⛔ 拒绝重写：${esc(p.blocked_reason)}</div>` : ''}
      <div class="row" style="margin-top:10px">
        <button class="btn primary" ${p.blocked ? 'disabled data-base-disabled="1"' : ''} data-act="applyNew">另存为新 VPK</button>
        <button class="btn danger" ${p.blocked ? 'disabled data-base-disabled="1"' : ''} data-act="applyReplace">原地替换（先备份）</button>
      </div>
    </div>
    <div class="grp"><div class="gb" style="max-height:150px">
      ${p.drop_files.slice(0, 150).map(f => `<div class="f">- ${esc(f)}</div>`).join('')}
      ${p.will_drop > 150 ? `<div class="muted">… 其余 ${p.will_drop - 150} 个</div>` : ''}
    </div></div>`;
  applyBusy();
}

async function applyStrip(mode) {
  if (S.busy) { toast('上一个操作还没完成', 'bad'); return; }
  const pv = await api('/api/vpk/apply', { key: S.refineKey, drop: [...S.drop], mode }, 'POST');
  if (!pv.need_confirm) { toast(pv.error || '无法执行', 'bad'); return; }
  openConfirm('确认改写 VPK', pv, async () => {
    busy(true, '正在备份并重写 VPK…');
    const r = await api('/api/vpk/apply', { key: S.refineKey, drop: [...S.drop], mode, confirm: true }, 'POST');
    busy(false);
    if (!r.ok) { toast(r.error, 'bad'); return; }
    toast(`完成：剔除 ${r.stripped['剔除']} 项，保留 ${r.stripped['保留']} 项，校验${r.verify_passed ? '通过' : '未通过'}`, r.verify_passed ? 'ok' : 'bad');
    closeDrawer();
    openResult('改写结果', `
      <div class="note ${r.verify_passed ? '' : 'bad'}">
        <b>成品：</b><span class="mono small">${esc(r.out)}</span><br>
        <b>体积：</b>${esc(r.out_size_h)}　<b>模式：</b>${r.mode === 'replace' ? '原地替换' : '另存新文件'}<br>
        <b>原件备份：</b><span class="mono small">${esc(r.backup)}</span>
      </div>
      <div class="note ${r.verify.误删.length || r.verify.未删净.length ? 'bad' : ''}">
        <b>完整性校验：</b>${r.verify.ok ? '✅ 通过 —— 目标已全部消失，其余文件一个不少' : '❌ 未通过'}<br>
        未删净：${r.verify.未删净.length}　误删：${r.verify.误删.length}　大小不符：${r.verify.大小不符.length}
        ${r.verify.误删.length ? `<div class="mono small" style="margin-top:6px">误删：${r.verify.误删.slice(0, 20).map(esc).join('<br>')}</div>` : ''}
      </div>`);
    await loadScan(true);
  }, mode === 'replace' ? '将**原地覆盖**原 VPK 文件（原件已备份到 data/backup/）' : '将另存为 *_stripped.vpk，原文件不动');
}

/* ================= 详情抽屉 ================= */
let drawerToken = 0;   // 连点两个「详情」时，只让最后一次请求的结果落到抽屉里
async function openMod(key) {
  const my = ++drawerToken;
  openDrawer('载入中…', spinner('正在读取…'));
  const r = await api('/api/mod?key=' + encodeURIComponent(key));
  if (my !== drawerToken) return;
  if (!r.ok) { $('#drawerBody').innerHTML = `<div class="note bad">${esc(r.error)}</div>`; return; }
  const m = r.mod, c = r.components;
  $('#drawerTitle').textContent = m.title;
  const body = $('#drawerBody');
  body.innerHTML = `
    <div class="card">
      <div class="kv">
        <div class="k">文件名</div><div class="mono small">${esc(m.key)}</div>
        <div class="k">主分类</div><div><span class="pill acc">${esc(m.category_zh)}</span> ${esc(m.category_source || '')}</div>
        <div class="k">内容判定</div><div>${esc(S.scan.categories[m.auto_category]?.zh || m.auto_category)} ${m.mismatch ? '<span class="pill warn">与仓库归类冲突</span>' : ''}</div>
        <div class="k">启用状态</div><div>${m.enabled ? '<span class="pill ok">已启用</span>' : '<span class="pill">未启用</span>'} ${m.registered ? '' : '<span class="pill bad">未登记在 addonlist</span>'}</div>
        <div class="k">体积 / 文件数</div><div>${esc(m.size_h)} · ${m.entry_count} 个文件 · VPK v${m.vpk_version}</div>
        <div class="k">仓库分类</div><div>${esc(m.repo_category || '（非 FireAxe 仓库）')}</div>
        <div class="k">创意工坊</div><div>${m.workshop_url ? `<a href="${escAttr(m.workshop_url)}" target="_blank">${esc(m.published_id)}（${esc(m.updated_h)}）</a>` : '–'}</div>
        <div class="k">实体路径</div><div class="mono small">${esc(m.real_path)}</div>
        <div class="k">标签</div><div>${(m.tags || []).map(t => `<span class="pill acc">${esc(t)}</span>`).join(' ') || '<span class="muted">未命中素材标签</span>'}</div>
        <div class="k">官方内容标签</div><div>${(m.content_tags || []).map(t => `<span class="pill">${esc(t)}</span>`).join(' ') || '<span class="muted">addoninfo 未声明</span>'}</div>
      </div>
    </div>
    <div class="card">
      <h3>判定依据 <span class="tagline">每一步都写清楚了为什么</span></h3>
      ${(m.evidence || []).map(e => `<div class="ev ${e.startsWith('⚠') ? 'warn' : ''}">${esc(e)}</div>`).join('')}
    </div>
    ${c && c.groups ? `<div class="card">
      <h3>内部结构 <span class="tagline">${c.total_files} 个文件</span></h3>
      <div class="tblwrap" style="max-height:340px"><table>
        <tr><th>部件</th><th style="width:80px">类型</th><th style="width:66px" class="num">文件</th><th style="width:80px" class="num">体积</th><th>说明</th></tr>
        ${c.groups.map(g => `<tr><td><b>${esc(g.name)}</b></td><td><span class="pill">${esc(g.kind)}</span></td>
          <td class="num">${g.count}</td><td class="num">${esc(g.size_h)}</td><td class="muted">${esc(g.note || '')}</td></tr>`).join('')}
      </table></div>
    </div>` : ''}
    <div class="card">
      <h3>操作</h3>
      <div class="row" id="modOps"></div>
    </div>`;
  // 操作按钮用 DOM 建、key 走 dataset —— mod.key 是文件名，绝不能拼进内联 handler
  $('#modOps').append(
    el('button', { class: 'btn primary', 'data-act': 'refine', 'data-key': m.key, text: '去组件精修' }),
    el('button', { class: 'btn', 'data-act': 'reassignFrom', 'data-key': m.key, text: '改模型归属' }),
    el('button', { class: 'btn', 'data-act': 'extractVpk', 'data-key': m.key, text: '解包到文件夹' }),
    el('button', { class: 'btn danger', 'data-act': 'isolateOne', 'data-key': m.key, text: '移入隔离区…' }),
    m.real_path ? el('button', { class: 'btn ghost', 'data-act': 'openFolderOf', 'data-p': m.real_path, text: '打开所在文件夹' }) : null);
  openDrawer(m.title);
  applyBusy();
}
async function openDir(p) {
  const r = await api('/api/open-folder', { path: p }, 'POST');
  if (!r.ok) toast(r.error, 'bad');
}
async function openFolderOf(p) {
  const dir = String(p ?? '').replace(/[\\/][^\\/]*$/, '');
  const r = await api('/api/open-folder', { path: dir }, 'POST');
  if (!r.ok) toast(r.error, 'bad');
}
async function extractVpk(key) {
  const pv = await api('/api/vpk/extract', { key }, 'POST');
  if (!pv.need_confirm) { toast(pv.error || '无法执行', 'bad'); return; }
  openConfirm('确认解包', pv, async () => {
    busy(true, '正在解包…');
    const r = await api('/api/vpk/extract', { key, confirm: true }, 'POST');
    busy(false);
    toast(r.ok ? `解包完成：${r.count} 个文件 → ${r.out_dir}` : r.error, r.ok ? 'ok' : 'bad');
  }, `将把 ${pv.preview.count} 个文件（${pv.preview.size_h}）解包到 ${pv.preview.out_dir}`);
}

/* 只此一处定义：原来 523 行还有一个同步版被这里覆盖，属死代码 */
async function isolateOne(key) {
  const pv = await api('/api/isolate', { keys: [key] }, 'POST');
  if (!pv.need_confirm) { toast(pv.error || '无法执行', 'bad'); return; }
  openConfirm('移入隔离区', pv, async (isolateRepo) => {
    busy(true, '正在移入隔离区…');
    const r = await api('/api/isolate', { keys: [key], confirm: true, isolate_repo: isolateRepo }, 'POST');
    busy(false);
    if (!r.ok) { toast(r.error, 'bad'); return; }
    S.sel.clear();
    toast('已移入隔离区（可随时还原）', 'ok');
    await loadScan(true);
  }, '内容会完整保留在隔离区，addonlist.txt 里的条目会被摘除。');
}

/* ================= 审计 ================= */
async function rAudit() {
  const v = $('#view');
  const seq = newView();
  v.innerHTML = `<h2>冲突与缺失</h2><div class="lead">加载中…</div>${spinner('正在比对 ' + S.scan.summary.total + ' 个 mod 的内部资源表…')}`;
  // 同一份审计结果并发只算一次（连点导航不会重复比对 206 个 mod）
  if (!S.audit) S.audit = await once('audit', () => api('/api/audit'));
  if (stale(seq)) return;   // 期间用户切走了，旧响应不许覆盖新页面
  const a = S.audit;
  if (!a || !a.conflicts) { v.innerHTML = `<div class="note bad">审计失败：${esc(a?.error || '未知')}</div>`; return; }
  const c = a.conflicts, rd = a.redundancy, p = a.prerequisites, u = a.unlinked;
  const auditTotal = c.path_count + rd.total_findings + p.total + u.total;
  const nav = $('#navAudit'); if (nav) nav.textContent = auditTotal;

  const tabs = [['conflicts', '覆盖冲突', c.path_count], ['redundancy', '多余与异常', rd.total_findings],
                ['prereq', '前置缺失', p.total], ['unlinked', '未接入', u.total]];
  let body = '';
  if (S.auditTab === 'conflicts') body = auditConflicts(c);
  else if (S.auditTab === 'redundancy') body = auditRedundancy(rd);
  else if (S.auditTab === 'prereq') body = auditPrereq(p);
  else body = auditUnlinked(u);

  v.innerHTML = `<h2>冲突与缺失</h2>
    <div class="lead">覆盖冲突 ${c.path_count} 条 · 多余异常 ${rd.total_findings} 项 · 前置缺失 ${p.total} 条 · 未接入 ${u.total} 项</div>
    <div class="subtabs">
      ${tabs.map(([k, zh, n]) => `<span class="chip ${S.auditTab === k ? 'on' : ''}" data-act="setAuditTab" data-t="${k}">${zh}<span class="c">${n}</span></span>`).join('')}
      <div class="spacer" style="flex:1"></div>
      <button class="btn sm ghost" data-act="auditReset">重新审计</button>
    </div>${body}`;
  applyBusy();
  // 切回本页时，如果后台还有复核任务在跑，把轮询接上
  if (S.auditTab === 'conflicts' && S.ai.job && S.ai.job.state === 'running') aiPoll(S.ai.job.id);
}

/* 冲突页 */
function auditConflicts(c) {
  return `
      <div id="aiWrap">${aiPanel()}</div>
      <div class="note">${esc(c.note)}</div>
      <div class="card">
        <h3>按目录聚合 <span class="tagline">哪些目录下的内容被多个 mod 同时提供</span></h3>
        ${c.dir_groups.length ? `<div class="tblwrap"><table>
          <tr><th>目录</th><th style="width:110px">类型</th><th style="width:66px" class="num">冲突文件</th><th style="width:70px" class="num">涉及 mod</th><th>涉及 mod</th></tr>
          ${c.dir_groups.map(g => `<tr><td class="mono small">${esc(g.dir)}</td>
            <td><span class="pill">${esc(g.kind)}</span></td>
            <td class="num">${g.files}</td><td class="num">${g.mod_count}</td>
            <td class="mono small">${g.mods.map(esc).join('<br>')}${g.mod_count > 8 ? '<br>…' : ''}</td></tr>`).join('')}
        </table></div>` : '<div class="muted">没有发现冲突。</div>'}
      </div>
      <div class="card">
        <h3>最冲突的 mod 组合 <span class="tagline">两两之间共享多少条资源</span></h3>
        ${c.pairs.length ? `<div class="tblwrap" style="max-height:340px"><table>
          <tr><th style="width:170px">mod A</th><th style="width:170px">mod B</th><th style="width:80px" class="num">共享资源</th><th>样例</th></tr>
          ${c.pairs.slice(0, 40).map(pr => `<tr><td class="mono small">${esc(pr.a)}</td><td class="mono small">${esc(pr.b)}</td>
            <td class="num">${pr.shared}</td><td class="mono small">${pr.sample.map(esc).join('<br>')}</td></tr>`).join('')}
        </table></div>` : '<div class="muted">没有互相覆盖的 mod。</div>'}
      </div>
      <div class="card">
        <h3>逐条明细 <span class="tagline">前 300 条</span></h3>
        <div class="tblwrap" style="max-height:420px"><table>
          <tr><th style="width:110px">类型</th><th>资源路径</th><th style="width:64px" class="num">mod 数</th><th style="width:230px">涉及 mod</th></tr>
          ${c.items.map(it => `<tr><td><span class="pill">${esc(it.kind)}</span></td>
            <td class="mono small">${esc(it.path)}</td><td class="num">${it.mod_count}</td>
            <td class="mono small">${it.mods.slice(0, 6).map(esc).join('<br>')}${it.mod_count > 6 ? '<br>…' : ''}</td></tr>`).join('')}
        </table></div>
      </div>`;
}

/* 多余与异常页 */
function auditRedundancy(rd) {
  const meta = {
    broken_link: ['断链', 'bad', '链接指向的实体文件已经不存在 —— 游戏加载它会失败'],
    unregistered: ['未登记', 'warn', '文件在 addons 里，但 addonlist.txt 没登记 → 游戏不会加载它'],
    dead_entry: ['空挂条目', 'warn', 'addonlist 登记了，但文件已经不在了'],
    not_vpk: ['伪装扩展名', 'bad', '扩展名是 .vpk，内容其实是压缩包 —— 游戏无法识别'],
    empty: ['结构异常', 'bad', 'VPK 结构有问题或文件损坏'],
    zero_entries: ['空 VPK', 'warn', '结构正常但内部一个文件都没有'],
    duplicate_content: ['内容重复', 'warn', '内部文件清单与体积完全一致 → 大概率重复订阅'],
    duplicate_target: ['重复指向', 'warn', '多个 addons 条目指向同一个实体文件'],
    multi_vpk_dir: ['目录内多版本', 'warn', '同一个 workshop 目录里有多个 VPK'],
    outdated: ['旧版本', 'warn', '文件名带 OUTDATED 标记'],
  };
  return Object.entries(rd.groups).map(([k, arr]) => {
    const [zh, cls, why] = meta[k] || [k, 'gray', ''];
    if (!arr.length) return '';
    const hideSize = k === 'duplicate_content' || k === 'duplicate_target';
    return `<div class="card">
      <h3><span class="pill ${cls}">${zh}</span> ${arr.length} 项 <span class="tagline">${esc(why)}</span></h3>
      <div class="tblwrap" style="max-height:300px"><table>
        <tr><th style="width:200px">mod</th><th>详情</th>${hideSize ? '' : '<th style="width:80px" class="num">体积</th>'}</tr>
        ${arr.slice(0, 60).map(it => `<tr>
          <td class="mono small">${esc(it.key || it.dir || '')}</td>
          <td class="mono small">${esc(it.title || it.note || it.error || (it.mods || []).join(' / '))}
            ${it.target ? `<br>${esc(it.target)}` : ''}
            ${it.titles ? `<br>${it.titles.map(esc).join('　|　')}` : ''}</td>
          ${hideSize ? '' : `<td class="num">${esc(it.size_h || '')}</td>`}
        </tr>`).join('')}
      </table></div>
      ${arr.length > 60 ? `<div class="muted" style="margin-top:6px">仅显示前 60 项。</div>` : ''}
    </div>`;
  }).join('') || '<div class="card"><div class="muted">没有发现多余或异常的 VPK。</div></div>';
}

/* 前置缺失页 */
function auditPrereq(p) {
  const byKind = {};
  p.findings.forEach(f => (byKind[f.kind] ||= []).push(f));
  return `<div class="note">比对基准：${esc(p.checked_against)}。只有「已启用」的 mod 参与检查。</div>` +
    (Object.keys(byKind).length ? Object.entries(byKind).map(([k, arr]) => `<div class="card">
      <h3>${esc(k)} <span class="tagline">${arr.length} 项</span></h3>
      <div class="tblwrap" style="max-height:320px"><table>
        <tr><th style="width:190px">mod</th><th>问题</th><th style="width:56px">置信度</th></tr>
        ${arr.slice(0, 80).map(f => `<tr><td class="mono small">${esc(f.key)}</td>
          <td>${esc(f.detail)}</td><td>${f.confidence === '高' ? '<span class="pill bad">高</span>' : f.confidence === '中' ? '<span class="pill warn">中</span>' : '<span class="pill gray">低</span>'}</td></tr>`).join('')}
      </table></div></div>`).join('') : '<div class="card"><div class="muted">没有发现前置缺失。</div></div>');
}

/* 未接入页 */
function auditUnlinked(u) {
  return `<div class="card">
    <h3>仓库里有、但游戏加载不到 <span class="tagline">${u.total} 项</span></h3>
    ${u.total ? `<div class="tblwrap"><table>
      <tr><th style="width:120px">分类目录</th><th>标题</th><th>原因</th></tr>
      ${u.items.map(x => `<tr><td>${esc(x.category || '')}</td><td>${esc(x.title)}</td>
        <td class="muted">${esc(x.reason)}</td></tr>`).join('')}
    </table></div>` : '<div class="muted">没有遗漏。</div>'}
  </div>`;
}

/* ================= 游戏文件 ================= */
async function rGameFiles() {
  const v = $('#view');
  const seq = newView();
  v.innerHTML = `<h2>启动 / 加载 / 大厅</h2>${spinner('正在读取游戏文件状态…')}`;
  if (!S.gf) S.gf = await once('gf', () => api('/api/gamefiles'));
  if (stale(seq)) return;
  const g = S.gf;
  if (!g || !g.groups) { v.innerHTML = `<div class="note bad">读取失败：${esc(g?.error || '未知')}</div>`; return; }
  const tabs = [['startup', '启动动画'], ['lobby', '大厅背景'], ['loading', '加载画面']];
  const items = g.groups[S.gfTab]?.items || [];
  v.innerHTML = `
    <h2>启动 / 加载 / 大厅</h2>
    <div class="lead">共 ${g.total} 个可替换位置，当前已替换 ${g.replaced_count} 个。
      替换前会自动把「原始状态」抽出来存成基线备份，随时可一键还原。</div>
    <div class="note warn">${esc(g.warning)}</div>
    <div class="subtabs">
      ${tabs.map(([k, zh]) => `<span class="chip ${S.gfTab === k ? 'on' : ''}" data-act="setGfTab" data-t="${k}">${zh}<span class="c">${g.groups[k].items.length}</span></span>`).join('')}
      <div class="spacer" style="flex:1"></div>
      <button class="btn sm danger" data-act="gfRestoreAll">全部还原到原始</button>
    </div>
    <div class="card">
      <h3>${esc(g.groups[S.gfTab].zh)} <span class="tagline">备份目录：${esc(g.backup_dir)}</span></h3>
      <div class="tblwrap"><table>
        <tr><th>位置</th><th style="width:150px">状态</th><th style="width:84px" class="num">当前体积</th><th style="width:84px" class="num">原始体积</th><th style="width:110px">替换时间</th><th style="width:250px">操作</th></tr>
        ${items.map(it => `<tr>
          <td><div>${esc(it.label)}</div><div class="mono small">${esc(it.rel)}</div></td>
          <td>${it.replaced ? '<span class="pill ok">已替换</span>' : (it.in_pak ? '<span class="pill">原始（在 pak01 内）</span>' : '<span class="pill gray">不存在</span>')}
            ${it.backup_count ? `<span class="pill gray">${it.backup_count} 份备份</span>` : ''}</td>
          <td class="num">${esc(it.size_h)}</td>
          <td class="num">${esc(it.origin_size_h || '-')}</td>
          <td class="muted" style="font-size:11.5px">${esc(it.replaced_at || '-')}</td>
          <td class="row">
            <button class="btn sm primary" data-act="gfPick" data-rel="${escAttr(it.rel)}">选择文件替换</button>
            ${it.replaced ? `<button class="btn sm" data-act="gfRestore" data-rel="${escAttr(it.rel)}">还原</button>` : ''}
            ${it.rel.endsWith('.txt') ? `<button class="btn sm ghost" data-act="gfEdit" data-rel="${escAttr(it.rel)}">编辑</button>` : ''}
          </td></tr>`).join('')}
      </table></div>
    </div>
    <div id="gfEditBox"></div>
    <div class="card">
      <h3>说明 <span class="tagline">为什么这么做</span></h3>
      <div class="ev mono small">media\\*.bik —— 散装文件，直接覆盖 left4dead2\\media\\ 下同名文件即可生效</div>
      <div class="ev mono small">materials\\vgui\\loadingscreen_*.vtf —— 原本打包在 pak01_dir.vpk 内；放同路径散装文件到 left4dead2\\ 下，散装优先覆盖</div>
      <div class="ev mono small">media\\startupvids.txt —— 控制启动时播哪几段视频（可用来跳过片头）</div>
    </div>`;
}

async function gfPick(rel) {
  const ext = rel.endsWith('.txt') ? 'txt' : (rel.split('.').pop() || '');
  const p = await api('/api/pick-file?ext=' + encodeURIComponent(ext), null);
  if (!p.ok) {
    if (p.error && p.error.includes('未选择')) return;
    const manual = prompt('系统对话框不可用，请手动填写要使用的文件完整路径：\n' + (p.error || ''));
    if (!manual) return;
    return doReplace(rel, manual);
  }
  doReplace(rel, p.path);
}
async function doReplace(rel, src) {
  if (S.busy) { toast('上一个操作还没完成', 'bad'); return; }
  busy(true, '正在备份并替换…');
  const r = await api('/api/gamefiles/replace', { rel, src_path: src }, 'POST');
  busy(false);
  if (!r.ok) { toast(r.error, 'bad'); return; }
  toast(`已替换 ${rel}（${r.size_h}）；原始状态已存为基线备份，可一键还原`, 'ok');
  S.gf = null; rGameFiles();
}
async function gfRestore(rel) {
  if (!confirm('确认把 ' + rel + ' 还原到最初状态？（当前文件会先备份一份）')) return;
  busy(true, '正在还原…');
  const r = await api('/api/gamefiles/restore', { rel, which: 'baseline' }, 'POST');
  busy(false);
  toast(r.ok ? `已还原：${r.mode}` : r.error, r.ok ? 'ok' : 'bad');
  S.gf = null; rGameFiles();
}
async function gfRestoreAll() {
  if (!confirm('确认把所有被动过的游戏文件都还原到最初状态？')) return;
  busy(true, '正在全部还原…');
  const r = await api('/api/gamefiles/restore-all', {}, 'POST');
  busy(false);
  /* 后端 core/gamefiles.py 的 restore_all 在任一文件还原失败时回 ok=False
     （{"ok": not failed, "restored": [...], "failed": [...]}）。原来这里只读 restored/failed 两个数组，
     从不看 ok：一旦整个请求就没通（接口报错 / 连不上），两数组都是空 →
     界面显示「还原完成：0 项 · ok」，用户以为文件已经回到原始状态，其实一个字节都没动。
     对会改写磁盘文件的工具，这种「把失败说成成功」比崩溃更危险，必须先判 ok 再判计数。 */
  if (!r || !r.ok) {
    const n = (r && r.failed && r.failed.length) || 0;
    const msg = (r && r.error) || (n ? `${n} 个文件还原失败（磁盘可能仍是被替换后的内容）` : '未知错误');
    toast('还原失败：' + msg, 'bad');
    S.gf = null; rGameFiles();
    return;
  }
  const restored = r.restored || [], failed = r.failed || [];
  toast(`还原完成：${restored.length} 项${failed.length ? '，失败 ' + failed.length : ''}`, failed.length ? 'bad' : 'ok');
  S.gf = null; rGameFiles();
}
async function gfEdit(rel) {
  const r = await api('/api/gamefiles/read?rel=' + encodeURIComponent(rel));
  if (!r.ok) { toast(r.error, 'bad'); return; }
  const box = $('#gfEditBox');
  if (!box) return;
  box.innerHTML = `<div class="card">
    <h3>编辑 ${esc(rel)} <span class="tagline">来源：${esc(r.source)}</span></h3>
    <textarea id="gfText"></textarea>
    <div class="row" style="margin-top:9px">
      <button class="btn primary" data-act="gfSave" data-rel="${escAttr(rel)}">保存并生效</button>
      <span class="muted">写入前会自动备份。</span>
    </div></div>`;
  // 正文走 .value 而不是拼进 HTML：文件内容不进解析器，`</textarea>` 之类没有机会
  const ta = $('#gfText'); if (ta) ta.value = r.text ?? '';
  box.scrollIntoView({ behavior: 'smooth' });
}
async function gfSave(rel) {
  const ta = $('#gfText');
  if (!ta) return;
  busy(true, '正在写入…');
  const r = await api('/api/gamefiles/write', { rel, text: ta.value }, 'POST');
  busy(false);
  toast(r.ok ? '已保存并生效' : r.error, r.ok ? 'ok' : 'bad');
  S.gf = null; rGameFiles();
}

/* ================= 隔离区 ================= */
async function rStaging() {
  const v = $('#view');
  const seq = newView();
  v.innerHTML = `<h2>隔离区</h2>${spinner('读取中…')}`;
  const r = await api('/api/staging');
  if (stale(seq)) return;
  // 读取失败和「隔离区是空的」必须分开：把失败显示成空会让用户以为数据没了
  if (!r || r.error) {
    S.staging = [];
    const nav0 = $('#navStaging'); if (nav0) nav0.textContent = '';
    v.innerHTML = `<h2>隔离区</h2><div class="note bad">读取隔离区失败：${esc((r && r.error) || '未知错误')}</div>`;
    return;
  }
  const items = r.items || [];
  S.staging = items;
  const nav = $('#navStaging'); if (nav) nav.textContent = items.length;
  v.innerHTML = `
    <h2>隔离区</h2>
    <div class="lead">「卸载」= 把 mod 移到这里，而不是销毁。素材完整保留，随时可以搬回去。
      每一条都记着它的原始位置，还原时不用你再指路。</div>
    <div class="card">
      <div class="row" style="margin-bottom:9px">
        <b>${items.length} 项</b>
        <span class="muted">共 ${(items.reduce((a, b) => a + (b.size || 0), 0) / GB).toFixed(2)} GB</span>
        ${items.length ? `<button class="btn sm danger" data-act="recycleStaging">丢进回收站…</button>` : ''}
      </div>
      ${items.length ? `<div class="tblwrap"><table>
        <tr><th>条目</th><th style="width:90px" class="num">体积</th><th style="width:74px">类型</th><th style="width:120px">移入时间</th><th>原始位置</th><th style="width:150px">操作</th></tr>
        ${items.map((it, i) => `<tr>
          <td class="mono small">${esc(it.name)}</td>
          <td class="num">${esc(it.size_h)}</td>
          <td>${it.is_dir ? '<span class="pill">目录</span>' : '<span class="pill">文件</span>'}</td>
          <td class="muted" style="font-size:11.5px">${esc(it.isolated_at_h || it.mtime_h)}</td>
          <td class="mono small">${esc(it.original_path || '未记录')}</td>
          <td><button class="btn sm primary" data-act="restoreStaging" data-i="${i}">还原</button></td>
        </tr>`).join('')}
      </table></div>` : '<div class="muted">隔离区是空的。</div>'}
    </div>`;
  applyBusy();
}
async function restoreStagingIdx(i) {
  const it = S.staging[i];
  if (!it) return;
  let target = it.original_dir || '';
  if (!target) {
    target = prompt('这条记录没有留下原始位置，请指定还原目录：', (S.boot && S.boot.config && S.boot.config.repo_dir) || '');
    if (target === null) return;
  }
  busy(true, '正在还原…');
  const r = await api('/api/staging/restore', { path: it.path, target_dir: target }, 'POST');
  busy(false);
  toast(r.ok ? `已还原到 ${r.restored_to}` : r.error, r.ok ? 'ok' : 'bad');
  if (r.ok) await loadScan(true); else rStaging();
}
async function recycleStaging() {
  const all = S.staging.map(x => x.path);
  if (!all.length) { toast('隔离区是空的', 'bad'); return; }
  // 预览必须给全量：后端非 confirm 分支只回显不动作，而确认框里展示的清单
  // 必须等于真正会被处理的范围 —— 对销毁动作，「看到的」和「执行的」不能不一致。
  const pv = await api('/api/staging/recycle', { paths: all }, 'POST');
  if (pv.error) { toast(pv.error, 'bad'); return; }
  if (!pv.need_confirm) { toast('无法执行', 'bad'); return; }
  openConfirm('丢进回收站', pv, async () => {
    let done = 0, broke = false;
    busy(true, '正在丢进回收站…');
    try {
      for (let i = 0; i < all.length; i += 10) {
        const chunk = all.slice(i, i + 10);
        const rr = await api('/api/staging/recycle', { paths: chunk, confirm: true }, 'POST');
        if (!rr.ok && !(rr.recycled || []).length && !(rr.fallback || []).length) {
          toast('第 ' + (i / 10 + 1) + ' 批失败：' + (rr.error || '未知'), 'bad');
          broke = true;
          break;
        }
        done += (rr.recycled || []).length + (rr.fallback || []).length;
        if (rr.note) toast(rr.note, '');
      }
    } finally {
      busy(false);
    }
    /* 收尾这句必须按实际结果说话：原来无论断在哪批都无条件打「已处理 N / M 项 · ok」，
       等于把「第 k 批失败」的红色提示立刻盖成绿色成功 —— 用户只看到最后那一眼。
       销毁动作没说清到底删了多少，比崩溃更糟。 */
    if (broke || done < all.length) {
      toast(`已处理 ${done} / ${all.length} 项 —— 有 ${all.length - done} 项没能处理，见上方错误`, 'bad');
    } else {
      toast(`已处理 ${done} / ${all.length} 项`, 'ok');
    }
    closeDrawer(); rStaging();
  }, `共 ${all.length} 项，会分 ${Math.ceil(all.length / 10)} 批执行（每批 ≤10）。这一步是真正的销毁动作 —— 虽然进的是 Windows 回收站、仍可找回，但请确认你不再需要它们。`);
}

/* ================= 备份与日志 ================= */
async function rSafety() {
  const v = $('#view');
  const seq = newView();
  v.innerHTML = `<h2>备份与日志</h2>${spinner('读取中…')}`;
  const [b, l, s] = await Promise.all([api('/api/backups'), api('/api/log'), api('/api/status')]);
  if (stale(seq)) return;
  if (!s || !s.policy) { v.innerHTML = `<div class="note bad">读取失败：${esc(s?.error || '未知')}</div>`; return; }
  const bItems = (b && b.items) || [], lLines = (l && l.lines) || [];
  v.innerHTML = `
    <h2>备份与日志</h2>
    <div class="lead">所有破坏性动作之前都会先备份；所有动作都留痕。这两个目录是这套工具的安全网。</div>
    <div class="grid g2">
      <div class="card">
        <h3>安全策略 <span class="tagline">不可绕过</span></h3>
        <div class="kv">
          <div class="k">批量上限</div><div>单批 ≤ ${s.policy.batch_limit} 项，超出自动分批</div>
          <div class="k">删除方式</div><div>${esc(s.policy.delete_goes_to)}（不永久删除）</div>
          <div class="k">卸载语义</div><div>移入隔离区，可还原，不销毁</div>
          <div class="k">写文件</div><div>临时文件 + 原子替换，不留半成品</div>
          <div class="k">改写 VPK</div><div>先备份 → 写入 → 重读校验（目标消失 + 其余不缺）</div>
          <div class="k">备份目录</div><div class="mono small">${esc(s.policy.backup_dir)}</div>
          <div class="k">隔离目录</div><div class="mono small">${esc(s.policy.staging_dir)}</div>
          <div class="k">日志文件</div><div class="mono small">${esc(l && l.path)}</div>
        </div>
        <div class="row" style="margin-top:10px">
          <button class="btn sm ghost" data-act="openDir" data-p="${escAttr(s.policy.backup_dir)}">打开备份目录</button>
          <button class="btn sm ghost" data-act="openDir" data-p="${escAttr(s.policy.staging_dir)}">打开隔离目录</button>
        </div>
      </div>
      <div class="card">
        <h3>备份文件 <span class="tagline">${bItems.length} 个</span></h3>
        <div class="tblwrap" style="max-height:340px"><table>
          <tr><th>文件</th><th style="width:80px" class="num">体积</th><th style="width:110px">时间</th></tr>
          ${bItems.slice(0, 120).map(x => `<tr><td class="mono small">${esc(x.rel)}</td>
            <td class="num">${(x.size / MB).toFixed(2)} MB</td>
            <td class="muted" style="font-size:11.5px">${new Date(x.mtime * 1000).toLocaleString('zh-CN')}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">还没有备份（说明你还没动过任何东西）</td></tr>'}
        </table></div>
      </div>
    </div>
    <div class="card">
      <h3>操作日志 <span class="tagline">最近 300 条，倒序</span></h3>
      <div class="tblwrap" style="max-height:420px">
        ${lLines.map(x => `<div class="ev mono small">${esc(x)}</div>`).join('') || '<div class="muted">暂无日志。</div>'}
      </div>
    </div>`;
  applyBusy();
}

/* ================= 设置 ================= */
function openSettings() {
  const c = (S.boot && S.boot.config) || {};
  const cands = (S.boot && S.boot.candidates) || [];
  const tl = S.boot && S.boot.taglib;
  $('#drawerBody').innerHTML = `
    <div class="card">
      <h3>路径设置</h3>
      <div class="kv" style="grid-template-columns:110px 1fr">
        <div class="k">游戏目录</div><div><input type="text" id="cfgGame" value="${escAttr(c.game_dir || '')}" style="width:100%"></div>
        <div class="k">FireAxe 仓库</div><div><input type="text" id="cfgRepo" value="${escAttr(c.repo_dir || '')}" style="width:100%"></div>
        <div class="k">标签库</div><div><input type="text" id="cfgTag" value="${escAttr(c.taglib || '')}" style="width:100%"></div>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="btn primary" data-act="saveCfg">保存并重新扫描</button>
        <span class="muted">游戏目录要填到 <span class="mono">…\\Left 4 Dead 2\\left4dead2</span></span>
      </div>
    </div>
    <div class="card">
      <h3>本机探测到的候选 <span class="tagline">按可信度打分，分数高者才可能是真在玩的安装</span></h3>
      <div class="tblwrap"><table>
        <tr><th>路径</th><th style="width:66px" class="num">得分</th><th>依据</th></tr>
        ${cands.map(x => `<tr><td class="mono small">${esc(x.path)}</td><td class="num">${x.score}</td>
          <td class="mono small">${(x.why || []).map(esc).join('<br>')}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">没探测到候选目录</td></tr>'}
      </table></div>
    </div>
    <div class="card">
      <h3>标签库 <span class="tagline">${tl ? esc(tl.source) : '未探测到'}</span></h3>
      <div class="kv">
        <div class="k">标签数</div><div>${tl ? tl.tags : '-'}</div>
        <div class="k">路径规则</div><div>${tl ? tl.rules + ' 条' : '-'}</div>
        <div class="k">状态</div><div>${tl ? (tl.error ? '<span class="pill bad">' + esc(tl.error) + '</span>' : '<span class="pill ok">正常</span>') : '<span class="pill gray">未知</span>'}</div>
      </div>
      <div class="muted" style="margin-top:8px">留空则使用内置兜底标签库；填 NekoVpk 的 TaggedAssets.jsonc 路径可获得素材级精度。</div>
    </div>
    <div id="aiSetWrap">${aiSettingsCard()}</div>`;
  openDrawer('设置');
  aiLoadCfg();
}

/* ================= AI 冲突复核 ================= */
/* 规则引擎只能发现「路径撞了」，模型才判断得出「撞了会不会真出问题」。
   这里的三条纪律：
     ① 密钥永远不回显 —— 界面只知道「设没设」；
     ② 远程端点必须显眼标红，且首次复核要明确确认一次；
     ③ 模型输出一律当**不可信文本**转义后再插进 DOM。 */
function aiSettingsCard() {
  const a = S.ai.cfg;
  if (!a) return '<div class="card"><h3>AI 冲突复核</h3>' + spinner('读取配置…') + '</div>';
  const presetOpts = S.ai.presets.map(p =>
    `<option value="${escAttr(p.id)}" ${p.id === a.preset ? 'selected' : ''}>${esc(p.name)}</option>`).join('');
  const cur = S.ai.presets.find(p => p.id === a.preset) || {};
  return `
    <div class="card">
      <h3>AI 冲突复核 <span class="tagline">让模型判断「路径撞了」到底会不会真出问题</span></h3>
      <div class="row" style="margin-bottom:10px">
        <label class="sw"><input type="checkbox" id="aiEnabled" ${a.enabled ? 'checked' : ''}><i></i></label>
        <span class="muted">启用后，「冲突与缺失」页会出现复核按钮</span>
      </div>
      <div class="kv" style="grid-template-columns:96px 1fr">
        <div class="k">服务商</div>
        <div class="row">
          <select id="aiPreset" data-act="aiPickPreset">${presetOpts}</select>
          <span class="muted small">${esc(cur.hint || '')}</span>
        </div>
        <div class="k">接口地址</div>
        <div class="row">
          <input type="text" id="aiBase" value="${escAttr(a.base_url || '')}" style="flex:1;min-width:280px"
                 data-act="aiRefreshPriv" placeholder="http://127.0.0.1:1234/v1">
          <span id="aiPrivTag"></span>
        </div>
        <div class="k">模型</div>
        <div class="row">
          <input type="text" id="aiModel" list="aiModelList" value="${escAttr(a.model || '')}" style="flex:1;min-width:240px"
                 placeholder="留空 = 自动挑第一个">
          <datalist id="aiModelList">${(S.ai.modelList || []).map(m => `<option value="${escAttr(m)}"></option>`).join('')}</datalist>
        </div>
        <div class="k">API Key</div>
        <div class="row">
          <input type="password" id="aiKey" style="flex:1;min-width:240px"
                 placeholder="${a.api_key_set ? '已保存（•••••••）—— 留空则不改' : '本机模型一般不用填'}">
          <button class="btn sm ghost" data-act="aiClearKey">清除已存密钥</button>
        </div>
        <div class="k">节奏</div>
        <div class="row">
          每批 <input type="number" id="aiBatch" min="1" max="20" value="${a.batch_cases}"> 组
          　单次上限 <input type="number" id="aiMax" min="1" max="60" value="${a.max_cases}"> 组
        </div>
        <div class="k">超时</div>
        <div class="row">
          <input type="number" id="aiTimeout" min="10" max="600" value="${a.timeout}"> 秒
          <span class="muted small">本地小模型跑得慢，调大一点</span>
        </div>
      </div>
      <div class="row" style="margin-top:12px">
        <button class="btn primary" data-act="aiSave">保存 AI 设置</button>
        <button class="btn" data-act="aiTest">测试连接</button>
        <span class="muted small">测试会顺便把可用模型列出来</span>
      </div>
      <div id="aiTestOut" style="margin-top:10px"></div>
      <div class="note" style="margin-top:12px">
        <b>隐私边界</b>：复核时只发送 <b>mod 标题、分类标签、VPK 内部资源路径、加载顺序</b>；
        <b>不发送</b> VPK 内容、不发送游戏文件、不发送磁盘上的任何绝对路径。
        API Key 用 Windows DPAPI（当前账户）加密后存进 <span class="mono">data/ai.json</span>，
        文件里只有密文 —— 换台机器或换个 Windows 账户都解不开。
      </div>
    </div>`;
}

async function aiLoadCfg() {
  if (!S.ai.presets.length || !S.ai.cfg) {
    const r = await api('/api/ai/config');
    if (r && r.config) {
      S.ai.cfg = r.config; S.ai.presets = r.presets || [];
      S.ai.limit = r.config.max_cases || 12;
    }
  }
  // 面板首次渲染时配置可能还没取到（当时画的是「读取配置…」）—— 取到后补画一次，
  // 否则抽屉会永远停在 spinner 上，AI 设置再也填不了。
  const w = $('#aiSetWrap');
  if (w && !$('#aiEnabled')) w.innerHTML = aiSettingsCard();
  aiRefreshPriv();
}

/* 「本机 / 远程」指示器：判定权**只归服务端**，前端一个正则都不留。
   这里原本自备一套 host 正则自己算（前缀匹配会把 https://10.attacker.example/v1、
   https://192.168.1.1.evil.com/v1 也标成「本机 · 数据不出网」）—— 那是在骗用户：
   他一看写着「不出网」，就敢往里填 API Key 了。
   而且同一件事算两遍，前端和后端迟早会给出不一样的答案。
   现在只用 /api/ai/config 的权威字段 config.is_local（core/ai.py 的 public_cfg 里算好的）。 */
function aiRefreshPriv() {
  const tag = $('#aiPrivTag');
  if (!tag) return;
  const a = S.ai.cfg;
  if (!a) { tag.innerHTML = ''; return; }
  const v = (($('#aiBase') || {}).value || '').trim();
  const saved = String(a.base_url || '').trim();
  // 输入框改过但还没保存时，服务端的 is_local 描述的是「已保存的那个地址」。
  // 拿它去标当前输入同样是在撒谎，所以这个中间态只给中性提示，不冒充「本机」。
  if (v !== saved) {
    tag.innerHTML = v ? '<span class="privtag pending">未保存 · 保存后按服务端判定</span>' : '';
    return;
  }
  tag.innerHTML = saved
    ? `<span class="privtag ${a.is_local ? 'local' : 'remote'}">${a.is_local ? '本机 · 数据不出网' : '远程 · 数据会出网'}</span>`
    : '';
}
function aiPickPreset() {
  const sel = $('#aiPreset');
  if (!sel) return;
  const p = S.ai.presets.find(x => x.id === sel.value);
  if (p && p.base_url) { const b = $('#aiBase'); if (b) b.value = p.base_url; }
  const hint = sel.parentElement && sel.parentElement.querySelector('.small');
  if (hint && p) hint.textContent = p.hint || '';
  aiRefreshPriv();
}
function aiClearKey() {
  const k = $('#aiKey');
  if (!k) return;
  k.value = ''; k.dataset.clear = '1'; toast('保存后生效', '');
}

function aiFormBody() {
  const g = id => $('#' + id);
  if (!g('aiEnabled') || !g('aiKey') || !g('aiPreset')) {
    toast('设置面板已关闭，请重新打开「设置」再试', 'bad');
    return null;
  }
  const b = {
    enabled: g('aiEnabled').checked,
    preset: g('aiPreset').value,
    base_url: g('aiBase').value.trim(),
    model: g('aiModel').value.trim(),
    batch_cases: +g('aiBatch').value || 4,
    max_cases: +g('aiMax').value || 12,
    timeout: +g('aiTimeout').value || 120,
  };
  const k = g('aiKey');
  if (k.dataset.clear === '1') b.clear_key = true;
  else if (k.value.trim()) b.api_key = k.value.trim();
  return b;
}

async function aiSave() {
  const body = aiFormBody();
  if (!body) return;
  const r = await api('/api/ai/config', body, 'POST');
  if (!r.ok) { toast(r.error || '保存失败', 'bad'); return; }
  S.ai.cfg = r.config;
  const k = $('#aiKey'); if (k) { k.value = ''; delete k.dataset.clear; }
  toast('AI 设置已保存', 'ok');
  closeDrawer();
}

async function aiTest() {
  const out = $('#aiTestOut');
  if (!out) return;
  const body = aiFormBody();
  if (!body) return;
  out.innerHTML = spinner('正在连接…');
  const r = await api('/api/ai/test', body, 'POST');
  if (!r.ok) {
    out.innerHTML = `<div class="note bad"><b>连不上</b><br>${esc(r.error || '未知错误')}
      <div class="muted small" style="margin-top:6px">常见原因：模型服务没启动 / 地址少了 <span class="mono">/v1</span> / API Key 不对</div></div>`;
    return;
  }
  S.ai.modelList = r.models || [];
  out.innerHTML = `<div class="note" style="border-color:#35704d">
      <b>连接正常</b>（${r.latency}s）　可用模型 ${r.models.length} 个
      ${r.model && !r.model_ok ? '<br><span class="pill warn">你填的模型不在列表里，复核可能报错</span>' : ''}
      ${r.model ? '<br>将使用：<span class="mono">' + esc(r.model) + '</span>' : '<br><span class="pill warn">服务没返回任何模型，请手动填写模型名</span>'}
    </div>`;
}

/* ================= 冲突页 · AI 复核面板 ================= */
const RISK_ZH = { high: '高危', medium: '中等', low: '轻微', none: '不算冲突', unknown: '未判定' };
const RISK_ORDER = { high: 0, medium: 1, low: 2, none: 3, unknown: 4 };

function aiPanel() {
  const a = S.ai.cfg || {};
  const job = S.ai.job;
  const head = `<h3>AI 冲突复核
      <span class="tagline">让模型判断「路径撞了」到底会不会真出问题</span>
      ${a.base_url ? `<span class="privtag ${a.is_local ? 'local' : 'remote'}">${a.is_local ? '本机模型' : '远程模型'}</span>` : ''}
    </h3>`;

  let inner;
  if (!a.enabled) {
    inner = `<div class="muted">还没启用。到「设置 → AI 冲突复核」接一个模型接口
      （本机 LM Studio / Ollama，或任意 OpenAI 兼容服务），就能让模型逐条判断：
      下面这些「路径相撞」里哪些真的会打架、会丢掉什么、该怎么办。</div>
      <div class="row" style="margin-top:10px">
        <button class="btn" data-act="settings">去设置</button>
        <span class="muted small">规则引擎只能发现「撞了」；模型才能判断「撞了要不要紧」</span>
      </div>`;
  } else if (!job) {
    inner = `<div class="muted">规则引擎机械地找出了 <b>${S.audit?.conflicts?.path_count ?? '-'}</b> 条路径冲突，
      但路径相同 ≠ 真会打架（同一个目录里各放不同文件就不算）。模型会结合 mod 的性质与加载顺序逐条判断。</div>
      <div class="row" style="margin-top:11px">
        复核前 <input type="number" id="aiLimit" min="1" max="60" value="${S.ai.limit}"> 组（从冲突最多的开始）
        <button class="btn primary" data-act="aiStart">开始复核</button>
        <span class="muted small">${esc(a.model || '自动挑模型')} · ${esc(a.base_url || '')}</span>
      </div>`;
  } else if (job.state === 'running') {
    const pct = job.total ? Math.round(job.done / job.total * 100) : 0;
    inner = `<div class="row"><span class="spin"></span>
        <span>正在复核… <b>${job.done}/${job.total}</b> 组 · 已用 ${job.elapsed}s</span>
        <div class="spacer"></div>
        <button class="btn sm danger" data-act="aiCancel">取消</button></div>
      <div class="aibar"><i style="width:${pct}%"></i></div>
      ${(job.log && job.log.length) ? `<div class="ailog">${job.log.map(esc).join('<br>')}</div>` : ''}`;
  } else {
    inner = aiResults(job);
  }
  return `<div class="card" id="aiCard">${head}${inner}</div>`;
}

function aiResults(job) {
  const rows = (job.results || []).slice().sort(
    (x, y) => (RISK_ORDER[x.risk] ?? 9) - (RISK_ORDER[y.risk] ?? 9));
  const cnt = {};
  rows.forEach(r => cnt[r.risk] = (cnt[r.risk] || 0) + 1);
  const stateZh = { done: '已完成', error: '失败', cancelled: '已取消' }[job.state] || job.state;
  const cls = job.state === 'done' ? 'ok' : job.state === 'error' ? 'bad' : 'warn';

  let html = `<div class="row" style="margin-bottom:6px">
      <span class="pill ${cls}">${stateZh}</span>
      <span class="muted small">${rows.length} 组 · 耗时 ${job.elapsed}s · 模型 ${esc(job.meta?.model || '')}</span>
      <div class="spacer"></div>
      <button class="btn sm" data-act="aiStart">重新复核</button></div>`;

  if (job.error) html += `<div class="note bad">${esc(job.error)}</div>`;
  html += `<div class="row" style="margin:8px 0 4px">
      ${['high', 'medium', 'low', 'none', 'unknown'].filter(k => cnt[k])
        .map(k => `<span class="risk ${k}">${RISK_ZH[k]} ${cnt[k]}</span>`).join('')}</div>`;
  if (job.overall) html += `<div class="note" style="white-space:pre-wrap"><b>整体结论</b>\n${esc(job.overall)}</div>`;

  html += rows.map(r => `
    <div class="airow">
      <div class="h"><span class="risk ${esc(r.risk)}">${esc(RISK_ZH[r.risk] || r.risk)}</span>
        <span class="mono small">${esc(r.dir || '')}</span>
        ${r.kind ? `<span class="pill gray">${esc(r.kind)}</span>` : ''}
        ${r.files ? `<span class="muted small">${r.files} 个文件</span>` : ''}</div>
      <div class="d">${esc(r.what)}</div>
      ${r.lose ? `<div class="d">会被吃掉：<b>${esc(r.lose)}</b></div>` : ''}
      ${r.advice ? `<div class="advice">${esc(r.advice)}</div>` : ''}
      ${(r.mods && r.mods.length) ? `<div class="meta">涉及 mod：${r.mods.map(esc).join(' · ')}</div>` : ''}
    </div>`).join('');
  return html;
}

/* 只重画 AI 那张卡，不整页重绘 —— 否则轮询时整页闪 */
function renderAiCard() {
  const node = $('#aiWrap');
  if (node) node.innerHTML = aiPanel();
}

async function aiStart(ack) {
  if (S.ai.job && S.ai.job.state === 'running') { toast('复核已在进行中', ''); return; }
  const lim = $('#aiLimit') ? (+$('#aiLimit').value || 12) : S.ai.limit;
  S.ai.limit = lim;
  const r = await api('/api/ai/analyze', { limit: lim, ack_remote: !!ack }, 'POST');
  if (!r.ok) {
    if (r.need_remote_ack) {
      // 远程端点：把「要发什么」讲清楚再动手，这是外发动作，必须他点头
      const go = confirm(
        `AI 接口是远程服务（${r.host}）。\n\n` +
        `复核会把以下内容发送过去：\n` +
        `· mod 标题、分类标签\n· VPK 内部资源路径\n· 加载顺序\n\n` +
        `不会发送：文件内容、游戏文件、磁盘绝对路径。\n\n确认开始复核？`);
      if (go) return aiStart(true);
      return;
    }
    toast(r.error || '启动失败', 'bad');
    return;
  }
  S.ai.job = { id: r.job, state: 'running', done: 0, total: r.cases, results: [], log: [], elapsed: 0,
               meta: { model: S.ai.cfg?.model || '自动' } };
  renderAiCard();
  aiPoll(r.job);
}

async function aiCancel() {
  if (!S.ai.job) return;
  const r = await api('/api/ai/cancel', { id: S.ai.job.id }, 'POST');
  // 原来把返回值直接丢掉：请求失败也照样写「已请求取消」，
  // 用户以为停了、其实服务端还在跑并继续写审计日志。失败必须说出来。
  if (!r || !r.ok) { toast('取消失败：' + ((r && r.error) || '未知错误'), 'bad'); return; }
  toast('已请求取消，正在收尾…');
}

/* 轮询：只认最新一代（gen 变了就整条链作废）、页面隐藏时停、离开冲突页时停、
   连续失败会退避到 3 秒一次并设上限 —— 原来是无上限的 600ms 死循环。 */
let aiGen = 0;
function stopAiPoll() {
  clearTimeout(S.ai.timer);
  S.ai.timer = null;
  aiGen++;
}
function aiPoll(id) {
  stopAiPoll();
  const gen = aiGen;
  const tick = async (n) => {
    if (gen !== aiGen) return;                 // 已切页 / 已取消 / 已重开一轮
    if (document.hidden) return;               // 页面不可见：暂停，visible 时由 visibilitychange 接回
    if (!$('#aiWrap')) return;                 // 离开冲突页
    const st = await api('/api/ai/job?id=' + encodeURIComponent(id));
    if (gen !== aiGen) return;
    if (!st.ok) {
      S.ai.job = Object.assign({}, S.ai.job, { state: 'error', error: st.error });
      renderAiCard();
      return;
    }
    S.ai.job = st;
    renderAiCard();
    if (st.state === 'running') {
      if (n >= 240) { toast('复核轮询已暂停，切回本页可继续', 'bad'); return; }
      S.ai.timer = setTimeout(() => tick(n + 1), Math.min(600 * Math.pow(1.15, n), 3000));
    }
    else if (st.state === 'done') toast(`复核完成：${(st.results || []).length} 组`, 'ok');
    else if (st.state === 'error') toast('复核失败，原因见面板', 'bad');
  };
  S.ai.timer = setTimeout(() => tick(0), 600);
}

async function saveCfg() {
  if (S.busy) { toast('上一个操作还没完成', 'bad'); return; }
  const r = await api('/api/config', {
    game_dir: $('#cfgGame').value.trim(), repo_dir: $('#cfgRepo').value.trim(), taglib: $('#cfgTag').value.trim()
  }, 'POST');
  if (!r.ok) { toast(r.error, 'bad'); return; }
  toast('设置已保存，正在重新扫描…', 'ok');
  closeDrawer();
  S.boot = (await api('/api/bootstrap')) || {};
  S.audit = null; S.gf = null;
  const hp = $('#headPath');
  hp.replaceChildren('游戏目录 ', el('b', { text: (r.config && r.config.game_dir) || '' }));
  await loadScan(true);
  switchTab('overview');
}

/* ================= 抽屉 / 确认框 ================= */
function openDrawer(title, html) {
  $('#drawerTitle').textContent = title || '详情';
  if (html) $('#drawerBody').innerHTML = html;
  $('#drawer').classList.add('open'); $('#scrim').classList.add('on');
}
function openResult(title, html) {
  $('#drawerTitle').textContent = title;
  $('#drawerBody').innerHTML = html;
  $('#drawer').classList.add('open'); $('#scrim').classList.add('on');
}
function closeDrawer() {
  $('#drawer').classList.remove('open'); $('#scrim').classList.remove('on');
  cfmCb = null;   // 关掉确认框就作废回调：绝不让一个「确认」在别处被点出来
}

let cfmCb = null;
function openConfirm(title, pv, onOk, extra) {
  openDrawer(title);
  const isIsolate = !!(pv.preview);
  $('#drawerBody').innerHTML = `
    <div class="card">
      <h3>操作确认</h3>
      <div class="note warn">${esc(pv.warning || '请确认以下操作')}</div>
      ${extra ? `<div class="note">${esc(extra)}</div>` : ''}
      ${pv.count ? `<div class="ev">涉及 <b>${pv.count}</b> 项</div>` : ''}
      ${isIsolate ? `<div class="tblwrap"><table>
        <tr><th>mod</th><th>标题</th><th style="width:80px" class="num">体积</th></tr>
        ${pv.preview.map(x => `<tr><td class="mono small">${esc(x.key)}</td><td>${esc(x.title)}</td><td class="num">${esc(x.size_h)}</td></tr>`).join('')}
      </table></div>` : ''}
      ${pv.items ? `<div class="grp"><div class="gb">${pv.items.map(x => `<div class="f">${esc(x)}</div>`).join('')}</div></div>` : ''}
      ${pv.dest ? `<div class="ev mono small">去向：${esc(pv.dest)}</div>` : ''}
      ${pv.plan ? `<div class="note"><div class="kv">
        <div class="k">原始</div><div>${pv.plan.total} 个文件 · ${esc(pv.plan.src_size_h)}</div>
        <div class="k">将剔除</div><div><b>${pv.plan.will_drop}</b> 个 · ${esc(pv.plan.drop_size_h)}</div>
        <div class="k">保留</div><div>${pv.plan.will_keep} 个</div>
      </div></div>` : ''}
      <div class="row" style="margin-top:12px">
        <button class="btn primary" id="cfmOk" data-act="cfmOk">确认执行</button>
        ${isIsolate ? `<label class="chip"><input type="checkbox" id="cfmRepo" checked> 连仓库素材一起搬走</label>` : ''}
        <button class="btn ghost" data-act="drawerClose">取消</button>
      </div>
    </div>`;
  cfmCb = onOk;
}

/* ================= 启动 ================= */
boot();

/* ================= 获取 mod（一键入库 + 创意工坊订阅） ================= */
function rAcquire() {
  const ac = S.ac;
  $('#view').innerHTML = `
    <h2>获取 mod</h2>
    <div class="lead">把 mod 弄进你的 FireAxe 体系：本地 VPK 一键入库，或从创意工坊直接订阅下载。
      两者最终都落到同一个仓库结构（<span class="mono">&lt;仓库&gt;/&lt;分类&gt;/&lt;标题&gt;.workshop/</span>），
      addons 里建链接、addonlist.txt 自动登记。</div>
    <div class="row" style="margin-bottom:12px">
      <span class="chip ${ac.sub === 'ingest' ? 'on' : ''}" data-act="acSub" data-sub="ingest" role="button" tabindex="0">⇵ 一键入库</span>
      <span class="chip ${ac.sub === 'ws' ? 'on' : ''}" data-act="acSub" data-sub="ws" role="button" tabindex="0">⇩ 创意工坊订阅</span>
    </div>
    <div id="acBox">${ac.sub === 'ws' ? acWsBody() : acIngestBody()}</div>`;
  if (ac.sub === 'ws') acRescanScmd();
}

/* ---------- 子页 1：一键入库 ---------- */
function acIngestBody() {
  const ac = S.ac, p = ac.plan;
  return `
    <div class="card">
      <h3>选择 VPK 文件</h3>
      <div class="row">
        <input id="acSrc" style="width:460px" placeholder="VPK 文件的完整路径"
               value="${escAttr(ac.src || '')}">
        <button class="btn" data-act="acPick">浏览…</button>
        <button class="btn primary" data-act="acPlan">预演</button>
      </div>
      <div class="muted" style="margin-top:6px">从任何渠道搞到的 .vpk 都行 —— 网页下载的、别人发的、旧备份里的。
        预演会解析 VPK 内部、自动判定分类，不写任何文件。</div>
    </div>
    <div class="card" id="acModeCard" style="margin-top:12px">
      <h3>入库方式</h3>
      <label class="row" style="gap:6px"><input type="radio" name="acMode" value="move"
        data-act="acMode" ${ac.mode === 'move' ? 'checked' : ''}> 移动入库（源文件进回收站，可还原）</label>
      <label class="row" style="gap:6px"><input type="radio" name="acMode" value="copy"
        data-act="acMode" ${ac.mode === 'copy' ? 'checked' : ''}> 复制入库（保留源文件不动）</label>
    </div>
    <div id="acPlanBox" style="margin-top:12px">${
      p ? (p.ok ? acPlanBody(p) : `<div class="note bad">${esc(p.error || '预演失败')}</div>`) : ''}</div>
    <div class="card" style="margin-top:12px">
      <h3>链接能力自检</h3>
      <div class="muted" style="margin-bottom:6px">入库时 addons 里放的是「链接」还是「副本」，
        取决于本机权限。点一下就知道这台机器能建哪种。</div>
      <button class="btn small" data-act="acProbeLink">检测</button>
      <div id="acProbeBox" style="margin-top:8px"></div>
    </div>`;
}

function acPlanBody(p) {
  const info = p.info || {};
  return `
    <div class="card">
      <h3>预演结果</h3>
      <div class="note ${p.clash_dir || p.clash_vpk || p.dup_link ? 'warn' : ''}">
        <div class="kv" style="grid-template-columns:96px 1fr">
          <div class="k">标题</div><div><b>${esc(info.title || '')}</b></div>
          <div class="k">判定分类</div><div><span class="pill ok">${esc(info.category_zh || '')}</span>
            <span class="muted small">${esc(info.category_source || '')}</span></div>
          <div class="k">体积 / 文件</div><div>${esc(info.size_h || '')} · ${info.total_files || 0} 个内部文件</div>
          <div class="k">落到</div><div class="mono small">${esc(p.cat_dir || '')}\\<b>${esc(info.title || '')}</b>.workshop\\${esc(p.dst_name || '')}</div>
        </div>
        ${p.dup_link ? `<div class="note bad" style="margin-top:8px">addons 里已有同一个 mod（<span class="mono">${esc(p.dup_link)}</span>），不能重复入库</div>` : ''}
        ${p.clash_dir && !p.dup_link ? `<div class="note warn" style="margin-top:8px">目标 .workshop 目录已存在：执行时会先备份原 VPK 再覆盖</div>` : ''}
      </div>
      <div class="muted" style="margin-top:8px">判定依据（前几条）：${(info.evidence || []).map(esc).join('；')}</div>
      <div class="row" style="margin-top:12px">
        <button class="btn primary" data-act="acIngest" ${p.dup_link ? 'disabled' : ''}>确认入库</button>
        <span class="muted">入库后自动同步扫描结果，界面立刻能看到新 mod</span>
      </div>
    </div>`;
}

/* ---------- 子页 2：创意工坊订阅 ---------- */
function acWsBody() {
  const ac = S.ac, sc = ac.scmd, j = ac.job;
  const scState = !sc ? '<span class="muted">探测中…</span>'
    : sc.found ? `<span class="pill ok">已找到</span> <span class="mono small">${esc(sc.path)}</span>`
    : `<span class="pill warn">未安装</span> <span class="muted small">${esc(sc.hint || '')}</span>
       <button class="btn primary small" data-act="acInstall">一键安装</button>`;
  return `
    <div class="card">
      <h3>从创意工坊下载</h3>
      <div class="row">
        <input id="acWsInput" style="width:460px" placeholder="创意工坊链接或纯数字 item id（如 3393762832）">
        <button class="btn primary" data-act="acDownload">下载并入库</button>
        <button class="btn" data-act="acBrowser">浏览器下载</button>
      </div>
      <div class="muted" style="margin-top:6px">下载走 Valve 官方的 SteamCMD（匿名登录，公开 item 可直接下）；
        下载完成的 VPK 自动走「一键入库」同一套落地流程。
        若当前网络环境 SteamCMD 通道不可用（如 Steam++ 的 hosts 劫持半开状态），
        可改用「浏览器下载」——浏览器能读系统代理，绕开劫持。</div>
      <div class="row" style="margin-top:10px">
        <span class="muted">SteamCMD：</span>${scState}
        <button class="btn small" data-act="acRescanScmd">重新探测</button>
      </div>
      <div class="row" style="margin-top:6px" id="acEnvRow"></div>
    </div>
    <div id="acJobBox" style="margin-top:12px">${j ? acJobBody(j) : ''}</div>`;
}

function acJobBody(j) {
  const st = j.state;
  const pill = st === 'done' ? 'ok' : st === 'failed' ? 'bad' : st === 'cancelled' ? 'gray' : 'warn';
  const r = j.result || {};
  // install（装 SteamCMD）和 ugc（PowerShell 直下）都有 percent 进度
  const isProgress = j.kind === 'install' || j.kind === 'ugc';
  const bar = (isProgress && st === 'running') ? `
      <div class="row" style="margin-top:8px"><div class="muted small">下载进度</div>
        <progress value="${j.percent || 0}" max="100" style="flex:1"></progress>
        <span class="small">${j.percent || 0}%</span></div>` : '';
  return `
    <div class="card">
      <h3>${j.kind === 'install' ? '安装 SteamCMD' : j.kind === 'ugc' ? '创意工坊直下' : '下载任务'} <span class="pill ${pill}">${
        st === 'running' ? '进行中' : st === 'done' ? '完成' : st === 'cancelled' ? '已取消' : '失败'}</span>
        <span class="tagline">item ${esc(j.item_id || '')}</span></h3>
      ${st === 'running' ? `<div class="row"><button class="btn small" data-act="acCancel">取消任务</button></div>` : ''}
      ${j.kind === 'install' && r.ok ? `<div class="note ok">安装完成：<span class="mono small">${esc(r.path || '')}</span></div>` : ''}
      ${r.title ? `<div class="note ok">已入库：<b>${esc(r.title)}</b></div>` : ''}
      ${bar}
      ${r.error ? `<div class="note bad">${esc(r.error)}</div>` : ''}
      <details style="margin-top:8px"><summary class="muted">运行日志（${(j.log || []).length} 行）</summary>
        <div class="grp"><div class="gb mono small">${(j.log || []).map(esc).join('<br>')}</div></div>
      </details>
    </div>`;
}

/* ---------- 交互 ---------- */
async function acPick() {
  const r = await api('/api/pick-file?ext=vpk');
  if (r.ok && r.path) {
    S.ac.src = r.path;
    const inp = $('#acSrc');
    if (inp) inp.value = r.path;
    S.ac.plan = null;
    const box = $('#acPlanBox');
    if (box) box.innerHTML = '';
  } else if (r.error) {
    toast(r.error);
  }
}

async function acPlan() {
  const inp = $('#acSrc');
  S.ac.src = (inp ? inp.value : '').trim();
  if (!S.ac.src) { toast('先选一个 VPK 文件'); return; }
  const box = $('#acPlanBox');
  if (box) box.innerHTML = spinner('正在解析 VPK…');
  const p = await api('/api/acquire/plan', { src_path: S.ac.src }, 'POST');
  S.ac.plan = p;
  if (box) box.innerHTML = p.ok ? acPlanBody(p)
    : `<div class="note bad">${esc(p.error || '预演失败')}</div>`;
}

async function acIngest() {
  const ac = S.ac;
  if (!ac.src) { toast('先选一个 VPK 文件'); return; }
  const pv = await api('/api/acquire/ingest',
    { src_path: ac.src, mode: ac.mode, confirm: false }, 'POST');
  if (pv.ok) { toast('已入库'); return; }
  if (!pv.need_confirm) { toast(pv.error || '入库失败'); return; }
  openConfirm('确认入库', pv, async () => {
    const r = await api('/api/acquire/ingest',
      { src_path: ac.src, mode: ac.mode, confirm: true }, 'POST');
    if (r.ok) {
      S.ac.plan = null;
      S.ac.src = '';
      toast('入库成功' + (r.link_mode === '副本（降级）' ? '（当前环境建不了符号链接，addons 里是副本）' : ''));
      loadScan(true);
    } else {
      toast(r.error || '入库失败');
    }
  });
}

async function acRescanScmd() {
  S.ac.scmd = await api('/api/acquire/steamcmd');
  if (S.ac.sub === 'ws' && $('#acBox')) $('#acBox').innerHTML = acWsBody();
  acEnv();
}

/* 环境状态行：SteamCMD 通道可不可用（hosts 劫持 / PAC 探测） */
async function acEnv() {
  const row = $('#acEnvRow');
  if (!row) return;
  const e = await api('/api/acquire/env');
  if (!e.ok) { row.innerHTML = ''; return; }
  if (e.steamcmd_usable) {
    row.innerHTML = '<span class="pill ok">通道正常</span> <span class="muted small">SteamCMD 可直接下载</span>';
  } else {
    row.innerHTML = `<span class="pill warn">通道受限</span>
      <span class="muted small">${esc(e.reason || '')}</span>
      <span class="muted small">→ 用「浏览器下载」</span>`;
  }
}

async function acBrowser() {
  const inp = $('#acWsInput');
  const text = (inp ? inp.value : '').trim();
  if (!text) { toast('先贴创意工坊链接或 item id'); return; }
  const r = await api('/api/acquire/browser', { input: text }, 'POST');
  if (!r.ok) { toast(r.error || '打开浏览器失败'); return; }
  const box = $('#acJobBox');
  if (box) box.innerHTML = `<div class="note warn"><b>已打开创意工坊页面</b>：${esc(r.hint || '')}
    <div class="muted small" style="margin-top:4px">下完 VPK 后回到本页「一键入库」选它即可。</div></div>`;
  toast('浏览器已打开，下完 VPK 回来一键入库');
}

async function acDownload() {
  const inp = $('#acWsInput');
  const text = (inp ? inp.value : '').trim();
  if (!text) { toast('先贴创意工坊链接或 item id'); return; }
  const r = await api('/api/acquire/workshop', { input: text }, 'POST');
  if (r.need_browser) {
    // 环境闸门拦下：hosts 劫持 + PAC 不在线，SteamCMD 必死——直接导航到浏览器路线
    toast(r.error || '当前环境 SteamCMD 不可用');
    const b = await api('/api/acquire/browser', { input: text }, 'POST');
    if (b.ok) {
      S.ac.job = null;
      const box = $('#acJobBox');
      if (box) box.innerHTML = `<div class="note warn"><b>已改用浏览器下载</b>：${esc(b.hint || '')}
        <div class="muted small" style="margin-top:4px">下完 VPK 后回到本页「一键入库」选它即可。</div></div>`;
    } else {
      toast(b.error || '打开浏览器失败');
    }
    return;
  }
  if (!r.ok) { toast(r.error || '启动下载失败'); return; }
  S.ac.job = { id: r.job, item_id: '', state: 'running', log: [], result: null };
  acJobPoll(r.job);
}

async function acJobPoll(id) {
  if (S.ac.timer) { clearTimeout(S.ac.timer); S.ac.timer = null; }
  const tick = async () => {
    const j = await api('/api/acquire/job?id=' + encodeURIComponent(id));
    if (j.ok) S.ac.job = j.job;
    if (S.ac.sub === 'ws' && $('#acJobBox')) $('#acJobBox').innerHTML = acJobBody(S.ac.job);
    if (S.ac.job && S.ac.job.state === 'running') {
      S.ac.timer = setTimeout(tick, 1200);
    } else if (S.ac.job && S.ac.job.state === 'done') {
      if (S.ac.job.kind === 'install') {
        toast('SteamCMD 安装完成');
        acRescanScmd();          // 刷新探测状态，「一键安装」按钮变「已找到」
      } else {
        toast('下载并入库完成');
        loadScan(true);
      }
    }
  };
  tick();
}

async function acCancel() {
  const j = S.ac.job;
  if (!j) return;
  await api('/api/acquire/cancel', { id: j.id }, 'POST');
  toast('已取消（SteamCMD 进程可能还在跑完当前 item，产物不会入库）');
}

/* 一键安装 SteamCMD：先拿知情确认文案，用户点头才真下 */
async function acInstall() {
  const first = await api('/api/acquire/install', { ack: false }, 'POST');
  if (first.ok) { acRescanScmd(); toast('SteamCMD 已就绪'); return; }
  if (!first.need_ack) { toast(first.error || '启动安装失败'); return; }
  openConfirm('安装 SteamCMD', {
    warning: first.error, count: 1,
    items: ['来源：Valve 官方网站（steamcdn-a.akamaihd.net）',
            '装入：data/steamcmd/（随数据目录走，便携）',
            '体积：安装包约 3 MB，首次运行自动更新到约 100 MB',
            '协议：Steam 安装协议适用（developer.valvesoftware.com）'],
  }, async () => {
    const r = await api('/api/acquire/install', { ack: true }, 'POST');
    if (!r.ok) { toast(r.error || '启动安装失败'); return; }
    S.ac.job = { id: r.job, kind: 'install', item_id: '', state: 'running', log: [], result: null, percent: 0 };
    acJobPoll(r.job);
  });
}

/* 链接能力自检：告诉用户这台机器能建符号链接还是只能硬链接 */
async function acProbeLink() {
  const box = $('#acProbeBox');
  if (!box) return;
  box.innerHTML = spinner('检测中…');
  const r = await api('/api/acquire/linkprobe');
  const e = r.errors || {};
  box.innerHTML = `<div class="note ${r.symbolic ? 'ok' : (r.hardlink ? 'warn' : 'bad')}">
    <div class="kv" style="grid-template-columns:92px 1fr">
      <div class="k">符号链接</div><div>${r.symbolic
        ? '✅ 可用（FireAxe 同款，带 -> 箭头）'
        : '❌ 不可用 <span class="muted small">' + esc(e.symbolic || '') + '</span>'}</div>
      <div class="k">硬链接</div><div>${r.hardlink
        ? '✅ 可用（同一份数据，不占双倍空间）'
        : '❌ 不可用 <span class="muted small">' + esc(e.hardlink || '') + '</span>'}</div>
      <div class="k">同卷</div><div>${r.same_volume
        ? '✅ 仓库与 addons 在同一分区'
        : '❌ 不在同一分区（硬链接不可用）'}</div>
    </div>
    <div class="muted small" style="margin-top:6px">${esc(r.note || '')}</div>
  </div>`;
}
