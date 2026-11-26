# -*- coding: utf-8 -*-
"""
本地 HTTP 服务：界面与后端之间的通道

为什么不用 pywebview
--------------------
pywebview 的 Windows 后端依赖 pythonnet，而 pythonnet 要把 Python.Runtime.dll
交给 .NET Framework 加载。.NET Framework 加载程序集时**不支持非 ASCII 路径** ——
只要程序装在含中文的目录下（国内几乎必然），启动就报
"Failed to resolve Python.Runtime.Loader.Initialize"。
实测：同一份 exe 放到 C:\\lt_test 能跑，放在含中文的目录就跑不起来。

换成 Python 标准库的 http.server + 浏览器应用模式窗口，完全不碰 .NET：
  - 中文路径无障碍
  - 打包体积小（不需要 GUI 框架）
  - 用系统已有的 Edge/Chrome 的 --app 模式，窗口没有地址栏和标签页，
    看起来就是个独立软件
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 关闭页面后多久没有心跳就认为用户走了（秒）
# 给得宽松些：浏览器最小化时定时器会被节流，太短会误判
IDLE_TIMEOUT = 180

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    # 数学字体（随程序分发，见 web/fonts）。给对 MIME 挑剔的浏览器留好后路：
    # 返回 application/octet-stream 时部分浏览器会拒绝加载 @font-face。
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}


class ApiHandler(BaseHTTPRequestHandler):
    api = None            # 由 start_server 注入 Api 实例
    web_root: Path = None
    last_seen = 0.0
    idle_timeout = IDLE_TIMEOUT
    on_idle = None        # 空闲超时的回调（用来停服务）

    # ---- 基础 ----

    def log_message(self, *args):
        pass    # 静音，避免刷屏

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel: str):
        # 防目录穿越：只允许 web_root 下的文件
        target = (self.web_root / rel.lstrip("/")).resolve()
        try:
            target.relative_to(self.web_root.resolve())
        except ValueError:
            self.send_error(403, "forbidden")
            return

        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self.send_error(404, "not found")
            return

        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_png(self, data: bytes | None):
        if not data:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        # 同一篇的同一页不会变，交给浏览器缓存，翻上翻下不用重新渲染
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    # ---- 路由 ----

    def do_GET(self):
        ApiHandler.last_seen = time.time()
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", ""):
            self._send_file("index.html")
        elif path == "/pageimg":
            # 原页/插图/表格/公式：/pageimg?fp=<指纹>&p=6&i=0（i 图，t 表，f 公式，都不给是整页）
            q = parse_qs(parsed.query)
            fp = (q.get("fp") or [""])[0]
            try:
                page = int((q.get("p") or ["1"])[0] or 1)
                index = int((q.get("i") or ["-1"])[0] or -1)
                table = int((q.get("t") or ["-1"])[0] or -1)
                formula = int((q.get("f") or ["-1"])[0] or -1)
            except ValueError:
                page, index, table, formula = 1, -1, -1, -1
            self._send_png(self.api.page_image(fp, page, index, table, formula) if fp else None)
        elif path == "/api/ping":
            self._send_json({"ok": True})
        else:
            self._send_file(path)

    def do_POST(self):
        ApiHandler.last_seen = time.time()
        path = urlparse(self.path).path

        if path == "/api/ping":
            self._send_json({"ok": True})
            return

        if not path.startswith("/api/"):
            self.send_error(404, "not found")
            return

        method_name = path[len("/api/"):]
        fn = getattr(self.api, method_name, None)
        # 只允许调用公开方法，别把内部的东西暴露出去
        if method_name.startswith("_") or not callable(fn):
            self._send_json({"ok": False, "error": f"没有这个方法：{method_name}"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw or "{}")
            args = payload.get("args", [])
            if not isinstance(args, list):
                args = [args]
            result = fn(*args)
            self._send_json(result if isinstance(result, dict) else {"ok": True, "data": result})
        except TypeError as e:
            self._send_json({"ok": False, "error": f"参数不对：{e}"}, 400)
        except Exception as e:
            self._send_json({"ok": False, "error": f"后端出错：{e}"}, 500)


def start_server(api, web_root: Path, timeout_seconds: int = IDLE_TIMEOUT):
    """
    起服务并返回 (server, port, idle_checker)。
    端口交给系统分配（bind 0），避免固定端口被占用。
    """
    ApiHandler.api = api
    ApiHandler.web_root = Path(web_root)
    ApiHandler.last_seen = time.time()
    ApiHandler.idle_timeout = timeout_seconds

    server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    port = server.server_address[1]

    def watch_idle():
        """界面关掉之后自动退出，避免后台残留一个看不见的进程"""
        while True:
            time.sleep(10)
            if time.time() - ApiHandler.last_seen > ApiHandler.idle_timeout:
                try:
                    server.shutdown()
                except Exception:
                    pass
                return

    threading.Thread(target=watch_idle, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port
