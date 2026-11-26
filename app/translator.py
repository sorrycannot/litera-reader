# -*- coding: utf-8 -*-
"""
翻译调度

三件事按顺序做，顺序不能换：
  1. 先扫全文抽术语表，锁定译法
  2. 再按章节分批，并发翻译
  3. 按段落编号回填

为什么必须先抽术语表
--------------------
如果每批独立翻译，hazard ratio 这批译"风险比"、下批译"危险比率"，
读起来会以为在讲两个概念。术语表是唯一能保证全篇一致的手段。

为什么要分批并发
----------------
一篇 20 页论文译成中文约 1.5 万字。一次性请求要等好几分钟，期间界面毫无反应；
分批发还能让「摘要和引言」先出来，使用者二十秒内就能开始读。

编号是硬保证
------------
每段带 id，请求里带 id、返回里带 id，按 id 回填。这样即使模型返回顺序乱了、
漏了几段，也不会错位——最坏情况是某段没有译文（保留英文），而不是挂错译文。
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import prompts
from .llm import LLMClient, LLMError, estimate_tokens

# 优先翻译的章节：这几个部分先出来，使用者就能立刻开始读
PRIORITY_SECTIONS = ("Abstract", "Introduction", "前置")

# 抽术语只喂开头这么多字符。
# 术语集中在标题、摘要、方法开头，把整篇（4 万字）塞进去，模型要读完才开口，
# 实测让首批译文的出现时间从 10 秒变成好几分钟，而术语质量没有提升。
TERMS_MAX_CHARS = 12000
TERMS_TIMEOUT = 90        # 抽术语超时就放弃：宁可这次没有术语表，也不能卡住译文


@dataclass
class TranslationResult:
    translations: dict[int, str] = field(default_factory=dict)
    skipped_ids: list[int] = field(default_factory=list)    # 置信度低，故意不翻
    missing_ids: list[int] = field(default_factory=list)    # 翻了但模型漏了
    terms: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class _Shim:
    """只带 id / text 的壳，用来把段落临时重新编号后交给模型"""

    __slots__ = ("id", "text")

    def __init__(self, i: int, text: str):
        self.id = i
        self.text = text


# ---------------- JSON 解析 ----------------

def parse_json_block(raw: str):
    """
    模型返回的 JSON 常常裹着 ```json 围栏，或者前面带一句"好的，以下是结果"。
    这里逐级降级，尽量把 JSON 抠出来。
    """
    if not raw:
        return None
    s = raw.strip()

    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()

    try:
        return json.loads(s)
    except Exception:
        pass

    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = s.find(open_ch), s.rfind(close_ch)
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    return None


def parse_terms(raw: str) -> list[dict]:
    data = parse_json_block(raw)
    if isinstance(data, dict):
        items = data.get("terms") or data.get("data") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []

    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        en = str(it.get("en") or it.get("term") or "").strip()
        zh = str(it.get("zh") or it.get("cn") or "").strip()
        definition = str(it.get("definition") or it.get("desc") or "").strip()
        if not en or not zh:
            continue
        key = en.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"en": en, "zh": zh, "definition": definition})
    return out


def parse_translations(raw: str, expected_ids: set[int]) -> dict[int, str]:
    """
    解析逐段译文。只接受 expected_ids 里的编号，防止模型串段或编造编号。
    """
    data = parse_json_block(raw)
    if isinstance(data, dict):
        items = (data.get("translations") or data.get("data")
                 or data.get("paragraphs") or data.get("result") or [])
    elif isinstance(data, list):
        items = data
    else:
        items = []

    got: dict[int, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        raw_id = it.get("id", it.get("index", it.get("no")))
        try:
            pid = int(str(raw_id).strip())
        except (TypeError, ValueError):
            continue
        zh = it.get("zh") or it.get("translation") or it.get("text") or it.get("content")
        if pid in expected_ids and zh and str(zh).strip():
            got[pid] = str(zh).strip()
    return got


# ---------------- 分批 ----------------

def make_batches(paras: list, max_tokens: int = 2000, max_items: int = 10) -> list[list]:
    """
    按输入 token 数和段数双重上限切批。
    段数也要限，是因为输出长度才是真正的瓶颈：一批塞 30 段，输出的 JSON 会很长，
    容易被 max_tokens 截断，变成解析失败。
    """
    batches: list[list] = []
    cur: list = []
    cur_tok = 0

    for p in paras:
        t = estimate_tokens(p.text)
        if t > max_tokens:
            # 单段就超长，只能单独成批，避免拖累整批
            if cur:
                batches.append(cur)
                cur, cur_tok = [], 0
            batches.append([p])
            continue
        if cur and (cur_tok + t > max_tokens or len(cur) >= max_items):
            batches.append(cur)
            cur, cur_tok = [], 0
        cur.append(p)
        cur_tok += t

    if cur:
        batches.append(cur)
    return batches


# ---------------- 调度器 ----------------

class Translator:
    def __init__(self, client: LLMClient, *, max_workers: int = 4,
                 chunk_tokens: int = 2000, chunk_items: int = 10):
        self.client = client
        self.max_workers = max_workers
        self.chunk_tokens = chunk_tokens
        self.chunk_items = chunk_items

    # ---------- 术语 ----------

    def extract_terms(self, struct) -> list[dict]:
        """扫全文抽术语表。失败不阻断翻译，退回空表。"""
        fulltext = "\n\n".join(p.text for p in struct.paragraphs)[:TERMS_MAX_CHARS]
        try:
            raw = self.client.chat_json(
                prompts.build_terms_messages(fulltext, max_chars=TERMS_MAX_CHARS),
                max_tokens=2500, timeout=TERMS_TIMEOUT, retries=0,
            )
        except LLMError:
            return []
        return parse_terms(raw)

    # ---------- 单批翻译 ----------

    def _translate_one(self, batch: list, terms: list[dict]) -> dict[int, str]:
        """
        翻一批，返回 {真实段落号: 译文}。

        为什么要给模型「重新从 1 编号」再映射回来：
        补漏的时候，剩下的段落号是 11、12、13、14 这种不连续的。模型看到这四个号，
        会「好心」地理解成第 1、2、3、4 段，输出 id=1..4 —— 我们按原号去取，
        一条都取不到，那几段就永远是「…」。
        发给它连续的 1..N，回来再照位置换回真号，从根上避免这件事；
        提示词里也写明了「id 必须照抄」，但提示词管不住所有模型。
        """
        shims = [_Shim(i + 1, p.text) for i, p in enumerate(batch)]
        in_tokens = sum(estimate_tokens(p.text) for p in batch)
        # 中文输出 token 数通常与英文输入相当或略多，给 1.6 倍余量；上限 8192 防止个别服务限制
        out_tokens = min(8192, max(600, int(in_tokens * 1.6) + 200))
        raw = self.client.chat_json(
            prompts.build_translate_messages(shims, terms),
            max_tokens=out_tokens,
            temperature=0.1,
        )
        got = parse_translations(raw, {s.id for s in shims})
        return {batch[sid - 1].id: zh for sid, zh in got.items() if 0 < sid <= len(batch)}

    # ---------- 主流程 ----------

    def translate_document(self, struct, *, on_batch=None, on_stage=None,
                           terms: list[dict] | None = None) -> TranslationResult:
        """
        翻译整篇。

        on_batch(dict[id -> 译文])：每批完成就回调一次，界面可以边翻边显示
        on_stage(text)：阶段提示（"正在提取术语"、"正在翻译摘要"……）
        """
        result = TranslationResult()

        # 只翻可信段落。置信度低的保持英文原文，宁缺勿错。
        # 注意标题不受长度门槛限制：章节标题天然很短（"Abstract"、"Methods"），
        # 但它们恰恰最需要翻译，否则中文读者不知道这段在讲什么。
        translatable = [
            p for p in struct.paragraphs
            if p.trusted and p.kind in ("body", "heading", "caption", "table_note")
            and (p.kind == "heading" or len(p.text) >= 12)
        ]
        result.skipped_ids = [p.id for p in struct.paragraphs if not p.trusted]

        if not translatable:
            return result

        # 分优先级：摘要和引言先出，使用者能立刻开始读
        priority = [p for p in translatable if p.section in PRIORITY_SECTIONS]
        rest = [p for p in translatable if p.section not in PRIORITY_SECTIONS]

        done = {"n": 0}

        def run(group: list, label: str, term_list: list[dict]):
            batches = make_batches(group, self.chunk_tokens, self.chunk_items)
            if not batches:
                return
            if on_stage:
                on_stage(f"正在翻译{label}（{len(batches)} 批）…")
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {pool.submit(self._translate_one, b, term_list): b for b in batches}
                for fut in as_completed(futures):
                    try:
                        got = fut.result()
                    except LLMError as e:
                        result.errors.append(str(e))
                        continue
                    result.translations.update(got)
                    done["n"] += 1
                    if on_batch:
                        on_batch(got)

        if terms is not None:
            result.terms = terms
            run(priority, "摘要与引言", terms)
            run(rest, "正文其余部分", terms)
        else:
            # 术语表和首批译文**同时**开工。
            # 原来是「先把全文喂进去抽术语，抽完才开始翻第一段」。一旦模型默认带思考，
            # 这一步实测能吃掉 9 分钟，界面上只有一句"正在提取术语"，一个中文都没有 ——
            # 使用者只会以为软件坏了，然后关掉窗口（v1.0 收到的反馈就是这样）。
            # 现在先出摘要和引言，术语表在后台线程里慢慢抽，抽好再用于正文。
            box: dict = {}
            box_lock = threading.Lock()

            def job():
                try:
                    got = self.extract_terms(struct)
                except Exception:
                    got = []
                with box_lock:
                    box["terms"] = got

            th = threading.Thread(target=job, daemon=True)
            th.start()
            if on_stage:
                on_stage("先翻摘要和引言，术语表在后台同时提取…")
            run(priority, "摘要与引言", [])
            th.join(timeout=TERMS_TIMEOUT + 30)      # 抽术语自己带超时，这只是兜底
            terms = box.get("terms") or []
            result.terms = terms
            if on_stage:
                on_stage(f"术语表就绪（{len(terms)} 条），继续翻正文…" if terms
                         else "没提取到术语表，继续翻正文（术语栏这次会是空的）")
            run(rest, "正文其余部分", terms)

        # 3) 补漏：模型偶尔会漏段，对漏掉的再要一次小批
        missing = [p for p in translatable if p.id not in result.translations]
        if missing:
            if on_stage:
                on_stage(f"有 {len(missing)} 段没拿到译文，正在重试…")
            for batch in make_batches(missing, self.chunk_tokens, max(4, self.chunk_items // 2)):
                try:
                    got = self._translate_one(batch, terms)
                except LLMError as e:
                    result.errors.append(str(e))
                    continue
                result.translations.update(got)
                if on_batch:
                    on_batch(got)

        result.missing_ids = [p.id for p in translatable if p.id not in result.translations]
        return result
