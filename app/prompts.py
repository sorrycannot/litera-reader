# -*- coding: utf-8 -*-
"""
提示词模板

集中放这里，方便调优。改提示词不动业务代码。

几条贯穿所有提示词的硬规则，都是为了医学文献的安全性：
  - 乱码和断裂公式必须原样保留，不许猜。模型编一个看起来通顺的假数据，比留个乱码危险得多。
  - 术语全篇统一。同一篇里 hazard ratio 一会儿"风险比"一会儿"危险比率"，读者会怀疑译文。
  - 只输出 JSON，不写解释。否则解析要多写一堆兜底逻辑。
"""

SYSTEM_TRANSLATE = (
    "你是一位专业的医学与生命科学文献翻译，服务于需要精读英文论文的中文研究者。"
    "你的译文准确、通顺，符合中文医学文献的表达习惯，不生硬直译。"
)

TRANSLATE_RULES = """
翻译规则（必须严格遵守）：
1. 逐段翻译，编号一一对应。不许合并段落、不许拆分段落、不许遗漏，顺序不能变。
2. **id 必须原样照抄下面方括号里的数字**。不许重新编号，不许从 1 开始重排。
   比如原文标的是 [37]、[38]，输出里就得是 37、38，不能写成 1、2。
3. 术语必须使用下面给出的对照表，全篇保持一致。表里没有的术语，按你判断的学术通用译法，并保持全篇统一。
4. 以下内容保持英文原样，不要翻译：
   - 基因、蛋白、药物、量表名称（如 IL-6、TNF-α、HAMD-17、metformin）
   - 统计量与数值（如 p < 0.001、HR 1.34、95% CI 1.18-1.52、n = 12,438）
   - 缩写首次出现时写成「中文（英文缩写）」，之后就只用缩写
5. 遇到乱码、断裂的公式、无法辨认的字符，原样保留该片段，绝对不要猜测或补全。
6. 不要添加原文没有的内容，不要写译者注，不要在译文里加解释性括注。
7. 只输出 JSON，不要任何前后说明文字。
"""

TRANSLATE_FORMAT = """
输出格式（严格）：
{"translations":[{"id":1,"zh":"第一段的中文译文"},{"id":2,"zh":"第二段的中文译文"}]}
"""

SYSTEM_TERMS = "你是医学文献的专业术语整理助手，擅长判断哪些术语会真正卡住中文读者。"

TERM_RULES = """
从下面这篇论文里，挑出对中文读者理解最关键的专业术语，数量 15 到 30 个。

挑选标准：
- 领域专有概念，不是普通词汇（"patient"、"study" 这类不要）
- 统计方法学术语优先（如 hazard ratio、propensity score matching、intention-to-treat）
- 缩写词优先，并在定义里给出全称
- 不要收录：作者名、机构名、期刊名、药物商品名、普通形容词

每个术语给出三项：
- en：英文原词，小写（缩写保持大写）
- zh：中文学术通用译法
- definition：一到两句话说清这是什么，不要复述译法，要讲清概念本身

只输出 JSON，不要任何前后说明：
{"terms":[{"en":"hazard ratio","zh":"风险比","definition":"两组发生某结局事件的风险之比……"}]}
"""

SYSTEM_CARD = "你是医学文献的精读助手，擅长把一篇论文压缩成结构化的要点卡片。"

CARD_RULES = """
读完下面这篇论文，生成一张结构化的文献卡片。用中文填写，简明准确。

四个部分：
1. research_question：这篇想回答什么问题（一句话）
2. methods：研究对象与方法。包含人群、样本量、研究设计类型、主要干预或暴露、随访时长、主要终点
3. findings：核心结论。必须带上关键数据（效应量、置信区间、p 值、事件数）
4. limitations：局限性。既包括作者自己承认的，也包括方法学上可质疑的

要求：
- 数字必须来自原文，不许估算或编造。原文没给的数据就写"原文未报告"
- 每部分 1 到 3 句，不要长篇大论
- 如果这篇不是原创研究（比如综述），methods 部分写研究方法学，findings 写主要观点

只输出 JSON：
{"research_question":"","methods":"","findings":"","limitations":""}
"""


def build_terms_messages(fulltext: str, max_chars: int = 24000) -> list[dict]:
    """术语抽取的请求消息。全文太长时只取前 max_chars 个字符，术语分布通常够用。"""
    body = fulltext[:max_chars]
    return [
        {"role": "system", "content": SYSTEM_TERMS},
        {"role": "user", "content": f"{TERM_RULES}\n\n=== 论文正文 ===\n{body}"},
    ]


def build_translate_messages(paragraphs: list, terms: list[dict]) -> list[dict]:
    """
    逐段翻译的请求消息。
    术语表放在前面，让模型先建立译法，再动手翻。
    """
    term_lines = []
    for t in terms:
        en = (t.get("en") or "").strip()
        zh = (t.get("zh") or "").strip()
        if en and zh:
            term_lines.append(f"- {en} → {zh}")
    term_block = "术语对照表：\n" + "\n".join(term_lines) if term_lines else "术语对照表：（本篇暂未提取）"

    para_block = "\n\n".join(f"[{p.id}] {p.text}" for p in paragraphs)

    return [
        {"role": "system", "content": SYSTEM_TRANSLATE},
        {"role": "user", "content": (
            f"{TRANSLATE_RULES}\n\n{term_block}\n\n{TRANSLATE_FORMAT}\n\n"
            f"=== 待翻译段落 ===\n{para_block}"
        )},
    ]


def build_card_messages(fulltext: str, max_chars: int = 30000) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_CARD},
        {"role": "user", "content": f"{CARD_RULES}\n\n=== 论文正文 ===\n{fulltext[:max_chars]}"},
    ]
