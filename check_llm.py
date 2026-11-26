#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 接口连通性自检脚本（零依赖，只用 Python 标准库）

用途
----
确认你手上的 API Key / 中转地址到底能不能用、能用哪些模型、
调用一次大概花多少钱。拿到 key 后先跑这个，能省掉后面大量排错时间。

用法
----
  :: 方式1：临时指定（官方 DeepSeek）
  python check_llm.py --key sk-xxxxxxxx

  :: 方式2：临时指定（Wetokens 中转等任意 OpenAI 兼容服务）
  python check_llm.py --key xxxxxxxx ^
      --base-url https://wetokens-api.wetokens.cn/wetokens/v1/openai/v1

  :: 方式3：只列出可用模型，不做真实调用
  python check_llm.py --list

  :: 方式4：什么都不传，自动读环境变量或同目录 config.json
  python check_llm.py

Key 读取优先级
--------------
  命令行 --key  >  环境变量 LLM_API_KEY  >  DEEPSEEK_API_KEY  >  OPENAI_API_KEY
  >  同目录 config.json 里的 "api_key"

退出码
------
  0 = 完全可用    1 = 鉴权失败/网络不通    2 = 参数缺失
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Windows 控制台默认 GBK，中文会乱码，强制切成 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# DeepSeek 官方默认配置
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

# 预估用：一篇典型论文约 2 万输入 token、3 千输出 token
PAPER_INPUT_TOKENS = 20000
PAPER_OUTPUT_TOKENS = 3000
# 每百万 token 单价（美元，DeepSeek V4.1-Flash 谷时价，2026-09 官方价目）
PRICE_IN_PER_M = 0.15
PRICE_OUT_PER_M = 0.60
USD_TO_CNY = 7.1


def mask(key: str) -> str:
    """把 key 打码，避免截图或贴日志时泄露"""
    if not key:
        return "(空)"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}****{key[-4:]} (长度 {len(key)})"


def load_config_file() -> dict:
    """读脚本同目录的 config.json（可选文件，不存在就返回空字典）"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        print(f"[警告] config.json 解析失败，已忽略：{e}")
        return {}


def resolve_settings(args) -> tuple:
    """按优先级确定 base_url / api_key / model"""
    cfg = load_config_file()

    api_key = (
        args.key
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or cfg.get("api_key", "")
    )
    base_url = (
        args.base_url
        or os.environ.get("LLM_BASE_URL")
        or cfg.get("base_url", "")
        or DEFAULT_BASE_URL
    )
    model = args.model or os.environ.get("LLM_MODEL") or cfg.get("model", "") or DEFAULT_MODEL
    return base_url.rstrip("/"), api_key.strip(), model


def build_endpoints(base_url: str) -> list:
    """
    不同服务商的路径前缀不一样：
      官方 DeepSeek      https://api.deepseek.com        -> /chat/completions 或 /v1/chat/completions
      Wetokens 中转      .../wetokens/v1/openai/v1       -> /chat/completions
      其他 OneAPI 类      https://xx.com/v1               -> /chat/completions
    所以按候选顺序依次尝试，第一个成功的即采用。
    """
    if base_url.endswith("/v1"):
        candidates = [base_url]
    else:
        candidates = [base_url + "/v1", base_url]

    endpoints = []
    for b in candidates:
        endpoints.append((b + "/chat/completions", b + "/models"))
    return endpoints


def http_json(url: str, api_key: str, payload=None, timeout: int = 40):
    """发一个请求并解析 JSON。返回 (data, error_message)"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "litera-check/1.0",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace")), None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        return None, f"HTTP {e.code} {e.reason} | {body}"
    except urllib.error.URLError as e:
        return None, f"网络不可达：{e.reason}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 接口连通性自检")
    ap.add_argument("--key", help="API Key（sk-... 或中转站 key）")
    ap.add_argument("--base-url", dest="base_url", help="接口地址，如 https://api.deepseek.com")
    ap.add_argument("--model", help="模型名，默认 deepseek-flash")
    ap.add_argument("--list", action="store_true", help="只列出可用模型，不做真实调用")
    args = ap.parse_args()

    base_url, api_key, model = resolve_settings(args)

    print("=" * 62)
    print(" LLM 接口自检")
    print("=" * 62)
    print(f" 接口地址 : {base_url}")
    print(f" API Key  : {mask(api_key)}")
    print(f" 目标模型 : {model}")
    print("-" * 62)

    if not api_key:
        print("[失败] 没有找到 API Key。")
        print("       请用 --key 传入，或设置环境变量 LLM_API_KEY，")
        print("       或在脚本同目录建 config.json：{\"api_key\": \"你的key\"}")
        return 2

    endpoints = build_endpoints(base_url)

    # ---------- 第 1 步：探测地址 + 拉模型列表 ----------
    print("[1/2] 探测接口地址与模型列表 ...")
    chat_url = models_url = None
    model_names = []
    for ep_chat, ep_models in endpoints:
        data, err = http_json(ep_models, api_key)
        if data is not None:
            chat_url, models_url = ep_chat, ep_models
            items = data.get("data") or []
            model_names = [m.get("id", "?") for m in items if isinstance(m, dict)]
            print(f"      ✅ 地址有效：{ep_models.replace('/models', '')}")
            break
        # /models 不被支持时，退一步只要地址能连上就算过（部分中转站关了这个接口）
        print(f"      · {ep_models} -> {err}")

    if chat_url is None:
        # 兜底：直接用第一个候选地址试真实调用
        chat_url = endpoints[0][0]
        print(f"      ⚠ 拿不到模型列表，直接用 {chat_url} 试调用")

    if model_names:
        print(f"      可用模型 {len(model_names)} 个：")
        for i in range(0, min(len(model_names), 12), 3):
            print("        " + "  ".join(f"{n:<28}" for n in model_names[i:i + 3]))
        if len(model_names) > 12:
            print(f"        ... 其余 {len(model_names) - 12} 个略")
        # 目标模型不在列表里就给出提示（不阻断，有些站列表不全）
        if model not in model_names:
            print(f"      ⚠ 注意：列表里没有 '{model}'，如果调用失败请换一个上面的模型名")

    if args.list:
        print("-" * 62)
        print(" 仅列出模型，未做真实调用。")
        return 0

    # ---------- 第 2 步：真实调用一次 ----------
    print(f"[2/2] 用 '{model}' 发一次真实请求 ...")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字：可用"}],
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
    }
    t0 = time.time()
    data, err = http_json(chat_url, api_key, payload)
    cost = time.time() - t0

    if data is None:
        print(f"      ❌ 调用失败：{err}")
        print("-" * 62)
        print(" 排错顺序建议：")
        print("   1) 401/403 → key 错了或没权限，重新复制（注意别带空格）")
        print("   2) 404        → base-url 路径不对，试试去掉或加上 /v1")
        print("   3) 余额不足   → 去平台充值")
        print("   4) 超时/不可达 → 网络或代理问题，先 ping 一下域名")
        return 1

    content = ""
    try:
        content = data["choices"][0]["message"]["content"].strip()
    except Exception:
        content = json.dumps(data, ensure_ascii=False)[:200]

    usage = data.get("usage") or {}
    print(f"      ✅ 调用成功，耗时 {cost:.2f}s")
    print(f"      模型回复：{content}")
    if usage:
        print(
            f"      token 用量：输入 {usage.get('prompt_tokens', '?')} / "
            f"输出 {usage.get('completion_tokens', '?')}"
        )

    # ---------- 成本预估 ----------
    per_paper = (
        PAPER_INPUT_TOKENS / 1e6 * PRICE_IN_PER_M
        + PAPER_OUTPUT_TOKENS / 1e6 * PRICE_OUT_PER_M
    )
    per_paper_cny = per_paper * USD_TO_CNY
    print("-" * 62)
    print(" 成本预估（按 DeepSeek-V4.1-Flash 谷时价）")
    print(f"   单篇论文（2万输入 + 3千输出）：约 ¥{per_paper_cny:.3f}")
    print(f"   100 篇                        ：约 ¥{per_paper_cny * 100:.1f}")
    print(f"   充 10 元大约能跑              ：{int(10 / per_paper_cny)} 篇")
    print("=" * 62)
    print(" 结论：接口可用，可以进入下一步开发。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
