/* 文献精读助手 · 前端逻辑
 *
 * 与 Python 的交互约定：
 *   - 所有后端方法都返回 {ok, data, error}
 *   - 长任务（翻译）通过 window.__push(事件名, 数据) 主动推送给前端
 *
 * 为什么译文要边翻边显示：
 *   一篇 20 页文献整篇翻完要几分钟。如果等全部完成再渲染，期间界面毫无反馈，
 *   用户会以为卡死了。所以每批译文一到就插进对应段落，让人能立刻开始读摘要。
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
/**
 * 按 id 取元素。
 *
 * 必须用它，不能用 $("pf-base") —— $ 是 querySelector，
 * 传 "pf-base" 会被当成**标签名**去匹配，返回 null。
 * 之前服务商选了不填地址，就是因为 applyProvider 里这么写了：
 * 第一行取提示元素就拿到 null，抛 TypeError，后面填地址的代码根本没执行到。
 * 而单元测试的 $ 桩是「按 key 直接取」，把这个问题掩盖了。
 */
const byId = (id) => document.getElementById(id);

const state = {
  ready: false,
  dataDir: "",
  profiles: [],
  ui: { show_zh: true, font_size: 16, sidebar_open: true, zh_only: false },
  paper: null,
  setupDir: "",
  terms: [],
  termCount: 0,
};

let toastTimer = null;

/* ============ 通用 ============ */

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 6000 : 3000);
}

/* 与后端的通信：本地 HTTP 服务，POST /api/<方法名>，参数放在 args 数组里。
 * 后端统一返回 {ok, data, error}。不用 try/catch 猜，看 ok 就知道成没成。 */
async function call(fn, ...args) {
  try {
    const resp = await fetch(`/api/${fn}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args }),
    });
    const r = await resp.json();
    if (!r) return { ok: false, error: "后端没有返回结果" };
    if (!r.ok && r.error) toast(r.error, true);
    return r;
  } catch (e) {
    const raw = String(e);
    // 后端进程已经退出时，浏览器只会给一句 "Failed to fetch"，使用者完全看不懂。
    // 真实情况是：窗口还在，但本地服务已经关了，点什么都没反应。
    const offline = /Failed to fetch|NetworkError|Load failed|net::ERR/i.test(raw);
    const msg = offline
      ? "后台服务已经退出了（窗口还在，但已经连不上）。请关掉这个窗口，重新双击「文献精读助手.exe」。"
      : `调用 ${fn} 失败：${raw}`;
    toast(msg, true);
    return { ok: false, error: msg, offline };
  }
}

/**
 * 心跳：每 25 秒告诉后台「窗口还开着」。
 *
 * 后端用「多久没收到请求」来判断窗口是不是关了（超时就退出进程）。
 * 但人开着窗口读文献、想事情的时候本来就不产生请求，
 * 没有心跳的话，静置 90 秒程序就自己退出了 —— 窗口变成空壳，
 * 之后点任何按钮都只会报 Failed to fetch。
 */
let _heartbeat = null;
function startHeartbeat() {
  if (_heartbeat || location.hash === "#demo") return;
  const beat = () => {
    fetch("/api/ping", { method: "POST", body: "{}" }).catch(() => { /* 服务没了就只能靠提示了 */ });
  };
  _heartbeat = setInterval(beat, 25000);
  beat();
}

function showView(name) {
  $$(".view").forEach((v) => v.classList.add("hidden"));
  const el = $(`#view-${name}`);
  if (el) el.classList.remove("hidden");
}

/* ============ 状态同步 ============ */

function applySnapshot(snap) {
  if (!snap) return;
  state.dataDir = snap.dataDir || state.dataDir;
  state.profiles = snap.profiles || [];
  state.ui = Object.assign(state.ui, snap.ui || {});
  state.termCount = snap.termCount ?? state.termCount;

  $("#tab-term-count").textContent = snap.termCount ?? 0;
  $("#tab-card-count").textContent = snap.cardCount ?? 0;

  if (snap.cloudHint) {
    const hint = $("#setup-cloud-hint");
    hint.textContent =
      `⚠ 这个位置看起来是云同步目录（${snap.cloudHint}）。数据库放在这里，` +
      `同步冲突可能导致术语库损坏。建议换成本地文件夹。`;
    hint.classList.remove("hidden");
  }

  // 没有一套能用的配置时，在顶栏挂个提示。否则删光配置的人拖进 PDF 才发现翻不了。
  const usable = (snap.profiles || []).some((p) => p.ready);
  $("#profile-badge").classList.toggle("hidden", usable);

  renderLibrary(snap.library || []);
}

/* ============ 服务商预设 ============ */
/*
 * 每家的接口地址和模型名都不一样，让使用者自己查文档填，是最容易卡住的一步。
 * 这里把常见几家的参数固化下来：选完服务商，地址和模型自动填好，只剩密钥要粘。
 * 地址都是 OpenAI 兼容入口（我们的请求就是按这个协议发的）。
 */
const PROVIDERS = [
  {
    // 默认项：什么都不填。一定要有这一项，否则下拉框一打开就显示「阿里云百炼」，
    // 看着像是已经选好了，但地址框其实是空的 —— 使用者会以为「选了没反应」。
    id: "",
    label: "请选择服务商",
    base: "",
    model: "",
    tip: "选一家会自动把地址和模型填进下面；手上是学校/实验室接口、中转站的，选最下面的「其他」自己填。",
  },
  {
    id: "bailian",
    label: "阿里云百炼 · 国内站（通义千问）",
    base: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: "qwen-plus",
    tip: "密钥：bailian.console.aliyun.com → 右上角 API-KEY 管理 → 创建（需实名认证，开通免费）。国内站账号用这个地址，新加坡账号请选下面那个国际站。",
  },
  {
    id: "bailian_intl",
    label: "阿里云百炼 · 国际站（新加坡）",
    base: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    model: "qwen-plus",
    tip: "国际站（Alibaba Cloud）账号才用这个地址。国内站密钥配到这里会报 401 invalid_api_key。国际站可用模型比国内少。",
  },
  {
    id: "deepseek",
    label: "DeepSeek 官方",
    base: "https://api.deepseek.com",
    model: "deepseek-chat",
    tip: "密钥：platform.deepseek.com → 充值 → API keys 创建。价格低，读论文够用；想更强可改 deepseek-reasoner。",
  },
  {
    id: "kimi",
    label: "月之暗面 Kimi",
    base: "https://api.moonshot.cn/v1",
    model: "kimi-latest",
    tip: "密钥：platform.moonshot.cn → 账户充值 → API Key 管理。kimi-latest 会自动指向当前最新模型。",
  },
  {
    id: "zhipu",
    label: "智谱 GLM",
    base: "https://open.bigmodel.cn/api/paas/v4",
    model: "glm-4-flash",
    tip: "密钥：open.bigmodel.cn → API Keys。glm-4-flash 长期免费额度，适合先试；要更好质量换成控制台里的新模型名。",
  },
  {
    id: "siliconflow",
    label: "硅基流动 SiliconFlow",
    base: "https://api.siliconflow.cn/v1",
    model: "",
    tip: "地址已填好。模型名必须带组织前缀（如 deepseek-ai/DeepSeek-V3），从「模型广场」复制完整名称粘到模型名里，别自己拼。",
  },
  {
    id: "ark",
    label: "火山方舟（豆包）",
    base: "https://ark.cn-beijing.volces.com/api/v3",
    model: "",
    tip: "地址已填好。模型名填控制台「在线推理」里的模型 ID（如 doubao-seed-2-0-lite-260215）或推理接入点 ep-xxxx，两者都能用。",
  },
  {
    id: "hunyuan",
    label: "腾讯混元",
    base: "https://api.hunyuan.cloud.tencent.com/v1",
    model: "hunyuan-turbos-latest",
    tip: "密钥：腾讯云 TokenHub 控制台创建。并发默认 5，翻译大批段落时会自动排队重试，不用管。",
  },
  {
    id: "custom",
    label: "其他 / 中转站 / 学校接口（手动填）",
    base: "",
    model: "",
    tip: "填对方给你的接口地址（通常以 /v1 结尾）和模型名。地址别带 /chat/completions，程序会自己拼。",
  },
];

function providerById(id) {
  return PROVIDERS.find((p) => p.id === id) || PROVIDERS[0];
}

/** 把下拉框填好；只填一次，之后由用户切换 */
function fillProviderSelect(sel) {
  if (!sel || sel.dataset.ready) return;
  sel.innerHTML = PROVIDERS.map(
    (p) => `<option value="${p.id}">${p.label}</option>`
  ).join("");
  sel.dataset.ready = "1";
}

/** 选中某个服务商：把地址和模型填进表单。
 * - 不碰「名称」：名字是使用者自己认这套配置的线索，替他起名只会在列表里造成混乱
 * - 选「请选择服务商」时什么都不填，只换提示
 * - 选「其他」时也不填，留给使用者手动填
 */
function applyProvider(id, ids) {
  const p = providerById(id);
  const tipEl = ids.tip ? byId(ids.tip) : null;
  if (tipEl) tipEl.textContent = p.tip;

  const baseEl = byId(ids.base);
  if (!baseEl) return;                  // 元素不在页面上就安静退出，别抛错把后面全带崩
  if (!p.id || p.id === "custom") return;

  baseEl.value = p.base;
  const modelEl = ids.model ? byId(ids.model) : null;
  if (modelEl) modelEl.value = p.model;
  // 明确告诉使用者「真的填进去了」。之前地址改动太安静，
  // 下拉框又是预选状态，看起来就像「切了没反应」。
  toast(`已填入 ${p.label} 的地址和模型`);
}

/**
 * 地址框与服务商下拉的双向一致：
 * 使用者如果手动把地址改成别家的，下拉框跟着切到「其他」。
 * 否则会出现「显示阿里云百炼、地址却是 api.deepseek.com」这种自相矛盾的状态，
 * 报错时根本分不清是地址错还是密钥错。
 */
function bindBaseGuard(baseId, providerId, tipId) {
  const el = byId(baseId);
  if (!el) return;
  el.addEventListener("input", () => {
    const sel = byId(providerId);
    if (!sel) return;
    const p = providerById(sel.value);
    if (!p.id || p.id === "custom") return;              // 「请选择」或已经是「其他」
    if (p.base && el.value.trim() === p.base) return;    // 与预设一致，不改
    sel.value = "custom";
    const t = tipId ? byId(tipId) : null;
    if (t) t.textContent = providerById("custom").tip;
  });
}

/* ============ 首次引导 ============ */

function showSetup() {
  $("#setup-datadir").textContent = state.dataDir || "尚未选择";
  state.setupDir = state.dataDir || "";
  // 首次进来：地址是空的就显示「请选择服务商」，别替使用者预选一家，
  // 否则下拉框看着像已经选好了、输入框却是空的，会让人以为功能坏了
  const sel = $("#setup-provider");
  fillProviderSelect(sel);
  const curBase = $("#setup-base").value.trim();
  const match = curBase
    ? (PROVIDERS.find((p) => p.base && p.base === curBase) || providerById("custom"))
    : providerById("");
  sel.value = match.id;
  $("#setup-provider-tip").textContent = match.tip;
  showView("setup");
}

async function setupChooseDir() {
  const r = await call("pick_folder");
  if (!r.ok || !r.data) return;      // 用户取消，什么都不做
  state.setupDir = r.data;
  $("#setup-datadir").textContent = r.data;
  const s = await call("set_data_dir", r.data, false);
  if (s.ok) applySnapshot(s.data);
}

/* 系统对话框用不了时的兜底：用户自己粘贴路径 */
async function setupApplyManual() {
  const v = $("#setup-dir-manual").value.trim();
  if (!v) { toast("先把路径粘进去", true); return; }
  const s = await call("set_data_dir", v, false);
  if (s.ok) {
    state.setupDir = state.dataDir || v;
    $("#setup-datadir").textContent = state.dataDir || v;
    $("#setup-dir-manual").value = "";
    applySnapshot(s.data);
  }
}

/**
 * 统一的换目录逻辑：对话框选中的、手动粘贴的都走这里，
 * 免得两条路径行为不一致（之前改目录只有对话框那一份代码，手填的没法复用）。
 */
async function changeDataDir(dir, ask = true) {
  let migrate = false;
  if (ask && (state.termCount || 0) > 0) {
    migrate = confirm(
      `把现有的 ${state.termCount} 条术语和译文缓存一起搬到新目录吗？\n\n` +
      "确定 = 搬过去\n取消 = 只换位置，旧数据留在原处"
    );
  }
  const s = await call("set_data_dir", dir, migrate);
  if (!s.ok) return false;
  applySnapshot(s.data);
  $("#settings-datadir").textContent = state.dataDir;
  if (migrate) toast("数据已迁移到新目录");
  return true;
}

async function setupTest() {
  const base = $("#setup-base").value.trim();
  const key = $("#setup-key").value.trim();
  const model = $("#setup-model").value.trim();
  const out = $("#setup-test-result");
  if (!key) {
    out.textContent = "先填密钥";
    out.className = "small warn";
    return;
  }
  out.textContent = "正在测试…";
  out.className = "small muted";
  const r = await call("test_raw", base, key, model);
  if (r.ok && r.data) {
    out.textContent = (r.data.success ? "✓ " : "✗ ") + r.data.message;
    out.className = "small " + (r.data.success ? "" : "warn");
  } else {
    out.textContent = r.error || "测试失败";
    out.className = "small warn";
  }
}

async function setupFinish() {
  const name = $("#setup-name").value.trim();
  const base = $("#setup-base").value.trim();
  const model = $("#setup-model").value.trim();
  const key = $("#setup-key").value.trim();

  if (!name) {
    toast("给这套配置起个名字，比如「百炼免费额度」——以后在列表里靠它区分", true);
    $("#setup-name").focus();
    return;
  }
  if (!base) {
    toast("接口地址还是空的：在上面选一个服务商就会自动填", true);
    return;
  }
  if (!model) {
    toast("模型名还是空的：选服务商会自动填，比如 qwen-plus", true);
    return;
  }
  if (!key) {
    toast("还没填密钥。没有它没法翻译，先去申请一个。", true);
    $("#setup-key").focus();
    return;
  }

  const payload = { name, base_url: base, api_key: key, model };
  const active = state.profiles.find((p) => p.active);
  if (active) payload.id = active.id;

  const r = await call("save_profile", payload);
  if (!r.ok) return;
  applySnapshot(r.data);
  showHome();
  toast("配置好了，拖一篇 PDF 进来试试");
}

/* 通用确认框。返回被选中那一项的 value，取消返回 null。
 *
 * 为什么不用 window.confirm：删除文献要让人在「只删记录」和「连术语一起删」
 * 之间选，confirm 只有「确定/取消」，逼着人二选一猜意思。
 * 用自绘的框，按钮上直接写清楚后果。 */
function askChoice({ title, body = "", choices = [], cancelLabel = "取消" }) {
  return new Promise((resolve) => {
    const box = $("#confirm");
    const acts = $("#confirm-actions");
    $("#confirm-title").textContent = title;
    $("#confirm-body").textContent = body;
    acts.innerHTML = "";

    const done = (val) => {
      box.classList.add("hidden");
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (e) => { if (e.key === "Escape") done(null); };

    for (const c of choices) {
      const b = document.createElement("button");
      b.className = "btn" + (c.kind ? " " + c.kind : "");
      b.textContent = c.label;
      b.onclick = () => done(c.value);
      acts.appendChild(b);
    }
    const cancel = document.createElement("button");
    cancel.className = "btn ghost";
    cancel.textContent = cancelLabel;
    cancel.onclick = () => done(null);
    acts.appendChild(cancel);

    box.classList.remove("hidden");
    document.addEventListener("keydown", onKey);
    box.onclick = (e) => { if (e.target === box) done(null); };
  });
}

/* ============ 首页 ============ */

function showHome() {
  showView("home");
  renderLibrary(state.libraryCache || []);
}

function renderLibrary(list) {
  state.libraryCache = list;
  const box = $("#library-list");
  if (!box) return;
  if (!list.length) {
    box.innerHTML = `<div class="empty">还没有打开过任何文献。</div>`;
    return;
  }
  box.innerHTML = "";
  for (const it of list) {
    const done = it.paragraphs > 0 && it.translated >= it.paragraphs;
    const div = document.createElement("div");
    div.className = "lib-item";
    div.innerHTML = `
      <div class="lib-main">
        <div class="lib-title">${escapeHtml(it.title || it.filename || "未命名")}</div>
        <div class="lib-meta">${it.pages} 页 · ${it.paragraphs} 段 · ${it.translated} 段已译 · ${(it.created_at || "").slice(0, 10)}</div>
      </div>
      <span class="lib-badge ${done ? "done" : ""}">${done ? "已译完" : "部分译文"}</span>
      <button class="lib-del" title="删除这篇文献" aria-label="删除这篇文献">×</button>`;
    div.onclick = () => openCached(it.fingerprint);
    div.querySelector(".lib-del").onclick = (e) => {
      e.stopPropagation();
      deleteLibraryItem(it);
    };
    box.appendChild(div);
  }
}

/* 删一篇文献。
 *
 * 删的是本机的译文/插图缓存，不动原 PDF。术语是跨文献积累的资产，
 * 所以先问一句：只删记录（术语留着），还是连这篇带进来的词一起删。
 * 数字要摆在按钮上：让人知道「一起删」到底删掉几条。 */
async function deleteLibraryItem(it) {
  const info = await call("paper_delete_info", it.fingerprint);
  if (!info.ok) return;
  const d = info.data;
  if (d.translating) {
    toast("这篇正在翻译，等它翻完再删", true);
    return;
  }

  // 标题单独占一行、超长截断。以前把标题塞进「删除《…》？」那一行，
  // 长标题会被从中间切断，还留个孤零零的「》？」，像排版坏了。
  const raw = (it.title || it.filename || "这篇文献").replace(/\s+/g, " ").trim();
  const name = raw.length > 56 ? raw.slice(0, 56) + "…" : raw;
  const lines = [
    `《${name}》`,
    `${d.pages} 页 · ${it.translated}/${it.paragraphs} 段已译`,
    ``,
    `会删掉本机保存的译文、插图与表格缓存，以及这篇的文献卡片。原 PDF 文件不动。`,
    `以后想再看，重新导入同一份 PDF 就行 —— 只是要重新译一遍。`,
  ];
  if (d.terms_total) {
    lines.push(`这篇往术语库带进了 ${d.terms_total} 条词，其中 ${d.terms_exclusive} 条只有这篇用过。`);
  } else {
    lines.push(`这篇没有往术语库新增词。`);
  }

  const choices = [
    { label: "只删文献，保留术语", value: "record" },
  ];
  if (d.terms_total) {
    choices.push({
      label: `连术语一起删（${d.terms_exclusive} 条）`,
      value: "all",
      kind: "danger-soft",
    });
  }

  const pick = await askChoice({
    title: "删除这篇文献？",
    body: lines.join("\n"),
    choices,
  });
  if (!pick) return;

  const r = await call("delete_paper", it.fingerprint, pick === "all");
  if (!r.ok) return;

  state.libraryCache = (state.libraryCache || []).filter((x) => x.fingerprint !== it.fingerprint);
  renderLibrary(state.libraryCache);
  const g = r.data || {};
  const extra = g.terms_removed
    ? `，并删掉 ${g.terms_removed} 条术语${g.terms_unlinked ? `（${g.terms_unlinked} 条别的文献也在用，已保留）` : ""}`
    : (g.terms_unlinked ? `，术语库里 ${g.terms_unlinked} 条共用的词已保留` : "");
  toast(`已删除《${(g.filename || name).slice(0, 24)}》${extra}`);
  // 术语数变了，顶部徽标跟着走
  call("refresh").then((x) => { if (x.ok) applySnapshot(x.data); });
}

async function openCached(fp) {
  const r = await call("open_cached", fp);
  if (!r.ok) return;
  enterReader(r.data.paper);
  if (r.data.paper.terms?.length) renderSidebar();
  if (r.data.notice) showNotice(r.data.notice, false);
  // 旧版缓存被重新解析了：段落编号全变了，旧译文对不上，直接重翻
  if (r.data.needs_translate) startTranslate();
}

async function pickPdf() {
  const r = await call("pick_pdf");
  if (!r.ok || !r.data) return;
  enterReader(r.data.paper);
  if (r.data.fromCache) {
    toast("这篇之前翻过，直接打开了，没花钱");
    renderSidebar();
  } else {
    startTranslate();
  }
  // 后端在打开文献时发现的「能读但不完全对」的问题，常驻显示，别让使用者以为是排版坏了
  const warns = r.data.warnings || [];
  if (warns.length) showNotice(warns[0], true);
}

/* ============ 阅读页 ============ */

function enterReader(paper) {
  if (!paper) return;
  state.paper = paper;
  showView("reader");
  hideNotice();
  // 标题用文章真正的标题（解析时从首页认出来 / PDF 元数据），
  // 文件名只在缩略提示里看得到 —— "1-s2.0-S0092867413003875-main.pdf" 当标题太丑
  const titleEl = $("#reader-title");
  titleEl.textContent = paper.title || paper.filename || "—";
  titleEl.title = paper.filename ? `${paper.title || "—"}\n${paper.filename}` : (paper.title || "");
  document.body.classList.toggle("zh-only", !!state.ui.zh_only);
  $("#chk-zh-only").checked = !!state.ui.zh_only;
  $("#sidebar").classList.toggle("collapsed", !state.ui.sidebar_open);

  const box = $("#content");
  box.innerHTML = "";
  box.className = "content" + fontClass(state.ui.font_size);

  // 按页推进：一段一段铺，跨页的地方把「上一页的图」摆出来。
  // 图跟着页走，才能在读方法学的时候顺手看到对应的森林图/脑影像。
  const paras = paper.paragraphs || [];
  // 图注先算好归属：从正文流里摘出来，贴到对应图下面（见 planFigureNotes）
  const plan = planFigureNotes(paras, paper.figures || {});
  let curPage = null;
  for (let i = 0; i < paras.length; i++) {
    const p = paras[i];
    if (curPage !== null && p.page !== curPage) box.appendChild(renderPageEnd(paper, curPage, plan));
    curPage = p.page;                    // 提前设好，下面 continue 摘图注也不会漏页

    if (plan.take.has(p.id)) continue;   // 已作为图注贴到图下面，别在正文里再出现一次
    if (p.kind === "table") {
      // 表下面的注解紧跟着表，一起塞进表格卡片 —— 它们本来就是一体
      const notes = [];
      while (i + 1 < paras.length && paras[i + 1].kind === "table_note"
             && paras[i + 1].table_index === p.table_index) {
        notes.push(paras[++i]);
      }
      box.appendChild(renderTableCard(p, notes));
    } else {
      // table_note 落在没有表格的页上时（表格没识别出来），当普通段落显示，别丢内容
      box.appendChild(renderPara(p));
    }
  }
  if (curPage !== null) box.appendChild(renderPageEnd(paper, curPage, plan));
  box.scrollTop = 0;

  if (!(paper.paragraphs || []).length) {
    showNotice("这篇 PDF 没有可读的文字层，多半是扫描件（整页都是图）。这类文件暂时读不了，"
      + "需要先做一次 OCR。", true);
  }

  const terms = paper.terms || [];
  state.terms = terms;
  renderSidebar();
  updateProgress();
}

/* ---- 图注归位 ----
 *
 * 为什么图和它的注释会分家：图注（"Fig. 1 …"）是 PDF 的文字层，被解析成
 * caption 段落，混在正文里按阅读顺序显示；插图是位图区域，统一在页末渲染。
 * 两套东西没有关联，于是图注飘在正文中间、图却在页尾，有的图注还被版面
 * 拆成两截、中间夹着好几段正文。表格的注解早已改成贴进表格卡片，图这边漏了。
 *
 * 这里在渲染前把「以 Fig/Figure/图 + 编号开头、且后端判为 caption」的图注
 * 段落摘出来，按页内顺序配给该页的插图，贴到图的正下方 —— 和表注一个道理。
 * 表格标题（Table N）、页脚小字一律不动，避免误伤。
 *
 * 配对用插图 bbox 的上沿 y 排序，让「第一条图注」配「最上面那张图」：
 * 多图页（一页两张图很常见）才不会错配。配不上的图注退回正文，内容不丢。
 */
// 编号写法要和后端 CAPTION_PAT 对齐：Figure 1 / Fig. S1 / Extended Data Fig. 3 /
// Supplementary Table 2。前缀不认，补充材料的图注就永远挂不到对应的图上。
const CAP_PREFIX = "(?:(?:extended\\s+data|supplementary|supporting|online)\\s+)?";
const FIG_CAP_PAT = new RegExp("^\\s*" + CAP_PREFIX + "(fig(ure)?\\.?|图)\\.?\\s*[sS]?\\d+", "i");
const TAB_CAP_PAT = new RegExp("^\\s*" + CAP_PREFIX + "(tab(le)?|表)\\.?\\s*[sS]?\\d+", "i");

function planFigureNotes(paras, figures) {
  const take = new Set();                 // 要从正文流里摘走的段落 id
  const byFig = {};                      // "页:图下标" -> [图注段落, ...]

  const orderOf = (page) => {
    const list = figures[String(page)] || figures[page] || [];
    return list
      .map((b, i) => ({ i, y: (b && b[1]) || 0, x: (b && b[0]) || 0 }))
      .sort((a, b) => (a.y - b.y) || (a.x - b.x))
      .map((o) => o.i);
  };

  // 按页收集主图注；后续无编号的 caption（被截断的后半截 / 图例小字）跟在后面
  const perPage = {};
  let last = null;
  let lastPage = -1;
  for (const p of paras) {
    const t = (p.text || "").trim();
    if (p.page !== lastPage) { last = null; lastPage = p.page; }
    const isTable = TAB_CAP_PAT.test(t);
    const isNumbered = p.kind === "caption" && FIG_CAP_PAT.test(t) && !isTable;
    // 无编号图注（Science 的 RESEARCH ARTICLE SUMMARY：说明直接印在图下面，
    // 前面没有 "Fig. 1"）。后端按「小字 + 紧贴图下沿」认出来并打了 fig_legend，
    // 这里允许它自己起一组 —— 但只在这一页还有图没配上注时才另起，
    // 免得它把后面真正的编号图注挤掉。
    const unclaimed = orderOf(p.page).length > (perPage[p.page] || []).length;
    const isLegendStart = p.kind === "caption" && !isNumbered && !isTable
      && !last && unclaimed && (p.flags || []).includes("fig_legend");
    if (isNumbered) {
      const list = perPage[p.page] = perPage[p.page] || [];
      // 图注跨两栏排版时（Nature Communications 的 Fig. 1 那样：右栏后半截
      // 先出现、左栏带 "Fig. N" 的正文后出现），后半截会被当成「无编号图注」
      // 先起一组，轮到真正的编号图注时就没图可配、整段掉在正文里。
      // 这种情况合成一组：带编号的那段当主注，先出现的碎片排在它后面。
      const frag = list.find((g) => g.legendOnly);
      if (frag) {
        const merged = { main: p, extra: [frag.main, ...frag.extra].filter(Boolean) };
        list.splice(list.indexOf(frag), 1, merged);
        last = merged;
      } else {
        const g = { main: p, extra: [] };
        list.push(g);
        last = g;
      }
    } else if (isLegendStart) {
      const g = { main: p, extra: [], legendOnly: true };
      (perPage[p.page] = perPage[p.page] || []).push(g);
      last = g;
    } else if ((isTable && p.kind === "caption") || p.kind === "heading") {
      // 表格标题、章节标题是明确的边界：后面的小字属于表/新章节，不是上一张图的续注。
      // 至于 body 正文，不能当断点 —— 图注常被版面拆成两截，中间夹着好几段正文。
      last = null;
    } else if (last && p.kind === "caption" && t) {
      last.extra.push(p);
    }
  }

  for (const [page, groups] of Object.entries(perPage)) {
    const order = orderOf(page);
    if (!order.length) continue;          // 这页没识别出位图（多是矢量图），图注留在正文
    groups.forEach((g, k) => {
      if (k >= order.length) return;     // 注比图还多，多余的留在正文，不硬塞
      const list = [g.main, ...g.extra];
      byFig[`${page}:${order[k]}`] = list;
      for (const q of list) take.add(q.id);
    });
  }

  // 图注溢出到下一页：图排在前一页页底、说明接在下一页页首（Nature 的
  // Extended Data 很常见，上一页图注干脆写着「See next page for caption.」）。
  // 这种注在本页配不到图，会孤零零留在正文流里，读者根本对不上是哪张图。
  // 条件收得很紧：本页有注无图、上一页只有一张图，且上一页要么没有图注、
  // 要么只挂着「见下页」这类占位注（占位注被真图注顶替掉）。
  for (const [page, groups] of Object.entries(perPage)) {
    if (orderOf(page).length) continue;
    const prev = String(Number(page) - 1);
    const prevBoxes = figures[prev] || figures[Number(prev)] || [];
    if (prevBoxes.length !== 1) continue;
    const prevGroups = perPage[prev];
    if (prevGroups) {
      const s = ((prevGroups[0] && prevGroups[0].main && prevGroups[0].main.text) || "").trim();
      const isStub = s.length <= 90 && /see\s+(the\s+)?next\s+page/i.test(s);
      if (!isStub) continue;              // 上一页有正经图注，别抢
    }
    const g = groups[0];
    if (!g) continue;
    const list = [g.main, ...g.extra];
    byFig[`${prev}:0`] = list;            // 有占位注时直接顶替
    for (const q of list) take.add(q.id);
  }
  return { take, byFig };
}

/* ===== planFigureNotes 以上是不依赖 DOM 的纯逻辑，dev/test_figures.js 直接截取测 ===== */

function renderFigNote(n) {
  const box = document.createElement("div");
  box.className = "fig-note";
  const en = document.createElement("div");
  en.className = "en";
  en.textContent = n.text;
  const zh = document.createElement("div");
  zh.className = "zh" + (n.zh ? "" : " pending");
  zh.dataset.noteId = n.id;              // zhEl(id) 靠它把译文回填进来
  zh.textContent = n.zh || "…";
  box.appendChild(en);
  box.appendChild(zh);
  return box;
}

/* 一页读完处的分隔条。以前是一条淡到几乎看不见的细线，「看原页」还要
 * 鼠标移上去才显出来；有插图的页干脆连页码都没有。现在每页结束都给一条
 * 带页码和总页数的醒目横条，按钮常驻。 */
function renderPageBreak(paper, page) {
  const bar = document.createElement("div");
  bar.className = "page-break";
  const label = document.createElement("span");
  label.className = "page-break-label";
  label.textContent = paper.pages
    ? `第 ${page} 页 / 共 ${paper.pages} 页`
    : `第 ${page} 页`;
  const btn = document.createElement("button");
  btn.className = "page-break-btn";
  btn.textContent = "看原页";
  btn.onclick = () => openPageViewer(page);
  bar.appendChild(label);
  bar.appendChild(btn);
  return bar;
}

/* 一页读完处：插图 + 图注一起摆出来，末尾接一条醒目的分页条。
 * 图片用 loading="lazy"，翻到那一页才请求，打开文献时不用等十几张图。 */
function renderPageEnd(paper, page, plan) {
  if (paper.fingerprint === "demo") return document.createElement("div");  // 演示模式没有后端，取不到图
  const figs = (paper.figures || {})[String(page)] || (paper.figures || {})[page] || [];
  const wrap = document.createElement("div");
  wrap.dataset.page = page;

  if (figs.length) {
    for (let i = 0; i < figs.length; i++) {
      const fig = document.createElement("figure");
      fig.className = "page-fig";

      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = `第 ${page} 页插图`;
      img.src = pageImageUrl(paper, page, i);
      img.onclick = () => openLightbox(img.src, `第 ${page} 页 · 第 ${i + 1} 张图`);
      img.onerror = () => {
        fig.innerHTML = `<div class="fig-cap">这张图没能显示。原 PDF 可能已经被移动或删除。</div>`;
      };
      fig.appendChild(img);

      // 真图注贴在图的正下方，跟表格卡片里的表注一样；英文 + 中文逐条对应
      const notes = (plan && plan.byFig[`${page}:${i}`]) || [];
      if (notes.length) {
        const capBox = document.createElement("figcaption");
        capBox.className = "fig-caption";
        for (const n of notes) capBox.appendChild(renderFigNote(n));
        fig.appendChild(capBox);
      } else {
        const cap = document.createElement("figcaption");
        cap.className = "fig-cap";
        cap.textContent = `第 ${page} 页插图 · 点击放大`;
        fig.appendChild(cap);
      }
      wrap.appendChild(fig);
    }
  }

  // 每页都给一条醒目的分页条（有图没图都一样），页码 + 总页数 + 常驻「看原页」
  wrap.appendChild(renderPageBreak(paper, page));
  return wrap;
}

function pageImageUrl(paper, page, index, table, formula) {
  const fp = encodeURIComponent(paper.fingerprint || "");
  let url = `/pageimg?fp=${fp}&p=${page}`;
  if (table !== undefined && table >= 0) url += `&t=${table}`;
  else if (formula !== undefined && formula >= 0) url += `&f=${formula}`;
  else if (index !== undefined && index >= 0) url += `&i=${index}`;
  return url;
}

/* ---- 放大看 ----
 *
 * 两种用途共用一个查看器：
 *   1. 点插图 → 只看那张图（没有翻页）
 *   2. 点「原页」→ 看这一页的原始版面，可以 ← → 前后翻整篇
 *
 * 为什么需要它：版面识别再准也有认错的时候（三栏、跑版、图里的字）。
 * 这时候最靠得住的还是原页 —— 但整页缩到正文宽度看不清小字，
 * 所以必须能放大、能拖着看。
 */

const lb = {
  mode: "page",     // page | fig
  fp: "",
  page: 1,
  total: 1,
  zoom: 1,
  tx: 0,
  ty: 0,
  drag: null,
};

const LB_FIG_HINT = " · 滚轮可放大";
const LB_ZOOM_MIN = 0.6;
const LB_ZOOM_MAX = 6;

function lbEls() {
  return {
    box: $("#lightbox"),
    stage: $("#lb-stage"),
    img: $("#lightbox-img"),
    tip: $("#lightbox-tip"),
    loading: $("#lb-loading"),
    zoomText: $("#lb-zoom"),
  };
}

function lbApply() {
  const { img, zoomText } = lbEls();
  if (!img) return;
  img.style.transform = `translate(${lb.tx}px, ${lb.ty}px) scale(${lb.zoom})`;
  img.style.cursor = lb.zoom > 1.02 ? (lb.drag ? "grabbing" : "grab") : "default";
  if (zoomText) zoomText.textContent = `${Math.round(lb.zoom * 100)}%`;
}

function lbSetZoom(next, cx = 0, cy = 0) {
  const z = Math.min(LB_ZOOM_MAX, Math.max(LB_ZOOM_MIN, next));
  if (Math.abs(z - lb.zoom) < 1e-4) return;
  // 以光标为中心缩放：光标下的那一点保持不动，才不会「越放大越不知道看哪了」
  lb.tx = cx - (cx - lb.tx) * (z / lb.zoom);
  lb.ty = cy - (cy - lb.ty) * (z / lb.zoom);
  lb.zoom = z;
  lbApply();
}

function lbReset() {
  lb.zoom = 1;
  lb.tx = 0;
  lb.ty = 0;
  lbApply();
}

function lbRenderPage() {
  const { img, tip, loading } = lbEls();
  if (!img) return;
  const url = `/pageimg?fp=${encodeURIComponent(lb.fp)}&p=${lb.page}`;
  loading?.classList.remove("hidden");
  img.classList.add("lb-stale");
  img.onload = () => {
    loading?.classList.add("hidden");
    img.classList.remove("lb-stale");
  };
  img.onerror = () => {
    loading?.classList.add("hidden");
    img.classList.remove("lb-stale");
  };
  img.src = url;
  lbReset();
  if (tip) tip.textContent = `第 ${lb.page} / ${lb.total} 页 · 滚轮放大，拖动移动，← → 翻页`;
  $("#lb-prev")?.toggleAttribute("disabled", lb.page <= 1);
  $("#lb-next")?.toggleAttribute("disabled", lb.page >= lb.total);
}

function lbStep(delta) {
  if (lb.mode !== "page") return;
  const next = lb.page + delta;
  if (next < 1 || next > lb.total) return;
  lb.page = next;
  lbRenderPage();
}

function openLightbox(src, tip, opts = {}) {
  const { box, tip: tipEl } = lbEls();
  if (!box || !src) return;
  lb.mode = opts.mode || "fig";
  lb.fp = opts.fp || "";
  lb.page = opts.page || 1;
  lb.total = opts.total || 1;
  $("#lb-prev").classList.toggle("hidden", lb.mode !== "page");
  $("#lb-next").classList.toggle("hidden", lb.mode !== "page");
  $("#lb-stage").classList.toggle("lb-fig", lb.mode !== "page");
  if (lb.mode === "page") {
    lbRenderPage();
  } else {
    const { img, loading } = lbEls();
    loading?.classList.add("hidden");
    img.src = src;
    if (tipEl) tipEl.textContent = tip || "";
    lbReset();
  }
  box.classList.remove("hidden");
  $("#lb-fit")?.focus({ preventScroll: true });
}

function openPageViewer(page) {
  const paper = state.paper;
  if (!paper || !paper.fingerprint || paper.fingerprint === "demo") return;
  const total = paper.pages || 1;
  const p = Math.min(total, Math.max(1, page || 1));
  openLightbox("/pageimg", "", { mode: "page", fp: paper.fingerprint, page: p, total });
}

function closeLightbox() {
  const { box } = lbEls();
  if (!box) return;
  box.classList.add("hidden");
  $("#lightbox-img").src = "";
  lb.drag = null;
}

/* 当前滚动到哪一页了。「原页」按钮按它决定打开哪一页 ——
 * 读到第 7 页却弹出第 1 页，等于让人自己找。 */
function currentVisiblePage() {
  const box = $("#content");
  if (!box) return 1;
  const boxTop = box.getBoundingClientRect().top;
  let page = 1;
  for (const el of box.querySelectorAll("[data-page]")) {
    const r = el.getBoundingClientRect();
    if (r.bottom - boxTop <= 4) continue;
    if (r.top - boxTop <= 24) {
      const got = parseInt(el.dataset.page, 10);
      if (got) page = got;
    } else {
      break;
    }
  }
  return page;
}

/* ---- 阅读页顶部的常驻提示 ---- */

function showNotice(text, isError = true) {
  const el = $("#reader-notice");
  if (!el) return;
  el.className = "reader-notice" + (isError ? " error" : "");
  el.innerHTML = "";
  const span = document.createElement("span");
  span.textContent = text;
  const close = document.createElement("button");
  close.className = "btn tiny ghost";
  close.textContent = "知道了";
  close.onclick = hideNotice;
  el.appendChild(span);
  el.appendChild(close);
}

function hideNotice() {
  const el = $("#reader-notice");
  if (el) el.classList.add("hidden");
}

function fontClass(size) {
  if (size <= 14) return " font-s";
  if (size >= 18) return " font-l";
  return "";
}

/* 找一段的译文容器。普通段落是 .para 里的 .zh，表注在表格卡片里，用 data-note-id 标着 */
function zhEl(id) {
  return document.querySelector(`.para[data-id="${id}"] .zh, [data-note-id="${id}"]`);
}

function renderPara(p) {
  if (p.kind === "table") return renderTableCard(p);
  if (p.kind === "formula") return renderFormulaCard(p);

  const wrap = document.createElement("div");
  const cls = ["para"];
  if (p.kind === "heading") cls.push("heading");
  if (p.kind === "caption") cls.push("caption");
  // flags 现在只用来「轻轻淡化」，不再代表「这段不翻」。
  // 版面顺序不确定（layout_uncertain）尤其不能当成不翻的理由 —— 一页里有个跨栏表格，
  // 整页十几段就全留英文，那才是真的看不懂。
  const shaky = (p.flags || []).some((f) => f === "mixed_font" || f === "long_single_line");
  if (shaky) cls.push("untrusted");
  wrap.className = cls.join(" ");
  wrap.dataset.id = p.id;
  wrap.dataset.page = p.page;

  // 跨页合段的段落：开头是从上一页接过来的半句话，不说一句读者会以为漏字
  const from = p.page_from || p.page;
  const to = p.page_to || p.page;
  if (to > from) {
    const tag = document.createElement("div");
    tag.className = "para-span";
    tag.textContent = `跨第 ${from}–${to} 页`;
    wrap.appendChild(tag);
  }

  const en = document.createElement("div");
  en.className = "en";
  en.textContent = p.text;
  wrap.appendChild(en);

  const zh = document.createElement("div");
  zh.className = "zh";
  if (p.zh) {
    zh.textContent = p.zh;
  } else {
    // 还没翻到，占个位显示省略号，避免译文出现时页面跳动太厉害
    zh.classList.add("pending");
    zh.textContent = "…";
  }
  wrap.appendChild(zh);
  return wrap;
}

/* 表格：按原样显示截图，不翻译。
 *
 * 表格里的字是「数字 + 缩写」的密排，翻成中文会把列宽、对齐全毁掉，
 * 而且表格常横跨两栏，硬塞进单栏文本流里读起来是灾难。
 * 截图保真，读者看表本来就是看数字的排布。
 *
 * 表下面的注解（缩写是什么、星号代表哪个显著性水平）反过来必须翻译，
 * 而且要贴在表下面 —— 它是表的一部分，扔进正文流就断了和表的对应关系。 */
/* 显示公式：按原页截一块贴上来，不翻译。
 *
 * 文字层里的公式是碎的：∑ 单独一行、上下标各成 5pt 的小行、分式直接消失，
 * 翻出来是「i=11(abs (Y − ^Yadd)ji < u)」这种谁也看不懂的东西。
 * 截图是矢量的，放大到原文一样清晰，也保证后面所有公式都能正常显示
 * （不依赖浏览器有没有装数学字体）。
 */
function renderFormulaCard(p) {
  const paper = state.paper;
  const idx = p.formula_index || 0;

  const fig = document.createElement("figure");
  fig.className = "page-formula";
  fig.dataset.id = p.id;

  const img = document.createElement("img");
  img.loading = "lazy";
  img.alt = p.text || `第 ${p.page} 页公式`;
  img.src = pageImageUrl(paper, p.page, -1, -1, idx);
  img.onclick = () => openLightbox(img.src, `第 ${p.page} 页 · 公式`);
  img.onerror = () => {
    // 原 PDF 被移走了也要有个交代，别让读者对着一块空白
    fig.classList.add("broken");
    fig.innerHTML = `<div class="formula-fallback">${escapeHtml(p.text || "公式")}</div>`;
  };

  fig.appendChild(img);
  return fig;
}

function renderTableCard(p, notes = []) {
  const paper = state.paper;
  const idx = p.table_index || 0;

  const fig = document.createElement("figure");
  fig.className = "page-fig page-table";
  fig.dataset.id = p.id;

  const img = document.createElement("img");
  img.loading = "lazy";
  img.alt = `第 ${p.page} 页表格 ${idx + 1}`;
  img.src = pageImageUrl(paper, p.page, -1, idx);
  img.onclick = () => openLightbox(img.src, `第 ${p.page} 页 · 表格 ${idx + 1}` + LB_FIG_HINT);
  img.onerror = () => {
    fig.innerHTML = `<div class="fig-cap">这张表没能显示。原 PDF 可能已经被移动或删除。</div>`;
  };

  const cap = document.createElement("figcaption");
  cap.className = "fig-cap";
  cap.textContent = `第 ${p.page} 页 · 表格 ${idx + 1} · 按原样显示，不翻译`;

  fig.appendChild(img);
  fig.appendChild(cap);

  for (const n of notes) {
    const box = document.createElement("div");
    box.className = "table-note";

    const en = document.createElement("div");
    en.className = "en";
    en.textContent = n.text;

    const zh = document.createElement("div");
    zh.className = "zh" + (n.zh ? "" : " pending");
    zh.dataset.noteId = n.id;
    zh.textContent = n.zh || "…";

    box.appendChild(en);
    box.appendChild(zh);
    fig.appendChild(box);
  }
  return fig;
}

async function startTranslate(force = false) {
  const r = await call("start_translate", force);
  if (!r.ok) return;
  if (r.data && r.data.message) {
    toast(r.data.message);
    return;
  }
  // 重新翻译时进度条要归零，否则一上来就显示「已译 72/72」，看着像没在动
  setProgress(force ? 1 : 2, force ? "重新翻译中…" : "准备中…");
  startPolling();
}

/* 「重新翻译」：会重新计费，先问一句。
 * 不清空已有译文 —— 新的逐段覆盖上去，中途失败也不至于一片空白。 */
async function retranslate() {
  if (!state.paper) return;
  const ok = window.confirm(
    "重新翻一遍这篇？\n\n· 会重新调用模型，产生费用（一篇十几页大概几毛钱）\n" +
    "· 旧译文不会立刻删掉，新的逐段覆盖上去\n" +
    "· 换过模型/接口之后想重看效果，用这个"
  );
  if (!ok) return;
  for (const p of state.paper.paragraphs || []) {
    const el = zhEl(p.id);
    if (el) { el.textContent = "…"; el.classList.add("pending"); }
  }
  hideNotice();
  startTranslate(true);
}

function setProgress(percent, text) {
  $("#progress-fill").style.width = Math.max(0, Math.min(100, percent)) + "%";
  if (text !== undefined) $("#reader-progress").textContent = text;
}

function updateProgress() {
  const paras = state.paper?.paragraphs || [];
  const canTranslate = (p) => p.kind !== "table" && p.kind !== "formula";   // 表格/公式按原样显示，不计入进度
  const total = paras.filter(canTranslate).length;
  const done = paras.filter((p) => canTranslate(p) && p.zh).length;
  if (!total) return;
  const pct = Math.round((done / total) * 100);
  $("#progress-fill").style.width = pct + "%";
  $("#reader-progress").textContent = done >= total ? "已译完" : `已译 ${done}/${total} 段`;
}

/* ============ 侧栏 ============ */

function renderSidebar() {
  const box = $("#sidebar-list");
  box.innerHTML = "";
  const terms = state.terms || [];
  if (!terms.length) {
    box.innerHTML = `<div class="empty">${state.paper?.paragraphs?.some(p => p.zh)
      ? "这篇没有提取到术语。"
      : "译文出来后会自动提取这篇的专业名词。"}</div>`;
    return;
  }
  for (const t of terms) {
    const div = document.createElement("div");
    div.className = "term-item";
    div.innerHTML = `
      <div class="term-head">
        <span class="term-en">${escapeHtml(t.en || "")}</span>
        <span class="term-zh">${escapeHtml(t.zh || "")}</span>
      </div>
      <div class="term-def">${escapeHtml(t.definition || "")}</div>`;
    div.onclick = () => jumpToTerm(t.en);
    box.appendChild(div);
  }
}

function jumpToTerm(en) {
  if (!en) return;
  const needle = en.toLowerCase();
  const paras = state.paper?.paragraphs || [];
  for (const p of paras) {
    if ((p.text || "").toLowerCase().includes(needle)) {
      const el = $(`.para[data-id="${p.id}"]`);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.style.transition = "background .4s";
        el.style.background = "#fff8e6";
        setTimeout(() => { el.style.background = ""; }, 1600);
      }
      return;
    }
  }
  toast("正文里没找到这个词的原文");
}

/* ============ 划词 ============ */

let popContextText = "";

async function handleSelection(ev) {
  const pop = $("#popover");
  const sel = window.getSelection();
  const text = (sel?.toString() || "").trim();

  if (!text || text.length > 80 || !sel.rangeCount) {
    pop.classList.add("hidden");
    return;
  }
  // 只在正文区生效，侧栏里选字不该弹
  const anchor = sel.anchorNode?.nodeType === 3 ? sel.anchorNode.parentElement : sel.anchorNode;
  if (!anchor || !anchor.closest("#content")) {
    pop.classList.add("hidden");
    return;
  }

  const rect = sel.getRangeAt(0).getBoundingClientRect();
  pop.classList.remove("hidden");
  pop.style.left = Math.min(rect.left, window.innerWidth - 360) + "px";
  pop.style.top = Math.min(rect.bottom + 8, window.innerHeight - 200) + "px";

  $("#pop-word").textContent = text;
  $("#pop-zh").textContent = "";
  $("#pop-def").textContent = "正在查询…";
  $("#pop-def").className = "pop-def loading";

  // 上下文取所在段落，让模型解释"这句话里"的意思，而不是词典释义
  const paraEl = anchor.closest(".para");
  popContextText = paraEl ? paraEl.querySelector(".en")?.textContent || "" : "";

  const r = await call("lookup_term", text, popContextText);
  if (!r.ok) {
    $("#pop-def").textContent = r.error || "查不到";
    $("#pop-def").className = "pop-def";
    return;
  }
  const d = r.data;
  $("#pop-word").textContent = d.en || text;
  $("#pop-zh").textContent = d.zh || "";
  $("#pop-def").textContent = d.definition || "（没有更多说明）";
  $("#pop-def").className = "pop-def";
  if (d.source === "local") $("#pop-zh").textContent = (d.zh || "") + " · 本地词库";
}

/* ============ 设置抽屉 ============ */

function openSettings() {
  renderProfileSelect();
  $("#settings-datadir").textContent = state.dataDir || "—";
  const active = state.profiles.find((p) => p.active);
  if (active) fillProfileForm(active);
  else resetProfileForm();     // 一套都没有：表单清空，等服务商和名称自己填
  $("#settings").classList.remove("hidden");
}

/** 清空配置表单，回到「新建一套」的状态 */
function resetProfileForm() {
  $("#pf-name").value = "";
  $("#pf-base").value = "";
  $("#pf-key").value = "";
  $("#pf-key").placeholder = "sk-...";
  $("#pf-model").value = "";
  $("#pf-test-result").textContent = "";
  const sel = $("#pf-provider");
  fillProviderSelect(sel);
  sel.value = "";
  $("#pf-provider-tip").textContent = providerById("").tip;
}

function renderProfileSelect() {
  const sel = $("#profile-select");
  sel.innerHTML = "";
  if (!state.profiles.length) {
    // 没有任何配置时不能留一个空的 select，那看起来像个 bug
    sel.appendChild(new Option("（还没有配置，在下面新建一套）", ""));
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  for (const p of state.profiles) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = `${p.name}${p.ready ? "" : "（未填全）"}`;
    if (p.active) opt.selected = true;
    sel.appendChild(opt);
  }
}

function fillProfileForm(p) {
  if (!p) return;
  $("#pf-name").value = p.name || "";
  $("#pf-base").value = p.base_url || "";
  $("#pf-key").value = "";
  $("#pf-key").placeholder = p.masked_key || "留空表示不修改";
  $("#pf-model").value = p.model || "";
  // 反推这套配置属于哪家，下拉跟着走，提示也换掉。
  // 地址为空 → 显示「请选择服务商」；有地址但对不上任何预设（学校接口、中转站）→ 显示「其他」。
  const sel = $("#pf-provider");
  fillProviderSelect(sel);
  const curBase = (p.base_url || "").trim();
  const match = curBase ? PROVIDERS.find((x) => x.base && x.base === curBase) : null;
  sel.value = match ? match.id : (curBase ? "custom" : "");
  $("#pf-provider-tip").textContent = providerById(sel.value).tip;
}

async function saveProfile() {
  const pid = $("#profile-select").value || "";
  const name = $("#pf-name").value.trim();
  const base = $("#pf-base").value.trim();
  const model = $("#pf-model").value.trim();

  // 这些必填项在界面上就拦住，别等后端报错或存下一套跑不起来的配置
  if (!name) return toast("先给这套配置起个名字，比如「百炼免费额度」", true);
  if (!base) return toast("接口地址是空的：上面选个服务商会自动填，或者自己粘贴", true);
  if (!model) return toast("模型名是空的：选服务商会自动填，比如 qwen-plus", true);

  const payload = {
    id: pid,
    name,
    base_url: base,
    api_key: $("#pf-key").value.trim(),
    model,
  };
  const r = await call("save_profile", payload);
  if (!r.ok) return;
  applySnapshot(r.data);
  renderProfileSelect();
  // 新增的那套后端会设为「当前使用」，更新则是原来那套，两者都取 active 回填
  const active = state.profiles.find((p) => p.active);
  if (active) fillProfileForm(active);
  else resetProfileForm();
  toast(pid ? "已保存" : "已新增一套配置");
}

/**
 * 测试连接。
 *
 * 关键点：**拿表单里当前填的内容去测**，不要求先保存。
 * 原来这里只调 test_profile(已保存配置的 id)，于是没保存就点会得到
 * 「没有可用的配置」——使用者明明刚把地址密钥填好，却说没有配置，很莫名其妙。
 */
async function testProfile() {
  const out = $("#pf-test-result");
  const pid = $("#profile-select").value || "";
  const base = $("#pf-base").value.trim();
  const model = $("#pf-model").value.trim();
  const key = $("#pf-key").value.trim();

  out.className = "small muted";

  // 编辑已保存的配置时，密钥框是空的（意思是「不修改」），
  // 那就让后端用这套配置里存的密钥去测
  if (!key && pid) {
    out.textContent = "正在用已保存的密钥测试…";
    const r = await call("test_profile", pid);
    if (r.ok && r.data) {
      out.textContent = (r.data.success ? "✓ " : "✗ ") + r.data.message;
      out.className = "small " + (r.data.success ? "" : "warn");
    } else {
      out.textContent = r.error || "测试失败";
      out.className = "small warn";
    }
    return;
  }

  if (!key) {
    out.textContent = "先把密钥粘进来再测";
    out.className = "small warn";
    return;
  }
  if (!base) {
    out.textContent = "接口地址是空的：上面选个服务商会自动填";
    out.className = "small warn";
    return;
  }
  if (!model) {
    out.textContent = "模型名是空的：选服务商会自动填，比如 qwen-plus";
    out.className = "small warn";
    return;
  }

  out.textContent = "正在测试…";
  const r = await call("test_raw", base, key, model);
  if (r.ok && r.data) {
    // 测通了但还没保存，特地说一句，否则关掉窗口这套就白填了
    out.textContent = r.data.success
      ? `✓ ${r.data.message}　（别忘了点「保存这套」）`
      : `✗ ${r.data.message}`;
    out.className = "small " + (r.data.success ? "" : "warn");
  } else {
    out.textContent = r.error || "测试失败";
    out.className = "small warn";
  }
}

function newProfile() {
  if (state.profiles.length >= 5) {
    toast("最多 5 套配置，先删掉一套再新建", true);
    return;
  }
  resetProfileForm();
  $("#profile-select").selectedIndex = -1;   // 不选中任何一套，保存时就是新增
  toast("选个服务商，起个名字，填上密钥，然后点「保存这套」");
}

async function deleteProfile() {
  const pid = $("#profile-select").value;
  if (!pid) {
    toast("先在「当前使用」里选一套再删", true);
    return;
  }
  const target = state.profiles.find((p) => p.id === pid);
  if (!confirm(`确定删除「${target ? target.name : "这套配置"}」？密钥也会一起删掉。`)) return;
  const r = await call("delete_profile", pid);
  if (!r.ok) return;
  applySnapshot(r.data);
  renderProfileSelect();
  const next = state.profiles.find((p) => p.active);
  if (next) fillProfileForm(next);
  else resetProfileForm();      // 删空了：回到「新建一套」的状态
  toast("已删除");
}

/* ============ 术语库页 ============ */

async function showTerms() {
  showView("terms");
  await loadTerms("");
}

async function loadTerms(keyword) {
  const r = await call("search_terms", keyword);
  if (!r.ok) return;
  const list = r.data || [];
  const box = $("#terms-list");
  box.innerHTML = `<div class="terms-row header">
      <div>英文</div><div>中文</div><div>定义</div><div class="t-hits">命中</div>
    </div>`;
  if (!list.length) {
    box.innerHTML += `<div class="empty">还没有收录任何术语。翻译过的文献会自动把专业名词收进来。</div>`;
    return;
  }
  for (const t of list) {
    const row = document.createElement("div");
    row.className = "terms-row";
    row.innerHTML = `
      <div class="t-en">${escapeHtml(t.display_en || t.en)}</div>
      <div class="t-zh">${escapeHtml(t.zh || "")}</div>
      <div class="t-def">${escapeHtml(t.definition || "")}</div>
      <div class="t-hits">${t.hits || 1}</div>`;
    box.appendChild(row);
  }
}

/* ============ 文献卡片页 ============ */

async function showCards() {
  showView("cards");
  await loadCards("");
}

async function loadCards(keyword) {
  const r = await call("list_cards", keyword);
  if (!r.ok) return;
  const list = r.data || [];
  const box = $("#cards-list");
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = `<div class="empty">还没有文献卡片。每篇文献翻译完成后会自动生成一张。</div>`;
    return;
  }
  for (const c of list) {
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `
      <div class="card-title">${escapeHtml(c.title || c.filename || "未命名")}</div>
      ${cardField("研究问题", c.research_question)}
      ${cardField("对象与方法", c.methods)}
      ${cardField("核心结论", c.findings)}
      ${cardField("局限性", c.limitations)}`;
    box.appendChild(div);
  }
}

function cardField(label, value) {
  if (!value) return "";
  return `<div class="card-field">
      <div class="card-label">${label}</div>
      <div class="card-text">${escapeHtml(value)}</div>
    </div>`;
}

/* ============ 工具 ============ */

function escapeHtml(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ============ 后端事件轮询 ============
 *
 * 为什么是轮询而不是后端主动推：
 *   pywebview 6 + WebView2 在后台线程调用 evaluate_js 会抛
 *   "CoreWebView2 can only be accessed from the UI thread"。
 *   后端把事件放进队列，前端定时来取，跟框架版本无关，最稳。
 */

let pollTimer = null;
let pollStopTimer = null;

function startPolling() {
  clearTimeout(pollStopTimer);
  pollStopTimer = null;
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const resp = await fetch("/api/poll_events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: '{"args":[]}',
      });
      const r = await resp.json();
      if (r && r.ok && Array.isArray(r.data)) {
        for (const item of r.data) handlePush(item.event, item.payload);
      }
    } catch (e) {
      /* 页面正在关闭时会报错，忽略即可 */
    }
  }, 400);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function handlePush(event, payload) {
  if (event === "progress") {
    setProgress(percentGuess(payload.stage), payload.stage);
  } else if (event === "batch") {
    const t = payload.translations || {};
    for (const [id, zh] of Object.entries(t)) {
      const el = zhEl(id);
      if (el) {
        el.textContent = zh;
        el.classList.remove("pending");
      }
      const p = state.paper?.paragraphs?.find((x) => String(x.id) === String(id));
      if (p) p.zh = zh;
    }
    updateProgress();
  } else if (event === "done") {
    updateProgress();
    const errs = payload.errors || [];
    if (errs.length) {
      toast(`有 ${errs.length} 批没翻成功：${errs[0]}`, true);
      showNotice(`有 ${errs.length} 批没翻成功：${errs[0]}`, true);
    } else if (!payload.translated) {
      // 一段都没翻出来，又不报错：要么只有表格/非正文，要么根本没有文字层。
      // 这种情况以前只是进度条停在 0，什么也不说，使用者只会觉得「这软件不翻译」。
      const why = payload.skipped
        ? `这篇只认出 ${payload.skipped} 处表格，没有可以翻译的正文段落（表格按原样显示）。`
        : "这篇没有抽到可翻译的正文段落，可能是扫描件（整页都是图，没有文字层）。";
      showNotice(`没有翻出中文：${why}`, true);
      toast("这篇没有翻出中文，看顶部的说明", true);
    } else {
      hideNotice();
      toast(`翻译完成：${payload.translated} 段，术语 ${payload.terms} 条`);
    }
    // 术语到位后刷新侧栏
    call("paper_terms").then((r) => {
      if (r.ok) {
        state.terms = r.data || [];
        renderSidebar();
      }
    });
    // 卡片在 done 之后才生成，留一段时间继续收事件
    pollStopTimer = setTimeout(stopPolling, 25000);
  } else if (event === "failed") {
    setProgress(0, "翻译失败");
    const msg = payload.error || "翻译失败";
    toast(msg, true);
    showNotice(`翻译没成功：${msg}`, true);
    // 重新翻译时界面上的旧译文已被「…」顶掉，失败就得放回去，别留一片省略号
    for (const p of state.paper?.paragraphs || []) {
      const el = zhEl(p.id);
      if (el && p.zh) {
        el.textContent = p.zh;
        el.classList.remove("pending");
      }
    }
    stopPolling();
  } else if (event === "card") {
    if (payload && payload.findings) toast("文献卡片已生成，在「文献卡片」里可以看");
    call("refresh").then((r) => { if (r.ok) applySnapshot(r.data); });
  }
}

function percentGuess(stage) {
  const s = stage || "";
  if (s.includes("摘要") || s.includes("后台")) return 10;
  if (s.includes("正文")) return 55;
  if (s.includes("重试")) return 90;
  if (s.includes("术语")) return 65;
  return 30;
}

/* ============ 事件绑定 ============ */

function bind() {
  // 引导
  $("#btn-setup-dir").onclick = setupChooseDir;
  $("#btn-setup-dir-apply").onclick = setupApplyManual;
  $("#setup-dir-manual").onkeydown = (e) => {
    if (e.key === "Enter") $("#btn-setup-dir-apply").click();
  };
  $("#btn-setup-test").onclick = setupTest;
  $("#btn-setup-finish").onclick = setupFinish;
  fillProviderSelect($("#setup-provider"));
  $("#setup-provider").onchange = (e) => applyProvider(e.target.value, {
    base: "setup-base", model: "setup-model", tip: "setup-provider-tip",
  });
  bindBaseGuard("setup-base", "setup-provider", "setup-provider-tip");

  // 首页
  $("#btn-pick-pdf").onclick = pickPdf;
  $("#btn-open-settings").onclick = openSettings;
  $("#profile-badge").onclick = openSettings;

  // 拖放
  const dz = $("#dropzone");
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); })
  );
  dz.addEventListener("drop", async (e) => {
    const f = e.dataTransfer?.files?.[0];
    const path = f?.path;
    if (!path) {
      toast("拖放拿不到文件路径，请用下面的按钮选择", true);
      return;
    }
    const r = await call("open_pdf", path);
    if (r.ok && r.data) {
      enterReader(r.data.paper);
      if (r.data.fromCache) renderSidebar();
      else startTranslate();
    }
  });

  // 标签切换
  $$(".tab").forEach((tab) => {
    tab.onclick = () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      const v = tab.dataset.view;
      if (v === "home") showHome();
      else if (v === "terms") showTerms();
      else if (v === "cards") showCards();
    };
  });
  $$("[data-back-home]").forEach((b) => (b.onclick = () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === "home"));
    showHome();
  }));

  // 阅读页
  $("#btn-back").onclick = () => showHome();
  $("#btn-retranslate").onclick = retranslate;
  $("#chk-zh-only").onchange = (e) => {
    document.body.classList.toggle("zh-only", e.target.checked);
    state.ui.zh_only = e.target.checked;
    call("set_ui", { zh_only: e.target.checked });
  };
  $$("[data-font]").forEach((b) => (b.onclick = () => {
    state.ui.font_size = Math.max(13, Math.min(21, state.ui.font_size + Number(b.dataset.font)));
    $("#content").className = "content" + fontClass(state.ui.font_size);
    call("set_ui", { font_size: state.ui.font_size });
  }));
  $("#btn-sidebar").onclick = () => {
    const sb = $("#sidebar");
    sb.classList.toggle("collapsed");
    state.ui.sidebar_open = !sb.classList.contains("collapsed");
    call("set_ui", { sidebar_open: state.ui.sidebar_open });
  };
  $("#btn-close-sidebar").onclick = () => {
    $("#sidebar").classList.add("collapsed");
    state.ui.sidebar_open = false;
    call("set_ui", { sidebar_open: false });
  };

  // 划词
  document.addEventListener("mouseup", (e) => {
    if (e.target.closest("#popover") || e.target.closest(".sidebar")) return;
    if (e.target.closest("#lightbox")) return;
    setTimeout(() => handleSelection(e), 10);
  });
  $("#pop-close").onclick = () => $("#popover").classList.add("hidden");

  // 插图放大：点图片本身不关，点四周暗色区域关
  $("#lightbox").onclick = (e) => {
    // 工具栏上的按钮自己处理，别让背景的「点空白关闭」抢掉
    if (e.target.closest(".lightbox-bar")) return;
    if (e.target.id === "lightbox-img") return;
    closeLightbox();
  };
  $("#lightbox-close").onclick = closeLightbox;

  // ---- 原页查看器：缩放 / 拖动 / 翻页 ----
  $("#btn-original") && ($("#btn-original").onclick = () => openPageViewer(currentVisiblePage()));
  $("#lb-zoom-in").onclick = () => lbSetZoom(lb.zoom * 1.35);
  $("#lb-zoom-out").onclick = () => lbSetZoom(lb.zoom / 1.35);
  $("#lb-fit").onclick = () => lbReset();
  $("#lb-prev").onclick = () => lbStep(-1);
  $("#lb-next").onclick = () => lbStep(1);

  // 滚轮缩放：以指针位置为锚点
  $("#lb-stage").addEventListener("wheel", (e) => {
    if ($("#lightbox").classList.contains("hidden")) return;
    e.preventDefault();
    const stage = $("#lb-stage").getBoundingClientRect();
    const cx = e.clientX - stage.left - stage.width / 2;
    const cy = e.clientY - stage.top - stage.height / 2;
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    lbSetZoom(lb.zoom * factor, cx, cy);
  }, { passive: false });

  // 双击：在 1 倍和 2.2 倍之间切换
  $("#lightbox-img").addEventListener("dblclick", (e) => {
    e.stopPropagation();
    if (lb.zoom > 1.2) {
      lbReset();
    } else {
      const stage = $("#lb-stage").getBoundingClientRect();
      lbSetZoom(2.2, e.clientX - stage.left - stage.width / 2,
                e.clientY - stage.top - stage.height / 2);
    }
  });

  // 按住拖动（放大后才需要）
  const lbImg = $("#lightbox-img");
  lbImg.addEventListener("mousedown", (e) => {
    if (lb.zoom <= 1.02) return;
    e.preventDefault();
    lb.drag = { x: e.clientX, y: e.clientY, tx: lb.tx, ty: lb.ty };
    lbApply();
  });
  window.addEventListener("mousemove", (e) => {
    if (!lb.drag) return;
    lb.tx = lb.drag.tx + (e.clientX - lb.drag.x);
    lb.ty = lb.drag.ty + (e.clientY - lb.drag.y);
    lbApply();
  });
  window.addEventListener("mouseup", () => {
    if (!lb.drag) return;
    lb.drag = null;
    lbApply();
  });

  // 设置
  $("#btn-close-settings").onclick = () => $("#settings").classList.add("hidden");
  $("#btn-save-profile").onclick = saveProfile;
  $("#btn-test-profile").onclick = testProfile;
  $("#btn-new-profile").onclick = newProfile;
  $("#btn-delete-profile").onclick = deleteProfile;
  fillProviderSelect($("#pf-provider"));
  $("#pf-provider").onchange = (e) => applyProvider(e.target.value, {
    base: "pf-base", model: "pf-model", tip: "pf-provider-tip",
  });
  bindBaseGuard("pf-base", "pf-provider", "pf-provider-tip");
  $("#profile-select").onchange = (e) => {
    call("activate_profile", e.target.value).then((r) => {
      if (!r.ok) return;
      applySnapshot(r.data);
      const p = state.profiles.find((x) => x.id === e.target.value);
      fillProfileForm(p);
    });
  };
  $("#btn-change-dir").onclick = async () => {
    const r = await call("pick_folder");
    if (!r.ok || !r.data) return;      // 取消就不动，别弹乱七八糟的提示
    await changeDataDir(r.data);
  };
  // 对话框用不了时的兜底：手动粘贴路径
  $("#btn-settings-dir-apply").onclick = async () => {
    const v = $("#settings-dir-manual").value.trim();
    if (!v) { toast("先把路径粘进去", true); return; }
    await changeDataDir(v);
    $("#settings-dir-manual").value = "";
  };
  $("#settings-dir-manual").onkeydown = (e) => {
    if (e.key === "Enter") $("#btn-settings-dir-apply").click();
  };
  $("#btn-open-dir").onclick = () => call("open_data_dir");

  // 术语库
  $("#term-search").oninput = debounce((e) => loadTerms(e.target.value), 300);
  $("#btn-export-terms").onclick = async () => {
    const r = await call("export_terms");
    if (r.ok && r.data) toast("已导出：" + r.data);
  };
  $("#btn-import-terms").onclick = async () => {
    const r = await call("import_terms");
    if (r.ok && r.data) {
      toast(`导入 ${r.data.imported} 条，库里现在共 ${r.data.total} 条`);
      loadTerms($("#term-search").value);
    }
  };

  // 卡片
  $("#card-search").oninput = debounce((e) => loadCards(e.target.value), 300);
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// 键盘：Esc 关闭浮层、抽屉与放大图；原页打开时 ← → 翻页、+/- 缩放
document.addEventListener("keydown", (e) => {
  const lbOpen = !$("#lightbox").classList.contains("hidden");
  if (lbOpen) {
    // 输入框里按方向键不该翻页
    if (e.target.matches("input, textarea")) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); lbStep(-1); return; }
    if (e.key === "ArrowRight") { e.preventDefault(); lbStep(1); return; }
    if (e.key === "+" || e.key === "=") { e.preventDefault(); lbSetZoom(lb.zoom * 1.35); return; }
    if (e.key === "-" || e.key === "_") { e.preventDefault(); lbSetZoom(lb.zoom / 1.35); return; }
    if (e.key === "0") { e.preventDefault(); lbReset(); return; }
  }
  if (e.key === "Escape") {
    $("#popover").classList.add("hidden");
    $("#settings").classList.add("hidden");
    closeLightbox();
  }
});

/* ============ 演示模式 ============
 * 只用于开发时单独检查阅读页排版，不连后端。启动加 --demo 触发。
 * 数据是编的，别当真。
 */
function demoPaper() {
  return {
    fingerprint: "demo",
    filename: "Sleep Duration and Cardiovascular Risk.pdf",
    pages: 12,
    terms: [
      { en: "hazard ratio", zh: "风险比", definition: "两组发生某结局事件的风险之比。生存分析中常用，是 Cox 回归的直接输出；HR = 1 表示两组风险相同。" },
      { en: "confidence interval", zh: "置信区间", definition: "对总体参数的区间估计。95% 置信区间表示重复抽样时约 95% 的区间会覆盖真值。" },
      { en: "prospective cohort", zh: "前瞻性队列", definition: "先确定暴露、再向前随访观察结局的研究设计，因果推断强度高于横断面研究。" },
    ],
    paragraphs: [
      {
        id: 1, page: 1, section: "前置", kind: "heading", flags: [],
        text: "Association Between Nocturnal Sleep Duration and Cardiovascular Risk in Middle-Aged Adults: A Prospective Cohort Study",
        zh: "中老年人夜间睡眠时长与心血管风险的关联：一项前瞻性队列研究",
      },
      {
        id: 2, page: 1, section: "Abstract", kind: "body", flags: [],
        text: "Background: Short and long sleep durations have both been linked to adverse cardiovascular outcomes, but whether the relationship is causal or confounded by socioeconomic and behavioural factors remains uncertain. Methods: We followed 12,438 participants aged 45 to 64 years without baseline cardiovascular disease for a median of 9.4 years. Results: Compared with the reference group of seven to eight hours, participants sleeping five hours or less had a higher risk of major adverse cardiovascular events (hazard ratio 1.34, 95% confidence interval 1.18 to 1.52).",
        zh: "背景：睡眠时间过短和过长均与不良心血管结局相关，但这一关系究竟是因果性的，还是受社会经济与行为因素混杂，目前仍不确定。方法：我们随访了 12,438 名 45 至 64 岁、基线无心血管疾病的参与者，中位随访 9.4 年。结果：与每晚 7 至 8 小时的参照组相比，睡眠 5 小时及以下者发生主要不良心血管事件的风险更高（风险比 1.34，95% 置信区间 1.18 至 1.52）。",
      },
      {
        id: 3, page: 1, section: "Introduction", kind: "heading", flags: [],
        text: "1. Introduction", zh: "1. 引言",
      },
      {
        id: 4, page: 1, section: "Introduction", kind: "body", flags: [],
        text: "Cardiovascular disease remains the leading cause of death worldwide, accounting for an estimated 17.9 million deaths each year. Over the past two decades, a growing body of epidemiological evidence has implicated sleep duration as a modifiable risk factor, independent of traditional determinants such as hypertension, dyslipidaemia, and diabetes mellitus. The observation that both insufficient and excessive sleep confer risk has been reported in more than thirty prospective cohorts across diverse populations.",
        zh: "心血管疾病仍是全球首位死因，每年约造成 1,790 万人死亡。过去二十年间，越来越多的流行病学证据表明，睡眠时长是一个可改变的风险因素，且独立于高血压、血脂异常和糖尿病等传统决定因素。不足与过量睡眠均带来风险这一现象，已在不同人群中三十余项前瞻性队列研究中被报道。",
      },
      {
        id: 5, page: 1, section: "Methods", kind: "heading", flags: [],
        text: "2. Methods", zh: "2. 方法",
      },
      {
        id: 6, page: 2, section: "Methods", kind: "body", flags: ["short"],
        text: "those with missing sleep data.",
        zh: "",
      },
      {
        id: 7, page: 2, section: "Results", kind: "body", flags: [],
        text: "During 116,918 person years of follow up, 1,047 major adverse cardiovascular events were recorded. After full adjustment, the hazard ratio for five hours or less was 1.34 with a 95 percent confidence interval of 1.18 to 1.52, and for ten hours or more it was 1.28 with a confidence interval of 1.09 to 1.51.",
        zh: "在 116,918 人年的随访期间，共记录到 1,047 例主要不良心血管事件。完全校正后，睡眠 5 小时及以下的风险比为 1.34（95% 置信区间 1.18 至 1.52），10 小时及以上为 1.28（置信区间 1.09 至 1.51）。",
      },
      {
        id: 8, page: 3, section: "Results", kind: "caption", flags: [],
        text: "Figure 1. Kaplan-Meier curves for major adverse cardiovascular events by sleep duration category.",
        zh: "图 1. 按睡眠时长分层的重大不良心血管事件 Kaplan-Meier 曲线。",
      },
      {
        id: 9, page: 3, section: "Discussion", kind: "heading", flags: [],
        text: "3. Discussion", zh: "3. 讨论",
      },
      {
        id: 10, page: 3, section: "Discussion", kind: "body", flags: [],
        text: "Several limitations warrant consideration. Sleep duration was self reported and measured at a single time point, which precludes assessment of change over time and introduces measurement error. Residual confounding by unmeasured factors such as shift work, noise exposure, and diagnosed sleep disorders cannot be excluded.",
        zh: "本研究存在若干局限需加以考虑。睡眠时长为自我报告，且仅在单一时间点测量，无法评估其随时间的变化，并引入了测量误差。倒班工作、噪声暴露及已诊断的睡眠障碍等未测量因素所造成的残余混杂亦无法排除。",
      },
    ],
  };
}

/* ============ 启动 ============ */

async function boot() {
  if (state.ready) return;
  state.ready = true;
  bind();
  startHeartbeat();       // 先起心跳，别让后台因为「太久没请求」自己退出

  if (location.hash === "#demo") {
    enterReader(demoPaper());
    return;
  }

  const r = await call("bootstrap");
  if (!r.ok) {
    toast(r.error || "启动失败", true);
    return;
  }
  applySnapshot(r.data);

  const st = r.data.state;
  if (st === "first_run") {
    showSetup();
  } else if (st === "missing") {
    toast("上次的数据目录找不到了（可能 U 盘没插或被删），请重新选一个", true);
    showSetup();
  } else if (st === "not_writable") {
    toast("数据目录写不进去，换一个位置吧", true);
    showSetup();
  } else {
    if (r.data.cloudHint) {
      toast(`提醒：数据目录在云同步目录里（${r.data.cloudHint}），有损坏风险`, true);
    }
    showHome();
  }
}

/* ============ 真实 DOM 自测 ============ */
/*
 * 用真实浏览器元素跑一遍「选服务商 → 地址是否被填」。
 *
 * 留着它是有原因的：这段逻辑曾经栽在 $ 少写一个 "#" 上
 * （querySelector("pf-base") 把 pf-base 当标签名，返回 null，抛错，
 *   地址永远填不进去），而当时的单元测试用桩模拟，把这个问题掩盖了，
 * 测试全绿但真实界面不工作。
 *
 * 用法：地址栏加 #selftest，结果直接画在页面左上角。
 * 日常使用不会触发，不影响正常流程。
 */
async function runSelftest() {
  const out = [];
  try {
    await boot();
    openSettings();
    const sel = $("#pf-provider");
    out.push("下拉选项数 = " + sel.options.length);
    out.push("onchange 是否绑定 = " + (typeof sel.onchange === "function"));
    out.push("初始地址 = " + JSON.stringify($("#pf-base").value));

    sel.value = "bailian";
    sel.dispatchEvent(new Event("change"));
    out.push("选[百炼国内站] 后地址 = " + $("#pf-base").value);
    out.push("选[百炼国内站] 后模型 = " + $("#pf-model").value);

    sel.value = "deepseek";
    sel.dispatchEvent(new Event("change"));
    out.push("选[DeepSeek] 后地址 = " + $("#pf-base").value);

    const base = $("#pf-base");
    base.value = "https://x.example.com";
    base.dispatchEvent(new Event("input"));
    out.push("手改地址后服务商 = " + JSON.stringify(sel.value));

    // 没填密钥就点测试连接：应该给中文提示，而不是走到后端报「没有可用配置」
    $("#pf-key").value = "";
    await testProfile();
    out.push("空密钥点测试 = " + $("#pf-test-result").textContent);
  } catch (e) {
    out.push("抛出异常：" + e);
  }
  const pre = document.createElement("pre");
  pre.id = "selftest-out";
  pre.style.cssText = "position:fixed;left:0;top:0;z-index:99999;background:#fff;color:#111;"
    + "padding:12px;font:13px/1.6 monospace;border:2px solid #b42318;white-space:pre-wrap;max-width:90vw";
  pre.textContent = out.join("\n");
  document.body.appendChild(pre);
}

if (location.hash === "#selftest") {
  runSelftest();
} else if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
