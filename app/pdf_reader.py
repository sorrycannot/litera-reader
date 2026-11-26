# -*- coding: utf-8 -*-
"""
PDF 抽取与段落切分 —— 整个工具的地基

为什么这一层决定成败
--------------------
PDF 里没有「段落」这个东西，只有一堆带坐标的文本碎片。
如果切错，下面挂的中文译文就整体错位半屏；而使用者英文读得慢，
根本发现不了错位，会把上一段的意思当成这一段记住。这比没有译文更糟。

所以本模块除了切段，还要给每段打「置信度标记」（flags）：
有可疑特征的段落会被标出来，后续翻译环节可以选择不翻、只保留英文，
避免给出错位的译文。

处理链
------
  逐页抽行（含坐标、字号）
    → 判断单栏/双栏
    → 处理跨栏的通栏元素
    → 按栏内顺序合并成段落（三信号：行距突变 / 首行缩进 / 字号跳变）
    → 剔除页眉页脚（跨页重复 + 位于页边）
    → 在 References 处截断
    → 打标签（正文/标题/图表说明）与置信度
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pymupdf
except ImportError:  # 旧版本兼容
    import fitz as pymupdf

# ---------------- 常量 ----------------

MIN_PARA_CHARS = 20        # 少于这个字符数的段落视为碎片
HEADING_RATIO = 1.12       # 字号超过正文这个倍数，视为标题
HEADING_RATIO_BIG = 1.45   # 超过这个倍数直接是标题层（1.12~1.45 之间还要看是否像标题）
DENSE_DRAWINGS = 80        # 一页矢量元素超过这个数，视为「整页是矢量图」
COLUMN_MID_TOLERANCE = 0.06  # 判断「跨中线」的容差（相对页宽）
# 跨页合并的上限。正常段落极少超过三千字符，超过就是判断错了（比如整页被按单栏
# 排序，左右两栏的行混在一起）。设个上限，宁可少接一次，也不要产出一段看不清的巨块。
MAX_MERGED_CHARS = 3200

REF_HEAD_PAT = re.compile(
    r"^\s*(\d+\.?\s*)?(references?|bibliography|literature\s+cited|参考文献|引用文献)\s*[:：]?\s*$",
    re.IGNORECASE,
)

# 参考文献**之后**重新回到正文的章节标题。很多期刊（Elsevier / MDPI / Frontiers /
# 部分 Nature 子刊）把附录、补充材料、致谢、数据可用性声明排在参考文献后面，
# 这些是要翻译的正文，不能跟着参考文献一起当噪音丢掉。
# 只在「已进入参考文献区之后」用这个正则找重启点，不会影响正文里的同名句子。
POST_REF_HEAD_PAT = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s*)?(?:"
    r"appendix|appendices|supplementary|supplemental|supporting\s+information"
    r"|online\s+supplement|extended\s+data|additional\s+file"
    r"|acknowledge?ments?|author\s+contributions?|authors?\s+contributions?"
    r"|cr?edit(?:\s+authorship\s+contribution\s+statement)?"
    r"|data\s+availability|availability\s+of\s+data|code\s+availability"
    r"|materials?\s+availability|competing\s+interests?|conflicts?\s+of\s+interest"
    r"|declarations?|funding|financial\s+support|ethics\s+statement"
    r"|consent|glossary|abbreviations?|nomenclature|endnotes?"
    r"|附录|补充材料|补充信息|补充说明|致\s*谢|作者贡献|利益冲突|数据可用性|声明"
    r")",
    re.IGNORECASE,
)

SECTION_PAT = re.compile(
    r"^\s*(\d+\.?\s*)?(abstract|summary|introduction|background|methods?|materials?\s+and\s+methods?"
    r"|patients?\s+and\s+methods?|subjects?|results?|findings?|discussion|conclusions?"
    r"|limitations?|acknowledge?ments?|supplementary|appendix|study\s+design"
    r"|statistical\s+analysis|摘要|引言|前言|方法|结果|讨论|结论)\b",
    re.IGNORECASE,
)

# 图注/表注的「编号」写法。顶级期刊的补充材料另有一套：
#   Figure 1 / Fig. 1 / Fig. S1 / Extended Data Fig. 3 / Supplementary Table 2
# 前缀不认，图注就会被当成正文翻译，而且永远挂不到对应的图上 ——
# Nature 篇的 Extended Data Fig. 1~10 就是这么和图分家的。
_CAP_PREFIX = r"(?:(?:extended\s+data|supplementary|supporting|online)\s+)?"
_CAP_NUM = r"(?:fig(?:ure)?|tab(?:le)?|图|表)\.?\s*[sS]?\d+"

CAPTION_PAT = re.compile(
    r"^\s*" + _CAP_PREFIX + _CAP_NUM, re.IGNORECASE
)

# 表注「Table 1 | …」单独认一下。表注和表头/表注小字常常挤在同一块小字里，
# 但它们不是同一段话（表注是表题，后面那些是表体），拼在一起只会更难读，
# 所以 _absorb_caption_blocks 只处理图注，不碰表注。
TABLE_CAPTION_PAT = re.compile(
    r"^\s*" + _CAP_PREFIX + r"(?:tab(?:le)?|表)\.?\s*[sS]?\d+", re.IGNORECASE
)

# 正文里引用图的句子：「Figure 1 connects state representation…」。
# 图注是「编号 + 标题短语」（编号后紧跟句点或大写名词），引用句是
# 「编号 + 空格 + 小写动词」。前者才是 caption，后者是正经正文，别标成灰字。
FIG_REF_PAT = re.compile(
    r"^\s*" + _CAP_PREFIX
    + r"(?:[Ff]ig(?:ure)?|[Tt]ab(?:le)?|图|表)\.?\s*[sS]?\d+"
    # ① 编号 + 面板号 + 右括号/逗号：「Figure 4b). The mutant is a monomer…」
    + r"(?:\s*[A-Za-z]{1,2}\s*[\)\],;]"
    # ② 编号 + 小写动词：「Figure 1 connects state representation…」。
    #    只认**纯小写单词**（≥3 个字母）—— 以前写成「空格 + 任意小写字符」，
    #    于是真图注 "Fig. 4 iPLA2β dimerization and mechanism of CaM inhibition"
    #    因为蛋白名以小写 i 开头，被当成引用句丢掉；整页的图注边界就全错位了。
    + r"|\s*[a-z]?\s*[a-z]{3,}"
    # ③ 编号 + 括号补充：「Figure S5 (genes marked in red) highlights…」
    + r"|\s*[\(\[]"
    + r")"
)

# 多面板图的图注续文常以面板编号开头：「(A) Five hundred brains were collected…」。
# 这类续文紧跟在图注标题后面、字号明显小于正文，本该和主图注一起放在图下面，
# 但旧逻辑只认「小写字母开头」，于是 (A)(B)(C) 这种被当成了正文翻译。
PANEL_NOTE_PAT = re.compile(r"^\s*\([A-Za-z0-9]{1,3}\)\s*\S")

# 摘要、关键词常常和正文挤在同一行（"Abstract Psychopathy is one of…"），
# 直接翻会得到「摘要 精神病态是……」，读着别扭。拆出来当小标题。
FRONT_LABEL_PAT = re.compile(r"^(abstract|summary|keywords?|摘要|关键词)\s+", re.IGNORECASE)

# 句末标点：用来判断跨页段落是否被页边界切断
SENT_END_PAT = re.compile(r"[.!?:;\"'”’)\]]\s*$")

# 一行文字「像不像标题」。字号只比正文大一点点时（1.12~1.45 倍），
# 光看字号分不出「摘要」和「小标题」，得再看这行本身长什么样：
# 标题不会以句号逗号结尾，也不会是一句十几个词的长句。
TITLE_WORD_LIMIT = 12


def _all_caps(text: str) -> bool:
    letters = [c for c in (text or "") if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def _looks_like_title(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    letters = [c for c in t if c.isalpha()]
    # 没有字母、或字母占比不到一半：「0.5 10」「37 65 15」是图里的刻度，不是标题
    if len(letters) < 3 or len(letters) < len(t.replace(" ", "")) * 0.5:
        return False
    # 带运算符的是公式片段（"recall/sensitivity = TP + FN"、"ql=ql−1 ·#discoveries"）
    if re.search(r"[=+−×÷<>≤≥±]", t):
        return False
    # 数字太多的是化学式、坐标、表格碎片（"H2N"、"TN+ FP"、"17 0.4"）
    if sum(c.isdigit() for c in t) > len(t.replace(" ", "")) * 0.25:
        return False
    # 全大写（INTRODUCTION / RESULTS）几乎一定是标题
    if all(c.isupper() for c in letters):
        return True
    if len(t) > 70:
        return False
    if re.search(r"[,.;:!?]\s*$", t):      # 以标点收尾：是一句话，不是标题
        return False
    if re.search(r"[,;]\s", t):            # 句中有逗号分号：是句子
        return False
    return len(t.split()) <= TITLE_WORD_LIMIT

# 首页底部那块小字的特征（作者单位、通讯方式、投稿日期、版权、基金）
#
# 为什么要专门认它：这块内容在首页是**双栏小字**，而首页因为大标题通栏会被判成单栏，
# 于是左右两栏同一水平线上的行被拼成一句，抽出的是
# 「University Hospital Düsseldorf, Heinrich Heine University p.pieperhoff@fz-juelich.de」
# 这种把两个单位缝在一起的东西 —— 翻出来是纯噪音，还占着正文的位置。
FOOTNOTE_PAT = re.compile(
    r"(@|https?://|doi\.org|\bReceived\b|\bAccepted\b|\bPublished\s+online\b|©|"
    r"\bUniversit(y|ät|aet)\b|\bInstitute\b|\bDepartment\b|\bHospital\b|"
    r"\bCent(re|er)\b|\bSchool\b|\bFaculty\b|\bCollege\b|\bClinic\b|"
    r"\bLaborator(y|ies)\b|\bCorresponding\s+author\b|E-?mail|"
    r"\bFunding\b|\bConflict\s+of\s+interest\b|\bORCID\b|"
    # 期刊首页的固定按钮/标签（Nature 系排在作者行左侧）
    r"\bCheck\s+for\s+updates\b|\bSupplementary\s+information\b|"
    r"\bElectronic\s+supplementary\b|\bOpen\s+Access\b)",
    re.IGNORECASE,
)

# 出版社印在页边上的「下载水印」。Science 系是在右侧竖着排一整条：
#   "Downloaded from https://www.science.org at China Pharmaceutical University on October 17, 2026"
# 它既不是正文也不是图注，翻译出来就是一行莫名其妙的机构名+日期（用户截图里就出现了）。
# 各家措辞不同（Wiley / Elsevier / ACS / JSTOR / Springer 都用这一套），
# 但以 "Downloaded from/by/via/for" 开头的占绝大多数，其余按常见固定句式补。
WATERMARK_PAT = re.compile(
    r"^\s*(?:Downloaded\s+(?:from|by|via|for)\b"
    r"|This\s+article\s+is\s+protected\s+by\s+copyright\b"
    r"|For\s+personal\s+use\s+only\b"
    r"|Terms\s+of\s+use\b|Distributed\s+by\b"
    r"|Not\s+for\s+redistribution\b)",
    re.IGNORECASE,
)

# 表格区域最小尺寸（小于这个的忽略，多半是排版用的细框）
MIN_TABLE_W, MIN_TABLE_H = 60.0, 40.0


# ---------------- 数据结构 ----------------

@dataclass
class Para:
    id: int
    page: int
    text: str
    size: float
    kind: str                 # body / heading / caption / table
    section: str
    flags: list[str] = field(default_factory=list)
    table_index: int = -1     # kind == "table" 时，指向该页第几张表（从 0 起）
    formula_index: int = -1   # kind == "formula" 时，指向该页第几块公式（从 0 起）
    page_from: int = 0        # 跨页段落：起始页（不跨页时与 page 相同）
    page_to: int = 0          # 跨页段落：结束页

    @property
    def trusted(self) -> bool:
        """
        这段该不该送去翻译。

        注意：**版面顺序不确定（layout_uncertain）不算理由**。
        以前一页里只要有个跨栏的表格，整页十几段就全部拒绝翻译，界面上写着
        「无法识别，宁愿不翻」—— 使用者看到的是整页英文，而段落切分其实没问题，
        只是顺序可能偏了一点。顺序偏一点远好过整页不翻。
        """
        return self.kind in ("body", "heading", "caption", "table_note")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "page": self.page,
            "text": self.text,
            "size": self.size,
            "kind": self.kind,
            "section": self.section,
            "flags": self.flags,
            "table_index": self.table_index,
            "formula_index": self.formula_index,
            # 跨页合段后，阅读页要能说明「这段从第 3 页起、接到第 4 页」
            "page_from": self.page_from or self.page,
            "page_to": self.page_to or self.page,
        }


@dataclass
class DocStructure:
    fingerprint: str
    filename: str
    pages: int
    columns: int
    body_size: float
    paragraphs: list[Para]
    title: str = ""                                   # 文章标题（首页版面或 PDF 元数据）
    path: str = ""                                    # 原 PDF 路径，用来渲染页面/插图
    figures: dict = field(default_factory=dict)       # {页码: [[x0,y0,x1,y1], ...]}
    tables: dict = field(default_factory=dict)        # {页码: [[x0,y0,x1,y1], ...]}
    formulas: dict = field(default_factory=dict)      # {页码: [[x0,y0,x1,y1], ...]} 显示公式

    @property
    def total_chars(self) -> int:
        return sum(len(p.text) for p in self.paragraphs)

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "filename": self.filename,
            "title": self.title,
            "pages": self.pages,
            "columns": self.columns,
            "body_size": self.body_size,
            "total_chars": self.total_chars,
            "paragraph_count": len(self.paragraphs),
            "path": self.path,
            "figures": {str(k): v for k, v in self.figures.items()},
            "tables": {str(k): v for k, v in self.tables.items()},
            "formulas": {str(k): v for k, v in self.formulas.items()},
            "paragraphs": [p.to_dict() for p in self.paragraphs],
        }


# ---------------- 工具函数 ----------------

def file_fingerprint(path: Path) -> str:
    """
    按文件内容算指纹，不是按文件名。
    这样同一篇文献改了名、换了目录，仍然命中同一份缓存，不重复花钱翻译。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _cx(line: dict) -> float:
    return (line["bbox"][0] + line["bbox"][2]) / 2.0


def _cy(line: dict) -> float:
    return (line["bbox"][1] + line["bbox"][3]) / 2.0


def _height(line: dict) -> float:
    return max(line["bbox"][3] - line["bbox"][1], 1.0)


def _norm_text(text: str) -> str:
    """数字归一化，让 'Page 3' 和 'Page 4' 归为同一模式"""
    return re.sub(r"\d+", "#", text.strip().lower())


# ---------------- 抽行 ----------------

def _is_bold_span(s: dict) -> bool:
    """这一小段字是不是粗体（PDF 的 flags 第 4 位，或字体名里带 Bold）"""
    try:
        if int(s.get("flags", 0)) & 16:
            return True
    except Exception:
        pass
    name = (s.get("font") or "").lower()
    return any(k in name for k in ("bold", "black", "heavy"))


def _rebuild_from_words(bbox, words: list) -> str:
    """
    用 words 层把一行拼回来（带空格）。

    有些期刊（本例是 Oxford 系的 Briefings in Bioinformatics）把整段文字排成
    一个 span，PyMuPDF 的 dict 模式拿到的就是
    「Theabilityofacompoundtopermeateacrosstheblood–brainbarrier」——
    空格是画上去的间距，不是空格字符。words 模式会按间距重新切词，
    所以拿它来补空格，误差比丢掉整段文字小得多。
    """
    x0, y0, x1, y1 = bbox
    picked = []
    for w in words:
        cy = (w[1] + w[3]) / 2.0
        if not (y0 - 1.5 <= cy <= y1 + 1.5):
            continue
        if w[2] < x0 - 2 or w[0] > x1 + 2:
            continue
        picked.append(w)
    picked.sort(key=lambda w: w[0])
    return " ".join(w[4] for w in picked).strip()


def _merge_row_fragments(frags: list[dict], page_width: float) -> list[dict]:
    """
    把「同一水平线被拆成好几截」的片段接回一行。

    为什么会有这种片段：PDF 里一行文字若有局部字体变化、或做了两端对齐的拉伸，
    PyMuPDF 的 dict 层会把同一行切成多个 line 对象。Cell 这篇的摘要就是这样 ——
    「highlighting an immune- and microglia-specific」被拆成 5 个 line，
    x 间隔 8.6pt（两端对齐拉宽的空格），于是下游把它们当成 5 行，
    每截各自成段，界面上就成了「an」「immune-」「and」这种孤零零的词。

    判据要能区分「行内空隙」和「栏间空隙」：
      · 行内空隙 ≈ 1 个空格被拉宽（≤ 1.05 × 字号，8.6pt vs 字号 10pt）
      · 栏间空隙 ≥ 栏间距（本例 12.1pt）
    再加一条保险：合并后的宽度不能超过页宽的 62%，单栏内容不该那么宽。
    """
    if len(frags) < 2:
        return frags

    # 按 y 分带。带的容差取半行高，避免把上下紧邻的两行吸到一起。
    ordered = sorted(frags, key=lambda l: (l["bbox"][1], l["bbox"][0]))
    bands: list[list[dict]] = []
    for f in ordered:
        if bands:
            last = bands[-1][-1]
            if abs(_cy(f) - _cy(last)) <= 0.45 * max(_height(f), _height(last)):
                bands[-1].append(f)
                continue
        bands.append([f])

    max_w = page_width * 0.62
    mid_x = page_width / 2.0
    out: list[dict] = []
    for band in bands:
        if len(band) == 1:
            out.append(band[0])
            continue
        band.sort(key=lambda l: l["bbox"][0])
        cur = dict(band[0])
        for nxt in band[1:]:
            gap = nxt["bbox"][0] - cur["bbox"][2]
            size = max(cur["size"], nxt["size"])
            x0_new = min(cur["bbox"][0], nxt["bbox"][0])
            x1_new = max(cur["bbox"][2], nxt["bbox"][2])
            width_after = x1_new - x0_new
            # 合并后横跨页面中线 = 把左右两栏缝成一句，绝对不干。
            # 这一条比「间隔够不够近」更硬：栏间距有的刊只有 9pt，
            # 光靠间距分不清「行内空格」和「栏间空隙」，但左右栏必然跨中线。
            crosses_mid = (x0_new < mid_x - 8 and x1_new > mid_x + 8)
            joinable = (
                -1.0 <= gap <= 1.35 * size          # 行内空隙（两端对齐会拉宽到 1.3 倍字号）
                and abs(cur["size"] - nxt["size"]) <= 1.2
                and width_after <= max_w
                and not crosses_mid
            )
            if not joinable:
                out.append(cur)
                cur = dict(nxt)
                continue
            # 空隙极小说明是一个词被拆开，直接拼；否则补一个空格
            sep = "" if gap < 0.3 * size else " "
            cur["text"] = (cur["text"].rstrip() + sep + nxt["text"].lstrip()).strip()
            cur["bbox"] = (x0_new, min(cur["bbox"][1], nxt["bbox"][1]),
                           x1_new, max(cur["bbox"][3], nxt["bbox"][3]))
            cur["size"] = size
            cur["fonts"] = max(cur["fonts"], nxt["fonts"])
            cur["bold"] = bool(cur.get("bold")) and bool(nxt.get("bold"))
        out.append(cur)
    return out


def _is_margin_watermark(ln: dict, text: str, page_w: float, page_h: float) -> bool:
    """
    这一行是不是出版社印在页边上的水印（不是正文）。

    两条判据，任一命中就算：
      1. 文字本身就是水印句式（`WATERMARK_PAT`）—— 与方向无关，
         Wiley/Cell 那种横排在页脚的水印也一起清掉；
      2. **竖排/斜排**且是整句（≥40 字）且落在页边带里。这一条是兜底：
         别家水印换个说法（"This copy is for..."）也能拦住。

    为什么要求「竖排 + 长句 + 页边」三条同时成立：图里的坐标轴标签
    （`Prediction error (L2)`、`OD600`）也是竖排、也常常贴到页边一字不差地落在
    左边距里 —— 但它们都很短（十几个字符），不会被误杀。
    """
    if WATERMARK_PAT.match(text or ""):
        return True
    dx, dy = (ln.get("dir") or (1.0, 0.0))[:2]
    if abs(dx) > 0.9 and abs(dy) < 0.2:      # 正常横排
        return False
    if len(text or "") < 40:
        return False
    x0, y0, x1, y1 = ln["bbox"]
    return (x0 >= page_w * 0.88 or x1 <= page_w * 0.12
            or y1 <= page_h * 0.04 or y0 >= page_h * 0.96)


def extract_lines(page, page_no: int) -> list[dict]:
    """把一页拆成带坐标与字号的文本行"""
    d = page.get_text("dict")
    words: list | None = None
    try:
        rect = page.rect
        pw, ph = float(rect.width), float(rect.height)
    except Exception:
        pw, ph = 595.0, 842.0
    frags = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:      # type=1 是图片
            continue
        for ln in block.get("lines", []):
            spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            # 拼 span 时按 x 间距补空格。有些 PDF 把每个词做成独立 span，
            # 词与词之间只有坐标间隔、没有空格字符，直接 join 会拼出
            # 「meaningfulgene modules involved inthe developmentof」。
            parts: list[str] = []
            prev_end: float | None = None
            for s in spans:
                t = s["text"]
                if prev_end is not None and parts and not parts[-1].endswith(" ") and not t.startswith(" "):
                    size = float(s.get("size", 10) or 10)
                    if s["bbox"][0] - prev_end > 0.2 * size:
                        parts.append(" ")
                parts.append(t)
                prev_end = float(s["bbox"][2])
            text = "".join(parts).strip()
            if not text:
                continue
            # 页边的下载水印（竖排的 "Downloaded from https://… at … on …"）直接丢掉：
            # 它不是正文，混进去会被当一整段翻译。
            if _is_margin_watermark(ln, text, pw, ph):
                continue
            total = sum(len(s["text"]) for s in spans) or 1
            bold_chars = sum(len(s["text"]) for s in spans if _is_bold_span(s))
            main_span = max(spans, key=lambda s: len(s["text"]))
            frags.append({
                "text": text,
                "bbox": tuple(round(float(v), 2) for v in ln["bbox"]),
                "size": round(max(float(s["size"]) for s in spans), 2),
                "page": page_no,
                "fonts": len({s.get("font", "") for s in spans}),
                # 这一行的主导字体。用来认「同字号、换字体」的小标题
                "font": main_span.get("font", ""),
                # 整行大部分是粗体才算「粗体行」：一行里夹个把粗体缩写不算，
                # 否则每行都可能被当成标题。
                "bold": bold_chars >= total * 0.6,
            })

    lines = _merge_row_fragments(frags, pw)

    # 一行又长又没有任何空白：字粘在一起了，用 words 层补空格
    for ln in lines:
        text = ln["text"]
        if len(text) <= 25 or " " in text:
            continue
        if words is None:
            try:
                words = page.get_text("words")
            except Exception:
                words = []
        fixed = _rebuild_from_words(ln["bbox"], words) if words else ""
        if fixed and " " in fixed and len(fixed) >= len(text) * 0.8:
            ln["text"] = fixed
    return lines


# ---------------- 版面判断 ----------------

def detect_layout(lines: list[dict], page_width: float) -> tuple[int, float]:
    """
    返回 (栏数, 中线x)。

    判据：双栏论文的行几乎都落在左半或右半，极少横跨中线；单栏论文的行
    从左边距一直排到右边距，中心基本压在中线上。所以「跨中线行占比」很好用。

    ⚠️ 首页是个例外：标题和摘要通栏，占比能到 0.3~0.45，而下半页又是双栏正文。
    以前卡在 0.15，首页一律判成单栏 —— 于是左右两栏同一水平线上的行被按 y 排序
    缝成一句（"…and maintains" + "brain usually by passive diffusion."），
    整页变成一段上万字的乱码。阈值放宽到 0.5：单栏页几乎所有行都居中（0.8 以上），
    仍然分得开；首页那种「上半通栏、下半双栏」则由 split_page_regions 按头部区处理。
    """
    if not lines:
        return 1, page_width / 2.0
    mid = page_width / 2.0
    near = sum(1 for l in lines if abs(_cx(l) - mid) < page_width * COLUMN_MID_TOLERANCE)
    # 按**行的跨度**分左右，不按中心：中心在左半、但右端越过中线的行是通栏行
    # （标题、摘要、整段正文），两边都不算。按中心分的话，中文单栏的整行会被
    # 当成「左栏」，另一半凑够数就误判成双栏，整页顺序全乱。
    left = sum(1 for l in lines if l["bbox"][2] <= mid + 5)
    right = sum(1 for l in lines if l["bbox"][0] >= mid - 5)
    total = len(lines)
    if left / total > 0.2 and right / total > 0.2 and near / total < 0.5:
        return 2, mid
    return 1, mid


def split_page_regions(lines: list[dict], mid: float, body_size: float,
                       page_height: float) -> tuple[list[tuple[list[dict], bool]], bool]:
    """
    把一页切成若干「区域」，按阅读顺序返回 [(区域行列表, 是否正文栏), ...]。

    为什么不能只看「是否跨中线」：
      首页的大标题、摘要正文确实横跨中线，但作者行、"Abstract" 这种小标题很短，
      够不到中线，会被误判成左栏内容，于是排到摘要后面去，顺序全乱。
      正确做法是把页首那一段通栏内容当成一个整体：只要该页有通栏元素、
      且它们都集中在页面上部，就把「y 不超过最后一个通栏元素」的所有行按 y 排序，
      作为一个头部区整体输出，其后才是双栏正文。

    第二个返回值 uncertain 表示这页的通栏元素不在页首，位置难以确定，
    此时不做重排，只标记出来让人工留意。
    """
    spanning = [l for l in lines if l["bbox"][0] < mid - 5 and l["bbox"][2] > mid + 5]

    if not spanning:
        return [
            ([l for l in lines if _cx(l) < mid], True),
            ([l for l in lines if _cx(l) >= mid], True),
        ], False

    # 头部区到哪儿为止？用**左栏正文的起点**当界线。
    #
    # 以前用「右栏第一行的上沿」。这在多数版式上等价，但 Nature 的 Brief Communication
    # 把摘要排在**右栏顶部**（左栏那个位置放的是收稿日期、Check for updates 这类小字），
    # 于是「右栏第一行」正好是摘要第一行，head_limit 卡在页顶 —— 摘要被当成右栏内容，
    # 而双栏是「左栏读完再读右栏」，摘要就跑到整页正文之后去了（读者看到正文在前、
    # 摘要跑到底下）。左栏正文起点才是「两栏正文开始的地方」，它以上的一律是头部。
    left_col = [l for l in lines if l["bbox"][2] <= mid + 5]
    right_lines = [l for l in lines if l["bbox"][0] >= mid - 5]
    widths = sorted(l["bbox"][2] - l["bbox"][0] for l in left_col)
    col_w = widths[len(widths) // 2] if widths else 0.0
    head_limit = None
    if col_w > 0:
        # 只认「排满一栏」的行：短行是日期/作者/Check for updates，
        # 比栏宽得多的行是通栏标题，两者都不能当「正文起点」
        cands = [l["bbox"][1] for l in left_col
                 if 0.9 * col_w <= (l["bbox"][2] - l["bbox"][0]) <= 1.15 * col_w]
        if cands:
            head_limit = min(cands)
    if head_limit is None:
        head_limit = min((l["bbox"][1] for l in right_lines), default=None)

    if head_limit is not None:
        # 头部区的行必须以左边距起排；右栏的行起点在中线右侧，不属于通栏内容。
        def in_head(l):
            return _cy(l) <= head_limit and l["bbox"][0] < mid - 5

        head = sorted([l for l in lines if in_head(l)], key=lambda l: (_cy(l), l["bbox"][0]))
        rest = [l for l in lines if not in_head(l)]
        # 通栏元素跑到头部区下面去了（跨整页的图表）：位置难说，标记出来
        uncertain = any(_cy(l) > head_limit for l in spanning)
        return [
            (head, False),
            ([l for l in rest if _cx(l) < mid], True),
            ([l for l in rest if _cx(l) >= mid], True),
        ], uncertain

    return [
        ([l for l in lines if _cx(l) < mid], True),
        ([l for l in lines if _cx(l) >= mid], True),
    ], True


# 段落合并 ----------------

# 行首的项目符号：bullet 用悬挂缩进排版，bullet 自己比续行更靠左，是「换一条」的
# 唯一可靠信号（首行缩进版式看着一样，不能靠缩进正负来分）。
BULLET_START_PAT = re.compile(r"^\s*(?:[•·▪●○■□◦‣∙⁃*]|[-–—](?:\s|$)|\(?[0-9]{1,2}[.)]\s)")
# 只有真·圆点符号才算「确定是悬挂缩进」。数字编号（"1. Introduction"）的续行
# 多半是顶格，套用悬挂规则反而会把正文焊起来，所以单独留一条严一点的。
BULLET_GLYPH_PAT = re.compile(r"^\s*(?:[•·▪●○■□◦‣∙⁃*]|[-–—](?:\s|$))")


def merge_paragraphs(lines: list[dict], body_size: float, body_font: str = "") -> list[list[dict]]:
    """
    同一栏内的行合并成段落。切段信号，命中任一即断开：
      a) 行间距明显大于正常行距（段间空隙）
      b) 字号变化（正文 → 标题 / 图注）
      c) 首行缩进（本行左边界比上一行明显右移）
      d) 字体在「正文」和「非正文」之间切换（同字号小标题，见下）
    """
    if not lines:
        return []
    ordered = sorted(lines, key=lambda l: (round(l["bbox"][1], 1), l["bbox"][0]))

    groups: list[list[dict]] = []
    current = [ordered[0]]
    prev = ordered[0]
    col_right = max((l["bbox"][2] for l in ordered), default=0.0)

    for ln in ordered[1:]:
        gap = ln["bbox"][1] - prev["bbox"][3]
        h = _height(prev)
        size_jump = abs(ln["size"] - prev["size"]) > 0.8
        indent = ln["bbox"][0] - prev["bbox"][0]

        # 字体换到了「另一类」：只在「正文 ↔ 非正文」之间断，
        # 正文内部换字体（基因名用斜体、公式用另一套字）不断，否则每行都成段。
        # 小标题在 PDF 里就是换了一套字体（Cell 是 AdvPSHN-H vs 正文 AdvPSA183），
        # 字号和正文一模一样，只靠字号和间距根本分不开。
        font_zone = False
        if body_font and ln.get("font") and prev.get("font"):
            font_zone = (ln["font"] == body_font) != (prev["font"] == body_font)
            # 行内小标题（run-in heading）不要断段：
            # 「**Conformation of the ANK domain.** The electron density map」整行的
            # 主导字体是粗体那套（粗体只占前半句），下一行回到正文，font_zone 就在
            # 这里切一刀，段落被拆成「…map」+「reveals nine ARs…」，各翻一遍。
            # 判据是上一行**排满整栏**：真正的独立小标题只占半行，右端离栏边还很远。
            if font_zone and prev["bbox"][2] >= col_right - max(2.0, 0.6 * body_size):
                font_zone = False

        # 这一段是不是项目符号开头（悬挂缩进）。是的话缩进规则要反着用：
        # bullet 自己顶格、续行缩进，所以「续行比上一行右移」不能断段。
        hanging = bool(BULLET_GLYPH_PAT.match(current[0]["text"] or ""))
        indented = indent > max(2.0, 0.55 * body_size)

        is_new = (
            gap > 0.55 * h
            or size_jump
            # 首行缩进的判据要和字号挂钩，但不能扣太死：很多期刊的缩进只有 8pt
            # （正文 9.8pt 的 0.8 倍），以前卡在 1.1 倍字号，整栏会被并成一段。
            # 悬挂缩进的段落例外：它的续行天然比 bullet 右移一个缩进量。
            or (indented and not hanging)
            # 项目符号行是「悬挂缩进」：bullet 本身比它的续行更靠左，
            # 所以从续行走到下一条 bullet 时是**负**缩进，旧逻辑当成「没缩进」，
            # 于是每条 bullet 都焊到上一条的最后一行：
            # 「tutorial for perturbation prediction. • For CPA, we used…」。
            # 不能简单地「负缩进就断段」—— 首行缩进版式的文章正好反过来
            # （首行 +17、续行回到栏左），那样会把每段都切成两截。
            # 所以只认「行首是项目符号且左移到上一行左边」这一种情况。
            or (ln["bbox"][0] < prev["bbox"][0] - 1.0
                and bool(BULLET_START_PAT.match(ln["text"] or "")))
            # 悬挂段落之后，突然出现比 bullet 还靠左的行 = 列表结束了，回到正文
            or (hanging and ln["bbox"][0] < current[0]["bbox"][0] - 1.0)
            # 粗细变了：小标题（Introduction / Methods）常和正文同号，只靠加粗区分，
            # 光看字号分不开，于是 "Introduction" 会跟后面那段焊成一句。
            or ln.get("bold") != prev.get("bold")
            or font_zone
            or gap < -0.6 * h          # 异常重叠（跨栏/跨块），必须断开
        )
        if is_new:
            groups.append(current)
            current = [ln]
        else:
            current.append(ln)
        prev = ln

    groups.append(current)
    return groups


# PDF 里那些「看着是空格、其实不是空格」的字符
_ODD_SPACE = {
    "\xa0": " ", "\u2002": " ", "\u2003": " ", "\u2004": " ", "\u2005": " ",
    "\u2006": " ", "\u2007": " ", "\u2008": " ", "\u2009": " ", "\u200a": " ",
    "\u202f": " ", "\u3000": " ", "\u200b": "",
}


def _clean_text(s: str) -> str:
    for k, v in _ODD_SPACE.items():
        s = s.replace(k, v)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def join_lines(lines: list[dict]) -> str:
    """
    拼接同段落的行，同时修掉两种断词：

      · 行末 `-`（hyphen-\\nation）
      · 行末软连字符 `\\xad`：Springer/Wiley 排版的常见写法，
        抽出来是 `con\\xadsistently`，看着是乱码，模型也会当成两个词
    """
    out = ""
    for ln in lines:
        t = _clean_text(ln["text"])
        if not t:
            continue
        if not out:
            out = t
            continue

        soft = out.endswith("\xad")
        if soft:
            stem = out[:-1].rstrip()
            # 软连字符后的续行是同一个词的下半截，直接接上
            out = stem + t if (t[:1].islower() or not stem.strip()) else stem + " " + t
        elif out.endswith("-") and len(out) > 1 and out[-2].isalpha() and t[:1].islower():
            out = out[:-1] + t
        else:
            out = out + " " + t
    return _clean_text(out)


# ---------------- 显示公式 ----------------

# 独立成行的显示公式（"FDP_l = ∑ …"、"(2)" 编号那一类）千万别当正文：
# PyMuPDF 抽出来的是一堆碎行，∑ 变成 "∑"、上标拆成独立的 5pt 小行、
# 分式直接消失，翻成中文更没法看（"i=11(abs (Y − ^Yadd)ji < u)"）。
# 正确做法是**把公式原样截成图片**：矢量公式渲染出来和原页一模一样。
FORMULA_BARRIER = "\x00F"          # 占位行的前缀，合并段落时用来断开

# 数学符号（用来判断「这行是不是公式」）。含这些字符的正文行很少见，
# 连续两行都带符号基本就是公式。
MATH_CHARS = set(
    "∑∏∐∫∮√∛±∓×÷≤≥≈≠≡≢∈∉∋⊂⊆⊃⊇∪∩∞∂∇⇒⇔→←↔↦∝∼≅⋅·⋯⋮⋱∅⊥∥∠′″∗∗⟨⟩‖"
    "−–✓"
)
# 数学字体：真·公式行几乎都用专门的数学字体排（STIX / Computer Modern / 各种 Math 字体），
# 和正文的字体名一眼就能分开。
MATH_FONT_PAT = re.compile(
    r"(math|stix|cmmi|cmsy|cmex|msam|msbm|euler|eusm|rsfs|lasy|stmary|"
    r"symbol|mtmi|mtsy|xits|latinmodern-math|"
    # TeX 系的数学字族：NewTXMI（数学斜体）、txmiaX、NewTXSY（符号）、
    # MathTime、MnSymbol 等等。注意不能写成 termes/txr 这种正文族，
    # 否则整页正文都被当成公式。
    r"newtx(t|s|m|e|x)?|txmi|txsy|txex|mnsymbol|pxmath|mathpazo|cambria\s*math)"
    r"|^txmia|^txsy", re.I)
# 数学字母：ℎ（U+210E）、𝑢（U+1D462）这类「数学斜体字母」只在公式里出现。
# 综述类文章的行内公式全用这套字母排，靠它把公式行认出来。
def _is_math_letter(ch: str) -> bool:
    o = ord(ch)
    return 0x1D400 <= o <= 0x1D7FF or 0x2100 <= o <= 0x214F
# 公式编号：「(4)」独占一行、右对齐在公式末尾
EQ_NUMBER_PAT = re.compile(r"^\s*[(（]\s*\d{1,3}\s*[a-zA-Z]?\s*[)）]\s*$")


def _math_marks(text: str) -> int:
    """一行里有多少个「只会在公式里出现」的字符（含组合符号：ŷ 的帽子）"""
    return sum(1 for ch in (text or "")
               if ch in MATH_CHARS or unicodedata.combining(ch) or _is_math_letter(ch))


def _is_math_line(line: dict) -> bool:
    """这一行像不像公式的一部分"""
    t = (line.get("text") or "").strip()
    if len(t) < 2:
        return False
    if MATH_FONT_PAT.search(line.get("font") or ""):
        return True
    marks = _math_marks(t)
    if marks >= 2:
        return True
    # 一个符号 + 很短的一行：「yadd = y A + yB −y∅,」这种
    return marks >= 1 and len(t) <= 45


def _col_bounds(lines: list[dict], mid: float) -> dict[bool, tuple[float, float]]:
    """按栏统计左右边界：key 是「在左栏吗」"""
    out: dict[bool, list[float]] = {}
    for ln in lines:
        left = ln["bbox"][0] < mid
        b = out.setdefault(left, [ln["bbox"][0], ln["bbox"][2]])
        b[0] = min(b[0], ln["bbox"][0])
        b[1] = max(b[1], ln["bbox"][2])
    return {k: (v[0], v[1]) for k, v in out.items()}


def _extract_formulas(lines: list[dict], body_size: float, mid: float,
                      ncol: int = 1) -> tuple[list[dict], list[dict]]:
    """
    把整行独立排版的显示公式从正文行里摘出来，原位换成一个「占位行」。

    返回 (公式块列表, 新的行列表)。公式块是 {"bbox", "text"}，
    占位行的 text 是 FORMULA_BARRIER + 序号，字号故意设成 999，
    这样前后都会被 size_jump 判定为断段 —— 公式前后本来就不是同一段话。

    三条判据合起来才认，宁可漏掉也不能错切正文：
      1. 行里有数学字体 / 数学符号；
      2. 整块横向明显窄于栏宽，而且不贴着栏左边界（显示公式是居中的），
         或者末尾有「(4)」这种公式编号 —— 正文里夹的 "b = 1/N∑iYtrain"
         贴着栏左、占满整行，会在这里被排除；
      3. 纵向相连的几行一起算一块（上标、下标、求和限都是独立小行）。
    """
    cand = [l for l in lines if _is_math_line(l)]
    if not cand:
        return [], lines

    bounds = _col_bounds(lines, mid)
    cand.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
    taken: set[int] = set()
    blocks: list[dict] = []

    # 单栏页上居中的公式天然跨过中线，不能拿中线切；双栏页才按栏分。
    def same_col(obj: dict, side: bool) -> bool:
        return True if ncol < 2 else (obj["bbox"][0] < mid) == side

    for seed in cand:
        if id(seed) in taken:
            continue
        side = seed["bbox"][0] < mid
        group = [seed]
        taken.add(id(seed))
        # 纵向贪心：同一栏、纵向相接的公式行并成一块
        for _ in range(12):
            gy0 = min(g["bbox"][1] for g in group)
            gy1 = max(g["bbox"][3] for g in group)
            grew = False
            for c in cand:
                if id(c) in taken or not same_col(c, side):
                    continue
                if c["bbox"][1] > gy1 + 3 or c["bbox"][3] < gy0 - 3:
                    continue
                if c["bbox"][3] - gy0 > 110:      # 一块公式不至于更高
                    continue
                group.append(c)
                taken.add(id(c))
                grew = True
            if not grew:
                break

        bx0 = min(g["bbox"][0] for g in group)
        by0 = min(g["bbox"][1] for g in group)
        bx1 = max(g["bbox"][2] for g in group)
        by1 = max(g["bbox"][3] for g in group)

        # 公式编号（" (4)"）常常是同一行带上另一套字体的独立小行
        eq = None
        for l in lines:
            if id(l) in taken or not same_col(l, side):
                continue
            if not EQ_NUMBER_PAT.match(l.get("text") or ""):
                continue
            if l["bbox"][1] <= by1 + 5 and l["bbox"][3] >= by0 - 5 and l["bbox"][0] > bx1 - 40:
                eq = l
                break
        if eq is not None:
            group.append(eq)
            taken.add(id(eq))
            bx1 = max(bx1, eq["bbox"][2])
            by0 = min(by0, eq["bbox"][1])
            by1 = max(by1, eq["bbox"][3])

        col_l, col_r = bounds.get(side, (bx0, bx1))
        col_w = max(col_r - col_l, 1.0)
        indented = bx0 > col_l + 6
        width_ratio = (bx1 - bx0) / col_w
        text = " ".join(g["text"] for g in group).strip()
        has_math_font = any(MATH_FONT_PAT.search(g.get("font") or "") for g in group)
        # 4 个以上英文单词 = 一句话。正文里「碰巧带符号」的行都长这样，
        # 数学字体排的公式行不会（它们的词是变量名，且短）。
        prose = not has_math_font and len(re.findall(r"[A-Za-z]{3,}", text)) >= 4

        ok = (
            (eq is not None or indented)
            and (width_ratio < 0.92 or eq is not None)
            and len(text) <= 300
            # 「∶i」这种 4pt 宽的碎片是行内公式漏出来的一小块，截出来没意义
            and (bx1 - bx0) >= 25 and (by1 - by0) >= 9
            and (eq is not None or not prose)
        )
        if not ok:
            for g in group:
                taken.discard(id(g))
            continue
        blocks.append({"bbox": [round(bx0, 2), round(by0, 2),
                                round(bx1, 2), round(by1, 2)],
                       "text": text, "ids": {id(g) for g in group}})

    if not blocks:
        return [], lines

    drop = set()
    for b in blocks:
        drop |= b["ids"]
    keep = [l for l in lines if id(l) not in drop]
    for i, b in enumerate(blocks):
        keep.append({
            "text": f"{FORMULA_BARRIER}{i}",
            "bbox": tuple(b["bbox"]),
            "size": 999.0,          # 故意离谱：前后都按「字号跳变」断段
            "page": lines[0].get("page", 0),
            "fonts": 1,
            "font": "",
            "bold": False,
        })
    for b in blocks:
        b.pop("ids", None)
    return blocks, keep


# ---------------- 插图 / 表格的版面分析 ----------------

# 小于这个尺寸的位图块基本是 logo、二维码、图标，不是论文插图
MIN_FIGURE_SIDE = 90

# 版面顶端（相对页高）以下才算正文/图区。页眉页脚（栏目名、DOI、页码）都在这之上。
# 原来取 0.07，但 Nature 的 Extended Data 图版几乎贴着页顶：panel 标题在 0.062 页高处，
# 用 0.07 会把图顶部切掉一条。页眉本身在 0.025~0.042，留 0.05 既能放过图，又能挡住页眉。
PAGE_TOP = 0.05

# 纯矢量绘制的示意图 —— Figure 1 那种「整张图是用线条和色块画出来的」，
# 在 get_image_info() 里一个位图都没有，只能靠矢量绘图元素的密度找出来。
VEC_MIN_DRAW = 60         # 一条连续矢量带至少这么多绘图元素
VEC_MIN_SPAN = 80         # 带高至少这么多 pt
VEC_BAND_STEP = 30.0      # 按 y 分带的粒度
VEC_MIN_WIDTH = 0.30      # 带至少横跨页宽的这个比例

# 三线表（booktabs）：等宽、等起点、贯穿页宽的细横线。
# 比「格子里是不是短数字」稳得多 —— 表格里写长句子也照样认得出来。
RULE_MIN_WIDTH = 0.60
RULE_MIN_LINES = 2
RULE_MIN_SPAN = 20

# 只匹配图注（表注不参与给矢量图收边）
FIG_CAPTION_PAT = re.compile(
    r"^\s*" + _CAP_PREFIX + r"(?:fig(?:ure)?|图)\.?\s*[sS]?\d+", re.IGNORECASE
)
# 目录的点线引导符：一行里出现好几个单独的「.」，那是目录不是表格
TOC_DOT_PAT = re.compile(r"^[.·…]+$")


def _box_area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _box_overlap(a, b) -> float:
    x = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    y = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return x * y


def _merge_boxes(boxes) -> list[list[float]]:
    """重叠过半就当同一块，取并集。位图块、矢量带、两套表格规则的结果都要合。"""
    out: list[list[float]] = []
    for b in sorted(boxes, key=lambda z: (z[1], z[0])):
        hit = None
        for r in out:
            m = min(_box_area(b), _box_area(r))
            if m > 0 and _box_overlap(b, r) > 0.5 * m:
                hit = r
                break
        if hit:
            hit[0] = min(hit[0], b[0]); hit[1] = min(hit[1], b[1])
            hit[2] = max(hit[2], b[2]); hit[3] = max(hit[3], b[3])
        else:
            out.append(list(b))
    return out


def _caption_tail_ok(text: str) -> bool:
    """
    "Fig. N" 后面还得有真正的图题，才算图注的开头。

    正文里引用图的句子常常被抽成单独一行、正好以 "Fig. 1b." 收尾
    （iPLA2β 第 3 页：「…the structure of the monomer is shown in」/「Fig. 1b.」）。
    这种行会被 FIG_CAPTION_PAT 认成图注，于是整页按它多切出一段，
    左侧正文连同图一起被并进「图」里裁掉。真实图注的编号后面一定跟着词，
    不会只剩一个面板字母加句点。
    """
    m = FIG_CAPTION_PAT.match(text or "")
    if not m:
        return False
    tail = (text or "")[m.end():]
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", tail))


def _fig_caption_tops(lines) -> list[float]:
    """本页所有 'Figure N …' 图注的上沿 y（升序）。

    用 extract_lines 的结果而不是 words 手工聚类：像 "Fig. 3" 这种，
    "Fig." 和编号 "3" 的基线差 1pt，words 层会被切成两行，
    手工按 y 聚类就漏掉了。extract_lines 已经按 span 把一行拼好，
    不会再拆散。

    要排掉正文里的引用句（"Figure S5 (genes marked in red) highlights…"）：
    它长得像图注的开头，但其实是正文。当成图注边界的话，整页会按它切段，
    表格、正文全被并进「图」里裁掉（Cell 那篇的 Table 1 就这么消失过）。
    """
    tops: list[float] = []
    for l in lines:
        t = (l.get("text") or "").strip()
        if FIG_CAPTION_PAT.match(t) and not FIG_REF_PAT.match(t) and _caption_tail_ok(t):
            tops.append(l["bbox"][1])
    return sorted(tops)


def _fig_caption_top(words) -> float | None:
    """本页第一处 'Figure N …' 的上沿。矢量图往往紧挨着图注，
    带的下边不能越过它 —— parse_pdf 会把区域内的文字行剔掉，
    图注一旦被包进图里，就没有中文对照了。"""
    rows: dict[int, list] = {}
    for w in words:
        key = round(((w[1] + w[3]) / 2.0) / 3.0)
        rows.setdefault(key, []).append(w)
    best = None
    for ws in rows.values():
        ws.sort(key=lambda x: x[0])
        txt = " ".join(str(x[4]) for x in ws)
        if FIG_CAPTION_PAT.match(txt) and not FIG_REF_PAT.match(txt):
            y0 = min(x[1] for x in ws)
            best = y0 if best is None else min(best, y0)
    return best


def _has_full_page_bitmap(page, lines) -> bool:
    """
    这页是否铺着一张盖住整个版心的位图（版式底图 / 扫描层 / 图形摘要背景）。

    这种位图不能当插图：它把整页正文都盖在下面，一旦算成图，`_inside`
    就会把这一页的字全当「图内文字」剔掉（综述那篇第 1 页的标题、作者、
    摘要就是这么整页消失的）。真·整页图版（Extended Data）文字很少，不满足条件。
    """
    pw, ph = float(page.rect.width), float(page.rect.height)
    if sum(1 for l in lines if len(l["text"]) >= 20) < 25:
        return False
    try:
        infos = page.get_image_info()
    except Exception:
        return False
    for im in infos:
        x0, y0, x1, y1 = (float(v) for v in im.get("bbox", (0, 0, 0, 0)))
        if (x1 - x0) >= pw * 0.85 and (y1 - y0) >= ph * 0.85:
            return True
    return False


def _is_highlight_box(g: dict, lines: list[dict] | None) -> bool:
    """
    这条 drawing 是不是「盖在正文上的高亮/底色块」，而不是图里的一个面板。

    判据：一个**单独的填色矩形**（没有描边、路径只有一个），又正好把一个文本行
    整整齐齐地包住 —— 荧光笔标注、表格单元格底色都是这样。真图的面板要么是位图，
    要么是由很多条路径拼出来的矢量图（坐标轴、曲线、箭头），不会是「一行高、
    和正文同宽」的纯色条。
    """
    if not lines:
        return False
    if (g.get("type") or "") not in ("f", "fs"):
        return False
    if len(g.get("items") or []) > 1:
        return False
    if g.get("color") is not None:        # 有描边：更像边框/坐标轴，不算高亮
        return False
    r = g["rect"]
    if (r.y1 - r.y0) > 26:                # 比一行文字高得多，可能是表格底色/图块
        return False
    for l in lines:
        lb = l["bbox"]
        # 高亮块常比文本行高出一两磅（给人留「荧光笔涂出去一点」的观感），
        # 容差给到 3pt；再大就会把图里的色块误判成高亮
        if (lb[1] - 3.0 <= r.y0 and r.y1 <= lb[3] + 3.0
                and lb[0] - 2.0 <= r.x0 and r.x1 <= lb[2] + 2.5):
            return True
    return False


def _panel_figures(page, lines) -> list[list[float]]:
    """
    识别「多面板大图」：由多个小位图块 + 少量矢量拼成的一张图。

    为什么位图/矢量带两条路都漏了它：
      · 顶级期刊（Nature 系）的多面板图，每个 panel 是独立的小位图
        （蜂群图、森林图），单块尺寸远小于 MIN_FIGURE_SIDE，逐个被过滤；
      · 而 panel 之间的矢量只有几十条，凑不够 VEC_MIN_DRAW 的密度带。
    结果是整张图既不进 figures、也不进 vector_bands，图内文字全散进正文。

    识别靠「图注分段」：图注（"Fig. N |" / "Figure N."）把页面纵向切成
    一段段，每段里的位图块 + 矢量元素合起来就是一张图的区域。两张独立图
    之间必有自己的图注隔开（如 Springer 的 Fig.3 / Fig.4），所以按图注
    分段天然不会把两张独立图焊成一张。
    """
    pw, ph = float(page.rect.width), float(page.rect.height)
    boxes: list[list[float]] = []
    try:
        for im in page.get_image_info():
            b = im["bbox"]
            if (b[2] - b[0]) > 15 and (b[3] - b[1]) > 15:   # 排除 logo/图标
                boxes.append([float(b[0]), float(b[1]), float(b[2]), float(b[3])])
    except Exception:
        pass
    try:
        for g in page.get_drawings():
            r = g["rect"]
            if r.height < 2 and r.width > pw * RULE_MIN_WIDTH:
                continue
            if r.y0 < ph * PAGE_TOP or r.y1 > ph * 0.96:
                continue
            if r.width <= 0 or r.height <= 0:
                continue
            # 正文里的**高亮/底色块**不是图元素：有的 PDF 把标注（荧光笔）存成
            # 一个填色矩形，正好盖住一行正文。它一旦算进图元素，整栏正文就会
            # 被并进「图」里裁掉（iPLA2β 第 3 页就丢了大半页正文）。
            if _is_highlight_box(g, lines):
                continue
            boxes.append([r.x0, r.y0, r.x1, r.y1])
    except Exception:
        pass
    if len(boxes) < 2:
        # 整页只有一张大图（一块位图、没有别的矢量元素）时也要往下走：
        # 图上方常印着面板标号/位点标签，按位图外接框裁会把它们切掉。
        one = boxes[0] if boxes else None
        if not (one and (one[2] - one[0]) >= 120 and (one[3] - one[1]) >= 120
                and one[1] - ph * PAGE_TOP <= 25):
            return []

    # 图注位置把页面纵向分段。多面板图必然有自己的图注（"Fig. N |"），
    # 没有图注的页（标题页、纯正文页）不能用「整段聚合」——那会把 logo、
    # 页眉装饰线这些零碎元素也聚成一张「假图」，把整页正文都吞掉。
    caps = _fig_caption_tops(lines)
    if not caps:
        return []
    bounds = [ph * PAGE_TOP] + caps + [ph * 0.96]
    out: list[list[float]] = []
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        sub = [b for b in boxes if lo <= b[1] < hi]
        # 只有一个大块、而且它紧贴页顶的页也照做：整页就一张大图的版式
        # （iPLA2β 第 7 页）没有别的元素可聚，但图上方印着几个突变位点标签，
        # 只按位图外接框裁会把标签切掉。要求「贴页顶」是为了不把别处
        # 单张大图的页也一起改（那会把图上方的正文又圈进来）。
        big_one = (len(sub) == 1
                   and (sub[0][2] - sub[0][0]) >= 120 and (sub[0][3] - sub[0][1]) >= 120
                   and sub[0][1] - ph * PAGE_TOP <= 25)
        if len(sub) < 2 and not big_one:
            continue
        x0 = min(b[0] for b in sub)
        x1 = max(b[2] for b in sub)
        # 这块图落在哪一栏：图只占半页时，另一栏的行不能参与「图区」的边界计算。
        fig_mid = (x0 + x1) / 2.0
        spans_mid = x0 < pw / 2.0 < x1

        def _same_side(l) -> bool:
            if spans_mid:
                return True
            if fig_mid > pw / 2.0:
                return l["bbox"][0] >= pw / 2.0 - 4
            return l["bbox"][2] <= pw / 2.0 + 4

        # 上界：图元素顶端往上留一点余量（实测 panel 标号/图名离图不超过 40pt），
        # 但**不能**一路拉到段首。段首可能是章节标题加整段正文 ——
        # 综述第 3 页的 Figure 1 在页面中部，图上方还排着 "1 Introduction"
        # 和好几段引言，拉到段首就会连标题带正文一起当图裁掉。
        top_el = min(b[1] for b in sub)
        y0 = max(lo + 1, top_el - 45)
        # 45pt 余量还会把「图上方的正文」圈进来：iPLA2β 第 3 页右栏正文一直排到
        # 图上方 24pt，第 2 页更是紧贴着图。所以再压一道：只要这段留白里有
        # **同一栏、整栏宽**的行（正文），就把上界压到它下沿。panel 标号（"a"、"b"）
        # 只有几个字宽，不满足这条，照样留在图里。
        for l in lines:
            lb = l["bbox"]
            if not (lb[1] <= top_el and lb[3] >= y0 - 1):
                continue
            if (lb[2] - lb[0]) > 0.35 * (x1 - x0) and _same_side(l):
                # 多留 4.5pt：裁图时还会向外扩 3pt，贴太近会把上一行正文的字脚带进来
                y0 = max(y0, min(lb[3] + 4.5, top_el))
        # 下界压到图注上沿：panel 标题、图例、坐标刻度这些纯文字就印在图注上方，
        # 不包进图里就会被当正文翻译、还和图分家。末尾哨兵(0.96ph)不是图注，
        # 那种页（整页一张图、无独立图注）就保持图元素外接框的下界。
        y1 = hi - 3 if any(abs(hi - c) < 0.5 for c in caps) else max(b[3] for b in sub)
        # 横向也要覆盖「图内文字」：多面板图的 y 轴标签（"Prediction error (L2)"）、
        # panel 标题（"c Using pretrained…"）印在位图块左侧，只包位图/矢量外接框
        # 会把它们切掉。但**只收同一栏的行** —— 图只占半页（iPLA2β 第 3 页的
        # Fig. 2 就只在右栏）时，另一栏的正文和它纵向重叠，
        # 无差别扩展会把整栏正文并进图里裁掉。
        for l in lines:
            ly = l["bbox"][1]
            if not (y0 <= ly < y1):
                continue
            if not _same_side(l):
                continue
            if l["bbox"][0] < x0:
                x0 = l["bbox"][0]
            if l["bbox"][2] > x1:
                x1 = l["bbox"][2]
        # 图只占一栏时，宽度不能拿「页宽的 40%」去卡 —— 一个栏宽的多面板图本来
        # 就只有半页宽（iPLA2β 的 Fig. 2 恰好卡在边界上）。这时改用它所在那一栏的
        # 文字宽度做基准。
        same_side_w = max((l["bbox"][2] - l["bbox"][0] for l in lines if _same_side(l)),
                          default=0.0)
        min_w = pw * 0.4 if spans_mid else max(90.0, 0.5 * same_side_w)
        if x1 - x0 >= min_w and y1 - y0 >= 60:
            # 图元素必须真的铺满这块区域。有的页在页顶只有一两条装饰线/小图标，
            # 却正好落在「页顶 ~ 图注」这一段里 —— 不校验的话会聚出一张
            # 覆盖整页的「图」，把正文全当图内文字裁掉（Cell 第 12 页就这么丢过整页）。
            # 分母用**去掉上方留白后的高度**：`y0` 为了收 panel 标号会往上放 45pt，
            # 那 45pt 里本来就没东西，算进去会让「元素铺满」的比例凭空变低
            # （iPLA2β 第 5 页的 Figure 4 就因此被否掉，三张小曲线图整块丢失）。
            core_h = max(y1 - min(b[1] for b in sub), 60.0)
            span = max(b[3] for b in sub) - min(b[1] for b in sub)
            # 元素太少时要更严：两条细装饰线（页眉线、分隔线）也能凑出 2 个元素，
            # 但它们又扁又小；真面板至少要有几十 pt 的宽和高。
            solid = sum(1 for b in sub
                        if (b[2] - b[0]) >= 40 and (b[3] - b[1]) >= 40)
            enough = big_one or len(sub) >= 3 or (len(sub) >= 2 and solid == len(sub))
            if enough and span >= 0.6 * core_h:
                out.append([round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)])
    return out


def _vector_bands(page, drawings, words) -> list[list[float]]:
    """把一页的矢量绘图元素按 y 分带，连续的高密度带合并成一张图。

    只认「元素密集」：一条页眉线、几条表格规则线不会凑够 VEC_MIN_DRAW；
    而真正的矢量示意图动辄几百上千个元素，且聚成一片连续区域。
    """
    pw, ph = float(page.rect.width), float(page.rect.height)
    pts = []
    for g in drawings:
        r = g["rect"]
        # 页眉/页脚的贯穿细线、三线表的规则线，都不是「图元素」
        if r.height < 2 and r.width > pw * RULE_MIN_WIDTH:
            continue
        if r.y0 < ph * PAGE_TOP or r.y1 > ph * 0.96:
            continue
        if r.width <= 0 or r.height <= 0:
            continue
        pts.append(r)
    if len(pts) < VEC_MIN_DRAW:
        return []

    dense: dict[int, int] = {}
    for r in pts:
        k = int(r.y0 // VEC_BAND_STEP)
        dense[k] = dense.get(k, 0) + 1
    keys = sorted(dense)
    groups, cur = [], [keys[0]]
    for k in keys[1:]:
        if k - cur[-1] <= 2:          # 允许中间空一两格
            cur.append(k)
        else:
            groups.append(cur); cur = [k]
    groups.append(cur)

    cap_y = _fig_caption_top(words)
    out: list[list[float]] = []
    for g in groups:
        lo, hi = g[0] * VEC_BAND_STEP, (g[-1] + 1) * VEC_BAND_STEP
        sub = [r for r in pts if lo - VEC_BAND_STEP <= r.y0 < hi + VEC_BAND_STEP]
        if len(sub) < VEC_MIN_DRAW or hi - lo < VEC_MIN_SPAN:
            continue
        x0 = min(r.x0 for r in sub); x1 = max(r.x1 for r in sub)
        y0 = min(r.y0 for r in sub); y1 = max(r.y1 for r in sub)
        if cap_y is not None and y0 < cap_y < y1 + 12:
            y1 = min(y1, cap_y - 3)
        if y1 - y0 < VEC_MIN_SPAN or x1 - x0 < pw * VEC_MIN_WIDTH:
            continue
        out.append([round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)])
    return out


def page_figures(page, drawings=None, words=None, vector_bands=None,
                 lines=None) -> list[list[float]]:
    """
    找出一页里的插图区域，返回几个 [x0, y0, x1, y1]。

    三种来源：
      · 位图块（照片、脑影像、病理切片、统计图）：get_image_info() 直接给 bbox
      · 纯矢量画的示意图（流程图、树图、散点图）：一个位图都没有，
        靠 drawings 的密度带找出来 —— 见 _vector_bands
      · 多面板大图（多个小位图块 + 少量矢量拼成）：见 _panel_figures

    过去只认位图，Figure 1 这类纯矢量图就完全不显示，还常被表格规则
    误裁出一小块，读者看到的正是「图没能完整显示」。裁页渲染是整页
    clip，矢量内容照样画得出来，所以只要给对 bbox 就行。
    """
    out: list[list[float]] = []
    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    pw, ph = float(page.rect.width), float(page.rect.height)
    # 铺满整页的位图多半是版式底图/扫描层，不是插图：它盖住整页正文，
    # 一旦当成图，`_inside` 就会把这一页的字全当图内文字剔掉 ——
    # 综述那篇的第 1 页（标题、作者、摘要）就是这么整页消失的。
    # 那些真是「整页一张图」的页（Extended Data 图版）文字很少，不受影响。
    full_page_bg = _has_full_page_bitmap(page, lines or [])
    for info in infos:
        x0, y0, x1, y1 = (float(v) for v in info.get("bbox", (0, 0, 0, 0)))
        if x1 - x0 < MIN_FIGURE_SIDE or y1 - y0 < MIN_FIGURE_SIDE:
            continue
        if full_page_bg and (x1 - x0) >= pw * 0.85 and (y1 - y0) >= ph * 0.85:
            continue
        out.append([round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)])

    if drawings is None:
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
    if words is None:
        try:
            words = page.get_text("words")
        except Exception:
            words = []
    if vector_bands is None:
        vector_bands = _vector_bands(page, drawings, words)
    # 多面板大图：单个位图块/矢量带都够不着，靠图注分段聚合
    panels = _panel_figures(page, lines) if lines is not None else []
    # 位图块与矢量带一起合并：同一张图被拆成位图 + 矢量标注时不该出两个卡片
    merged = _merge_boxes(out + vector_bands + panels)
    # 裁到页面内。矢量图里常有超出页面的路径（裁剪框、延伸出页边的标注），
    # 不裁的话裁页会带上一大片空白，看着像「图没对上」。
    rect = page.rect
    clipped: list[list[float]] = []
    for b in merged:
        x0 = max(b[0], rect.x0); y0 = max(b[1], rect.y0)
        x1 = min(b[2], rect.x1); y1 = min(b[3], rect.y1)
        if x1 - x0 < MIN_FIGURE_SIDE or y1 - y0 < MIN_FIGURE_SIDE:
            continue
        clipped.append([round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)])
    return clipped


# 表格探测失败时的原因，留给上层提示用。
# 为什么要记：这类失败是「静默」的 —— 表格行只是回到正文里，界面不报错，
# 使用者看到的只是「有表格的地方排版很奇怪」，很难反馈清楚。
LAST_TABLE_ERROR: str | None = None

# 表格判定参数
TABLE_GAP = 9.0          # 同一行里两个单元格之间的最小水平间隔（pt）
TABLE_MIN_CELLS = 3      # 一行至少被切出这么多格才算表格行
TABLE_MIN_ROWS = 3       # 至少连续这么多行才算表格
_NUMERIC_CELL = re.compile(r"^[\s\d.,()\[\]<>≥≤±%~\-–—/:：*#†‡a-zA-Z]{0,20}$")


def _row_words(words: list, tol: float = 2.5) -> list[list[tuple]]:
    """把 get_text("words") 的结果按 y 聚成行"""
    rows: list[list[tuple]] = []
    for w in sorted(words, key=lambda x: (round((x[1] + x[3]) / 2.0, 1), x[0])):
        cy = (w[1] + w[3]) / 2.0
        if rows and abs((rows[-1][0][1] + rows[-1][0][3]) / 2.0 - cy) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    for r in rows:
        r.sort(key=lambda x: x[0])
    return rows


def _split_cells(row: list[tuple], gap: float = TABLE_GAP) -> list[list[tuple]]:
    """一行里按水平间隔切成若干「单元格」"""
    cells: list[list[tuple]] = []
    for w in row:
        if cells and w[0] - cells[-1][-1][2] < gap:
            cells[-1].append(w)
        else:
            cells.append([w])
    return cells


def _ruled_table_bands(page, drawings, vector_bands) -> list[list[float]]:
    """三线表（booktabs）：找等宽、同起点、贯穿页宽的细横线组。

    LaTeX 排的表格几乎都有这套规则线（顶线 / 表头分隔线 / 底线）。
    它比「格子里是不是短数字」稳得多：表格里写的是长句子也认得出来，
    而目录的点线、示意图里的引导线都不会同时满足「等宽 + 同起点 + 贯穿页宽」。
    线之间还得有多行文字、且至少一行被切成多格，才算真表格。
    """
    pw, ph = float(page.rect.width), float(page.rect.height)
    lines: list[tuple] = []
    for g in drawings:
        r = g["rect"]
        if r.height < 2 and r.width > pw * RULE_MIN_WIDTH and r.y0 > ph * 0.07:
            lines.append((round(r.y0, 1), round(r.x0, 1), round(r.x1, 1)))
    if len(lines) < RULE_MIN_LINES:
        return []

    groups: dict[tuple, list[float]] = {}
    for y, x0, x1 in lines:
        key = next((k for k in groups if abs(k[0] - x0) < 3 and abs(k[1] - x1) < 3), None)
        if key is None:
            key = (x0, x1)
            groups[key] = []
        groups[key].append(y)

    try:
        rows = _row_words(page.get_text("words"))
    except Exception:
        return []
    rowinfo = []
    for row in rows:
        cells = _split_cells(row)
        ws = [c for cell in cells for c in cell]
        if not ws:
            continue
        rowinfo.append((min(c[1] for c in ws), max(c[3] for c in ws), len(cells)))

    out: list[list[float]] = []
    for (x0, x1), ys in groups.items():
        if len(ys) < RULE_MIN_LINES:
            continue
        y0, y1 = min(ys), max(ys)
        if y1 - y0 < RULE_MIN_SPAN:
            continue
        band = [x0, y0, x1, y1]
        # 落在矢量图带里的是示意图的引导线，不是表格
        if any(_box_area(band) and _box_overlap(band, v) > 0.5 * _box_area(band)
               for v in vector_bands):
            continue
        inside = [r for r in rowinfo if r[0] >= y0 - 2 and r[1] <= y1 + 2]
        # 至少一行被切成 >=3 格才算真表格。双栏正文也会被切成左右两「格」，
        # 但每行只有 2 格（长句子），那种是正文分栏、不是表格 ——
        # 之前用 >=2 就把 Nature 这类双栏正文页误判成整页表格，标题摘要全丢。
        if len(inside) < 2 or not any(r[2] >= 3 for r in inside):
            continue
        out.append([round(v, 1) for v in band])
    return out


def page_tables(page, drawings=None, vector_bands=None) -> list[list[float]]:
    """
    找出一页里的表格区域，返回几个 [x0, y0, x1, y1]。

    为什么不用 PyMuPDF 自带的 find_tables：它在新版里走 onnx 模型，
    实测一篇 13 页的文献光检测就要 **18.6 秒**（每页 1~2 秒），
    等于把「打开文献」从半秒拖成二十秒 —— 为了一个表格不值得。
    而且它依赖 onnxruntime + numpy，打包进 exe 还要多几十兆。

    两条互补的判据，都是毫秒级、不依赖任何模型：
      1. 三线表规则线（_ruled_table_bands）：等宽贯穿细线组，长句子表格也能认
      2. 连续多格行：表格每行被切成一格一格，正文的行不会

    宁可漏（漏了只是表格文字留在正文里，内容不丢），也不要误判 ——
    误判会把正文裁进「表格」里，读者看到的就是缺页。所以目录的点线行、
    以及落在矢量示意图内部的短标签，都会被排除掉。
    """
    global LAST_TABLE_ERROR
    if drawings is None:
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
    if vector_bands is None:
        try:
            vector_bands = _vector_bands(page, drawings, page.get_text("words"))
        except Exception:
            vector_bands = []

    out: list[list[float]] = []
    try:
        words = page.get_text("words")
    except Exception as e:
        LAST_TABLE_ERROR = f"{type(e).__name__}: {e}"
        return _merge_boxes(out + _ruled_table_bands(page, drawings, vector_bands))
    if len(words) < 6:
        return _merge_boxes(out + _ruled_table_bands(page, drawings, vector_bands))

    page_w = float(page.rect.width)

    # 先把每行的几何信息算出来，并标出「像表格行」的那些
    rows = []
    for row in _row_words(words):
        cells = _split_cells(row)
        if not cells:
            continue
        ws = [w for c in cells for w in c]
        cells_txt = [" ".join(w[4] for w in c) for c in cells]
        short = sum(1 for t in cells_txt if _NUMERIC_CELL.match(t))
        # 目录行：「1 Introduction . . . . . . 3」被切成一堆单字符格子，
        # 全是短格，跟数字表格长得一模一样。单独的点线引导符 >= 3 个就排除。
        dots = sum(1 for w in row if TOC_DOT_PAT.match(str(w[4]).strip()))
        rows.append({
            "x0": min(w[0] for w in ws), "x1": max(w[2] for w in ws),
            "y0": min(w[1] for w in ws), "y1": max(w[3] for w in ws),
            "n_cells": len(cells),
            "text": " ".join(cells_txt),
            "is_caption": bool(CAPTION_PAT.match(cells_txt[0])),
            "is_toc": dots >= 3,
            "core": (dots < 3 and len(cells) >= TABLE_MIN_CELLS
                     and short >= len(cells) * 0.6),
        })

    def belongs(r: dict, band_x0: float, band_x1: float, band_y: float,
                above: bool = False) -> bool:
        """这一行能不能算进表格：横向要重叠，纵向要贴得近，而且不能是一整句话"""
        if r.get("is_toc"):
            return False
        overlap = min(band_x1, r["x1"]) - max(band_x0, r["x0"])
        narrow = min(band_x1 - band_x0, r["x1"] - r["x0"])
        if narrow <= 0 or overlap < 0.5 * narrow:
            return False
        # 正文的整行（很长、没有分格）不能并进来，否则表格图会把正文一起裁走
        if r["n_cells"] < 2 and len(r["text"]) > 45:
            return False
        # 往上扩的时候 band_y 是表格上沿，得用「这一行的下沿到上沿」的距离；
        # 以前这里的 gap 被钳在 0，等于任何一行都能并进来 —— 表格上方一句短话
        # （"and who rated the"）会被当成表格的一部分裁走，正文就少了一行。
        if above:
            gap = band_y - r["y1"]
        else:
            gap = max(r["y0"] - band_y, 0)
        return -2 < gap < 14

    bands: list[list[float]] = []
    n = len(rows)
    i = 0
    while i < n:
        if not rows[i]["core"]:
            i += 1
            continue
        j = i
        while j + 1 < n and rows[j + 1]["core"]:
            j += 1
        if j - i + 1 < TABLE_MIN_ROWS:
            i = j + 1
            continue

        a, b = i, j
        # 向外扩：表格里「整行合并」的那种行（只写一个 PCL-R）格子数不够，
        # 但它是表的一部分，漏掉就会把最后一行裁掉。
        while a > 0 and not rows[a - 1]["is_caption"]:
            band = rows[a:b + 1]
            if not belongs(rows[a - 1], min(r["x0"] for r in band),
                           max(r["x1"] for r in band), rows[a]["y0"], above=True):
                break
            a -= 1
        while b + 1 < n:
            band = rows[a:b + 1]
            if not belongs(rows[b + 1], min(r["x0"] for r in band),
                           max(r["x1"] for r in band), rows[b]["y1"]):
                break
            b += 1

        band = rows[a:b + 1]
        x0 = min(r["x0"] for r in band)
        x1 = max(r["x1"] for r in band)
        y0 = min(r["y0"] for r in band)
        y1 = max(r["y1"] for r in band)
        # 上方紧挨着的是表标题（"Table 1 …"）时，别把它裁进表格图里 ——
        # 标题本身会作为正文段落显示，图里再来半截字很脏。
        if a > 0 and rows[a - 1]["is_caption"]:
            y0 = max(y0, rows[a - 1]["y1"])
        if x1 - x0 >= page_w * 0.3 and y1 - y0 >= 30:
            bands.append([round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)])
        i = b + 1

    # 叠加三线表规则线识别的表格（长句子表格靠「短数字格」认不出来，全靠这里）
    bands = _merge_boxes(bands + _ruled_table_bands(page, drawings, vector_bands))
    # 落在矢量示意图内部的「表格」是图里的短标签拼出来的，不是表格。
    # 不剔掉的话 Figure 1 会被裁出一小块当表格，正文反而丢一大片。
    if vector_bands:
        bands = [b for b in bands
                 if not any(_box_area(b) and _box_overlap(b, v) > 0.5 * _box_area(b)
                            for v in vector_bands)]
    return bands


def table_notes_under(lines: list[dict], band: list[float], body_size: float) -> list[dict]:
    """
    表格正下方的注解（"Note: …"、"Values are mean (SD)"、"*p < 0.05" 之类）。

    这些注是表的一部分：读表必须看它（缩写是什么、星号代表哪个显著性水平）。
    留在正文流里，读者看到的是表格卡片之后孤零零一段英中对照，
    跟表对不上号，还得自己往回翻。

    判据：紧挨表格下沿、字号小于正文、横向与表格有重叠。
    字号回到正文大小、或者出现明显空档，就说明已经是下一段正文了。
    """
    below = [l for l in lines if l["bbox"][1] >= band[3] - 3]
    below.sort(key=lambda l: l["bbox"][1])
    out: list[dict] = []
    bottom = band[3]
    for l in below:
        if l["size"] > body_size * 0.95:
            break
        if l["bbox"][1] - bottom > 14:
            break
        overlap = min(band[2], l["bbox"][2]) - max(band[0], l["bbox"][0])
        if overlap <= 0:
            break
        out.append(l)
        bottom = l["bbox"][3]
    return out


def _inside(bbox, boxes) -> bool:
    """这一行的中心点落进某个区域了吗"""
    if not boxes:
        return False
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    for b in boxes:
        if b[0] - 2 <= cx <= b[2] + 2 and b[1] - 2 <= cy <= b[3] + 2:
            return True
    return False


def _find_ref_start_page(pages_lines: dict[int, list[dict]]) -> int | None:
    """
    定位「参考文献正文」从哪一页开始，返回 0-based 页码；没有则 None。

    参考文献常作为大字号标题（和章节标题同级，如 14.3pt）单独起一行，
    其后的页全是 8~9pt 的条目小字。这些页会污染正文字号的加权统计，
    所以要在算 body_size 之前先找到它、把后面的页排除。

    判据：某页存在一行，文本命中 References 标题正则，且字号明显偏大
    （>= 12pt，正文一般 10~11pt、参考文献条目 8~9pt、页眉更小）。
    不依赖 body_size，避免「要定位它就得先知道字号、而字号又被它污染」的死循环。
    """
    for pno in sorted(pages_lines.keys()):
        for l in pages_lines[pno]:
            t = l["text"].strip()
            if len(t) < 60 and REF_HEAD_PAT.match(t) and l["size"] >= 12:
                return pno
    return None


def _looks_like_ref_entry(text: str) -> bool:
    """像不像一条参考文献条目（用来否决「把条目误认成章节标题」）。"""
    t = text.strip()
    if re.match(r"^\s*\[?\d{1,3}[\.\)]\s", t):
        return True
    if re.search(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)\s*[\.。]?\s*$", t):
        return True
    if len(re.findall(r"\b(?:19|20)\d{2}\b", t)) >= 2:
        return True
    # 卷期页码、doi、et al：条目里遍地都是，章节标题里不会有。
    # 「Methods 16, 8 (2019), 715–721. doi:10.1038/…」这种条目就是以章节名开头的，
    # 只靠「关键词在行首」会把整段参考文献又放回来翻译。
    if re.search(r"\bdoi\b|doi\.org|https?://", t, re.IGNORECASE):
        return True
    if re.search(r"\bet\s+al\b", t, re.IGNORECASE):
        return True
    if re.search(r"\d+\s*[–\-]\s*\d+", t):          # 715–721 / 41-50
        return True
    if re.search(r"\b(?:19|20)\d{2}\b", t):          # 任何年份
        return True
    return False


def _is_resume_heading(text: str) -> bool:
    """
    这一段是不是「参考文献结束、正文重新开始」的章节标题。

    Nature 系把 Methods 排在参考文献**后面**，Elsevier/MDPI 则是附录、补充材料，
    另有致谢、数据可用性这类声明。这些都要继续翻译，所以都算重启点。

    判定要严：参考文献条目也会以章节名开头（"Methods 16, 8 (2019)…"、
    "Conclusions. In: Proc…"）。所以除了「关键词在行首」，还要求
    关键词后面基本没内容 —— 标题就是标题本身，条目后面还跟着一长串。
    """
    t = text.strip()
    if not t or len(t) > 70 or _looks_like_ref_entry(t):
        return False
    m = POST_REF_HEAD_PAT.match(t) or SECTION_PAT.match(t)
    if not m:
        return False
    rest = t[m.end():].strip(" :：.．-–—")
    return len(rest) <= 30


def _skip_reference_block(raw_paras: list[dict]) -> list[dict]:
    """
    跳过参考文献区，但**不是**「从这里砍到文末」。

    参考文献条目翻成中文毫无意义、还占额度，所以整块不翻。
    可不少期刊在参考文献后面还排着正文：Nature 系的 Methods、Elsevier 的附录、
    补充材料、致谢、数据可用性声明……一刀切掉就等于把后半篇变成空白。

    做法：遇到 References 标题，从它往后找第一个「重启正文」的章节标题
    （Methods / Appendix / Supplementary / Acknowledgements …），
    两者之间全是条目，丢掉，从重启点继续。文中有多段参考文献
    （正文一组、Methods 一组）时就重复这个过程。

    找不到重启点 → 维持旧行为，截到文末。
    """
    kept: list[dict] = []
    i, n = 0, len(raw_paras)
    while i < n:
        p = raw_paras[i]
        if not (REF_HEAD_PAT.match(p["text"]) and len(p["text"]) < 60):
            kept.append(p)
            i += 1
            continue
        # 已进入参考文献区：往后找重启点
        j = i + 1
        while j < n and not _is_resume_heading(raw_paras[j]["text"]):
            j += 1
        if j >= n:      # 后面没有正文了，说明这就是文末的参考文献
            break
        i = j           # 从重启点继续（References 标题本身不进正文）
    return kept


# 元数据里的「标题」经常不是标题：有的是文件名（Microsoft Word - xxx.doc），
# 有的是期刊名，有的是 DOI。这些都当没有。
BAD_META_TITLE = re.compile(
    r"(\.pdf$|\.docx?$|https?://|doi\.org|\buntitled\b|^microsoft\s+word|"
    r"^document\d*$|^slide\s*\d|^powerpoint|^quark|^indd)", re.I)


# 连字：PDF 里 "Identiﬁes" 抽出来带 U+FB01，显示成怪字符，统一还原成普通字母
LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
             "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}


def _clean_title(t: str) -> str:
    t = (t or "").replace("\u00ad", "")
    for k, v in LIGATURES.items():
        t = t.replace(k, v)
    return re.sub(r"\s+", " ", t).strip()


def _meta_title(doc) -> str:
    """PDF 元数据里的标题，能用才用"""
    try:
        raw = (doc.metadata or {}).get("title") or ""
    except Exception:
        return ""
    t = _clean_title(raw)
    if not 15 <= len(t) <= 300:
        return ""
    if BAD_META_TITLE.search(t):
        return ""
    # 至少三个词才算标题（"Nature" "Article" 这种不要）
    if len([w for w in t.split() if len(w) >= 2]) < 3:
        return ""
    return t


# 副标题 vs 作者行：副标题字号比主标题小一档，作者行也小，光看字号分不开
# （Springer 那篇主标题 16pt、作者 10pt；综述主标题 24.9pt、副标题 16.4pt）。
# 作者行有它的特征：姓名之间用 · 分隔、姓名后面挂上标数字、单位/邮箱。
AUTHOR_LINE_PAT = re.compile(
    r"[·•&@]|\b(?:University|Universität|Institute|Department|Hospital|School|"
    r"College|Faculty|Laboratory|Center|Centre|Clinic)\b|"
    r"(?<=[a-zäöüß])[\d,]{1,4}\b")


def _looks_like_authors(text: str) -> bool:
    return bool(AUTHOR_LINE_PAT.search(text or ""))


def _layout_title(lines: list[dict], body_size: float, page_height: float) -> str:
    """
    从首页版面上认标题：**字号最大的那块字**。

    为什么要按字号：首页上比正文大的东西只有标题和期刊刊头
    （"nature methods" 18pt vs 标题 26pt），取最大的一层就只剩标题。
    "...with psychopathy" 这种单行标题要连带把下面一号的副标题
    （"Representation Learning, …" 16.4pt）一起收进来，否则标题被腰斩。
    """
    if not lines:
        return ""
    body = [l for l in lines if l["bbox"][1] > page_height * 0.04]
    if not body:
        return ""
    big = [l for l in body if l["size"] >= max(body_size * 1.2, 12.0)]
    if not big:
        return ""
    top = max(l["size"] for l in big)
    head = [l for l in big if l["size"] >= top - 0.6]
    head.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
    text = join_lines(head)
    if len(text) < 12:
        return ""
    # 副标题：紧跟其后、字号比主标题小一档但仍明显大于正文
    if len(text) < 90:
        last = head[-1]
        lh = max(last["bbox"][3] - last["bbox"][1], 8.0)
        subs = [l for l in body
                if l["size"] >= top * 0.55 and l["size"] <= top - 1.0
                and last["bbox"][3] - 1 <= l["bbox"][1] <= last["bbox"][3] + 1.9 * lh
                and not _looks_like_authors(l["text"])]
        subs.sort(key=lambda l: (l["bbox"][1], l["bbox"][0]))
        tail = join_lines(subs) if subs else ""
        if tail and len(text) + len(tail) <= 300:
            text = _clean_title(text + " " + tail)
    return _clean_title(text)


def extract_title(doc, body_size: float) -> str:
    """
    文章标题。顺序：PDF 元数据 → 首页版面 → 空（让调用方退回文件名）。

    两个来源都要试：Nature/arXiv 这类 PDF 元数据里的标题是准的，
    而 Cell、Springer 的 PDF 元数据干脆是空的 —— 只有版面能认。
    """
    t = _meta_title(doc)
    if t:
        return t
    try:
        page = doc[0]
        lines = extract_lines(page, 1)
        return _layout_title(lines, body_size, float(page.rect.height))
    except Exception:
        return ""


def _strip_front_matter(lines: list[dict], body_size: float, page_height: float) -> list[dict]:
    """
    首页要扔掉两块东西：页眉，以及「标题和摘要之间」的作者/单位/投稿日期。

    这块在首页是双栏小字，但首页因为大标题通栏会被判成单栏，
    左右两栏同一水平线的行就被拼成一句，抽出的是
    「University Hospital Düsseldorf, Heinrich Heine University p.pieperhoff@fz-juelich.de」——
    两个不同单位缝在一起。翻出来是纯噪音，还占着正文的位置。

    判据（三个都满足才动手，避免误伤正文）：
      · 首页
      · 在页面下半部
      · 字号小于正文，且命中机构 / 邮箱 / 投稿日期 / 版权 / 基金这类特征词
    一旦定位到最后一条小字，它以下的整块都归为脚注区（作者块是连续的一片）。
    """
    if not lines:
        return lines

    ordered = sorted(lines, key=lambda l: l["bbox"][1])
    title = max(ordered, key=lambda l: l["size"])
    title_y = title["bbox"][1]

    # 摘要起点：标题之后第一个章节名
    abs_y = None
    for l in ordered:
        if l["bbox"][1] > title_y + 1 and len(l["text"]) < 40 and SECTION_PAT.match(l["text"]):
            abs_y = l["bbox"][1]
            break

    # 脚注区：作者单位、通讯方式、版权、基金。首页这一块**常常只占一栏**
    # （Science 的 summary 页放在右栏底部），所以按栏判定，不能按整页高度一刀切 ——
    # 一刀切会把另一栏还在往下写的正文尾巴一起切掉
    # （实测 Cat1 首页把 RESULTS 的最后三行「…dramatically reduced NAD+ cleavage.」吃掉）。
    small = body_size * 0.92
    foot_lines = [l for l in ordered
                  if l["size"] <= small
                  and l["bbox"][1] > page_height * 0.5
                  and FOOTNOTE_PAT.search(l["text"])]
    foot_y = (min(l["bbox"][1] for l in foot_lines) - 8) if foot_lines else None

    def _in_footnote(l: dict) -> bool:
        """这一行是不是落在脚注块里（按「同一栏、且在脚注首行之下」判）"""
        if foot_y is None or l["bbox"][1] < foot_y:
            return False
        x0, x1 = l["bbox"][0], l["bbox"][2]
        for f in foot_lines:
            # 留 8pt 容差就够：另一栏的正文常常只差十几个点就贴到脚注左边缘，
            # 容差给大了会把隔壁栏的正文一起算进脚注
            if x1 > f["bbox"][0] - 8 and x0 < f["bbox"][2] + 8:
                return True
        return False

    title_size = title["size"]
    # 标题块的下沿。标题常排两三行（Cell 24.9pt 占三行），用「字号≥标题 92%」
    # 且紧跟在标题首行下面的那些行算进来。
    title_bottom = title["bbox"][3]
    if title_size >= body_size * 1.15:
        title_bottom = max(
            [title["bbox"][3]] + [l["bbox"][3] for l in ordered
                                  if l["size"] >= title_size * 0.92
                                  and title_y - 1 <= l["bbox"][1] <= title_y + 3.2 * title_size]
        )

    # 「标题之后、正文之前」这一带（band）要清理掉作者、收稿/接受/上线日期、
    # "Check for updates" 这类小字。不清理的话，它们按 y 排序会**插进摘要中间**，
    # 摘要被切成「第一行 → 日期 → 剩下几行」，读起来就是断的。
    #
    # 两种首页：
    #   · 有 "Abstract" 小标题（Cell 那类）：标题到小标题之间全是作者块，整段丢掉。
    #   · 没有小标题（Nature 的 Brief Communication）：这一带里还夹着摘要本身，
    #     只丢「像首页小字」的行 —— 日期/作者/按钮文字，以及和它们同一水平行的碎字。
    band = None
    strict = False
    if abs_y is not None:
        band, strict = (title_bottom - 1, abs_y), True
    elif title_size >= body_size * 1.15:
        for l in ordered:
            if l["bbox"][1] <= title_bottom + 1:
                continue
            if len(l["text"]) >= 45 and abs(l["size"] - body_size) <= 0.4:
                band = (title_bottom - 1, l["bbox"][1])
                break

    # 这一带里「明显是首页小字」的行，以及和它们同处一行（±5pt）的碎字
    noise_y: list[float] = []
    if band is not None and not strict:
        for l in ordered:
            if not (band[0] < l["bbox"][1] < band[1]):
                continue
            if (l["size"] < body_size * 0.78
                    or FOOTNOTE_PAT.search(l["text"])
                    or AUTHOR_LINE_PAT.search(l["text"])):
                noise_y.append(_cy(l))

    def _band_noise(l) -> bool:
        t = l["text"]
        if l["size"] < body_size * 0.78 or FOOTNOTE_PAT.search(t) or AUTHOR_LINE_PAT.search(t):
            return True
        # 同一水平行上的碎字（"Constantin Ahlmann-Eltze" 紧挨着 "Received: …"）。
        # 只对短行生效：摘要正文也可能和日期几乎同一高度，长行一律不能误杀。
        if len(t) >= 45:
            return False
        cy = _cy(l)
        return any(abs(cy - ny) <= 5.0 for ny in noise_y)

    keep = []
    for l in ordered:
        y = l["bbox"][1]
        if y < title_y - 1:                 # 页眉：期刊名、DOI、ORIGINAL PAPER
            continue
        if band is not None and band[0] < y < band[1]:
            # 标题常常排成两三行（Cell 这篇标题占三行，字号 24.9pt），
            # 它们和下面的作者单位一样落在「标题到摘要之间」。
            # 只看 y 区间会把标题的后两行一起删掉，界面上只剩半句标题。
            if l["size"] >= title_size * 0.92:
                keep.append(l)
            elif not strict and not _band_noise(l):
                keep.append(l)              # 这一带里的摘要正文要留下
            continue                        # 其余是作者、单位、投稿日期
        if _in_footnote(l):
            continue                        # 页底：作者单位、通讯方式、版权、基金
        keep.append(l)
    return keep


# ---------------- 页眉页脚 ----------------

def find_running_headers(pages_lines: dict[int, list[dict]],
                         page_heights: dict[int, float],
                         total_pages: int) -> set[str]:
    """
    找跨页重复且位于页边的短文本。
    两个条件同时满足才剔除，避免误杀正文里重复出现的短句。
    """
    if total_pages < 2:
        return set()
    seen: dict[str, set[int]] = {}
    for pno, lines in pages_lines.items():
        ph = page_heights.get(pno, 800)
        for l in lines:
            y0, y1 = l["bbox"][1], l["bbox"][3]
            if not (y1 < ph * 0.08 or y0 > ph * 0.92):
                continue
            if len(l["text"]) > 110:
                continue
            seen.setdefault(_norm_text(l["text"]), set()).add(pno)
    # 只有两页的短文（letter、评论）也要能处理，此时要求两页都出现才算页眉
    threshold = max(2, int(total_pages * 0.4))
    keys = {k for k, pgs in seen.items() if len(pgs) >= threshold}

    # 期刊页眉常成对：左边是书名/期刊名（每页重复，已被认出），
    # 右边是当前章节名（只在那一两章出现，重复次数不够，漏网）。
    # 两者印在同一 y 上。把与已确认页眉同行、同处页边的文本一并纳入，
    # 否则章节标题会在正文里再出现一遍，读者看到的就是重复的标题。
    if keys:
        extra: set[str] = set()
        for pno, lines in pages_lines.items():
            ph = page_heights.get(pno, 800)
            anchor_y = [l["bbox"][1] for l in lines
                        if _norm_text(l["text"]) in keys
                        and l["bbox"][3] < ph * 0.08]
            if not anchor_y:
                continue
            for l in lines:
                if len(l["text"]) > 110 or l["bbox"][3] >= ph * 0.08:
                    continue
                if any(abs(l["bbox"][1] - y) <= 2 for y in anchor_y):
                    extra.add(_norm_text(l["text"]))
        keys |= extra
    return keys


def _is_toc_page(lines: list[dict]) -> bool:
    """
    判断一页是不是目录页（Contents / Table of Contents / 目录）。

    目录页不该翻译：一整页都是「章节名 …点线… 页码」，翻出来是纯噪音，
    还白白消耗额度。识别特征（不用字号阈值，避免不同期刊字号差异误伤）：
      · 有目录标题（Contents / 目录 / Table of Contents）；
      · 行数够多，且其中相当比例的行带点线引导符（. 或 · 或 … 连成串）。

    点线引导符是最强的通用信号：正常正文几乎不会出现一行里好几个
    孤立的「.」字符，而目录的每一条目都有。
    """
    if not lines or len(lines) < 6:
        return False
    has_title = any(
        re.match(r"^\s*(contents?|table\s+of\s+contents?|目录|目錄)\s*[:：]?\s*$",
                 l["text"], re.IGNORECASE)
        for l in lines
    )
    dot_lines = 0
    for l in lines:
        t = l["text"]
        # 点线引导符常是「. . . . .」这种带空格的散点，也可能连成「……」
        # 判据：一行里出现 >= 3 个孤立的点（中间可夹空格）
        if re.search(r"(?:\.\s*){3,}", t) or re.search(r"[·…]{2,}", t):
            dot_lines += 1
    ratio = dot_lines / len(lines)
    return has_title and ratio >= 0.25


def is_running_header(line: dict, page_height: float, keys: set[str],
                      body_size: float = 0.0) -> bool:
    if len(line["text"]) > 110:
        return False
    y0, y1 = line["bbox"][1], line["bbox"][3]
    if not (y1 < page_height * 0.08 or y0 > page_height * 0.92):
        return False
    # 页眉是「小字」。章节大标题（如 "1 Introduction"）虽然文本和页眉里的
    # 章节名一样、也落在页顶，但字号明显大于正文 —— 它是要翻译的真标题，
    # 不能删。页眉字号恒 ≤ 正文，用这个把「小字页眉」和「大字标题」分开。
    #
    # 但有些期刊的栏目标签（Nature 的 "Brief Communication" 10pt）比正文还大，
    # 光看字号会把它当成标题留下来，于是每一页都多出一个同名标题。
    # 这时靠位置分：页眉压在页面上边距（实测 0.026~0.042 页高），
    # 真章节标题排在版心顶端（≥0.07 页高）。顶部 5.5% 以内的一律算页眉。
    if body_size > 0 and line["size"] > body_size + 0.5:
        if y1 >= page_height * 0.055:
            return False
    return _norm_text(line["text"]) in keys


# ---------------- 跨页段落 ----------------

def _can_join(prev: dict, nxt: dict, uses_indent: bool) -> bool:
    """
    判断这两段是不是同一段话被栏/页边界切开的。

    主判据是**首行缩进**，不是标点：
      学术排版里段落首行缩进、续行顶格。跨页后的第一行如果顶格，说明是续段；
      如果缩进了，说明是新段落。这个规律比「结尾有没有句号」可靠得多 ——
      段落内部本来就有多个句子，在句号处跨页是完全正常的。

    有些期刊不用首行缩进、改用空行分段。那种排版下本函数退回启发式判断
    （最后一行是否排满 + 下一段是否小写开头），由调用方通过 uses_indent 决定走哪条路。
    """
    next_text = nxt.get("text", "")
    if not prev.get("text") or not next_text or len(next_text) < 20:
        return False

    # 公式是单独一张图，不能和正文拼成一段（拼了就变成「…as follows. yadd = …」，
    # 而且 formula_index 会在合并里丢掉，图也没了）。表注/表格同理。
    if prev.get("formula_index", -1) >= 0 or nxt.get("formula_index", -1) >= 0:
        return False

    # 字号不同的两块内容，角色不同（正文 10pt / 图注 8.5pt / 表头更小）。
    # 不按字号拦一下，图注会被当成「上一段没写完的续行」缝进正文，
    # 于是正文末尾突然接出 "Fig. 1 Associations of PCL-R factor 1 …"。
    ps, ns = prev.get("size", 0.0), nxt.get("size", 0.0)
    if ps and ns and abs(ps - ns) > 1.0:
        return False

    # 以 Fig./Table 开头是图注、表题的起点，不是上一段的续行。
    # 这里不看上一段是什么：两个图注首尾相接时（跨栏），合并会把两张图的说明
    # 焊成一句「…p-value (uncorr.) Fig. 3 Associations of…」，反而更难读。
    if CAPTION_PAT.match(next_text):
        return False

    # 紧贴在图下面的说明（无编号图注）同理：它属于那张图，不是上一栏正文的续行。
    # 这一条挡住了实测到的一处错接：Science 图形摘要页左栏正文最后一行排满
    # （last_line_ratio≈0.95），于是把右栏图注的头一行「Cat1 filament networks
    # provide immunity…」缝成了 RESULTS 段的结尾。
    # 但只拦「字号不同」的：同一张图的图注被排成两三块、字号一致时，
    # 拼回去才是对的（Nature 的 Fig. 1 就是那样）。
    if nxt.get("is_legend") and abs(ps - ns) > 0.2:
        return False

    if uses_indent:
        # 顶格 = 续行；缩进 = 新段落起始
        return nxt.get("first_line_indent", 99.0) <= 2.0

    # 无缩进排版（空行分段）：只能看断在哪儿。
    prev_text = prev.get("text", "")
    head = next_text.lstrip()[:1]
    if SENT_END_PAT.search(prev_text):
        # 句子已经收尾：下一段只有小写开头才算同一句的续写（极少见），
        # 大写或数字开头就是新段落。以前这里先看「最后一行排满没排满」，
        # 两端对齐的正文最后一行常常也排满，于是两段被焊成一段，
        # 一篇 15 页的文献能焊出一万字的单段。
        return bool(head) and head.islower()
    # 没在句末断开：行排满 或 连字符断词，都是话没说完
    if prev.get("last_line_ratio", 0) > 0.88 or prev_text.endswith("-"):
        return True
    return bool(head) and head.islower()


def _is_placeholder(p: dict) -> bool:
    """表格与表注是页面切片时插进来的占位，不是正文段落"""
    return p.get("table_index", -1) >= 0 or bool(p.get("is_table_note"))


def _place_table_markers(paras: list[dict]) -> list[dict]:
    """把表格占位/表注从「页末」挪回它在页内的真实位置。

    占位是在逐页生成段落时 append 的，默认落在整页最后。表格在页底的
    文献看不出问题，但表格排在页首时（这篇的 Table 1 就在第 5 页顶部），
    卡片会跑到整页最下面，跟表标题分家 —— 读者看到的就是排版错乱。

    双栏页的段落 y 是「左栏从上到下、再右栏从上到下」，不是单调的，
    按 y 插会把右栏正文提前。所以只在单栏页（y 基本递增）做归位，
    其余保持原来的页末顺序，宁可不动也不打乱阅读顺序。
    """
    out: list[dict] = []
    i = 0
    n = len(paras)
    while i < n:
        j = i
        while j < n and paras[j]["page"] == paras[i]["page"]:
            j += 1
        group = paras[i:j]
        body = [p for p in group if not _is_placeholder(p)]
        marks = [p for p in group if _is_placeholder(p)]
        ys = [p.get("_y", 0) for p in body]
        monotonic = all(ys[k] <= ys[k + 1] + 3 for k in range(len(ys) - 1))
        if marks and monotonic and body:
            merged = []
            pending = sorted(marks, key=lambda p: p.get("_y", 0))
            bi = 0
            for p in body:
                while pending and pending[0].get("_y", 0) <= p.get("_y", 0) + 3:
                    merged.append(pending.pop(0))
                merged.append(p)
            merged.extend(pending)   # 表格在正文之后的，留在页末
            out.extend(merged)
        else:
            out.extend(group)
        i = j
    return out


def _merge_cross_page(paras: list[dict], uses_indent: bool) -> list[dict]:
    """
    按阅读顺序遍历，把在「栏边界 / 页边界」被切开的段落接回去。

    为什么要限制在边界处：栏内正常相邻的两段，如果无差别合并，
    会把两段本不相干的话焊在一起。所以只处理上一段恰好是某栏最后一个 group 的情况。

    合并后的这一段**显示在字数较多的那一页上**：
    它本来是从那一页的正文里被挖走了一半，放回去才不突兀。若按起始页放，
    下一页开头会凭空空掉一块，读者会以为漏了一段。
    """
    out: list[dict] = []
    for p in paras:
        prev = None
        if not _is_placeholder(p):
            # 表格/表注是插在正文中间的占位，不算「隔断」：
            # 一段话在表格前起头、表格后接着写，是很常见的排版，跨过占位才接得上。
            for cand in reversed(out):
                if not _is_placeholder(cand):
                    prev = cand
                    break

        if prev is not None and prev.get("at_column_end") and _can_join(prev, p, uses_indent) \
                and len(prev["text"]) + len(p["text"]) <= MAX_MERGED_CHARS:
            prev.setdefault("_parts", [(prev["page"], len(prev["text"]))])
            prev["_parts"].append((p["page"], len(p["text"])))
            if prev["text"].endswith("-"):
                prev["text"] = prev["text"][:-1] + p["text"]
            else:
                prev["text"] = prev["text"] + " " + p["text"]
            prev["n_lines"] += p["n_lines"]
            prev["mixed_fonts"] = prev["mixed_fonts"] or p["mixed_fonts"]
            prev["page_uncertain"] = prev.get("page_uncertain") or p.get("page_uncertain")
            prev["last_line_ratio"] = p.get("last_line_ratio", 1.0)
            # 合并后这个位置不再是边界，避免连续吞并下一段
            prev["at_column_end"] = p.get("at_column_end", False)
            continue

        p.setdefault("_parts", [(p["page"], len(p["text"]))])
        out.append(p)

    for p in out:
        parts = p.pop("_parts", [(p["page"], len(p["text"]))])
        if len(parts) < 2:
            continue
        p["page_from"] = parts[0][0]
        p["page_to"] = parts[-1][0]
        # 字数最多的一页；并列时留在起始页（读者先看到的还是那页）
        p["page"] = max(parts, key=lambda kv: (kv[1], -kv[0]))[0]
    return out


def _is_figure_legend(p: dict, figs: list, body_size: float,
                      max_gap: float = 26.0) -> bool:
    """
    这一段是不是「紧贴在某张图下面」的无编号图注。

    Science 的 RESEARCH ARTICLE SUMMARY、部分 Cell/Nature 的图形摘要，图的说明
    直接印在图下方，前面没有 "Fig. 1" —— 前缀判据全部失效，只能靠位置：
      · 字号比正文小（说明文字一律小一号）
      · 顶边在图的下沿之下、且离得很近（≤26pt，隔着正文段落就不会命中）
      · 横向落在这张图的范围内（左边界别跑出去太多）
    """
    if not figs or not p.get("_bbox"):
        return False
    if p["size"] > body_size - 0.6:
        return False
    x0, y0, x1, _y1 = p["_bbox"]
    for f in figs:
        gap = y0 - f[3]
        if -2.0 <= gap <= max_gap and x1 > f[0] - 12 and x0 < f[2] + 12:
            return True
    return False


def _absorb_caption_blocks(paras: list[dict], body_size: float) -> list[dict]:
    """
    把被「粗细/字体变化」切成两截的图注拼回一段。

    Nature 系的 Extended Data 图注排成「粗体小标题 + 常规体正文」，两者是同一段、
    同一字号（本例都是 7pt），只因第一行加粗，merge_paragraphs 就把它当小标题断开了：

        [图注] Extended Data Fig. 5 | Reoccurrence of genes and perturbations for top
        [正文] predictions. (a) Reoccurrence of genes and (b) perturbations among the …

    结果图注只剩半截（读出来是「…for top」），后半段被当正文翻译，
    字号还和正文一样大 —— 读者看到的就是「标题只有一行，注释跑到下面去了」。
    更糟的是后半段若以大写基因名开头（"HBG2 and HBZ. Comparison …"），
    连「小写开头算图注续文」这条兜底都命不中，直接变成正经正文。

    判据只看「是不是同一块小字」：同页、紧挨着、字号相同且明显小于正文、
    且不是下一张图的图注起点。正文段落字号不同，不会被卷进来。
    """
    out: list[dict] = []
    i = 0
    n = len(paras)
    while i < n:
        p = paras[i]
        i += 1
        # 起点必须是图注（"Fig. 5 | …" / "Extended Data Fig. 5 | …"），而不是正文里的引用句；
        # 表注不在此列 —— 表题后面跟的是表头/表体，不是同一段话
        if _is_placeholder(p) or not CAPTION_PAT.match(p["text"]) \
                or FIG_REF_PAT.match(p["text"]) or TABLE_CAPTION_PAT.match(p["text"]):
            out.append(p)
            continue
        size = p.get("size", 0.0)
        # 只处理「小字图注」：字号和正文一样时，这个判据分不清图注续文和下一段正文
        if not size or size > body_size - 0.5:
            out.append(p)
            continue

        merged = False
        while i < n:
            q = paras[i]
            if _is_placeholder(q) or q.get("page") != p.get("page"):
                break
            qs = q.get("size", 0.0)
            if abs(qs - size) > 0.35:
                break
            # 下一句又是个图注开头（"Fig. 6 …"）：那是另一张图，别吞
            if CAPTION_PAT.match(q["text"]) and not FIG_REF_PAT.match(q["text"]):
                break
            if TABLE_CAPTION_PAT.match(q["text"]):
                break
            if len(p["text"]) + len(q["text"]) > 1500:
                break
            p["text"] = p["text"].rstrip() + " " + q["text"].lstrip()
            p["n_lines"] = p.get("n_lines", 1) + q.get("n_lines", 1)
            p["mixed_fonts"] = bool(p.get("mixed_fonts")) or bool(q.get("mixed_fonts"))
            p["page_uncertain"] = bool(p.get("page_uncertain")) or bool(q.get("page_uncertain"))
            p["last_line_ratio"] = q.get("last_line_ratio", p.get("last_line_ratio", 1.0))
            i += 1
            merged = True
        out.append(p)
        if merged:
            # 拼好的这一段是图注，别再被当成「栏末没写完的段落」和下一段焊起来
            p["at_column_end"] = False
    return out


# ---------------- 主流程 ----------------

def parse_pdf(path: str | Path) -> DocStructure:
    """把 PDF 解析成带编号、带标签、带置信度的段落列表"""
    path = Path(path)
    doc = pymupdf.open(str(path))
    total = doc.page_count

    pages_lines: dict[int, list[dict]] = {}
    page_h: dict[int, float] = {}
    page_w: dict[int, float] = {}
    figs_by_page: dict[int, list[list[float]]] = {}
    tabs_by_page: dict[int, list[list[float]]] = {}
    # 图形密集页：一页上几百上千个矢量元素，说明整页是一个（或几个）矢量画的图。
    # 这类图 get_image_info() 看不到（不是位图），所以图里的文字会混进正文 ——
    # 网络图的基因名、散点图的坐标刻度，抽出来是「HCK」「0.5 10」「37」这种。
    dense_pages: set[int] = set()
    # 铺满整页的版式底图页（扫描层/图形摘要背景）。这种页的「图内文字」混在
    # 正文里，但页面本身不是「图」—— 套用图形密集页的剔除规则（非正文字体 + 短）
    # 把图形摘要里的标签（"01 · Representation Learning"）挑掉，标题摘要留下。
    bg_pages: set[int] = set()
    for i in range(total):
        page = doc[i]
        pages_lines[i] = extract_lines(page, i + 1)
        page_h[i] = page.rect.height
        page_w[i] = page.rect.width
        # 一次扫描 drawings：图带、表带、图形密集页共用，别把同一页扫三遍
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
        try:
            words = page.get_text("words")
        except Exception:
            words = []
        vec = _vector_bands(page, drawings, words)
        figs = page_figures(page, drawings, words, vec, pages_lines[i])
        if figs:
            figs_by_page[i + 1] = figs
        tabs = page_tables(page, drawings, vec)
        if tabs:
            tabs_by_page[i + 1] = tabs
        if len(drawings) >= DENSE_DRAWINGS:
            dense_pages.add(i + 1)
        if _has_full_page_bitmap(page, pages_lines[i]):
            bg_pages.add(i + 1)

    # 0) 目录页整页剔除：全是「章节名 …点线… 页码」，翻出来是纯噪音，
    #    而且那些点线小字会把正文字号的加权统计带偏（8pt 点线几十上百行，
    #    字符权重压过 11pt 正文），导致后面标题、段落的字号判据全部失准。
    for pno in list(pages_lines.keys()):
        if _is_toc_page(pages_lines[pno]):
            del pages_lines[pno]

    # 0.5) 定位参考文献起始页，算 body_size 时把参考文献条目小字排除。
    #    一篇 37 页的综述可能有 10 页参考文献，每页 5000+ 字符的 8.97pt 小字，
    #    字符权重远超正文，不排除的话 body_size 会被拉到 9pt 以下，全盘失准。
    ref_start = _find_ref_start_page(pages_lines)

    # 1) 先用字符数加权算正文字号。页眉剔除要参考它：页眉是「小字」，
    #    而章节大标题（如 "1 Introduction" 14.3pt）虽然也在页顶、文本还跟
    #    页眉里的章节名一模一样，但字号明显大于正文 —— 只凭文本匹配会把
    #    真标题也删掉，所以得先知道正文字号，再区分「小字页眉 / 大字标题」。
    size_weight: dict[float, int] = {}
    font_weight: dict[str, int] = {}
    for pno, lines in pages_lines.items():
        if ref_start is not None and pno >= ref_start:
            continue
        for l in lines:
            # 字号四舍五入到 0.1 再累加：extract_lines 返回的 size 是 span 平均，
            # 同一字号（如 8.2pt）会被算成 8.19 / 8.21 两个值，不归并的话
            # 正文的字号权重被稀释，图注小字反而胜出，body_size 全盘失准。
            sz = round(l["size"], 1)
            size_weight[sz] = size_weight.get(sz, 0) + len(l["text"])
            f = l.get("font") or ""
            if f:
                font_weight[f] = font_weight.get(f, 0) + len(l["text"])
    body_size = max(size_weight.items(), key=lambda kv: kv[1])[0] if size_weight else 10.0
    # 文章标题：文件名当标题太难看（"1-s2.0-S0092867413003875-main.pdf"），
    # 第一个 heading 也不对（常常是 "Methods"/"Resource"）。这里真去首页认标题。
    doc_title = extract_title(doc, body_size)

    # 1.5) 剔除页眉页脚（页眉是小字，字号不会大于正文）
    header_keys = find_running_headers(pages_lines, page_h, total)
    for pno, lines in pages_lines.items():
        pages_lines[pno] = [l for l in lines
                            if not is_running_header(l, page_h[pno], header_keys, body_size)]
    # 正文用的字体。用来认「同字号但换了字体」的小标题 ——
    # 不少期刊（本例是 Cell）的小标题字体名是 AdvPSHN-H，里面没有 "Bold" 字样，
    # 靠 flags 也认不出粗体，只能靠「和正文不是同一个字体 + 短行 + 不像句子」来判断。
    body_font = max(font_weight.items(), key=lambda kv: kv[1])[0] if font_weight else ""

    # 2.5) 剔除「不是正文」的行：表格里的字、图片上的字、首页的作者单位、页底小字
    #
    #   表格里的字如果留着，会跟上下段落焊成「30.00 (7.53) PCL-R total 27.70 (4.62)…」；
    #   图片上叠的文字（矢量图里的坐标轴、脑区标注）同理。
    #   这两块已经用截图单独展示了，文字不必也不该再混进正文。
    #   表格下方的注解单独摘出来，挂到那张表上（见 table_notes_under 的说明）。
    notes_by_table: dict[tuple[int, int], list[dict]] = {}
    for pno in range(total):
        lines = pages_lines.get(pno)
        if not lines:
            continue
        tabs = tabs_by_page.get(pno + 1, [])
        note_lines: list[dict] = []
        for ti, band in enumerate(tabs):
            got = table_notes_under(lines, band, body_size)
            if got:
                notes_by_table[(pno + 1, ti)] = got
                note_lines.extend(got)

        note_ids = {id(l) for l in note_lines}
        drop = list(tabs) + list(figs_by_page.get(pno + 1, []))
        if drop or note_ids:
            lines = [l for l in lines
                     if id(l) not in note_ids and not _inside(l["bbox"], drop)]
        if pno == 0:
            lines = _strip_front_matter(lines, body_size, page_h[pno])
        # 图形密集页：图里的字（基因名、坐标刻度、子图编号）都不是正文。
        # 靠「不是正文那套字体 + 很短」认 —— 图注动辄上百字符，不会被误伤。
        # 但章节大标题（如 "1 Introduction" 14.3pt）也是「非正文字体 + 短」，
        # 会被误删 —— 标题字号明显大于正文（图内标签不会），用字号把它留下。
        if ((pno + 1) in dense_pages or (pno + 1) in bg_pages) and body_font:
            lines = [l for l in lines
                     if not (len(l["text"]) <= 40 and l.get("font") and l["font"] != body_font
                             and l["size"] <= body_size + 0.5)]
        # 页面最底下的页码、页脚线之类
        lines = [l for l in lines if l["bbox"][1] < page_h[pno] * 0.94]
        pages_lines[pno] = lines

    # 3) 逐页：分栏 → 通栏元素前置 → 栏内合并段落
    raw_paras: list[dict] = []
    col_votes: dict[int, int] = {}
    formulas_by_page: dict[int, list[list[float]]] = {}
    for pno in range(total):
        lines = pages_lines.get(pno)
        if not lines:
            continue
        ncol, mid = detect_layout(lines, page_w[pno])
        col_votes[ncol] = col_votes.get(ncol, 0) + 1

        # 显示公式：整行独立排版的，抽出来单独按图片展示（见 _extract_formulas）。
        # 必须在分栏之前做 —— 公式里那堆上下标碎行会把分栏判断带偏。
        blocks, lines = _extract_formulas(lines, body_size, mid, ncol)
        if blocks:
            formulas_by_page[pno + 1] = blocks
            pages_lines[pno] = lines

        if ncol == 2:
            regions, uncertain = split_page_regions(lines, mid, body_size, page_h[pno])
            bucket_list = regions
        else:
            bucket_list = [(lines, True)]
            uncertain = False

        for bucket, is_body_col in bucket_list:
            if not bucket:
                continue
            groups = merge_paragraphs(bucket, body_size, body_font)
            # 这一栏的左右边界，用来判断「某段最后一行有没有排满」
            col_left = min(l["bbox"][0] for l in bucket)
            col_right = max(l["bbox"][2] for l in bucket)
            col_w = max(col_right - col_left, 1.0)
            for gi, group in enumerate(groups):
                text = join_lines(group).strip()
                if not text:
                    continue
                # 公式占位行：原地留一个 formula 段落，前端按索引取截图
                if text.startswith(FORMULA_BARRIER):
                    try:
                        fi = int(text[len(FORMULA_BARRIER):])
                    except ValueError:
                        continue
                    blk = formulas_by_page.get(pno + 1, [])
                    raw_paras.append({
                        "text": blk[fi].get("text", "") if fi < len(blk) else "",
                        "page": pno + 1,
                        "_y": group[0]["bbox"][1],
                        "size": body_size,
                        "mixed_fonts": False,
                        "n_lines": 1,
                        "first_line_indent": 0.0,
                        "last_line_ratio": 1.0,
                        "at_column_end": False,
                        "page_uncertain": uncertain,
                        "formula_index": fi,
                    })
                    continue
                last = group[-1]
                entry = {
                    "text": text,
                    "page": pno + 1,
                    "_y": group[0]["bbox"][1],
                    # 整段的版面框：判断「这一段是不是某张图的说明」要用它
                    "_bbox": (min(l["bbox"][0] for l in group),
                              group[0]["bbox"][1],
                              max(l["bbox"][2] for l in group),
                              last["bbox"][3]),
                    "size": round(sum(l["size"] for l in group) / len(group), 2),
                    "mixed_fonts": any(l["fonts"] > 2 for l in group),
                    "n_lines": len(group),
                    # 首行相对栏左边界的偏移：接近 0 表示顶格（续行），明显大于 0 表示缩进（段首）
                    "first_line_indent": round(group[0]["bbox"][0] - col_left, 2),
                    # 最后一行右端离栏右边界的比例：接近 1 说明这行排满了，段落没结束
                    "last_line_ratio": round((last["bbox"][2] - col_left) / col_w, 3),
                    # 只有正文栏的最后一段才算「边界」，通栏头部区不参与跨栏合并
                    "at_column_end": is_body_col and gi == len(groups) - 1,
                    "page_uncertain": uncertain,
                    # 这一段用的字体（按字符数取主导的），以及整段是否只用了一种字体
                    "font": max(group, key=lambda l: len(l["text"])).get("font", ""),
                    "uniform_font": len({l.get("font") for l in group}) == 1,
                }
                # 紧贴在某张图下面的小字就是那张图的说明（Science 的图形摘要没有编号）。
                # 在合并之前就标出来：`_can_join` 要靠它拦住
                # 「左栏末尾的话没写完 → 把右栏图注缝成续行」这种错接。
                entry["is_legend"] = _is_figure_legend(
                    entry, figs_by_page.get(pno + 1, []), body_size)
                raw_paras.append(entry)

        # 表格：文字已经剔掉了，这里补一条占位段落，让前端在正确的位置摆出表格截图
        for ti, band in enumerate(tabs_by_page.get(pno + 1, [])):
            raw_paras.append({
                "text": f"[表格 {ti + 1}]",
                "page": pno + 1,
                "_y": band[1],
                "size": body_size,
                "mixed_fonts": False,
                "n_lines": 1,
                "first_line_indent": 0.0,
                "last_line_ratio": 1.0,
                "at_column_end": False,
                "page_uncertain": False,
                "table_index": ti,
            })
            notes = notes_by_table.get((pno + 1, ti))
            if notes:
                raw_paras.append({
                    "text": join_lines(notes).strip(),
                    "page": pno + 1,
                    "_y": notes[0]["bbox"][1],
                    "size": round(sum(l["size"] for l in notes) / len(notes), 2),
                    "mixed_fonts": False,
                    "n_lines": len(notes),
                    "first_line_indent": 0.0,
                    "last_line_ratio": 1.0,
                    "at_column_end": False,
                    "page_uncertain": False,
                    "table_index": ti,
                    "is_table_note": True,
                })

    mode_cols = max(col_votes.items(), key=lambda kv: kv[1])[0] if col_votes else 1

    # 4) 跨页段落先接回去，否则一段话会被页边界切成两截
    #    先判断这篇文献用不用「首行缩进」分段，决定用哪套合并判据
    multi = [p for p in raw_paras if p["n_lines"] >= 2]
    uses_indent = bool(multi) and (
        sum(1 for p in multi if p["first_line_indent"] > 5) / len(multi) > 0.5
    )
    raw_paras = _merge_cross_page(raw_paras, uses_indent)

    # 合并后的段落可能被挪到后一页（字数多的那页），顺序就不再是逐页递增了。
    # 稳定排序把页序拉回来：同页内的先后完全不动，只是把「归属后一页」的段落
    # 放回它该在的位置，否则前端按页切换时会先出现第 4 页、又缩回第 3 页。
    raw_paras.sort(key=lambda p: p["page"])
    raw_paras = _place_table_markers(raw_paras)

    # 4.5) 图注被「粗体小标题 + 常规体正文」的字体切换切成两截的，拼回去。
    #      必须在打标签之前做：拆开后后半截既不以 Fig. 开头、又可能大写开头，
    #      打标签时会被判成正文（见 _absorb_caption_blocks 的说明）。
    raw_paras = _absorb_caption_blocks(raw_paras, body_size)

    # 5) 参考文献整块不翻：条目翻成中文毫无意义，还占额度。
    #    但只跳「参考文献区」——后面的附录/补充材料/致谢/数据可用性照常翻。
    raw_paras = _skip_reference_block(raw_paras)

    # 6) 打标签 + 置信度，并编号
    paragraphs: list[Para] = []
    section = "前置"
    for p in raw_paras:
        text = p["text"]

        # 表格下方的注解：跟表一起显示，但要翻译（缩写是什么意思，读者得知道）
        if p.get("is_table_note"):
            paragraphs.append(Para(
                id=len(paragraphs) + 1,
                page=p["page"],
                text=text,
                size=p["size"],
                kind="table_note",
                section=section,
                flags=[],
                table_index=p.get("table_index", 0),
            ))
            continue

        # 表格：文字已经剔出正文，这里只留一个占位，前端按 table_index 摆表格截图
        if p.get("table_index", -1) >= 0:
            paragraphs.append(Para(
                id=len(paragraphs) + 1,
                page=p["page"],
                text=text,
                size=p["size"],
                kind="table",
                section=section,
                flags=[],
                table_index=p["table_index"],
            ))
            continue

        # 显示公式：文字层抽出来是碎的（∑ 单独一行、上下标各成一行、分式丢失），
        # 所以不翻译也不当正文，前端按 formula_index 在原页上截这一块显示。
        if p.get("formula_index", -1) >= 0:
            paragraphs.append(Para(
                id=len(paragraphs) + 1,
                page=p["page"],
                text=text,
                size=p["size"],
                kind="formula",
                section=section,
                flags=[],
                formula_index=p["formula_index"],
            ))
            continue

        # 章节名、References 这类结构标记天然很短，不能被长度下限误杀。
        # 之前就是这里把 "1. Introduction" 当碎片丢了，导致整篇 section 全变成「前置」。
        # 必须加长度上限：摘要正文以 "Background:" 开头，也会命中章节正则，但它不是标题。
        # 摘要/关键词与正文挤在同一行时，先把前面那个词拆成独立小标题
        m_label = FRONT_LABEL_PAT.match(text)
        if m_label and len(text) - m_label.end() >= 20:
            label = m_label.group(1).strip()
            if label.lower() in ("abstract", "summary", "摘要"):
                section = "Abstract"
            paragraphs.append(Para(
                id=len(paragraphs) + 1,
                # 小标题留在段首所在的页：跨页段落的正文可能挪到后一页，
                # 但 "Abstract" 这个词本身印在上一页
                page=p.get("page_from", p["page"]),
                text=label[:1].upper() + label[1:].lower() if label.isascii() else label,
                size=p["size"],
                kind="heading",
                section=section,
                flags=[],
            ))
            text = text[m_label.end():].lstrip()

        structural = bool(SECTION_PAT.match(text) or REF_HEAD_PAT.match(text)) and len(text) < 90
        # 字号怎么算「标题」要分两档：
        #   · 大字号层（≥ 1.45 倍）：标题就是标题，24.9pt 的文章标题、16pt 的中文一级标题
        #   · 略大一层（1.12~1.45 倍）：**很多期刊的摘要比正文大一号**（Cell 这篇摘要 10pt、
        #     正文 8.5pt，只有 1.18 倍）。这一层如果只看字号，摘要每行都会被当标题、
        #     标题又被字号跳变切开，界面上就只剩「an」「immune-」这种孤零零的词。
        #     所以这一层还要看「像不像标题」：短、没句末标点、不成段（≤2 行）。
        size_ratio = p["size"] / body_size if body_size else 1.0
        big = size_ratio >= HEADING_RATIO_BIG
        is_heading = structural or (big and len(text) < 160) or (
            size_ratio >= HEADING_RATIO
            and not big
            and len(text) < 90
            and p["n_lines"] <= 2
            and p["last_line_ratio"] < 0.92
            and _looks_like_title(text)
        )

        # 「同字号、换字体」的小标题（Cell 的 Leveraging a Systems Approach to LOAD）：
        # 字体名是 AdvPSHN-H，既没有 Bold 字样、flags 也不带粗体位，字号和正文一模一样，
        # 于是它被当成正文的续行，跟后面整段焊成 2900 字的一坨。
        # 判据：字体和正文不同 + 很短 + 最后一行明显没排满 + 读起来不像一句话。
        if not is_heading and body_font and p.get("font") and p["font"] != body_font \
                and p.get("uniform_font") and 0.88 <= size_ratio <= 1.12 \
                and p["n_lines"] <= 2 and p["last_line_ratio"] < 0.80 \
                and p["first_line_indent"] <= 3 \
                and len(text.split()) >= 2 \
                and len(text) < 100 and _looks_like_title(text):
            is_heading = True

        # 全大写的短行（SUPPLEMENTAL INFORMATION / ACKNOWLEDGMENTS）本身就是结构标记，
        # 字体可能和正文一样，靠字体分不出来。同样要求顶格 —— 图里的 "HO"/"FP"
        # 是全大写，但它们缩在图形中间，不从栏左边界起排。
        if not is_heading and 2 <= len(text) <= 60 and p["n_lines"] == 1 \
                and p["last_line_ratio"] < 0.9 and p["first_line_indent"] <= 3 \
                and _all_caps(text):
            is_heading = True
        # 图注有时会被图截成两截，后半截不以 "Fig." 开头，只剩 "show the individual data points…"。
        # 字号比正文小、又小写开头 —— 按图注收进去，至少显示成小字灰字，不会被当成正经正文读。
        # 但太短的碎片仍要照常丢掉：那多半是断行的残渣（"online"、"version"），不是图注。
        # 多面板图的图注续文以 "(A)"/"(B)"/"(1)" 开头（面板编号），也要归进图注，
        # 否则会像正文一样被翻译、还和图分家。
        # Science 的「RESEARCH ARTICLE SUMMARY」这类版式，图的说明**不带编号**
        # （"Cat1 filament networks provide immunity against phage infection. …"），
        # 只靠 "Fig." 前缀和「小写开头」都认不出来，于是被当正文翻译、还和图分家。
        # 用位置认：小字 + 紧贴在某张图的正下方 + 横向落在那张图的范围里。
        # 合并之前已经算过一遍（`_can_join` 用它挡错接），这里优先复用；
        # 跨页合并过的段落版面框会变，所以再按合并后的位置复核一次。
        legend = bool(p.get("is_legend")) or _is_figure_legend(
            p, figs_by_page.get(p["page"], []), body_size)
        is_caption = (bool(CAPTION_PAT.match(text)) and not FIG_REF_PAT.match(text)) or (
            p["size"] <= body_size - 0.8
            and (text[:1].islower() or PANEL_NOTE_PAT.match(text))
            and len(text) >= MIN_PARA_CHARS and not FIG_REF_PAT.match(text)
        ) or legend

        if not is_heading and not is_caption and len(text) < MIN_PARA_CHARS:
            continue

        if structural:
            m = SECTION_PAT.match(text)
            if m:
                section = m.group(2).strip().title()
        elif section == "前置" and len(text) > 400 and "Background:" in text[:80] \
                and "Methods:" in text and "Results:" in text:
            # 结构化摘要（Background/Methods/Results/Conclusions 连成一段）
            section = "Abstract"

        kind = "caption" if is_caption else ("heading" if is_heading else "body")

        flags = []
        # 无编号图注：前端据此把它配到正上方那张图上（见 planFigureNotes）
        if legend:
            flags.append("fig_legend")
        # 太短又不是标题：多半是切碎的残片
        if len(text) < 45 and kind == "body":
            flags.append("short")
        # 字体混排严重：可能把两段粘在一起，或把公式与散文混在一起
        if p["mixed_fonts"] and len(text) > 400:
            flags.append("mixed_font")
        # 单行成段却很长：排版异常
        if p["n_lines"] == 1 and len(text) > 600:
            flags.append("long_single_line")
        # 通栏元素不在页首，阅读顺序可能有误
        if p.get("page_uncertain"):
            flags.append("layout_uncertain")

        paragraphs.append(Para(
            id=len(paragraphs) + 1,
            page=p["page"],
            text=text,
            size=p["size"],
            kind=kind,
            section=section,
            flags=flags,
            page_from=p.get("page_from", p["page"]),
            page_to=p.get("page_to", p["page"]),
        ))

    doc.close()

    paragraphs = _merge_drop_caps(paragraphs)

    return DocStructure(
        fingerprint=file_fingerprint(path),
        filename=path.name,
        title=doc_title,
        pages=total,
        columns=mode_cols,
        body_size=body_size,
        paragraphs=paragraphs,
        path=str(path),
        figures=figs_by_page,
        tables=tabs_by_page,
        formulas={k: [b["bbox"] for b in v] for k, v in formulas_by_page.items()},
    )


def _merge_drop_caps(paragraphs: list[Para]) -> list[Para]:
    """
    把「首字下沉」拆出来的那个单字母标题并回正文。

    Nature Communications / Cell 的正文第一段常把首字母放大成一个装饰字
    （drop cap），文字层里它自成一个「行」：字号远大于正文、只有一个字母。
    于是段落分类把它当成了小标题，界面上先出现一个孤零零的「C」，
    下面才是「alcium-independent phospholipase A2β…」，读起来像排版事故。

    判据收得很紧：≤2 个字符、被判成 heading、紧接着的正文段落以**小写**开头
    （说明那个字母本来就是那个词的首字母）。
    """
    out: list[Para] = []
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        nxt = paragraphs[i + 1] if i + 1 < len(paragraphs) else None
        head = (p.text or "").strip()
        if (nxt is not None
                and p.kind == "heading"
                and 0 < len(head) <= 2
                and head.isalpha()
                and nxt.page == p.page
                and (nxt.text or "")[:1].islower()):
            merged = Para(
                id=0, page=p.page, text=head + nxt.text, size=nxt.size,
                kind="body", section=nxt.section, flags=list(nxt.flags),
                page_from=p.page, page_to=nxt.page_to,
            )
            out.append(merged)
            i += 2
            continue
        out.append(p)
        i += 1
    for n, q in enumerate(out, start=1):
        q.id = n
    return out


def render_page(path: str | Path, page_no: int, bbox: list[float] | None = None,
                dpi: int = 130, pad: float = 3.0) -> bytes | None:
    """
    把某一页（或页内的一个区域）渲染成 PNG 字节。

    为什么要「区域」而不是只给整页：论文里的图常常只占半页，
    整页截图缩到侧边栏那么大，图里的坐标轴、分组标签全糊成一团，等于没有。

    pad 是裁切时的外扩量。表格要传小一点：表格上方往往紧挨着表标题，
    外扩 3pt 就会把标题的下半截削进图里。
    """
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return None
    try:
        if page_no < 1 or page_no > doc.page_count:
            return None
        pg = doc[page_no - 1]
        clip = None
        if bbox and len(bbox) == 4:
            clip = pymupdf.Rect(bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
        pix = pg.get_pixmap(dpi=dpi, clip=clip)
        return pix.tobytes("png")
    except Exception:
        return None
    finally:
        doc.close()


def scan_figures(path: str | Path) -> dict[int, list[list[float]]]:
    """只扫插图区域，不解析文本。给「之前翻过、但缓存里还没有图」的老文献补数据。"""
    out: dict[int, list[list[float]]] = {}
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return out
    try:
        for i in range(doc.page_count):
            figs = page_figures(doc[i])
            if figs:
                out[i + 1] = figs
    except Exception:
        pass
    finally:
        doc.close()
    return out


def scan_tables(path: str | Path) -> dict[int, list[list[float]]]:
    """只扫表格区域，同上。"""
    out: dict[int, list[list[float]]] = {}
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return out
    try:
        for i in range(doc.page_count):
            tabs = page_tables(doc[i])
            if tabs:
                out[i + 1] = tabs
    except Exception:
        pass
    finally:
        doc.close()
    return out


def render_preview(struct: DocStructure) -> str:
    """生成人可读的预览，用来验收切分质量"""
    lines = [
        f"# 抽取预览：{struct.filename}",
        "",
        f"- 页数 {struct.pages}｜版面 {'双栏' if struct.columns == 2 else '单栏'}｜正文字号 {struct.body_size}pt",
        f"- 段落 {len(struct.paragraphs)} 段｜总字符 {struct.total_chars:,}",
        f"- 可信段落 {sum(1 for p in struct.paragraphs if p.trusted)} 段，"
        f"可疑 {sum(1 for p in struct.paragraphs if not p.trusted)} 段",
        "",
        "---",
        "",
    ]
    for p in struct.paragraphs:
        meta = f"[{p.id:03d}] p{p.page} · {p.section} · {p.kind} · {len(p.text)}字"
        if p.flags:
            meta += f" · 可疑:{','.join(p.flags)}"
        lines.append(f"### {meta}")
        lines.append("")
        lines.append(p.text)
        lines.append("")
    return "\n".join(lines)
