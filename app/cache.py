# -*- coding: utf-8 -*-
"""
译文缓存：同一篇文献不重复计费

按 PDF 内容指纹索引，不按文件名。改名、换目录、从别人文件夹拷来，
只要内容一样就命中同一份缓存 —— 不会让你为一篇文章付两次钱。

顺带解决另一件事：断网时，翻过的文章还能正常读。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

CACHE_VERSION = 18  # 18：图区不再吞掉另一栏正文（半页宽的图）+ 页内高亮块不算图元素 + 图注边界按「引用句」细分 + 行内小标题不再切开段落 + 首字下沉并回正文


class PaperCache:
    def __init__(self, papers_dir: str | Path):
        self.root = Path(papers_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        # 老缓存的标题补算结果（按指纹缓存）。只放内存：解析首页几十毫秒，
        # 每次刷新列表都重算一遍不划算，写回磁盘又没必要 —— 打开一次就会写正式的 title。
        self._titles: dict[str, str] = {}

    # ---------- 标题 ----------

    def title_of(self, data: dict) -> str:
        """缓存里的文章标题；老缓存当场从 PDF 首页补认一次"""
        t = (data.get("title") or "").strip()
        if t:
            return t
        fp = data.get("fingerprint") or ""
        if fp and fp in self._titles:
            return self._titles[fp]
        src = data.get("source_path") or ""
        got = ""
        if src and Path(src).is_file():
            try:
                import pymupdf
                from .pdf_reader import extract_title
                doc = pymupdf.open(src)
                try:
                    got = extract_title(doc, float(data.get("body_size") or 9.0))
                finally:
                    doc.close()
            except Exception:
                got = ""
        if fp:
            self._titles[fp] = got
        return got

    # ---------- 路径 ----------

    def _file(self, fingerprint: str) -> Path:
        return self.root / fingerprint / "paper.json"

    def has(self, fingerprint: str) -> bool:
        return self._file(fingerprint).is_file()

    # ---------- 存取 ----------

    def save(self, struct, translations: dict[int, str], terms: list[dict] | None = None,
             source_path: str | None = None) -> Path:
        """
        保存整篇结果。段落原文与译文合并在一起存，
        这样打开时一次读取就够，不用再跑一遍 PDF 解析。

        source_path 也要存：阅读页要按需把原 PDF 的页面/插图渲染出来，
        没有这个路径，翻过的文献再看就只剩干巴巴的文字。
        """
        fp = struct.fingerprint
        target = self._file(fp)
        target.parent.mkdir(parents=True, exist_ok=True)

        paragraphs = []
        for p in struct.paragraphs:
            paragraphs.append({
                "id": p.id,
                "page": p.page,
                "section": p.section,
                "kind": p.kind,
                "flags": p.flags,
                "text": p.text,
                "zh": translations.get(p.id, ""),
                # 表格/表注要靠它找到自己对应哪张表，丢了索引，重开缓存就摆错位置
                "table_index": getattr(p, "table_index", -1),
                # 公式同理：前端按这个索引去 /pageimg 取那一块截图
                "formula_index": getattr(p, "formula_index", -1),
                # 跨页合段的起止页：重开缓存也要能说明「这段从第 3 页接到第 4 页」
                "page_from": getattr(p, "page_from", 0) or p.page,
                "page_to": getattr(p, "page_to", 0) or p.page,
            })

        payload = {
            "version": CACHE_VERSION,
            "fingerprint": fp,
            "filename": struct.filename,
            "title": getattr(struct, "title", "") or "",
            "pages": struct.pages,
            "columns": struct.columns,
            "body_size": struct.body_size,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_path": source_path or getattr(struct, "path", "") or "",
            "figures": {str(k): v for k, v in (getattr(struct, "figures", {}) or {}).items()},
            "tables": {str(k): v for k, v in (getattr(struct, "tables", {}) or {}).items()},
            "formulas": {str(k): v for k, v in (getattr(struct, "formulas", {}) or {}).items()},
            "terms": terms or [],
            "paragraphs": paragraphs,
        }

        tmp = target.with_name("paper.json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
        return target

    def load(self, fingerprint: str) -> dict | None:
        f = self._file(fingerprint)
        if not f.is_file():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict) or "paragraphs" not in data:
            return None
        # 段落编号规则变过（首页作者单位、表格不再计入正文），旧编号对不上译文，
        # 硬读会把上一段的译文挂到下一段上 —— 宁可重解析一次。
        if int(data.get("version", 1)) < CACHE_VERSION:
            return None
        return data

    def load_any(self, fingerprint: str) -> dict | None:
        """
        不管版本，能读就读。

        用来救旧缓存：版本升级后 load() 会拒读，但里面还存着「原 PDF 在哪」——
        有这个路径就能重新解析，使用者不会因为升级而丢掉这篇文献的记录。
        """
        f = self._file(fingerprint)
        if not f.is_file():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def delete(self, fingerprint: str) -> bool:
        d = self.root / fingerprint
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            return True
        return False

    # ---------- 列表 ----------

    def list_all(self, limit: int = 100) -> list[dict]:
        """
        已缓存文献的概要列表，用于「最近打开」。
        只读每个文件的开头一部分，不把整篇译文读进内存。
        """
        out = []
        if not self.root.is_dir():
            return out
        for d in self.root.iterdir():
            if not d.is_dir():
                continue
            f = d / "paper.json"
            if not f.is_file():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                paras = data.get("paragraphs") or []
                # 老缓存没有 title 字段：当场从 PDF 首页补认；实在认不出才退回
                # 「第一个 heading」（那常常是 "Methods"/"Resource"）
                title = self.title_of(data)
                if not title:
                    for p in paras:
                        if p.get("kind") == "heading":
                            title = p.get("text", "")[:120]
                            break
                out.append({
                    "fingerprint": data.get("fingerprint", d.name),
                    "filename": data.get("filename", ""),
                    "title": title,
                    "pages": data.get("pages", 0),
                    "paragraphs": len(paras),
                    "translated": sum(1 for p in paras if p.get("zh")),
                    "created_at": data.get("created_at", ""),
                })
            except Exception:
                continue
        out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return out[:limit]
