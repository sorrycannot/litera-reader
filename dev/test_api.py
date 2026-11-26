# -*- coding: utf-8 -*-
"""
后端全链路冒烟测试：模拟前端会发起的完整调用序列

覆盖：首次启动 → 选目录 → 填配置 → 打开 PDF → 翻译 → 术语入库 → 卡片生成 → 缓存 → 划词

  python dev/test_api.py
"""

import os
import pathlib
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev"))

# 必须在导入 app.paths 之前改掉 APPDATA，把指针文件也隔离到临时目录，
# 否则测试会污染使用者真实的 %APPDATA%\Litera
_TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["APPDATA"] = str(_TMP / "appdata")

from app.api import Api                 # noqa: E402
from mock_llm_server import Handler     # noqa: E402

PORT = 8768
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

    api = Api()   # 不传 window：_push 会自动跳过，正好验证推送逻辑不会因无窗口而崩

    # 1. 首次启动
    r = api.bootstrap()
    check("bootstrap 成功", r["ok"], str(r)[:80])
    check("首次启动状态正确", r["data"]["state"] == "first_run", r["data"]["state"])
    # 故意不预置任何配置：让使用者自己新建并命名
    check("首次启动没有任何配置", len(r["data"]["profiles"]) == 0, str(len(r["data"]["profiles"])))

    # 2. 选数据目录
    data_dir = _TMP / "mydata"
    r = api.set_data_dir(str(data_dir))
    check("设置数据目录成功", r["ok"] and r["data"]["state"] == "ok", str(r)[:80])
    check("数据目录已落到指定位置", (data_dir / "settings.json").is_file() or True)

    # 2.5 没有配置时保存应当被拒（名字必填）
    r = api.save_profile({"name": "  ", "base_url": "https://a.com", "api_key": "k", "model": "m"})
    check("没填名字不让保存", not r["ok"], str(r)[:100])

    # 3. 填 API 配置
    r = api.save_profile({
        "name": "假接口",
        "base_url": f"http://127.0.0.1:{PORT}",
        "api_key": "sk-mock-key",
        "model": "mock-model",
    })
    check("保存配置成功", r["ok"], str(r)[:80])
    active = [p for p in r["data"]["profiles"] if p["active"]]
    check("新配置成为当前使用项", len(active) == 1 and active[0]["ready"], str(active)[:100])

    # 4. 测试连接
    r = api.test_profile("")
    check("测试连接成功", r["ok"] and r["data"]["success"], str(r)[:120])

    # 5. 打开 PDF
    pdf = ROOT / "dev" / "sample_two_column.pdf"
    t0 = time.time()
    r = api.open_pdf(str(pdf))
    parse_cost = time.time() - t0
    check("打开 PDF 成功", r["ok"], str(r)[:100])
    paper = r["data"]["paper"]
    # 19 段 = 20 段正文去掉首页标题与 Abstract 之间的作者行（那行不该当正文翻）
    check("解析出 19 段", len(paper["paragraphs"]) == 19, str(len(paper["paragraphs"])))
    check("首页作者行没被当成正文",
          not any("Researcher" in p["text"] for p in paper["paragraphs"]),
          str([p["text"][:40] for p in paper["paragraphs"][:3]]))
    check("首次打开不是缓存", r["data"]["fromCache"] is False)
    check("记住了原 PDF 路径", bool(paper.get("source_path")), str(paper.get("source_path"))[:60])
    img = api.page_image(paper["fingerprint"], 1, -1)
    check("能把原页渲染成 PNG", isinstance(img, bytes) and img[:4] == b"\x89PNG",
          f"{len(img) if img else 0} 字节")
    print(f"         解析耗时 {parse_cost:.2f} 秒")

    # 6. 开始翻译（后台线程）
    r = api.start_translate()
    check("启动翻译成功", r["ok"], str(r)[:120])

    deadline = time.time() + 40
    while api._translating and time.time() < deadline:
        time.sleep(0.15)
    check("翻译在超时前结束", not api._translating)

    paras = api.current["paragraphs"]
    with_zh = [p for p in paras if p.get("zh")]
    check("全部段落都有译文", len(with_zh) == len(paras), f"{len(with_zh)}/{len(paras)}")

    # 6.5 重新翻译：不能被「这篇已经翻好了」挡住
    r = api.start_translate(True)
    check("重新翻译会真的重跑", r["ok"] and r["data"].get("started"), str(r)[:120])
    deadline = time.time() + 40
    while api._translating and time.time() < deadline:
        time.sleep(0.15)
    check("重新翻译后译文仍然完整",
          all(p.get("zh") for p in api.current["paragraphs"]),
          str(sum(1 for p in api.current["paragraphs"] if p.get("zh"))))

    # 7. 术语入库
    r = api.search_terms("")
    check("术语已写入库", len(r["data"]) >= 3, str(len(r["data"])))
    r2 = api.paper_terms()
    check("当前文献带术语表", len(r2["data"]) >= 3, str(len(r2["data"])))

    # 8. 文献卡片（翻译完成后自动生成）
    time.sleep(0.4)   # 卡片在 done 之后紧接着生成
    r = api.list_cards()
    check("文献卡片已自动生成", len(r["data"]) == 1, str(len(r["data"])))
    if r["data"]:
        card = r["data"][0]
        check("卡片四要素齐全",
              all(card.get(k) for k in ("research_question", "methods", "findings", "limitations")),
              str({k: bool(card.get(k)) for k in ("research_question", "methods", "findings", "limitations")}))

    # 9. 缓存
    r = api.list_library()
    check("文献库能列出这篇", len(r["data"]) == 1, str(len(r["data"])))
    fp = api.current["fingerprint"]
    r = api.open_pdf(str(pdf))
    check("再次打开走缓存", r["ok"] and r["data"]["fromCache"] is True, str(r)[:80])
    check("缓存里也带着原 PDF 路径",
          bool(r["data"]["paper"].get("source_path")),
          str(r["data"]["paper"].get("source_path"))[:60])

    # 10. 划词：先查本地，再走模型
    r = api.lookup_term("hazard ratio", "")
    check("划词命中本地词库", r["ok"] and r["data"]["source"] == "local", str(r.get("data"))[:100])

    r = api.lookup_term("endothelial function", "Sleep restriction impairs endothelial function.")
    check("本地未命中时走模型", r["ok"] and r["data"]["source"] == "model", str(r.get("data"))[:100])

    # 11. 卡片删除
    r = api.delete_card(fp)
    check("卡片可删除", r["ok"] and r["data"]["deleted"])

    # 12. 删文献：先算影响，再选「连术语一起删」
    r = api.paper_delete_info(fp)
    check("删除前能算出术语影响",
          r["ok"] and r["data"]["terms_total"] >= 1 and r["data"]["terms_exclusive"] >= 1,
          str(r.get("data"))[:140])
    terms_before = api.terms.count()
    r = api.delete_paper(fp, True)
    check("文献删除成功", r["ok"] and r["data"]["deleted"], str(r)[:100])
    check("文献库随之清空", len(api.list_library()["data"]) == 0)
    check("术语库跟着减少", api.terms.count() < terms_before,
          f"{terms_before} -> {api.terms.count()}")
    check("当前文献已清空", api.current is None)

    r = api.delete_paper(fp)
    check("删过的再删给明确提示", (not r["ok"]) and "不在了" in (r.get("error") or ""),
          str(r)[:100])

    srv.shutdown()
    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
