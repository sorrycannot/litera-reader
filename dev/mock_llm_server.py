# -*- coding: utf-8 -*-
"""
本地假接口（mock），用于在没有真实 API Key 的情况下验证整条链路。

它模仿 OpenAI 兼容接口的行为：
  GET  /v1/models          返回模型列表
  POST /v1/chat/completions 返回构造的回复
  Authorization 里含 "bad" 时返回 401，用来测错误处理

能识别的请求类型：
  - 提示里含「抽取术语」→ 返回术语表 JSON
  - 提示里含 [编号] 段落   → 返回逐段译文 JSON
  - 其他                  → 返回「可用」

  python dev/mock_llm_server.py [端口]
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LATENCY = 0.12   # 模拟网络延迟，让并发调度的问题能被暴露出来


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass   # 静音，避免刷屏

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"data": [{"id": "mock-model"}, {"id": "deepseek-flash"}]})
        else:
            self._send(404, {"error": {"message": "path not found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode("utf-8", errors="replace")
        auth = self.headers.get("Authorization", "")

        if "bad" in auth:
            self._send(401, {"error": {"message": "invalid api key provided"}})
            return

        try:
            req = json.loads(raw)
        except Exception:
            self._send(400, {"error": {"message": "request body is not valid json"}})
            return

        time.sleep(LATENCY)
        user = "\n".join(m.get("content", "") for m in req.get("messages", [])
                         if m.get("role") == "user")
        content = self._make_reply(user)

        self._send(200, {
            "id": "mock-completion",
            "object": "chat.completion",
            "model": req.get("model", "mock-model"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 120, "completion_tokens": 260, "total_tokens": 380},
        })

    def _make_reply(self, user: str) -> str:
        # 1) 逐段翻译：提示里会出现「待翻译段落」这个锚点
        if "待翻译段落" in user:
            pairs = re.findall(r"^\[(\d+)\]\s*(.+)$", user, re.MULTILINE)
            items = [{"id": int(i), "zh": f"（译文 {i}）" + t.strip()[:20]} for i, t in pairs]
            return json.dumps({"translations": items}, ensure_ascii=False)

        # 2) 术语抽取：提示里会出现「关键的专业术语」
        if "专业术语" in user:
            return json.dumps({"terms": [
                {"en": "hazard ratio", "zh": "风险比",
                 "definition": "两组发生某结局事件的风险之比，生存分析中常用。"},
                {"en": "confidence interval", "zh": "置信区间",
                 "definition": "对总体参数的一个区间估计，95% 置信区间表示重复抽样时约 95% 的区间会覆盖真值。"},
                {"en": "prospective cohort", "zh": "前瞻性队列",
                 "definition": "先确定暴露、再向前随访观察结局的研究设计，因果推断强度高于横断面研究。"},
            ]}, ensure_ascii=False)

        # 3) 划词解释
        if "被选中的部分" in user:
            m = re.search(r"被选中的部分：(.+)", user)
            word = (m.group(1).strip() if m else "word")[:40]
            return json.dumps({
                "en": word,
                "zh": "（此处释义）",
                "definition": "在这句话里指的是……（假接口生成，用于验证链路）",
            }, ensure_ascii=False)

        # 4) 文献卡片
        if "文献卡片" in user:
            return json.dumps({
                "research_question": "睡眠时长与中年人心血管事件风险是否相关？",
                "methods": "前瞻性队列，12,438 名 45-64 岁无基线心血管病者，中位随访 9.4 年。",
                "findings": "以 7-8 小时为参照，≤5 小时 HR 1.34（95% CI 1.18-1.52）。",
                "limitations": "睡眠时长自报且仅基线测量一次；残余混杂无法排除；以欧洲裔为主。",
            }, ensure_ascii=False)

        # 4) 连接测试
        return "可用"


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock 接口已启动：http://127.0.0.1:{port}/v1")
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
