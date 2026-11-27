# -*- coding: utf-8 -*-
"""
OpenAI 兼容的模型调用客户端

设计取舍
--------
1. 只用标准库 urllib，不引入 requests —— 打包体积小一点，依赖少一个坑。
2. 接口地址自动探测：各家的路径前缀不一样（`/v1`、`/openai/v1`、干脆没有）。
   按候选顺序试，成功就记住。否则使用者填了 `https://api.xxx.com` 却收到 404，
   他只会觉得"这软件坏了"。
3. 错误翻译成人话：401 是密钥错、404 是地址错、余额不足要充值。
   直接把 `HTTPError 401` 甩给使用者，等于让他来找你。
4. 网络抖动和 5xx 自动重试；鉴权错误不重试（重试也没用，只会让人干等）。
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request


# ---------------- HTTPS 根证书 ----------------
#
# Windows 版一直没这问题：CPython 在 Windows 上直接从**系统证书存储**取根证书
# （ssl.load_default_certs 走 enum_certificates("ROOT")），不需要任何额外文件。
#
# macOS 打包后就不行了：
#   · 没有可枚举的系统证书存储；
#   · OpenSSL 的默认 CA 路径写死在打包时那份 Python 的
#     `.../etc/openssl/cert.pem`，在用户机器上根本不存在。
# 结果 context 里一个 CA 都没有，所有 HTTPS 请求都抛
# `CERTIFICATE_VERIFY_FAILED` —— 界面上显示的就是"连不上服务器 / 测试连接失败"，
# 看起来像网络问题，其实跟网络一点关系没有。
#
# 处理办法：启动后第一次发请求前探测一次。
#   · 默认 context 里已经有根证书（Windows 就是这样，80 个）→ 返回 None，走原来的路，
#     行为一个字都不变；
#   · 一个都没有（打包后的 macOS）→ 改用随程序分发的 certifi CA 列表（113 个）。
# 探测结果缓存，不重复算。

_SSL_CTX: ssl.SSLContext | None = None
_SSL_CTX_DONE = False


def _ssl_context() -> ssl.SSLContext | None:
    """
    返回要用的 SSL context；返回 None 表示「用 urllib 默认的」。
    """
    global _SSL_CTX, _SSL_CTX_DONE
    if _SSL_CTX_DONE:
        return _SSL_CTX
    _SSL_CTX_DONE = True

    try:
        default = ssl.create_default_context()
        if default.cert_store_stats().get("x509", 0) > 0:
            _SSL_CTX = None          # 默认就有根证书，别动它
            return None
    except Exception:
        pass

    try:
        import certifi
        _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _SSL_CTX = None              # certifi 也没了，只能听天由命
    return _SSL_CTX

DEFAULT_TIMEOUT = 180
DEFAULT_MAX_TOKENS = 4096

# ---- 关掉「深度思考」 ----
#
# 新一代模型（qwen3 系列、deepseek 思考版等）默认先写一大段思考过程再回答。
# 对翻译、抽术语这种确定性任务，思考几乎只带来坏处：
#   实测 qwen3.8-flash 翻同一段话，开思考 7.1 秒 / 417 个输出 token，
#   关思考 1.3 秒 / 45 个 token —— 慢 5 倍、贵 9 倍，译文质量没有区别。
# 一篇十几页的文献，这个差距就是「几分钟」和「二十几分钟」。
#
# 百炼（DashScope 兼容模式）认 enable_thinking=false 这个参数。
# 但不是所有中转站都认，发过去可能直接 400，所以只在两家百炼域名上发，
# 且一旦真的被拒，就永久关掉这个参数不再发（见 chat 里的降级分支）。
NO_THINKING_HOSTS = ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com")
THINKING_MODEL_PAT = re.compile(r"qwen-?3", re.IGNORECASE)


class LLMError(Exception):
    """统一的调用异常。message 是给人看的中文，不是技术栈信息。"""

    def __init__(self, message: str, *, retryable: bool = False, http_status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status


def estimate_tokens(text: str) -> int:
    """粗略估 token：中文约 1.5 字符 1 个，英文约 4 字符 1 个"""
    if not text:
        return 0
    cn = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    en = len(text) - cn
    return int(cn / 1.5 + en / 4) + 1


def _extract_error_message(body: str) -> str:
    """从各家五花八门的错误体里挖出那句有用的说明"""
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            err = d.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])[:200]
            if isinstance(err, str):
                return err[:200]
            for k in ("message", "msg", "detail"):
                if d.get(k):
                    return str(d[k])[:200]
    except Exception:
        pass
    return body.strip()[:200]


def _classify_http(status: int, body: str) -> LLMError:
    detail = _extract_error_message(body)
    low = (body or "").lower()

    # 各家在 400/403 上塞的错误码不一样，先按关键词挑出来说人话。
    # 百炼（DashScope 兼容模式）常见：invalid_api_key / ModelNotFound / Arrearage / AccessDenied
    if "arrearage" in low or "欠费" in detail or "overdue" in low:
        return LLMError(f"账户欠费了，服务商已停服。请去平台结清后重试。{detail}", http_status=status)
    if "model_not_found" in low or "modelnotfound" in low or "model not exist" in low:
        return LLMError(
            f"模型名不存在（{status}）。检查有没有拼错，或者你的账号没开通这个模型。"
            f"常见可用值：qwen-plus / qwen-max / qwen-turbo（百炼）、deepseek-chat（DeepSeek）。{detail}",
            http_status=status,
        )
    if status in (401, 403):
        return LLMError(f"密钥无效或没有权限（{status}）。请检查 API Key 是否复制完整、有没有多余空格。{detail}",
                        http_status=status)
    if status == 404:
        return LLMError(f"接口地址不对（404）。请检查地址是否少了或多了 /v1 之类的路径。{detail}",
                        http_status=status)
    if status == 402 or "insufficient" in low or "balance" in low or "余额" in detail:
        return LLMError(f"账户余额不足，请去服务商平台充值。{detail}", http_status=status)
    if status == 400:
        return LLMError(f"请求被拒绝（400）。常见原因是模型名写错、或这个模型不支持当前参数。{detail}",
                        http_status=status)
    if status == 429:
        return LLMError(f"请求太频繁，被限流了（429）。稍等一会儿会自动重试。{detail}",
                        retryable=True, http_status=status)
    if 500 <= status < 600:
        return LLMError(f"服务商那边出故障了（{status}），不是你的问题。稍后重试即可。{detail}",
                        retryable=True, http_status=status)
    return LLMError(f"调用失败（{status}）：{detail}", http_status=status)


def _post_json(url: str, headers: dict, payload: dict, timeout: float):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    # ctx 为 None 时 urlopen 走默认处理（Windows 就是这样）；
    # 打包后的 macOS 上会拿到 certifi 的 context，否则 HTTPS 必然验签失败。
    ctx = _ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise _classify_http(e.code, e.read().decode("utf-8", errors="replace"))
    except urllib.error.URLError as e:
        raise LLMError(f"连不上服务器：{e.reason}。检查网络、代理，或地址是否写错。", retryable=True)
    except TimeoutError:
        raise LLMError("请求超时。可能是网络慢，或这一批内容太长。", retryable=True)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 地址填成了网页而不是 API 入口时，拿到的就是 HTML
        snippet = raw.strip()[:120].replace("\n", " ")
        raise LLMError(f"接口返回的不是 JSON，地址可能填成了网站首页而不是 API 入口。返回内容开头：{snippet}")


class LLMClient:
    """一个配置档对应一个客户端实例"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 *, timeout: float = DEFAULT_TIMEOUT, max_retries: int = 2):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self._resolved_url: str | None = None   # 探测成功后缓存，后续不再试错
        # 该不该给这个模型发 enable_thinking=false（见模块顶部说明）
        host_ok = any(h in self.base_url for h in NO_THINKING_HOSTS)
        self.send_no_thinking = bool(host_ok and THINKING_MODEL_PAT.search(self.model))

    @classmethod
    def from_profile(cls, profile, **kwargs) -> "LLMClient":
        return cls(profile.base_url, profile.api_key, profile.model, **kwargs)

    # ---------- 内部 ----------

    def _endpoints(self) -> list[str]:
        b = self.base_url
        if b.endswith("/v1"):
            return [b + "/chat/completions"]
        return [b + "/v1/chat/completions", b + "/chat/completions"]

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "Litera/0.1",
        }

    def _payload(self, messages, max_tokens, temperature, json_mode) -> dict:
        p = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            p["response_format"] = {"type": "json_object"}
        if self.send_no_thinking:
            p["enable_thinking"] = False
        return p

    def _extract_content(self, data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"接口没有返回结果内容：{json.dumps(data, ensure_ascii=False)[:200]}")
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if not content:
            reason = choices[0].get("finish_reason")
            raise LLMError(f"模型返回了空内容（finish_reason={reason}）。可能是内容太长被截断，换个短一点的批次试试。")
        return content

    # ---------- 对外 ----------

    def chat(self, messages: list[dict], *, max_tokens: int = DEFAULT_MAX_TOKENS,
             temperature: float = 0.2, json_mode: bool = False,
             timeout: float | None = None, retries: int | None = None) -> str:
        """
        timeout / retries 可以单次覆盖。
        抽术语、测连接这类「宁可失败也别让人干等」的调用，会传更短的值。
        """
        urls = [self._resolved_url] if self._resolved_url else self._endpoints()
        wait = timeout or self.timeout
        tries = self.max_retries if retries is None else retries
        last_err: LLMError | None = None

        for ui, url in enumerate(urls):
            is_last_url = ui == len(urls) - 1
            attempt = 0
            while attempt <= tries:
                try:
                    data = _post_json(
                        url, self._headers(),
                        self._payload(messages, max_tokens, temperature, json_mode),
                        wait,
                    )
                    self._resolved_url = url
                    return self._extract_content(data)
                except LLMError as e:
                    last_err = e
                    # 服务不认 enable_thinking 这个参数（部分中转站会直接 400）：
                    # 去掉它原样重发一次，不消耗重试次数，也不惊动使用者。
                    if e.http_status == 400 and self.send_no_thinking:
                        self.send_no_thinking = False
                        continue
                    # 404/405 多半是路径前缀不对，换下一个候选地址，不必重试同一个
                    if e.http_status in (404, 405) and not is_last_url:
                        break
                    if not e.retryable or attempt >= tries:
                        raise
                    attempt += 1
                    time.sleep(1.2 * attempt)

        raise last_err or LLMError("调用失败，但没拿到具体原因")

    def chat_json(self, messages: list[dict], *, max_tokens: int = DEFAULT_MAX_TOKENS,
                  temperature: float = 0.0, timeout: float | None = None,
                  retries: int | None = None) -> str:
        """
        要求模型输出 JSON。
        先带 response_format 试；有些中转站不支持这个参数会直接 400，
        这时自动降级为普通请求，靠提示词约束格式。
        """
        try:
            return self.chat(messages, max_tokens=max_tokens,
                             temperature=temperature, json_mode=True,
                             timeout=timeout, retries=retries)
        except LLMError as e:
            if e.http_status == 400:
                return self.chat(messages, max_tokens=max_tokens,
                                 temperature=temperature, json_mode=False,
                                 timeout=timeout, retries=retries)
            raise

    def test(self) -> tuple[bool, str]:
        """测试连接。给界面上的「测试连接」按钮用。"""
        if not self.api_key:
            return False, "还没有填 API Key"
        if not self.base_url:
            return False, "还没有填接口地址"
        if not self.model:
            return False, "还没有填模型名"
        t0 = time.time()
        try:
            reply = self.chat(
                [{"role": "user", "content": "只回复两个字：可用"}],
                max_tokens=16, temperature=0, timeout=45, retries=0,
            )
        except LLMError as e:
            return False, str(e)
        cost = time.time() - t0
        return True, f"连接成功（{cost:.1f} 秒），模型回复：{reply.strip()[:40]}"
