# -*- coding: utf-8 -*-
"""
跨页段落的回归测试

一段话被页边界切成两截，是学术 PDF 里最常见、也最影响阅读的排版现象：
读到页底句子断了，翻过去得自己把两半接起来。程序必须合成一段，并且
**放在字数多的那一页**上 —— 那是这段内容真正占篇幅的地方，
放错页会让下一页开头凭空少一块。

现场生成两套 PDF，覆盖两种最常见的中英文期刊版式：

  A. 首行缩进分段（Springer / 多数中文期刊）
     · 大部分字在前一页（合并后应显示在第 1 页）
     · 大部分字在后一页（合并后应显示在第 2 页）
     · 中间隔着一张表
  B. 空行分段、不缩进（Elsevier / PLOS / Nature 系）
     · 页底句子没写完 → 必须接上
     · 页底句子已写完、下一页大写开头 → 必须**不**接（否则两段焊成一段）

  python dev/test_parse.py
"""

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf                                          # noqa: E402
from app.pdf_reader import (parse_pdf, _is_resume_heading,        # noqa: E402
                            _absorb_caption_blocks, _extract_formulas,
                            BULLET_GLYPH_PAT, CAPTION_PAT, FIG_REF_PAT,
                            _is_figure_legend, _is_margin_watermark,
                            _strip_front_matter, _can_join, extract_title,
                            _caption_tail_ok, _is_highlight_box, _panel_figures,
                            FIG_CAPTION_PAT, _merge_drop_caps, Para)

passed, failed = 0, 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [OK]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {extra}")


def write_para(page, lines, y=90, led=16, indent=12):
    """写一段：首行缩进 indent，其余顶格 —— 学术排版的标准样式"""
    for i, line in enumerate(lines):
        x = 60 + (indent if i == 0 else 0)
        page.insert_text((x, y), line, fontsize=10)
        y += led
    return y


def write_table(page, y):
    """在页面中部画一张小表（列对齐、数字密排，够 page_tables 认出来）"""
    cols = [60, 200, 320, 440]
    rows = [
        ["Group", "N", "Mean", "p"],
        ["Control", "12", "37.42", "0.32"],
        ["Case", "10", "28.90", "<0.001"],
        ["Total", "22", "33.10", "0.701"],
    ]
    for row in rows:
        for c, cell in enumerate(row):
            page.insert_text((cols[c], y), cell, fontsize=9)
        y += 18
    return y


LONG_TAIL = [
    "basal ganglia, and in the thalamus. These findings are consistent with earlier",
    "reports of reduced grey matter in subcortical structures, and they extend them",
    "by showing that the effect is present in both hemispheres and survives",
    "correction for intracranial volume, age and acquisition site. Taken together,",
    "the pattern suggests a distributed rather than a focal abnormality, which fits",
    "the network perspective on psychopathic traits that has been put forward in",
    "several recent reviews of the structural imaging literature and in at least",
    "two meta-analyses, although the effect sizes reported there were smaller than",
    "the ones we observe here, most likely because those samples were drawn from",
    "forensic populations with a higher prevalence of comorbid substance use.",
]


def make_pdf(path: pathlib.Path):
    doc = pymupdf.open()
    # ---- 第 1 页：A 段整段，B 段开头（短），C 段（整段）----
    p1 = doc.new_page(width=595, height=842)
    y = write_para(p1, ["Introduction"], y=90)
    y = write_para(p1, [
        "Psychopathy is a personality construct that represents one of the strongest",
        "predictors of aggressive and violent behaviour, and individuals with strong",
        "traits commit more crimes and cause greater injuries than other offenders.",
    ], y=y + 14)
    y = write_para(p1, [
        "Structural magnetic resonance imaging has been used in several studies in",
        "order to find interindividual differences in brain structure which are",
        "associated with psychopathy. Most of these effects were found bilaterally,",
        "and the effects in the basal ganglia were the most consistent ones across",
    ], y=y + 14)
    # 这一行排在页底，句号都没写完就往下一页跑

    # ---- 第 2 页：B 段接着写（长），D 段整段 ----
    p2 = doc.new_page(width=595, height=842)
    y = write_para(p2, LONG_TAIL, y=90, indent=0)
    y = write_para(p2, [
        "A second line of evidence comes from region of interest studies, which",
        "measured volumes of anatomically defined structures rather than voxelwise",
        "density, and which therefore do not depend on spatial normalisation.",
    ], y=y + 14)

    # ---- 第 3 页：E 段前半（长）+ 第 4 页：E 段后半（短）----
    p3 = doc.new_page(width=595, height=842)
    y = write_para(p3, LONG_TAIL[:9], y=90)
    # 后半段只有一行，落在第 4 页

    p4 = doc.new_page(width=595, height=842)
    write_para(p4, ["and acquisition site were entered as covariates in a second model."], y=90, indent=0)

    # ---- 第 5 页：F 段起头 → 中间隔一张表 → 第 6 页接着写 ----
    # 这是真实文献里最常见的一种：段落在表格上方起头，表格下面接着写。
    # 合并时不能被表格占位挡住，否则一段话永远接不上。
    p5 = doc.new_page(width=595, height=842)
    write_para(p5, [
        "The interview was conducted by a trained medical doctor or psychologist,",
        "who had been instructed in the use of the instrument by the first author",
        "and who rated the",
    ], y=90)
    write_table(p5, 330)

    p6 = doc.new_page(width=595, height=842)
    write_para(p6, [
        "extent to which each of the 20 items applied to the examined subject, using",
        "the item descriptions and scoring rules given in the manual. Inter-rater",
        "reliability was assessed in a subsample of ten subjects and was good.",
    ], y=90, indent=0)

    doc.save(str(path))
    doc.close()


def make_plain_pdf(path: pathlib.Path):
    """空行分段、不缩进的单栏版式：p1/p2 各一页，p3/p4 是一次跨页"""
    doc = pymupdf.open()

    p1 = doc.new_page(width=595, height=842)
    y = write_para(p1, [
        "Psychopathy is a personality construct that represents one of the strongest",
        "predictors of violence in forensic samples.",
    ], y=90, indent=0)
    y = write_para(p1, [
        "Several instruments have been developed for its assessment in custodial",
        "settings, and their psychometric properties differ considerably.",
    ], y=y + 22, indent=0)

    # 这一页的末段以句号收尾 —— 下一页大写开头是**另一段**，不能接
    p2 = doc.new_page(width=595, height=842)
    y = write_para(p2, [
        "Personality inventories were administered by trained research assistants",
        "who were blind to group membership. Internal consistency was acceptable",
        "for all subscales, ranging from 0.71 to 0.89 across the three samples,",
        "and item-total correlations exceeded the conventional threshold of 0.30.",
    ], y=90, indent=0)
    y = write_para(p2, [
        "Antagonism and disinhibition were assessed separately in a subsample of",
        "participants, using the abbreviated version of the inventory.",
    ], y=y + 22, indent=0)

    # p3 末段没写完（断在 "the"），p4 小写开头 → 必须接
    p3 = doc.new_page(width=595, height=842)
    y = write_para(p3, [
        "A second line of evidence comes from region of interest studies, which",
        "measured volumes of anatomically defined structures rather than voxelwise",
        "density. Most of these effects were found bilaterally, and the effects",
        "in the",
    ], y=90, indent=0)

    p4 = doc.new_page(width=595, height=842)
    write_para(p4, [
        "basal ganglia were the most consistent ones across samples. They survived",
        "correction for intracranial volume, age and acquisition site, and they",
        "were present in both hemispheres in every subsample we examined here,",
        "including the two community samples that were recruited separately and",
        "that differed from the forensic groups in age and in educational level.",
    ], y=90, indent=0)

    doc.save(str(path))
    doc.close()


def make_fragment_pdf(path: pathlib.Path):
    """
    造一页，专门复现 Cell 那篇的四个坑：

      1. 一行被拆成 5 个文本片段（两端对齐拉宽的空格，8.6pt）
         → 必须拼回一行，不能变成「an」「immune-」「and」三个孤零零的段落
      2. 摘要用 12pt、正文 10pt（比正文大一号）
         → 摘要仍算正文，不能每行都被当标题
      3. 小标题和正文同字号、只换字体（Helvetica-Bold）
         → 要单独成标题，不能和后面整段焊成一片
      4. 一整页矢量图，图里叠着短文字
         → 图里的字不算正文（"HCK""0.5 10" 这种）
    """
    doc = pymupdf.open()

    # —— 第 1 页：摘要字号偏大 ——
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((60, 80), "SUMMARY", fontsize=8.5, fontname="hebo")
    y = 105
    for line in [
        "The genetics of complex disease produce alterations in the molecular",
        "interactions of cellular pathways whose collective effect may become",
        "clear through the organized structure of molecular networks.",
    ]:
        p1.insert_text((60, y), line, fontsize=12)      # 比正文大一号
        y += 17

    # —— 第 1 页下半：一行拆成 5 个片段（模拟两端对齐）——
    frags = [("highlighting", 60), ("an", 122), ("immune-", 142),
             ("and", 191), ("microglia-specific", 217)]
    for word, x in frags:
        p1.insert_text((x, 200), word, fontsize=10)
    y = 217
    for line in [
        "module that is dominated by genes involved in pathogen phagocytosis,",
        "contains TYROBP as a key regulator, and is upregulated in LOAD.",
    ]:
        p1.insert_text((60, y), line, fontsize=10)
        y += 16

    # —— 第 1 页：同字号、换字体的小标题 ——
    p1.insert_text((60, 290), "Leveraging a Systems Approach to LOAD", fontsize=10, fontname="hebo")
    y = 307
    for line in [
        "We developed and applied an integrative network-based approach to",
        "identify modules of genes associated with neurodegenerative disease.",
    ]:
        p1.insert_text((60, y), line, fontsize=10)
        y += 16

    # —— 第 2 页：整页矢量图，图里叠着短文字 ——
    p2 = doc.new_page(width=595, height=842)
    for i in range(120):
        x = 60 + (i % 12) * 38
        yy = 80 + (i // 12) * 30
        p2.draw_circle((x, yy), 6, color=(0.6, 0.6, 0.6), width=0.5)
    for i, word in enumerate(["HCK", "FCER1G", "0.5 10", "37", "complement"]):
        p2.insert_text((70 + i * 90, 300), word, fontsize=8, fontname="hebo")
    p2.insert_text((60, 500), "Real body text on this page should survive the filter.",
                    fontsize=10)

    doc.save(str(path))
    doc.close()

def make_ref_tail_pdf(path: pathlib.Path):
    """
    造一篇「参考文献后面还有正文」的 PDF，专门复现三件事：

      1. 参考文献条目一条不翻（省额度，翻出来也没意义）
      2. 但参考文献**后面的**附录、数据可用性声明必须继续翻 ——
         Elsevier/MDPI 排附录、Nature 系把 Methods 排在参考文献后面，
         一刀切到文末等于把后半篇变成空白
      3. 以章节名开头的条目（"Methods 16, 8 (2019), 715–721. doi:…"）
         不能被当成「正文重启」的标题，否则整段参考文献又被翻译
    """
    doc = pymupdf.open()

    # —— 第 1 页：正文 ——
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((60, 90), "A Study of Things", fontsize=16)
    y = 130
    for line in [
        "We describe a study of things and of the ways in which things are made.",
        "The analysis rests on a large collection of observations gathered over",
        "several years, and on a model that relates each observation to a small",
        "number of explanatory variables. We find that the model explains most",
        "of the variation in the data, and that the residual variation is small.",
        "These results are consistent with earlier reports in the literature and",
        "suggest that the approach can be applied more broadly to other domains.",
        "In the remainder of this paper we describe the data, the model, and the",
        "inference procedure, and we close with a discussion of limitations that",
        "should be addressed in future work on this topic by other researchers.",
    ]:
        p1.insert_text((60, y), line, fontsize=10)
        y += 16
    p1.insert_text((60, y + 20), "Keywords: things; models; inference", fontsize=8)

    # —— 第 2 页：参考文献 + 附录（附录要翻译）——
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((60, 70), "References", fontsize=14)
    y = 100
    for entry in [
        "1. Smith, J. A study of things. Nature 600, 1-10 (2020).",
        "2. Doe, R. Another study of cells. Science 370, 20-30 (2021).",
        "Methods 16, 8 (2019), 715-721. doi:10.1038/s41592-019-0494-8",
        "3. Roe, K. & Poe, L. Notes on inference. Cell 180, 5-9 (2022).",
    ]:
        p2.insert_text((60, y), entry, fontsize=8)
        y += 28
    p2.insert_text((60, 250), "Appendix A", fontsize=12)
    y = 275
    for line in [
        "This appendix reports the full set of hyperparameters used in the model",
        "and the sensitivity analysis that accompanies the main results above.",
    ]:
        p2.insert_text((60, y), line, fontsize=10)
        y += 16

    # —— 第 3 页：第二段参考文献 + 数据可用性（要翻译）——
    p3 = doc.new_page(width=595, height=842)
    p3.insert_text((60, 70), "References", fontsize=14)
    p3.insert_text((60, 100), "4. Moe, T. Further notes. J. Things 12, 100-110 (2023).", fontsize=8)
    p3.insert_text((60, 128), "5. Loe, S. Closing remarks. J. Things 12, 120-130 (2023).", fontsize=8)
    p3.insert_text((60, 190), "Data availability", fontsize=12)
    p3.insert_text((60, 215), "All data are available on request from the corresponding author.",
                   fontsize=10)

    # —— 第 4 页：只有参考文献，后面没正文（应整块丢到文末）——
    p4 = doc.new_page(width=595, height=842)
    p4.insert_text((60, 70), "References", fontsize=14)
    p4.insert_text((60, 100), "6. Toe, U. A tail reference. Nature 601, 30-40 (2024).", fontsize=8)

    doc.save(str(path))
    doc.close()


def make_caption_split_pdf(path: pathlib.Path):
    """
    造一篇「图注被字体切换切成两截」的 PDF，复现 Nature Extended Data 版的排版：

        Extended Data Fig. 1 | Recurrence of genes for top        ← 粗体，7.5pt
        predictions. (a) Recurrence of genes and (b) perturbations ← 常规体，7.5pt
        HBG2 and HBZ. Comparison of the predicted expression …

    三行是同一段话。第一行加粗，merge_paragraphs 见到粗细变化就断段，
    于是图注只剩「…for top」，后面两行变成正文（第三行还大写开头，
    连「小写续文」这条兜底都命不中）。_absorb_caption_blocks 要把它拼回一段，
    但不能把后面 10pt 的正文也吞进来。
    """
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((60, 90), "A Study of Captions", fontsize=16)
    y = 120
    for line in [
        "We describe a study whose figures carry long captions in a font that is",
        "smaller than the body text. The body text is set in ten point type and it",
        "fills several lines, so that the parser can tell body from caption by size",
        "alone. Nothing in this paragraph is a caption, and none of it should be",
        "swallowed by the caption that follows further down the page.",
    ]:
        p1.insert_text((60, y), line, fontsize=10)
        y += 16

    y += 40
    p1.insert_text((60, y), "Extended Data Fig. 1 | Recurrence of genes for top",
                   fontsize=7.5, fontname="hebo")
    p1.insert_text((60, y + 10), "predictions. (a) Recurrence of genes and (b) perturbations among the",
                   fontsize=7.5)
    p1.insert_text((60, y + 20), "HBG2 and HBZ. Comparison of the predicted expression with the observed one.",
                   fontsize=7.5)

    p1.insert_text((60, y + 60), "This later paragraph is ordinary body text set in ten point type.",
                   fontsize=10)
    p1.insert_text((60, y + 76), "It must stay a paragraph of its own and must not be merged into the caption.",
                   fontsize=10)

    doc.save(str(path))
    doc.close()


def make_title_pdf(path: pathlib.Path, meta_title: str = ""):
    """
    造一个「首页有刊头、有标题、有作者」的 PDF，用来测文章标题识别。

    标题字号最大、刊头比标题小、作者行比正文还小且带 · 分隔和上标数字 ——
    真实首页就是这三层，取错一层，页头就会显示 "nature methods" 或作者名单。
    """
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((40, 42), "journal of reproducible things", fontsize=18)
    p1.insert_text((40, 92), "ORIGINAL PAPER", fontsize=9)
    p1.insert_text((40, 120), "Deep-learning models do not yet beat", fontsize=24,
                   fontname="hebo")
    p1.insert_text((40, 150), "simple linear baselines", fontsize=24, fontname="hebo")
    p1.insert_text((40, 185), "Constantin Ahlmann-Eltze1 \u00b7 Wolfgang Huber2", fontsize=9)
    p1.insert_text((40, 197), "1Institute of Things, Somewhere, Germany", fontsize=8)
    y = 230
    for line in [
        "We benchmark six deep-learning models against simple linear baselines on",
        "five perturbation datasets and find that none of the deep models is better.",
        "The result holds across datasets, splits and evaluation metrics.",
    ]:
        p1.insert_text((40, y), line, fontsize=10)
        y += 14
    if meta_title:
        doc.set_metadata({"title": meta_title})
    doc.save(str(path))
    doc.close()


def make_abstract_right_pdf(path: pathlib.Path):
    """
    造一页 Nature Brief Communication 式的首页：**摘要排在右栏顶部**，
    左栏那个位置放的是作者、收稿日期、"Check for updates" 这类小字。

    旧逻辑拿「右栏第一行」当头部区的下界，这一页正好是摘要第一行，
    于是摘要被当成右栏内容 —— 双栏是「左栏读完再读右栏」，
    摘要就跑到整页正文之后去了。左栏正文起点才是正确的界线。
    """
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((40, 42), "journal of methods", fontsize=18)
    p1.insert_text((40, 92), "Brief Communication", fontsize=10)
    p1.insert_text((40, 110), "Deep models do not beat", fontsize=24, fontname="hebo")
    p1.insert_text((40, 140), "linear baselines", fontsize=24, fontname="hebo")
    # 左栏顶部的小字（作者/日期/按钮）
    p1.insert_text((40, 200), "Anna Example1 \u00b7 Bo Tester2", fontsize=9)
    p1.insert_text((40, 216), "Received: 11 October 2024", fontsize=8.5)
    p1.insert_text((40, 232), "Accepted: 24 June 2025", fontsize=8.5)
    p1.insert_text((40, 248), "Check for updates", fontsize=8.5)
    # 右栏顶部的摘要（这是本次要修的重点）
    for i, line in enumerate([
        "Recent work on foundation models promises to learn representations of",
        "single-cell data that enable prediction of perturbation effects. Here",
        "we compared five models against deliberately simple baselines and",
        "found that none of them outperformed the baselines.",
    ]):
        p1.insert_text((217, 280 + i * 13), line, fontsize=10)

    # 两栏正文，从 y=400 开始（摘要下方）
    left = [
        "The success of large models has spawned efforts to apply the",
        "foundation model concept to biology. Several single-cell models",
        "trained on transcriptomics data have been published.",
    ]
    right = [
        "All models had a prediction error higher than the additive",
        "baseline. We also examined other summary statistics, such as",
        "the Pearson delta measure.",
    ]
    for i, line in enumerate(left):
        p1.insert_text((40, 400 + i * 12), line, fontsize=8.2)
    for i, line in enumerate(right):
        p1.insert_text((310, 400 + i * 12), line, fontsize=8.2)

    doc.save(str(path))
    doc.close()


def make_half_figure_pdf(path: pathlib.Path):
    """
    造一页「图只占右栏」的双栏页：左栏正文一直排到底，右栏上半是正文、
    下半是一张图（两个色块当面板），图注在右栏底部。

    旧逻辑会把整栏正文并进图区（x0 落到左栏），正文被当图内文字剔掉。
    """
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    y = 60
    for i in range(60):
        p1.insert_text((44, y), f"Left column body line number {i} keeps going", fontsize=9)
        y += 11
    y = 60
    for i in range(12):
        p1.insert_text((304, y), f"Right column body text line {i}", fontsize=9)
        y += 11
    p1.draw_rect(pymupdf.Rect(310, 240, 450, 360), color=None, fill=(0.2, 0.4, 0.8))
    p1.draw_rect(pymupdf.Rect(310, 380, 450, 500), color=None, fill=(0.8, 0.4, 0.2))
    p1.insert_text((310, 520), "Fig. 3 A half page figure. a First panel of the", fontsize=8)
    p1.insert_text((310, 529), "figure, b second panel of the figure.", fontsize=8)
    doc.save(str(path))
    doc.close()


def make_bullet_pdf(path: pathlib.Path):
    """
    造一篇「项目符号列表」的 PDF，复现用户的截图：

        • For scGPT, we used the same parameters and code as in their
          tutorial for perturbation prediction.
        • For CPA, we used the code from their tutorial on how to predict
          combinatorial CRISPR perturbations on the Norman dataset.

    bullet 用悬挂缩进（bullet 顶格、续行缩进），旧逻辑只看「向右缩进 = 断段」，
    于是每条 bullet 都焊到上一条的最后一行，翻出来是
    「tutorial for perturbation prediction. • For CPA, we used…」。
    """
    doc = pymupdf.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((60, 90), "Software versions and parameters", fontsize=12)
    y = 120
    for line in [
        "We ran the tools listed below with their default parameters unless noted.",
        "All scripts are available from the repository described in the methods.",
        "The following notes record the exact calls we used for each model.",
        "Nothing in this paragraph is a list item, so it must stay one paragraph.",
    ]:
        p1.insert_text((60, y), line, fontsize=10)
        y += 16

    y += 16
    for bullet, cont in [
        ("\u2022 For scGPT, we used the same parameters and code as in their",
         "tutorial for perturbation prediction."),
        ("\u2022 For CPA, we used the code from their tutorial on how to predict",
         "combinatorial CRISPR perturbations on the Norman dataset."),
        ("\u2022 For Geneformer, we fine-tuned the provided model by predict-",
         "ing the perturbation labels of the training data."),
    ]:
        p1.insert_text((60, y), bullet, fontsize=10)
        p1.insert_text((78, y + 16), cont, fontsize=10)
        y += 32

    doc.save(str(path))
    doc.close()


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="lt_parse_"))
    pdf = tmp / "cross_page.pdf"
    make_pdf(pdf)

    struct = parse_pdf(pdf)
    print("  段落：")
    for p in struct.paragraphs:
        mark = f"  ← 跨 {p.page_from}–{p.page_to}" if p.page_to > p.page_from else ""
        print(f"    [{p.id:2d}] p{p.page} {p.kind:8s} {len(p.text):4d}字 {p.text[:46]!r}{mark}")

    # B 段：p1 起头、p2 续写，且 p2 上的字更多
    joined = [p for p in struct.paragraphs if "Most of these effects" in p.text]
    check("跨页的两截合成了一段", len(joined) == 1, f"匹配到 {len(joined)} 段")
    if joined:
        p = joined[0]
        check("合并后内容完整（两页的字都在）",
              "Most of these effects" in p.text and "comorbid substance use" in p.text,
              p.text[:60])
        check("记下了起始页 1", p.page_from == 1, str(p.page_from))
        check("记下了结束页 2", p.page_to == 2, str(p.page_to))
        check("显示在字数多的第 2 页", p.page == 2, f"page={p.page}")
        check("接缝处没有丢字或重复",
              "the most consistent ones across basal ganglia" in p.text,
              repr(p.text[p.text.find("consistent ones") - 10:p.text.find("consistent ones") + 40]))
        check("这段要参与翻译", p.trusted, p.kind)

    # E 段：前半在第 3 页（字多），后半在第 4 页（一行），应显示在第 3 页
    tail_only = [p for p in struct.paragraphs if "entered as covariates" in p.text]
    check("第二个跨页段也合上了", len(tail_only) == 1, f"匹配到 {len(tail_only)} 段")
    if tail_only:
        p = tail_only[0]
        check("字多的在前一页时，留在第 3 页", p.page == 3, f"page={p.page}")
        check("同样记下了 3–4 页", (p.page_from, p.page_to) == (3, 4), f"{p.page_from}-{p.page_to}")

    # 页序必须单调，否则前端按页切换时会先出现第 2 页又缩回第 1 页
    pages = [p.page for p in struct.paragraphs]
    check("页序单调递增", pages == sorted(pages), str(pages))

    # D 段不该被误并进来（它在新的一段开头，有缩进）
    check("新段落没被误并", any(p.text.startswith("A second line of evidence") for p in struct.paragraphs))

    # 中间隔着一整张表，也要接得上
    across_table = [p for p in struct.paragraphs if "who rated the" in p.text]
    check("隔着一张表也能合上", len(across_table) == 1, f"匹配到 {len(across_table)} 段")
    if across_table:
        p = across_table[0]
        check("表格前后两截都在这段里",
              "who rated the" in p.text and "item descriptions and scoring rules" in p.text,
              p.text[:60])
        check("隔着表格合并也算跨页", (p.page_from, p.page_to) == (5, 6), f"{p.page_from}-{p.page_to}")
        check("显示在字多的第 6 页", p.page == 6, f"page={p.page}")
    check("表格本身仍单独成段",
          any(p.kind == "table" and p.page == 5 for p in struct.paragraphs))

    # ---------- B. 空行分段（不缩进）版式 ----------
    print("\n空行分段、不缩进的版式：")
    plain = tmp / "plain_style.pdf"
    make_plain_pdf(plain)
    s2 = parse_pdf(plain)
    for p in s2.paragraphs:
        mark = f"  ← 跨 {p.page_from}–{p.page_to}" if p.page_to > p.page_from else ""
        print(f"    [{p.id:2d}] p{p.page} {len(p.text):4d}字 {p.text[:48]!r}{mark}")

    check("空行确实切出了多段（没把整页焊成一段）",
          len(s2.paragraphs) >= 3, f"只切出 {len(s2.paragraphs)} 段")
    # 页底句号收尾 + 下一页大写开头：这是两段，不能接
    first_p2 = [p for p in s2.paragraphs if "Personality inventories were" in p.text]
    check("句号收尾 + 大写开头不合并", len(first_p2) == 1, f"匹配到 {len(first_p2)} 段")
    if first_p2:
        check("下一页的新段落没被焊上来", "Antagonism" not in first_p2[0].text,
              first_p2[0].text[-60:])
        check("上一段完整收尾（句号还在）", first_p2[0].text.rstrip().endswith("0.30."),
              first_p2[0].text[-30:])
    # 页底没写完：必须接
    cont = [p for p in s2.paragraphs if "A second line of evidence" in p.text]
    check("没写完的跨页段接上了", len(cont) == 1, f"匹配到 {len(cont)} 段")
    if cont:
        check("接上了下一页的内容", "basal ganglia" in cont[0].text and "the effects in the basal" in cont[0].text,
              cont[0].text[-60:])
        check("记下了 3–4 页", (cont[0].page_from, cont[0].page_to) == (3, 4),
              f"{cont[0].page_from}-{cont[0].page_to}")
        check("显示在字多的第 4 页", cont[0].page == 4, f"page={cont[0].page}")


    # ---------- C. 同一行被拆成片段 / 摘要字号偏大 / 换字体小标题 / 图内文字 ----------
    print("\n碎片、摘要字号、换字体小标题、图内文字：")
    frag_pdf = tmp / "fragments.pdf"
    make_fragment_pdf(frag_pdf)
    s3 = parse_pdf(frag_pdf)
    for p in s3.paragraphs:
        print(f"    [{p.id:2d}] p{p.page} {p.kind:8s} {len(p.text):4d}字 {p.text[:52]!r}")

    texts = [p.text for p in s3.paragraphs]
    joined = " ".join(texts)
    check("拆成 5 截的那一行拼回了整句",
          "highlighting an immune- and microglia-specific" in joined,
          joined[:160])
    check("没有「an」「immune-」「and」这种孤立段落",
          not [t for t in texts if t.strip() in ("an", "immune-", "and")],
          str([t for t in texts if len(t) < 12]))
    # 摘要那句必须还在，而且不是标题
    abs_para = [p for p in s3.paragraphs if "The genetics of complex disease" in p.text]
    check("摘要拼成了一整段", len(abs_para) == 1, f"匹配到 {len(abs_para)} 段")
    if abs_para:
        check("摘要没被当成标题", abs_para[0].kind == "body", abs_para[0].kind)
        check("摘要包含后半句", "organized structure of molecular networks" in abs_para[0].text,
              abs_para[0].text[-60:])
    # 换字体的小标题：单独一段，且没把正文焊上来
    sub = [p for p in s3.paragraphs if p.text.startswith("Leveraging a Systems")]
    check("换字体的小标题单独成段", len(sub) == 1, f"匹配到 {len(sub)} 段")
    if sub:
        check("小标题被判成标题", sub[0].kind == "heading", sub[0].kind)
        check("小标题没把正文焊进来", "We developed" not in sub[0].text, sub[0].text[-50:])
    # 图内文字不应出现
    leak = [t for t in texts if t.strip() in ("HCK", "FCER1G", "0.5 10", "complement")]
    check("矢量图里的字被剔除了", not leak, str(leak))
    check("同页的正常正文还在",
          any("Real body text on this page" in p.text and p.page == 2 for p in s3.paragraphs),
          str([p.text[:30] for p in s3.paragraphs if p.page == 2]))

    # —— D 段：参考文献之后的正文要继续翻译 ——
    ref_pdf = tmp / "ref_tail.pdf"
    make_ref_tail_pdf(ref_pdf)
    s4 = parse_pdf(ref_pdf)
    texts4 = [p.text for p in s4.paragraphs]
    joined4 = "\n".join(texts4)

    check("参考文献条目没被翻译", "A study of things. Nature" not in joined4
          and "Another study of cells" not in joined4
          and "Notes on inference" not in joined4,
          str([t[:30] for t in texts4 if "Nature" in t or "Science" in t]))
    check("以 Methods 开头的条目也没漏进来",
          "10.1038/s41592-019-0494-8" not in joined4,
          str([t[:50] for t in texts4 if "10.1038" in t]))
    check("参考文献后面的附录继续翻译",
          any("This appendix reports the full set" in t for t in texts4),
          str([t[:40] for t in texts4]))
    check("第二段参考文献后面的数据可用性继续翻译",
          any("available on request from the corresponding author" in t for t in texts4),
          str([t[:40] for t in texts4]))
    check("第二段参考文献的条目也没翻",
          "Further notes. J. Things" not in joined4 and "Closing remarks" not in joined4,
          str([t[:40] for t in texts4 if "Things" in t]))
    check("文末只有参考文献时整块丢掉",
          "A tail reference" not in joined4,
          str([t[:40] for t in texts4 if "tail" in t]))
    check("正文没被误伤", any("We describe a study of things" in t for t in texts4),
          str([t[:30] for t in texts4]))

    # 判据本身也要测：条目不能被当成「正文重启」的标题
    check("'Methods 16, 8 (2019)…' 不算章节标题",
          not _is_resume_heading("Methods 16, 8 (2019), 715-721. doi:10.1038/s41592-019-0494-8"))
    check("'Appendix A' 算章节标题", _is_resume_heading("Appendix A"))
    check("'Data availability' 算章节标题", _is_resume_heading("Data availability"))
    check("'SUPPLEMENTARY INFORMATION' 算章节标题", _is_resume_heading("SUPPLEMENTARY INFORMATION"))
    check("'Supplementary Fig. 3' 这类图注不算章节标题",
          not _is_resume_heading("Supplementary Fig. 3 | Additional analyses of the data."))

    # 图注/表注的编号写法要认全，否则补充材料的图注会被当正文、还挂不到图上
    check("'Extended Data Fig. 1 | …' 算图注",
          bool(CAPTION_PAT.match("Extended Data Fig. 1 | Dataset Overview."))
          and not FIG_REF_PAT.match("Extended Data Fig. 1 | Dataset Overview."))
    check("'Supplementary Table 2 …' 算表注",
          bool(CAPTION_PAT.match("Supplementary Table 2 Baseline characteristics")))
    check("'Fig. S1 | …' 算图注", bool(CAPTION_PAT.match("Fig. S1 | Additional analyses")))
    check("'Figure S5 (genes marked in red) highlights…' 是引用句，不算图注",
          bool(FIG_REF_PAT.match("Figure S5 (genes marked in red) highlights many")))
    check("'Figure 1 shows that…' 是引用句，不算图注",
          bool(FIG_REF_PAT.match("Figure 1 shows that the model fits")))
    check("'Fig. 3 Associations of PCL-R factor 2' 仍是图注",
          bool(CAPTION_PAT.match("Fig. 3 Associations of PCL-R factor 2"))
          and not FIG_REF_PAT.match("Fig. 3 Associations of PCL-R factor 2"))

    # —— E 段：图注被「粗体小标题 + 常规体正文」切成两截 ——
    cap_pdf = tmp / "caption_split.pdf"
    make_caption_split_pdf(cap_pdf)
    s5 = parse_pdf(cap_pdf)
    caps = [p for p in s5.paragraphs if CAPTION_PAT.match(p.text)]
    check("整段图注只产出一段（没被粗细变化切碎）", len(caps) == 1,
          str([(p.kind, p.text[:40]) for p in caps]))
    if caps:
        c = caps[0]
        check("图注后半截回到了图注里", "predictions. (a) Recurrence of genes" in c.text, c.text[-80:])
        check("大写开头的续文（HBG2 and HBZ.）也在图注里",
              "HBG2 and HBZ. Comparison" in c.text, c.text[-80:])
        check("整段图注判为 caption", c.kind == "caption", c.kind)
    check("图注后面的正文没被吞进来",
          not any("ordinary body text" in p.text for p in caps),
          str([p.text[:40] for p in caps]))
    check("图注后面的正文仍是独立段落",
          any("This later paragraph is ordinary body text" in p.text
              and p.kind == "body" for p in s5.paragraphs),
          str([(p.kind, p.text[:40]) for p in s5.paragraphs]))

    # 判据本身：字号和正文一样的图注不合并（分不清续文和下一段正文），
    # 也不许把下一张图的图注吞掉
    def fake_para(text, page=1, size=7.0):
        return {"text": text, "page": page, "size": size, "n_lines": 1,
                "mixed_fonts": False, "page_uncertain": False,
                "first_line_indent": 0.0, "last_line_ratio": 1.0,
                "at_column_end": False}

    same_size = _absorb_caption_blocks(
        [fake_para("Fig. 1 | A caption at body size"), fake_para("continues here.", size=10.0)],
        body_size=10.0)
    check("字号等于正文的图注不合并", len(same_size) == 2, str([p["text"] for p in same_size]))

    two_caps = _absorb_caption_blocks(
        [fake_para("Fig. 1 | First caption"), fake_para("Fig. 2 | Second caption")],
        body_size=10.0)
    check("两张图的图注不会焊成一段", len(two_caps) == 2, str([p["text"] for p in two_caps]))

    body_after = _absorb_caption_blocks(
        [fake_para("Fig. 1 | Short caption."), fake_para("regular body line", size=9.0)],
        body_size=10.0)
    check("字号回到正文就断开（10pt 正文不被吞）", len(body_after) == 2,
          str([(p["text"][:20], p["size"]) for p in body_after]))

    table_cap = _absorb_caption_blocks(
        [fake_para("Table 1. Top 20 Modules in PFC Ranked for Relevance"),
         fake_para("Top Functional and Cellular Category")],
        body_size=10.0)
    check("表注不吞表头小字（表题后面是表体，不是同一段话）", len(table_cap) == 2,
          str([p["text"][:30] for p in table_cap]))

    # —— F 段：项目符号列表 ——
    bullet_pdf = tmp / "bullets.pdf"
    make_bullet_pdf(bullet_pdf)
    s6 = parse_pdf(bullet_pdf)
    # 生成 PDF 时 Helvetica 把 • 抽成 ·（U+00B7），两种都算项目符号
    bl = [p for p in s6.paragraphs if BULLET_GLYPH_PAT.match(p.text)]
    check("三条 bullet 各成一段（没焊成一条）", len(bl) == 3,
          str([p.text[:40] for p in bl]))
    if len(bl) == 3:
        check("bullet 的续行跟着自己那条", "tutorial for perturbation prediction." in bl[0].text,
              bl[0].text[-60:])
        check("第二条 bullet 内容完整",
              "combinatorial CRISPR perturbations on the Norman dataset." in bl[1].text,
              bl[1].text[-60:])
        check("被连字符断开的词接回去了", "predicting the perturbation labels" in bl[2].text,
              bl[2].text)
    check("列表前面的正文没被拆开",
          any(p.text.startswith("Nothing in this paragraph is a list item") is False
              and "Nothing in this paragraph is a list item" in p.text for p in s6.paragraphs),
          str([p.text[:40] for p in s6.paragraphs]))

    # —— G 段：显示公式单独切块（判据是纯函数，直接喂行数据更准） ——
    def L(text, x0, y0, x1, y1, size=8.0, font="HardingText-Regular"):
        return {"text": text, "bbox": (x0, y0, x1, y1), "size": size,
                "font": font, "fonts": 1, "bold": False, "page": 1}

    page_lines = [
        L("interactions, is", 39.7, 375.9, 240.0, 386.0),
        L("FDPl = \u2211", 110.0, 396.5, 145.0, 417.0, 7.5, "HardingText-Regular2"),
        L("i=11(abs (Y \u2212\u0302 Yadd)ji < u)", 143.7, 396.0, 283.0, 411.7, 7.5,
          "HardingText-Regular2"),
        L("(4)", 283.6, 404.2, 296.0, 415.4, 7.5, "HardingText-Regular2"),
        L("and the TPR is", 39.7, 423.9, 130.0, 434.0),
        # 行内公式：贴着栏左、占满整行，还在句子里 —— 绝不能当显示公式切走
        L("to the row means of the training data (b = 1/N\u2211iYtrain", 306.1, 466.6,
          556.0, 480.2),
    ]
    blocks, kept = _extract_formulas(page_lines, body_size=8.2, mid=297.6, ncol=2)
    check("显示公式被切出来了", len(blocks) == 1, str([b["bbox"] for b in blocks]))
    if blocks:
        b = blocks[0]
        check("公式块把编号 (4) 也框进去了", b["bbox"][2] >= 296.0, str(b["bbox"]))
        check("公式块不与正文行混在一起", b["bbox"][1] >= 390 and b["bbox"][3] <= 420,
              str(b["bbox"]))
    check("行内公式没被切走",
          any("1/N\u2211iYtrain" in l["text"] for l in kept),
          str([l["text"][:30] for l in kept]))
    check("公式行换成了占位行（正文段落在这里断开）",
          any(l["text"].startswith("\x00F") for l in kept), str(len(kept)))

    # —— H 段：文章标题 ——
    title_pdf = tmp / "title.pdf"
    make_title_pdf(title_pdf)
    d = pymupdf.open(title_pdf)
    got = extract_title(d, 10.0)
    d.close()
    check("标题取的是字号最大的那两行", got.startswith("Deep-learning models do not yet beat"), got)
    check("标题把第二行也带上了", "simple linear baselines" in got, got)
    check("刊头没被当成标题", "journal of reproducible things" not in got, got)
    check("作者行没被当副标题并进来", "Ahlmann" not in got and "Huber" not in got, got)

    # 元数据优先：arXiv/Nature 的 PDF 元数据里标题是准的
    meta_pdf = tmp / "title_meta.pdf"
    make_title_pdf(meta_pdf, meta_title="A Better Title From Metadata")
    d = pymupdf.open(meta_pdf)
    got2 = extract_title(d, 10.0)
    d.close()
    check("元数据里的标题优先", got2 == "A Better Title From Metadata", got2)

    # 但元数据是「文件名」时不能信
    junk_pdf = tmp / "title_junk.pdf"
    make_title_pdf(junk_pdf, meta_title="Microsoft Word - draft-v3.doc")
    d = pymupdf.open(junk_pdf)
    got3 = extract_title(d, 10.0)
    d.close()
    check("元数据是文件名时退回版面识别",
          got3.startswith("Deep-learning models do not yet beat"), got3)

    # 连字要还原：PDF 里 "Identiﬁes" 抽出来是 U+FB01，显示成怪字符
    from app.pdf_reader import _clean_title
    check("连字还原成普通字母", _clean_title("Identi\ufb01es Nodes") == "Identifies Nodes",
          _clean_title("Identi\ufb01es Nodes"))

    # —— I 段：摘要排在右栏顶部的首页，顺序不能乱 ——
    abs_pdf = tmp / "abstract_right.pdf"
    make_abstract_right_pdf(abs_pdf)
    s7 = parse_pdf(abs_pdf)
    texts7 = [p.text for p in s7.paragraphs]
    idx_abs = next((i for i, t in enumerate(texts7) if t.startswith("Recent work on foundation")), -1)
    idx_body = next((i for i, t in enumerate(texts7) if t.startswith("The success of large models")), -1)
    check("摘要被认出来了", idx_abs >= 0, str(texts7[:4]))
    check("摘要排在正文之前", 0 <= idx_abs < idx_body if idx_body >= 0 else False,
          f"abstract={idx_abs} body={idx_body}")
    if idx_abs >= 0:
        check("摘要是一整段（没被日期行切成两半）",
              "none of them outperformed the baselines." in texts7[idx_abs],
              texts7[idx_abs][-60:])
    joined7 = "\n".join(texts7)
    check("首页的作者/日期/按钮小字没当正文翻",
          "Received: 11 October 2024" not in joined7
          and "Accepted: 24 June 2025" not in joined7
          and "Check for updates" not in joined7
          and "Anna Example" not in joined7,
          str([t[:30] for t in texts7 if "Received" in t or "Anna" in t]))

    # —— J 段：无编号图注（Science 的 RESEARCH ARTICLE SUMMARY） ——
    figs = [[302.5, 180.6, 557.5, 320.3]]

    def P(text, bbox, size=7.5):
        return {"text": text, "bbox": bbox, "_bbox": list(bbox), "size": size}

    check("紧贴图下沿的小字算图注",
          _is_figure_legend(P("Cat1 filament networks provide immunity", (303, 329.5, 548, 338.2)),
                            figs, 8.5))
    check("隔了一段正文就不算", not _is_figure_legend(
        P("ordinary body text", (303, 420, 548, 430)), figs, 8.5))
    check("字号和正文一样的不算图注", not _is_figure_legend(
        P("Cat1 filament networks provide immunity", (303, 329.5, 548, 338.2), 8.3), figs, 8.5))
    check("横向不在这张图范围内的不算", not _is_figure_legend(
        P("text somewhere else", (40, 329.5, 280, 338.2)), figs, 8.5))
    check("这张图正上方（y 更小）的正文不算", not _is_figure_legend(
        P("text above the figure", (303, 150, 548, 160)), figs, 8.5))

    # —— K 段：页边水印 / 脚注按栏切分 / 图注不被缝进正文 ——
    W = "Downloaded from https://www.science.org at China Pharmaceutical University on October 17, 2026"

    def R_(text, bbox, size=7.5, d=(1.0, 0.0)):
        return {"text": text, "bbox": bbox, "size": size, "dir": d,
                "font": "Arial", "fonts": 1, "bold": False, "page": 1}

    check("侧边竖排的下载水印被认出",
          _is_margin_watermark(R_(W, (563.8, 217.0, 574.4, 539.0), 7.5, (0.0, 1.0)),
                               W, 594.0, 756.0))
    check("横排的同一句话也算水印（Wiley 印在页脚）",
          _is_margin_watermark(R_(W, (40.0, 300.0, 550.0, 310.0)), W, 594.0, 756.0))
    check("竖排的坐标轴标签不能当水印",
          not _is_margin_watermark(R_("Prediction error (L2)", (45.0, 109.0, 55.0, 168.0),
                                      6.0, (0.0, -1.0)),
                                   "Prediction error (L2)", 595.0, 791.0))
    check("正文横排文字不是水印",
          not _is_margin_watermark(R_("a body line of ordinary text " * 3,
                                      (37.0, 500.0, 290.0, 510.0), 8.5),
                                   "a body line of ordinary text", 594.0, 756.0))

    # 首页脚注只占右栏：左栏同时还在往下写的正文不能被切掉
    front = [
        R_("A Very Long Title " * 3, (40.0, 60.0, 550.0, 100.0), 22.0),
        R_("left column body before the footnote", (37.0, 640.0, 285.0, 652.0), 8.5),
        R_("*Corresponding author. Email: a@b.org (P.Q.)", (303.0, 655.0, 551.0, 663.0), 7.0),
        R_("left column body after the footnote started", (37.0, 665.0, 283.0, 677.0), 8.5),
        R_("pateld@mskcc.org (D.J.P.)", (303.0, 668.0, 537.0, 676.0), 7.0),
    ]
    kept_text = [l["text"] for l in _strip_front_matter(front, 8.5, 756.0)]
    check("右栏脚注被清掉", not any("Corresponding author" in t or "mskcc" in t for t in kept_text),
          str(kept_text))
    check("左栏正文尾巴保住（脚注只切自己那一栏）",
          any(t.startswith("left column body after") for t in kept_text), str(kept_text))

    # 图注（无编号）不能被当成上一栏正文的续行缝进来
    body_p = {"text": "…formed by trigonal and pentagonal", "size": 8.5,
              "first_line_indent": 0.0, "last_line_ratio": 0.95}
    legend_p = {"text": "Cat1 filament networks provide immunity against phage", "size": 7.5,
                "first_line_indent": 99.0, "is_legend": True}
    check("字号不同的图注不缝进正文", not _can_join(body_p, legend_p, uses_indent=False))
    same = dict(legend_p, size=8.5)
    check("同字号的图注续块照常拼（同一张图排成几块）",
          _can_join(body_p, same, uses_indent=False))

    # —— L 段：半页宽的图不能把另一栏正文并进来（iPLA2β 第 3 页） ——
    def BL(text, x0, y0, x1, y1, size=9.5):
        return {"text": text, "bbox": (x0, y0, x1, y1), "size": size,
                "font": "Body", "fonts": 1, "bold": False, "page": 3}

    check("图注尾句要有真正的词（Fig. 1b. 不算图注）",
          not _caption_tail_ok("Fig. 1b."), "Fig. 1b.")
    check("正常图注通过", _caption_tail_ok("Fig. 2 Configuration of the dimer."))
    check("蛋白名小写开头不算「引用句」（Fig. 4 iPLA2β … 是真图注）",
          not FIG_REF_PAT.match("Fig. 4 iPLA2β dimerization and mechanism of CaM inhibition."))
    check("面板号 + 右括号是引用句", bool(FIG_REF_PAT.match(
        "Figure 4b). The mutant is a monomer in solution")))
    check("小写动词仍是引用句", bool(FIG_REF_PAT.match("Figure 1 connects state representation")))

    # 正文里的高亮块（荧光笔）不能当图元素
    hl = {"type": "f", "items": [(("l", (53.0, 614.5), (291.7, 625.6)))],
          "color": None, "rect": pymupdf.Rect(53.0, 614.5, 291.7, 625.6)}
    hl_lines = [BL("a highlighted body line", 53, 615, 291, 625)]
    check("盖住正文行的高亮块不算图元素", _is_highlight_box(hl, hl_lines))
    bar = {"type": "f", "items": [(("re",),)], "color": None,
           "rect": pymupdf.Rect(60.0, 300.0, 66.0, 340.0)}
    check("图里的实心色块保留", not _is_highlight_box(bar, hl_lines))

    # —— M 段：半页宽的图不能吞掉另一栏正文（用真实合成页跑一遍） ——
    half_pdf = tmp / "half_fig.pdf"
    make_half_figure_pdf(half_pdf)
    s_half = parse_pdf(half_pdf)
    boxes = s_half.figures.get(1) or []
    check("半页宽的图被认出来了", len(boxes) == 1, str(boxes))
    if boxes:
        check("图区没有伸进左栏（x0 ≥ 右栏起点）", boxes[0][0] >= 300, str(boxes[0]))
        check("图区下界压在图注上沿", boxes[0][3] <= 520, str(boxes[0]))
    left_body = [p2 for p2 in s_half.paragraphs
                 if p2.text.startswith("Left column body line number")]
    check("左栏正文没被当图内文字剔掉", len(left_body) >= 1, str(len(left_body)))
    if left_body:
        check("左栏正文是完整的（含最后一行）",
              "number 59" in left_body[0].text or "number 59" in " ".join(
                  x.text for x in left_body),
              left_body[0].text[-60:])
    right_body = [p2 for p2 in s_half.paragraphs if p2.text.startswith("Right column body text")]
    check("右栏正文也留着", len(right_body) >= 1, str(len(right_body)))

    # —— N 段：首字下沉不该变成一个小标题 ——
    drop = [
        Para(id=1, page=2, text="C", size=32.0, kind="heading", section="前置", flags=[]),
        Para(id=2, page=2, text="alcium-independent phospholipase A2b regulates", size=9.5,
             kind="body", section="前置", flags=[]),
        Para(id=3, page=2, text="Another paragraph stays as it is.", size=9.5,
             kind="body", section="前置", flags=[]),
    ]
    fixed = _merge_drop_caps(drop)
    check("首字下沉并回正文", len(fixed) == 2, str([(x.id, x.kind, x.text[:20]) for x in fixed]))
    if fixed:
        check("合并后第一段以大写首字母开头、是正文",
              fixed[0].kind == "body" and fixed[0].text.startswith("Calcium-independent"),
              fixed[0].text[:40])
        check("id 重新编号且连续", [x.id for x in fixed] == [1, 2], str([x.id for x in fixed]))

    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
