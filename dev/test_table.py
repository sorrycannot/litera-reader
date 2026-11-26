# -*- coding: utf-8 -*-
"""
表格检测的回归测试

表格检测是纯几何启发式（见 app/pdf_reader.py 的 page_tables），逻辑全在阈值上，
最容易「改一处，别处塌」。这里现场生成两页 PDF 来验：

  · 一页是典型的结果表（数字密排、列对齐）
  · 一页是普通正文（长句子、正常词距）

表格页必须检出，正文页必须检不出 —— 后者更重要：误判会把正文裁进「表格」里，
读者看到的就是缺页，比表格没识别出来严重得多。

  python dev/test_table.py
"""

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf                                    # noqa: E402
from app.pdf_reader import page_tables, parse_pdf  # noqa: E402

passed, failed = 0, 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [OK]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {extra}")


def make_table_page(doc):
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 70), "Table 1 Baseline characteristics", fontsize=9)
    cols = [60, 200, 320, 440]
    data = [
        ["Group", "N", "Mean age", "p"],
        ["Control", "12", "37.42 (11.70)", "0.32"],
        ["Case", "10", "28.90 (7.98)", "<0.001"],
        ["Total", "22", "33.10 (9.42)", "0.701"],
        # 真实的表注都很长（缩写定义 + 显著性说明），短句会被当成表格行
        ["Note: Values are mean (SD). PS = psychopathic subjects, C = controls.",
         "", "", ""],
    ]
    y = 92
    for row in data:
        for c, cell in enumerate(row):
            if cell:
                page.insert_text((cols[c], y), cell, fontsize=9)
        y += 18
    return (60, 80, 520, y)


def make_text_page(doc):
    page = doc.new_page(width=595, height=842)
    lines = [
        "Psychopathy is a personality construct that represents one of the strongest",
        "predictors of aggressive and violent behaviour. Individuals with strong traits",
        "commit more crimes and cause greater injuries than other offenders, and they",
        "respond poorly to treatment. Structural magnetic resonance imaging has been",
        "used in several studies in order to find interindividual differences in brain",
        "structure which are associated with psychopathy, but sample sizes have been",
        "small and the reported effect sizes inconsistent across cohorts.",
    ]
    y = 90
    for line in lines:
        page.insert_text((60, y), line, fontsize=10)
        y += 16


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="lt_tbl_"))
    pdf = tmp / "table_test.pdf"

    doc = pymupdf.open()
    make_table_page(doc)
    make_text_page(doc)
    doc.save(str(pdf))
    doc.close()

    doc = pymupdf.open(str(pdf))
    on_table = page_tables(doc[0])
    on_text = page_tables(doc[1])
    doc.close()

    print("  表格页检出：", [[round(v) for v in b] for b in on_table])
    print("  正文页检出：", [[round(v) for v in b] for b in on_text])

    check("表格页检出 1 个表格区", len(on_table) == 1, str(len(on_table)))
    if on_table:
        b = on_table[0]
        check("表格区覆盖表头", b[1] < 95, f"y0={b[1]}")
        check("表格区覆盖最后一行数据", b[3] > 145, f"y1={b[3]}")
        check("表格区没把表注框进来（注要单独成段）", b[3] < 158, f"y1={b[3]}")
        check("表格区没把上方的表标题框进来", b[1] > 72, f"y0={b[1]}")
        check("表格区宽度接近整表", b[2] - b[0] > 380, f"{round(b[2] - b[0])}pt")

    check("正文页不误判（这条最关键）", len(on_text) == 0, str(on_text))

    # 走一遍完整解析，确认表格段落生成、且表格文字没留在正文里
    struct = parse_pdf(pdf)
    tables = [p for p in struct.paragraphs if p.kind == "table"]
    check("解析结果里有表格段落", len(tables) == 1, str(len(tables)))
    leaked = [p for p in struct.paragraphs
              if p.kind != "table" and "37.42 (11.70)" in p.text]
    check("表格里的数字没混进正文", not leaked, str([p.text[:40] for p in leaked]))

    # 表注要单独成段、挂在表上 —— 留在正文流里就断了和表的对应关系
    notes = [p for p in struct.paragraphs if p.kind == "table_note"]
    check("表注被单独拆出来", len(notes) == 1, str(len(notes)))
    if notes:
        check("表注挂到了那张表上", notes[0].table_index == 0, str(notes[0].table_index))
        check("表注内容完整", "psychopathic subjects" in notes[0].text, notes[0].text[:60])
        check("表注要参与翻译", notes[0].trusted, str(notes[0].kind))

    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
