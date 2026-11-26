# -*- coding: utf-8 -*-
"""
模型调用层自测：起一个本地假接口，把正常路径和错误路径都跑一遍。

  python dev/test_llm.py
"""

import pathlib
import sys
import threading
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev"))

from app.llm import LLMClient, LLMError, estimate_tokens  # noqa: E402
from mock_llm_server import Handler                      # noqa: E402

PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"

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
    print(f"假接口已起：{BASE}\n")

    # 1. 正常连接
    good = LLMClient(BASE, "sk-mock-key", "mock-model")
    ok, msg = good.test()
    check("正常连接返回成功", ok, msg)
    print(f"         -> {msg}")

    # 2. 普通对话
    reply = good.chat([{"role": "user", "content": "你好"}], max_tokens=32)
    check("普通对话拿到内容", reply.strip() == "可用", repr(reply))

    # 3. 地址前缀自动探测（不带 /v1 也应能成功）
    noprefix = LLMClient(BASE, "sk-mock-key", "mock-model")
    r = noprefix.chat([{"role": "user", "content": "hi"}], max_tokens=16)
    check("自动探测 /v1 前缀", r.strip() == "可用", repr(r))
    print(f"         -> 解析到地址 {noprefix._resolved_url}")

    # 4. 密钥错误
    bad = LLMClient(BASE, "bad-key", "mock-model")
    try:
        bad.chat([{"role": "user", "content": "hi"}], max_tokens=16)
        check("错误密钥应当抛异常", False)
    except LLMError as e:
        check("错误密钥提示人话（含『密钥』）", "密钥" in str(e), str(e))
        print(f"         -> {e}")

    # 5. 地址错误
    dead = LLMClient("http://127.0.0.1:9", "sk-mock-key", "mock-model", max_retries=0)
    try:
        dead.chat([{"role": "user", "content": "hi"}], max_tokens=16)
        check("错误地址应当抛异常", False)
    except LLMError as e:
        check("错误地址提示人话（含『连不上』）", "连不上" in str(e), str(e))

    # 6. JSON 模式
    raw = good.chat_json([{"role": "user", "content": "抽不抽都行"}], max_tokens=64)
    check("JSON 模式有返回", bool(raw.strip()), raw[:40])

    # 7. token 估算
    t_en = estimate_tokens("a" * 400)
    t_cn = estimate_tokens("中" * 150)
    check("英文 400 字符约 100 token", 90 <= t_en <= 115, str(t_en))
    check("中文 150 字约 100 token", 90 <= t_cn <= 115, str(t_cn))

    srv.shutdown()
    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
