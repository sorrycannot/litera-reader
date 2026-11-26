// 验证图注归位逻辑（直接跑 web/app.js 里的真实代码片段，不伪造实现）
// 用法：node dev/test_figures.js
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
const start = src.indexOf("const CAP_PREFIX");
const end = src.indexOf("/* ===== planFigureNotes 以上");
if (start < 0 || end < 0) {
  console.error("没能定位到 planFigureNotes 段落，app.js 结构可能改了");
  process.exit(1);
}
const snippet = src.slice(start, end);
const api = new Function(snippet + "\nreturn { planFigureNotes, FIG_CAP_PAT, TAB_CAP_PAT };")();
const { planFigureNotes } = api;

let pass = 0, fail = 0;
function check(name, cond, extra = "") {
  if (cond) { pass++; console.log(`  ✓ ${name}`); }
  else { fail++; console.log(`  ✗ ${name} ${extra}`); }
}
const para = (id, page, kind, text) => ({ id, page, kind, text, zh: "" });
const note = (list, key) => (list || []).map((p) => p.id);

console.log("[1] 单图单注：最常见的情况");
{
  const paras = [
    para(1, 1, "body", "正文讲方法"),
    para(2, 1, "caption", "Fig. 1 Associations of PCL-R factor 1 with volumes"),
    para(3, 1, "body", "正文继续"),
  ];
  const figures = { 1: [[100, 60, 500, 280]] };
  const r = planFigureNotes(paras, figures);
  check("图注被摘出正文", r.take.has(2), JSON.stringify([...r.take]));
  check("图注挂到第 1 页第 0 张图", note(r.byFig["1:0"]).join() === "2", JSON.stringify(r.byFig));
  check("正文段落没被误摘", !r.take.has(1) && !r.take.has(3));
}

console.log("[2] 一页两张图：按 bbox 上沿 y 排序配对，不受数组顺序影响");
{
  const paras = [
    para(10, 8, "caption", "Fig. 3 first figure caption"),
    para(11, 8, "caption", "Fig. 4 second figure caption"),
  ];
  // 数组里「下面那张」排在前面（真实解析结果就是这样，见 s00406 第 8 页）
  const figures = { 8: [[85, 317, 510, 633], [99, 58, 496, 266]] };
  const r = planFigureNotes(paras, figures);
  check("第一条注配最上面的图（数组下标 1）", note(r.byFig["8:1"]).join() === "10",
    JSON.stringify(r.byFig));
  check("第二条注配下面的图（数组下标 0）", note(r.byFig["8:0"]).join() === "11",
    JSON.stringify(r.byFig));
  check("两条注都摘走", r.take.has(10) && r.take.has(11));
}

console.log("[3] 图注被版面拆成两截：后半截跟主注一起走");
{
  const paras = [
    para(20, 6, "caption", "Fig. 1 Associations of PCL-R factor 1"),
    para(21, 6, "body", "夹在中间的正文"),
    para(22, 6, "caption", "show the individual data points, the regression line"),
  ];
  const figures = { 6: [[99, 58, 496, 275]] };
  const r = planFigureNotes(paras, figures);
  check("主注 + 续注都归到同一张图", note(r.byFig["6:0"]).join() === "20,22",
    JSON.stringify(r.byFig));
  check("中间正文没被误摘", !r.take.has(21));
}

console.log("[4] 表格标题不能当图注");
{
  const paras = [
    para(30, 3, "caption", "Table 1 Subsample characteristics"),
    para(31, 3, "caption", "Fig. 1 A real figure caption"),
  ];
  const figures = { 3: [[80, 100, 500, 400]] };
  const r = planFigureNotes(paras, figures);
  check("Table 1 留在正文", !r.take.has(30), JSON.stringify([...r.take]));
  check("只有 Fig. 1 被摘走", r.take.has(31) && note(r.byFig["3:0"]).join() === "31");
}

console.log("[5] 没有位图的页（矢量图）：图注留在正文，不硬塞");
{
  const paras = [para(40, 9, "caption", "Figure 5. The Bayesian Brain Immune Module")];
  const r = planFigureNotes(paras, {});
  check("图注不摘", !r.take.has(40), JSON.stringify([...r.take]));
  check("没有 byFig 归属", Object.keys(r.byFig).length === 0);
}

console.log("[6] 注比图多：多余的留在正文，内容不丢");
{
  const paras = [
    para(50, 2, "caption", "Fig. 1 one"),
    para(51, 2, "caption", "Fig. 2 two"),
    para(52, 2, "caption", "Fig. 3 three"),
  ];
  const figures = { 2: [[10, 10, 100, 100]] };
  const r = planFigureNotes(paras, figures);
  check("只有第一条配到图", note(r.byFig["2:0"]).join() === "50");
  check("多出的两条仍在正文", !r.take.has(51) && !r.take.has(52));
}

console.log("[7] 页脚小字、没有主注的散 caption 不动");
{
  const paras = [
    para(60, 10, "caption", "contains supplementary material available at https://doi.org/"),
    para(61, 10, "body", "正文"),
  ];
  const figures = { 10: [[10, 10, 100, 100]] };
  const r = planFigureNotes(paras, figures);
  check("没有 Fig 编号的小字不摘", !r.take.has(60), JSON.stringify([...r.take]));
  check("没有 byFig 归属", Object.keys(r.byFig).length === 0);
}

console.log("[8] 多页各自配对，互不串页");
{
  const paras = [
    para(70, 3, "caption", "Figure 1. Sample Processing"),
    para(71, 4, "caption", "Figure 2. Differential Gene Expression"),
  ];
  const figures = { 3: [[89, 102, 507, 456]], 4: [[60, 102, 382, 404]] };
  const r = planFigureNotes(paras, figures);
  check("第 3 页注配第 3 页图", note(r.byFig["3:0"]).join() === "70");
  check("第 4 页注配第 4 页图", note(r.byFig["4:0"]).join() === "71");
  check("没有跨页串位", Object.keys(r.byFig).length === 2);
}

console.log("[9] figures 的 key 是字符串（走缓存 JSON 后的形态）也能配对");
{
  const paras = [para(80, 5, "caption", "Fig. 2 caption")];
  const figures = { "5": [[10, 20, 200, 200]] };
  const r = planFigureNotes(paras, figures);
  check("字符串 key 命中", note(r.byFig["5:0"]).join() === "80", JSON.stringify(r.byFig));
}

console.log("[10] 续注不会跨页黏到上一页的主注");
{
  const paras = [
    para(90, 3, "caption", "Figure 1. First figure"),
    para(91, 4, "caption", "a lowercase fragment on the next page"),
  ];
  const figures = { 3: [[10, 10, 100, 100]], 4: [[10, 10, 100, 100]] };
  const r = planFigureNotes(paras, figures);
  check("下一页的碎片没被算进上一页", note(r.byFig["3:0"]).join() === "90",
    JSON.stringify(r.byFig));
  check("第 4 页没有主注，碎片留正文", !r.take.has(91));
}

console.log("[11] 表格标题 / 章节标题是断点，后面的小字不黏到上一张图");
{
  const paras = [
    para(100, 3, "caption", "Fig. 1 a real figure caption"),
    para(101, 3, "body", "some body text"),
    para(102, 3, "caption", "Table 2 demographic characteristics"),
    para(103, 3, "caption", "* p < 0.05, values are mean (SD)"),
    para(104, 3, "caption", "Fig. 2 another figure"),
    para(105, 3, "heading", "Discussion"),
    para(106, 3, "caption", "a stray lowercase note"),
  ];
  const figures = { 3: [[10, 10, 100, 100], [10, 200, 100, 300]] };
  const r = planFigureNotes(paras, figures);
  check("Fig.1 只带自己，不带 Table 注", note(r.byFig["3:0"]).join() === "100"
    || note(r.byFig["3:1"]).join() === "100", JSON.stringify(r.byFig));
  check("Table 标题留在正文", !r.take.has(102));
  check("Table 的小字注留在正文", !r.take.has(103), JSON.stringify([...r.take]));
  check("章节标题后的散注留在正文", !r.take.has(106));
  check("Fig.2 仍被摘走", r.take.has(104));
}

console.log("[12] 面板编号续文（(A) 开头，已标 caption）归入主图注，不放正文");
{
  const paras = [
    para(200, 3, "caption", "Figure 1. Sample Processing"),
    para(201, 3, "caption", "(A) Five hundred brains were collected through the resource center"),
    para(202, 3, "body", "the next real body paragraph"),
  ];
  const figures = { 3: [[10, 10, 500, 400]] };
  const r = planFigureNotes(paras, figures);
  check("(A) 续文被摘出正文", r.take.has(201), JSON.stringify([...r.take]));
  check("(A) 续文和主注挂到同一张图", note(r.byFig["3:0"]).join() === "200,201",
    JSON.stringify(r.byFig));
  check("后续正文没被误摘", !r.take.has(202));
}

console.log("[13] 补充材料的图注（Extended Data / Supplementary / Fig. S1）也要配对");
{
  const paras = [
    para(300, 8, "caption", "Extended Data Fig. 1 | Dataset Overview. (a) Table with the size of the datasets"),
    para(301, 8, "caption", "and the number of perturbations. (b) UMAP on the perturbations per dataset"),
    para(302, 9, "caption", "Supplementary Fig. S2 | Alternative measures of the double perturbation"),
  ];
  const figures = { 8: [[88, 65, 509, 531]], 9: [[118, 60, 480, 425]] };
  const r = planFigureNotes(paras, figures);
  check("Extended Data Fig. 1 被摘出正文", r.take.has(300), JSON.stringify([...r.take]));
  check("它的续注也一起走", r.take.has(301));
  check("两条注挂到第 8 页那张图", note(r.byFig["8:0"]).join() === "300,301",
    JSON.stringify(r.byFig));
  check("Supplementary Fig. S2 也挂上了", note(r.byFig["9:0"]).join() === "302",
    JSON.stringify(r.byFig));
}

console.log("[15] 无编号图注（Science 的 RESEARCH ARTICLE SUMMARY）：说明直接印在图下");
{
  const legend1 = para(500, 1, "caption", "Cat1 filament networks provide immunity against phage infection. During the");
  legend1.flags = ["fig_legend"];
  const legend2 = para(501, 1, "caption", "type III CRISPR-Cas response, recognition of a target phage transcript");
  legend2.flags = ["fig_legend"];
  const paras = [
    para(499, 1, "body", "RESULTS: Upon activation of cOA production, NAD+ is depleted"),
    legend1,
    legend2,
    para(502, 1, "body", "CONCLUSION: Other prokaryotic defense systems also trigger filamentation"),
  ];
  const figures = { 1: [[302, 180, 557, 320]] };
  const r = planFigureNotes(paras, figures);
  check("无编号图注被摘出正文", r.take.has(500), JSON.stringify([...r.take]));
  check("它的后半截也一起走", r.take.has(501));
  check("两截挂到同一张图", note(r.byFig["1:0"]).join() === "500,501", JSON.stringify(r.byFig));
  check("正文没被误摘", !r.take.has(499) && !r.take.has(502));

  // 没有 fig_legend 标记的普通 caption 不能自己起一组（会顶掉编号图注）
  const stray = para(510, 2, "caption", "some lowercase fragment that is not a legend");
  const paras2 = [
    para(511, 2, "caption", "Fig. 2 | A properly numbered caption"),
    stray,
  ];
  const r2 = planFigureNotes(paras2, { 2: [[60, 60, 500, 300]] });
  check("编号图注优先", note(r2.byFig["2:0"])[0] === 511, JSON.stringify(r2.byFig));
}

console.log("[14] 图在页底、图注溢出到下一页：占位注被真图注顶替");
{
  const paras = [
    para(400, 15, "caption", "Extended Data Fig. 8 | See next page for caption."),
    para(401, 16, "caption", "Extended Data Fig. 8 | Alternative measures of the single perturbation"),
  ];
  // 第 15 页只有图，第 16 页只有图注（没有图）
  const figures = { 15: [[125, 66, 474, 701]] };
  const r = planFigureNotes(paras, figures);
  check("真图注挂到上一页那张图", note(r.byFig["15:0"]).join() === "401",
    JSON.stringify(r.byFig));
  check("「见下页」占位注不再显示", r.take.has(400) && !(r.byFig["15:0"] || []).some((p) => p.id === 400));
  check("真图注从正文流里摘走", r.take.has(401));
}

console.log("[15] 正文里的图表引用句不当图注（会被后端判成 body，前端也不该抢）");
{
  const paras = [
    para(500, 10, "body", "Figure S5 (genes marked in red) highlights many of the key genes"),
    para(501, 10, "caption", "Table 1. Top 20 Modules in PFC Ranked for Relevance to LOAD Pathology"),
  ];
  const figures = { 10: [[60, 78, 550, 438]] };
  const r = planFigureNotes(paras, figures);
  check("引用句留在正文", !r.take.has(500));
  check("表格标题不挂到图上", !r.take.has(501), JSON.stringify(r.byFig));
}

console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
