# -*- coding: utf-8 -*-
"""
端到端自测：真 PDF 抽取 → 术语抽取 → 分批翻译 → 编号回填

用本地假接口跑，不需要 API Key。
  python dev/test_translate.py
"""

import pathlib
import sys
import threading
import time
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev"))

from app.llm import LLMClient                       # noqa: E402
from app.pdf_reader import parse_pdf                # noqa: E402
from app.translator import Translator, make_batches  # noqa: E402
from mock_llm_server import Handler                 # noqa: E402

PORT = 8767

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
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    pdf = ROOT / "dev" / "sample_two_column.pdf"
    if not pdf.is_file():
        print("样本 PDF 不存在，先跑 python dev/make_sample.py")
        return 1

    struct = parse_pdf(pdf)
    print(f"PDF: {struct.filename} | {len(struct.paragraphs)} 段 | {struct.total_chars} 字符\n")

    client = LLMClient(f"http://127.0.0.1:{PORT}", "sk-mock", "mock-model")
    tr = Translator(client, max_workers=3)

    # 分批策略检查
    paras = [p for p in struct.paragraphs if p.trusted]
    batches = make_batches(paras)
    check("分批数合理（不空、不过碎）", 1 <= len(batches) <= len(paras),
          f"{len(batches)} 批 / {len(paras)} 段")

    # 跑完整翻译
    seen_batches = []
    stages = []
    t0 = time.time()
    result = tr.translate_document(
        struct,
        on_batch=lambda got: seen_batches.append(len(got)),
        on_stage=lambda s: stages.append(s),
    )
    cost = time.time() - t0

    print()
    for s in stages:
        print(f"   阶段：{s}")
    print()

    translatable_ids = {p.id for p in paras if p.kind in ("body", "heading", "caption")}
    check("术语表已提取", len(result.terms) >= 3, str(len(result.terms)))
    check("译文覆盖全部可翻段落",
          set(result.translations.keys()) == translatable_ids,
          f"拿到 {len(result.translations)} / 应有 {len(translatable_ids)}")
    check("没有漏翻的段落", not result.missing_ids, str(result.missing_ids))
    check("没有跳过的段落（样本全可信）", not result.skipped_ids, str(result.skipped_ids))
    check("没有调用错误", not result.errors, str(result.errors[:1]))
    check("回调被触发多次（说明是分批流式）", len(seen_batches) >= 2, str(seen_batches))

    # 错位检查：假接口返回的译文里带着它收到的那段原文开头，
    # 逐条对一下，就能看出译文有没有挂到别的段落上。
    # 注意不能用「译文里的编号 == 真实编号」来查 —— 发给模型的编号是批内序号（1..N），
    # 拿回来才映射成真实段落号，编号本来就不该相等。
    by_id = {p.id: p for p in struct.paragraphs}
    aligned = all(by_id[pid].text.strip().split()[0] in txt
                  for pid, txt in result.translations.items() if by_id[pid].text.split())
    check("译文挂对了段落（无错位）", aligned)

    # 「什么该翻、什么不该翻」的判定。这条以前判反了：
    # 一页里只要有个跨栏表格，整页段落都被标 layout_uncertain 拒翻，
    # 使用者看到的是整页英文 + 一句「无法识别，宁愿不翻」。
    from app.pdf_reader import Para
    check("版面顺序不确定的段落照样要翻",
          Para(id=1, page=1, text="x" * 50, size=10, kind="body",
               section="Methods", flags=["layout_uncertain"]).trusted)
    check("表格不翻（按原样显示）",
          not Para(id=2, page=1, text="[表格 1]", size=10, kind="table",
                   section="Methods").trusted)
    check("标题再短也要翻",
          Para(id=3, page=1, text="Methods", size=12, kind="heading",
               section="Methods").trusted)

    print(f"\n  耗时 {cost:.2f} 秒（假接口每批延迟 0.12 秒，真接口会慢几十倍）")
    print(f"  示例：[4] {result.translations.get(4, '')[:50]}")

    srv.shutdown()
    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
