// 验证服务商预设逻辑（直接跑 web/app.js 里的真实代码片段，不伪造实现）
// 用法：node dev/test_providers.js
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");

const start = src.indexOf("const PROVIDERS");
const end = src.indexOf("/* ============ 首次引导 ============ */");
if (start < 0 || end < 0) {
  console.error("没能定位到 PROVIDERS 段落，app.js 结构可能改了");
  process.exit(1);
}
const snippet = src.slice(start, end);

function makeEl(value = "") {
  return {
    value, dataset: {}, innerHTML: "", textContent: "",
    _listeners: {},
    addEventListener(type, fn) { this._listeners[type] = fn; },
    fire(type) { const f = this._listeners[type]; if (f) f(); },
  };
}
const store = {};

/**
 * 严格模拟真实的 querySelector：
 * 不带 "#" 就当成标签名去找 —— 页面上不存在 "pf-base" 这种标签，所以返回 null。
 *
 * 这一条必须保留。之前桩写得太宽松（按 key 直接取），
 * 于是 applyProvider 里误写的 $("pf-base") 在测试里照样能取到元素，
 * 测试全绿、真实界面却不填地址（真实 querySelector 返回 null → 抛错）。
 */
const $ = (sel) => {
  if (typeof sel !== "string" || !sel.startsWith("#")) return null;
  const id = sel.slice(1);
  return (store[id] = store[id] || makeEl());
};
/** 模拟 document.getElementById */
const byId = (id) => (store[id] = store[id] || makeEl());

let pass = 0, fail = 0;
function check(name, cond, extra = "") {
  if (cond) { pass++; console.log(`  ✓ ${name}`); }
  else { fail++; console.log(`  ✗ ${name} ${extra}`); }
}

const toasts = [];
const toast = (msg) => toasts.push(String(msg));

const fn = new Function("$", "byId", "toast", snippet +
  "\nreturn { PROVIDERS, providerById, fillProviderSelect, applyProvider, bindBaseGuard };");
const api = fn($, byId, toast);

console.log("[1] 预设数据完整性");
check("至少 8 家服务商", api.PROVIDERS.length >= 8, `实际 ${api.PROVIDERS.length}`);
check("第一项是「请选择服务商」且不带地址",
  api.PROVIDERS[0].id === "" && !api.PROVIDERS[0].base, JSON.stringify(api.PROVIDERS[0]));
for (const p of api.PROVIDERS) {
  check(`${p.id || "(空项)"} 有 label`, !!p.label);
  if (p.id && p.id !== "custom") {
    check(`${p.id} 有 base 且是 https`, /^https:\/\//.test(p.base), p.base);
    if (p.model) {
      check(`${p.id} 模型名可用`, p.model.length > 2, p.model);
    } else {
      check(`${p.id} 模型留空时提示了怎么填`, /模型名|模型 ID/.test(p.tip), p.tip.slice(0, 40));
    }
  }
  check(`${p.id || "(空项)"} 有提示文字`, !!p.tip);
}

console.log("\n[2] 关键参数");
const bl = api.providerById("bailian");
check("百炼国内站地址", bl.base === "https://dashscope.aliyuncs.com/compatible-mode/v1", bl.base);
check("百炼默认 qwen-plus", bl.model === "qwen-plus", bl.model);
const intl = api.providerById("bailian_intl");
check("百炼国际站地址带 -intl",
  intl.base === "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", intl.base);
check("国内站与国际站地址不同", bl.base !== intl.base);

console.log("\n[3] 选中服务商后，地址和模型要真的写进输入框");
const ids = { base: "pf-base", model: "pf-model", name: "pf-name", tip: "pf-provider-tip" };
store["pf-base"] = makeEl("https://old.example.com");
store["pf-model"] = makeEl("old-model");
store["pf-name"] = makeEl("我自己起的名字");
api.applyProvider("bailian", ids);
check("地址被替换", byId("pf-base").value === bl.base, byId("pf-base").value);
check("模型被替换", byId("pf-model").value === "qwen-plus", byId("pf-model").value);
check("名称没被改动（不替人命名）", byId("pf-name").value === "我自己起的名字", byId("pf-name").value);
check("提示已更新", byId("pf-provider-tip").textContent.includes("bailian.console.aliyun.com"),
  byId("pf-provider-tip").textContent.slice(0, 40));
check("弹出了「已填入」反馈", toasts.some((t) => t.includes("已填入") && t.includes("百炼")),
  JSON.stringify(toasts));

console.log("\n[4] 选「其他 / 中转站」不覆盖用户输入");
store["pf-base"].value = "https://my-proxy.example.com/v1";
store["pf-model"].value = "my-model";
api.applyProvider("custom", ids);
check("地址不变", byId("pf-base").value === "https://my-proxy.example.com/v1", byId("pf-base").value);
check("模型不变", byId("pf-model").value === "my-model", byId("pf-model").value);
check("提示换成自定义说明", byId("pf-provider-tip").textContent.includes("/v1"),
  byId("pf-provider-tip").textContent.slice(0, 30));

console.log("\n[5] 选「请选择服务商」不动已有内容");
store["pf-base"].value = "https://api.deepseek.com";
store["pf-model"].value = "deepseek-chat";
api.applyProvider("", ids);
check("地址不变", byId("pf-base").value === "https://api.deepseek.com", byId("pf-base").value);
check("模型不变", byId("pf-model").value === "deepseek-chat", byId("pf-model").value);

console.log("\n[6] 未知 id 回退到「请选择」，不会误填别家地址");
check("未知 id", api.providerById("no-such").id === "", api.providerById("no-such").id);

console.log("\n[7] 手改地址后服务商自动切到「其他」");
store["setup-base"] = makeEl("");
store["setup-provider"] = makeEl("");
store["setup-base"].value = bl.base;
store["setup-provider"].value = "bailian";
api.bindBaseGuard("setup-base", "setup-provider", "setup-provider-tip");
store["setup-base"].fire("input");
check("地址与预设一致时不动下拉", byId("setup-provider").value === "bailian", byId("setup-provider").value);

store["setup-base"].value = "https://api.deepseek.com";
store["setup-base"].fire("input");
check("地址被改成别家后切到「其他」", byId("setup-provider").value === "custom", byId("setup-provider").value);
check("提示也换成自定义说明", byId("setup-provider-tip").textContent.includes("/v1"),
  byId("setup-provider-tip").textContent.slice(0, 30));

store["setup-provider"].value = "";
store["setup-base"].value = "https://whatever.example.com";
store["setup-base"].fire("input");
check("「请选择」状态下不强行切走", byId("setup-provider").value === "", byId("setup-provider").value);

console.log("\n[8] 下拉框选项");
const sel = makeEl();
api.fillProviderSelect(sel);
check("选项数量与预设一致", (sel.innerHTML.match(/<option/g) || []).length === api.PROVIDERS.length);
check("包含百炼国内站", sel.innerHTML.includes("国内站"));
check("包含百炼国际站", sel.innerHTML.includes("国际站"));
check("包含「请选择服务商」", sel.innerHTML.includes("请选择服务商"));
check("重复调用不重复填充",
  (api.fillProviderSelect(sel), (sel.innerHTML.match(/<option/g) || []).length === api.PROVIDERS.length));

console.log(`\n结果：通过 ${pass}，失败 ${fail}`);
process.exit(fail ? 1 : 0);
