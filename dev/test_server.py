# -*- coding: utf-8 -*-
"""
HTTP 服务层自测：静态文件、API 路由、心跳、目录穿越防护

  python dev/test_server.py
"""

import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["APPDATA"] = str(_TMP / "appdata")

from app.api import Api          # noqa: E402
from app.server import start_server   # noqa: E402

passed, failed = 0, 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [OK]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {extra}")


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def post(url, args=None):
    body = json.dumps({"args": args or []}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    api = Api()
    server, port = start_server(api, ROOT / "web", timeout_seconds=120)
    base = f"http://127.0.0.1:{port}"

    # 静态资源
    st, body = get(base + "/")
    check("首页可访问", st == 200 and b"<html" in body.lower(), str(st))
    st, _ = get(base + "/app.js")
    check("app.js 可访问", st == 200, str(st))
    st, _ = get(base + "/style.css")
    check("style.css 可访问", st == 200, str(st))

    # 心跳
    st, body = get(base + "/api/ping")
    check("心跳接口正常", st == 200 and json.loads(body)["ok"])

    # API 调用
    st, r = post(base + "/api/bootstrap")
    check("bootstrap 返回成功", st == 200 and r["ok"], str(r)[:80])
    check("首次启动状态正确", r["data"]["state"] == "first_run", str(r["data"]["state"]))

    # 带参数调用
    data_dir = _TMP / "mydata"
    st, r = post(base + "/api/set_data_dir", [str(data_dir)])
    check("带参数调用正常", st == 200 and r["ok"], str(r)[:80])

    # 未知方法
    st, r = post(base + "/api/not_exist")
    check("未知方法返回 404", st == 404, str(st))

    # 内部方法不应暴露
    st, r = post(base + "/api/_push", ["x", {}])
    check("下划线方法被拒绝", st == 404, str(st))

    # 目录穿越
    try:
        st, body = get(base + "/..%2fmain.py")
        ok = (st == 403) or (b"import" not in body)
    except urllib.error.HTTPError as e:
        ok = e.code in (403, 404)
    check("目录穿越被拦住", ok)

    server.shutdown()
    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
