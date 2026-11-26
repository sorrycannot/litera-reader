# -*- coding: utf-8 -*-
"""
文献卡片：读完留下东西

需求 D2 的落点。精读真正的痛点在读完一周后 —— 这篇讲了什么、结论有多强、
和另外三篇什么关系。术语库沉淀的是「词」，卡片沉淀的才是「这篇文献本身」。

卡片在读完（翻译完成）时后台自动生成，使用者不需要多做任何动作。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    fingerprint       TEXT PRIMARY KEY,
    filename          TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL DEFAULT '',
    research_question TEXT NOT NULL DEFAULT '',
    methods           TEXT NOT NULL DEFAULT '',
    findings          TEXT NOT NULL DEFAULT '',
    limitations       TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cards_created ON cards(created_at DESC);
"""

FIELDS = ("research_question", "methods", "findings", "limitations")


class CardStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 可重入锁：upsert 在锁内会调用 get 读回结果，普通 Lock 会死锁
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def upsert(self, fingerprint: str, filename: str, card: dict) -> dict | None:
        if not fingerprint:
            return None
        now = datetime.now().isoformat(timespec="seconds")
        vals = {k: str(card.get(k, "") or "").strip() for k in FIELDS}
        title = str(card.get("title", "") or "").strip()

        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint FROM cards WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO cards (fingerprint, filename, title, research_question, methods,"
                    " findings, limitations, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (fingerprint, filename, title, vals["research_question"], vals["methods"],
                     vals["findings"], vals["limitations"], now, now),
                )
            else:
                # 重新生成时整体替换：卡片是「这一篇的当前认识」，不是增量累积
                self._conn.execute(
                    "UPDATE cards SET filename=?, title=?, research_question=?, methods=?,"
                    " findings=?, limitations=?, updated_at=? WHERE fingerprint=?",
                    (filename, title, vals["research_question"], vals["methods"],
                     vals["findings"], vals["limitations"], now, fingerprint),
                )
            self._conn.commit()
        return self.get(fingerprint)

    def get(self, fingerprint: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cards WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def search(self, keyword: str, limit: int = 100) -> list[dict]:
        kw = (keyword or "").strip()
        if not kw:
            return self.list(limit)
        like = f"%{kw}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE title LIKE ? OR research_question LIKE ?"
                " OR methods LIKE ? OR findings LIKE ? OR limitations LIKE ?"
                " ORDER BY updated_at DESC LIMIT ?",
                (like, like, like, like, like, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_title(self, fingerprint: str, title: str) -> bool:
        """单独改标题。早期卡片的标题是拿「第一个 heading」凑的（常常是
        "Methods"/"Resource"），这里用来一次性纠正，不必重新生成整张卡。"""
        if not fingerprint or not title:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET title=? WHERE fingerprint=?", (title, fingerprint))
            self._conn.commit()
            return cur.rowcount > 0

    def delete(self, fingerprint: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM cards WHERE fingerprint=?", (fingerprint,))
            self._conn.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
