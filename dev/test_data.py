# -*- coding: utf-8 -*-
"""
数据层自测：术语库合并策略、缓存往返、文献卡片

  python dev/test_data.py
"""

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.cache import PaperCache        # noqa: E402
from app.cards import CardStore         # noqa: E402
from app.termstore import TermStore     # noqa: E402

passed, failed = 0, 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [OK]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {extra}")


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    print(f"临时目录：{tmp}\n")

    # ---------- 术语库 ----------
    print("术语库：")
    ts = TermStore(tmp / "terms.db")

    ts.upsert("hazard ratio", "风险比", "两组风险之比。", source="paperA.pdf")
    check("新词写入", ts.count() == 1, str(ts.count()))

    # 更详细的定义应当胜出，命中数累加
    ts.upsert("hazard ratio", "风险比",
              "两组发生某结局事件的风险之比，是 Cox 回归的直接输出；HR=1 表示两组风险相同。",
              source="paperB.pdf")
    r = ts.get("hazard ratio")
    check("同词不重复入库", ts.count() == 1, str(ts.count()))
    check("定义取更详细的一版", "Cox 回归" in r["definition"], r["definition"][:30])
    check("命中次数累加", r["hits"] == 2, str(r["hits"]))
    check("来源文献去重追加", r["sources"] == ["paperA.pdf", "paperB.pdf"], str(r["sources"]))

    # 更短的定义不应覆盖长的那版
    ts.upsert("hazard ratio", "风险比", "风险之比。", source="paperC.pdf")
    r = ts.get("hazard ratio")
    check("较短定义不覆盖较长定义", "Cox 回归" in r["definition"], r["definition"][:30])

    # 划词查询：带复数、带标点都要能命中
    check("lookup 命中原词", (ts.lookup("hazard ratio") or {}).get("zh") == "风险比")
    check("lookup 去复数命中", (ts.lookup("Hazard Ratios") or {}).get("zh") == "风险比")
    check("lookup 去标点命中", (ts.lookup("  hazard ratio. ") or {}).get("zh") == "风险比")
    check("lookup 未收录返回空", ts.lookup("nonexistent term") is None)

    ts.upsert_many([
        {"en": "confidence interval", "zh": "置信区间", "definition": "对总体参数的区间估计。"},
        {"en": "propensity score matching", "zh": "倾向性评分匹配",
         "definition": "用倾向性评分把处理组与对照组配对，以控制混杂。"},
    ], source="paperA.pdf")
    check("批量写入", ts.count() == 3, str(ts.count()))
    check("按中文检索", len(ts.search("置信")) == 1, str(len(ts.search("置信"))))

    # 导出 / 导入
    csv_path = ts.export_csv(tmp / "export" / "terms.csv")
    check("导出 CSV 成功", csv_path.is_file())
    ts2 = TermStore(tmp / "terms2.db")
    n = ts2.import_csv(csv_path)
    check("导入到新库", n == 3 and ts2.count() == 3, f"导入 {n} 条，库内 {ts2.count()} 条")
    check("导入后内容保留", (ts2.get("hazard ratio") or {}).get("zh") == "风险比")

    # ---------- 删文献时的术语处置 ----------
    print("\n删文献时的术语处置：")
    ts3 = TermStore(tmp / "terms3.db")
    ts3.upsert("alpha", "甲", source="A.pdf", source_fp="fpA")
    ts3.upsert("beta", "乙", source="A.pdf", source_fp="fpA")
    ts3.upsert("beta", "乙", source="B.pdf", source_fp="fpB")      # 两篇共用
    ts3.upsert("gamma", "伽", source="B.pdf", source_fp="fpB")      # 只有 B 用
    # 老库数据：只有文件名、没有指纹，也要能按名字认出
    ts3._conn.execute(
        "INSERT INTO terms (en, display_en, zh, sources, source_fps, created_at, updated_at)"
        " VALUES ('legacy','legacy','旧','[\"A.pdf\"]','[]','','')")
    ts3._conn.commit()

    impact = ts3.source_impact("fpA", "A.pdf")
    check("算出这篇带进来 3 条", impact["total"] == 3, str(impact))
    check("其中 2 条只有这篇用过", impact["exclusive"] == 2, str(impact))

    got = ts3.delete_by_source("fpA", "A.pdf")
    check("删掉 2 条独占的", got["removed"] == 2, str(got))
    check("共用那条只摘来源", got["unlinked"] == 1, str(got))
    check("共用的词还在", bool(ts3.get("beta")), str(ts3.get("beta")))
    check("别的文献的词没动", bool(ts3.get("gamma")))
    check("老数据按文件名也认得出来", ts3.get("legacy") is None)
    check("删完只剩 2 条", ts3.count() == 2, str(ts3.count()))

    # ---------- 缓存 ----------
    print("\n译文缓存：")
    pc = PaperCache(tmp / "papers")

    class FakePara:
        def __init__(self, pid, text, zh="", page=1, page_from=0, page_to=0):
            self.id, self.text, self.zh = pid, text, zh
            self.page, self.section, self.kind, self.flags = page, "Methods", "body", []
            self.page_from, self.page_to = page_from or page, page_to or page

    class FakeStruct:
        fingerprint = "abc123def456"
        filename = "paper.pdf"
        pages = 2
        columns = 2
        body_size = 9.0

        def __init__(self):
            self.paragraphs = [
                FakePara(1, "First paragraph text here."),
                FakePara(2, "Second paragraph text here."),
                # 跨页合段的段落：显示在第 2 页，但起于第 1 页
                FakePara(3, "Merged paragraph text here.", page=2, page_from=1, page_to=2),
            ]

    st = FakeStruct()
    check("初始无缓存", not pc.has(st.fingerprint))
    pc.save(st, {1: "第一段译文", 2: "第二段译文", 3: "第三段译文"}, terms=[{"en": "a", "zh": "甲"}])
    check("保存后命中", pc.has(st.fingerprint))

    loaded = pc.load(st.fingerprint)
    check("读回段落数一致", len(loaded["paragraphs"]) == 3, str(len(loaded["paragraphs"])))
    merged = loaded["paragraphs"][2]
    check("跨页段的起始页也存了下来", merged.get("page_from") == 1 and merged.get("page_to") == 2,
          f"{merged.get('page_from')}-{merged.get('page_to')}")
    check("跨页段显示在字多的那一页", merged["page"] == 2, str(merged["page"]))
    check("译文随段落一起读回", loaded["paragraphs"][0]["zh"] == "第一段译文",
          loaded["paragraphs"][0]["zh"])
    check("术语表随缓存保存", loaded["terms"][0]["zh"] == "甲")

    lst = pc.list_all()
    check("缓存列表能列出", len(lst) == 1 and lst[0]["translated"] == 3, str(lst))

    # ---------- 文献卡片 ----------
    print("\n文献卡片：")
    cs = CardStore(tmp / "cards.db")
    cs.upsert(st.fingerprint, st.filename, {
        "title": "睡眠时长与心血管风险",
        "research_question": "睡眠时长是否与心血管事件相关？",
        "methods": "前瞻性队列，12,438 人，随访 9.4 年。",
        "findings": "≤5 小时 HR 1.34（95% CI 1.18-1.52）。",
        "limitations": "睡眠自报；残余混杂。",
    })
    check("卡片写入", cs.count() == 1, str(cs.count()))
    card = cs.get(st.fingerprint)
    check("卡片字段完整", card["findings"].startswith("≤5 小时"), card["findings"][:20])
    check("卡片可检索", len(cs.search("睡眠")) == 1, str(len(cs.search("睡眠"))))

    cs.upsert(st.fingerprint, st.filename, {"findings": "更新后的结论", "title": "新标题"})
    check("重新生成覆盖而非重复", cs.count() == 1 and cs.get(st.fingerprint)["findings"] == "更新后的结论")

    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
