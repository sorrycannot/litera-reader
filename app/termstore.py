# -*- coding: utf-8 -*-
"""
术语库：跨文献累积的专业名词

为什么用 SQLite 而不是 JSON
--------------------------
这是使用者唯一会长期积累、而且丢了最心疼的东西。它会从 20 条长到几千条：
  - JSON 每次全量读写，条目一多就明显卡
  - JSON 写到一半断电，整个文件就废了，攒半年的东西一次没
  - SQLite 是 Python 标准库自带（`import sqlite3`，不增加任何依赖），
    有事务（断电不会坏）、单文件（拷贝即备份）、几千几万条毫无压力

合并策略（对应需求「提炼合并，不覆盖」）
--------------------------------------
同一个词在不同文献里被反复遇到时，不能简单覆盖，否则库里会堆满互相矛盾的碎片：
  - 定义取更详细的那一版（明显更长者胜出）
  - 中文译法取最新一次（后出现的通常是在更多上下文中给出的译法）
  - 来源文献追加去重，命中次数累加
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    en          TEXT NOT NULL UNIQUE,     -- 归一化后的英文（小写），用于去重
    display_en  TEXT NOT NULL DEFAULT '', -- 展示用的原始大小写
    zh          TEXT NOT NULL DEFAULT '',
    definition  TEXT NOT NULL DEFAULT '',
    domain      TEXT NOT NULL DEFAULT '',
    hits        INTEGER NOT NULL DEFAULT 1,
    sources     TEXT NOT NULL DEFAULT '[]',
    source_fps  TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_terms_zh ON terms(zh);
CREATE INDEX IF NOT EXISTS idx_terms_hits ON terms(hits DESC);
"""

# 删文献时要能认出「这条术语是哪篇带进来的」。
# sources 里存的是文件名，重名/改名就对不上了，所以另加一列存指纹；
# 老库没有这一列，用 ALTER 补上（SQLite 支持 ADD COLUMN）。
MIGRATIONS = [
    ("source_fps", "ALTER TABLE terms ADD COLUMN source_fps TEXT NOT NULL DEFAULT '[]'"),
]

CSV_HEADER = ["英文", "中文", "定义", "领域", "命中次数", "来源文献"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TermStore:
    """术语库。整个程序共用一个实例。"""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 用 RLock（可重入）而不是 Lock：upsert 在锁内还要调用 get 读回合并结果，
        # 普通 Lock 会在同一线程上二次加锁直接死锁，进程卡住且没有任何报错，极难排查。
        # check_same_thread=False：翻译在后台线程跑，界面在主线程查询，必须允许跨线程
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """老库补列。加过就跳过，重复执行安全。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(terms)").fetchall()}
        for name, sql in MIGRATIONS:
            if name not in cols:
                self._conn.execute(sql)

    # ---------- 写 ----------

    def upsert(self, en: str, zh: str, definition: str = "", *,
               source: str = "", source_fp: str = "", domain: str = "") -> dict | None:
        """
        写入或合并一条术语。返回合并后的条目。

        source / source_fp 是「这条词从哪篇文献来的」：文件名给人看，
        指纹用来在删文献时精确认出它，二者都记，缺一不可。
        """
        en = (en or "").strip()
        zh = (zh or "").strip()
        if not en or not zh:
            return None
        key = re.sub(r"\s+", " ", en.lower())
        now = _now()

        with self._lock:
            row = self._conn.execute("SELECT * FROM terms WHERE en=?", (key,)).fetchone()

            if row is None:
                self._conn.execute(
                    "INSERT INTO terms (en, display_en, zh, definition, domain, hits, sources,"
                    " source_fps, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?,?,?)",
                    (key, en, zh, (definition or "").strip(), domain,
                     json.dumps([source] if source else [], ensure_ascii=False),
                     json.dumps([source_fp] if source_fp else [], ensure_ascii=False),
                     now, now),
                )
                self._conn.commit()
                return self.get(key)

            # 合并：定义取更详细的那版，译法取最新
            old_def = row["definition"] or ""
            new_def = (definition or "").strip()
            final_def = new_def if len(new_def) > len(old_def) * 1.15 else old_def

            sources = self._load_list(row["sources"])
            if source and source not in sources:
                sources.append(source)
            fps = self._load_list(row["source_fps"] if "source_fps" in row.keys() else "[]")
            if source_fp and source_fp not in fps:
                fps.append(source_fp)

            self._conn.execute(
                "UPDATE terms SET display_en=?, zh=?, definition=?, hits=hits+1,"
                " sources=?, source_fps=?, updated_at=? WHERE en=?",
                (en, zh, final_def, json.dumps(sources, ensure_ascii=False),
                 json.dumps(fps, ensure_ascii=False), now, key),
            )
            self._conn.commit()
        return self.get(key)

    @staticmethod
    def _load_list(raw) -> list[str]:
        try:
            got = json.loads(raw or "[]")
            return [str(x) for x in got] if isinstance(got, list) else []
        except Exception:
            return []

    def upsert_many(self, items: list[dict], source: str = "", source_fp: str = "") -> int:
        """批量写入（术语抽取结果直接喂进来）。返回实际写入条数。"""
        n = 0
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if self.upsert(it.get("en", ""), it.get("zh", ""),
                           it.get("definition", ""), source=source, source_fp=source_fp,
                           domain=it.get("domain", "")):
                n += 1
        return n

    # ---------- 读 ----------

    def get(self, en: str) -> dict | None:
        if not en:
            return None
        key = re.sub(r"\s+", " ", en.strip().lower())
        with self._lock:
            row = self._conn.execute("SELECT * FROM terms WHERE en=?", (key,)).fetchone()
        return self._row_to_dict(row) if row else None

    def lookup(self, text: str) -> dict | None:
        """
        划词时先查本地：命中就不必再调 API，既快又省钱。
        依次尝试：原词 → 去标点 → 去复数 → 模糊包含
        """
        t = (text or "").strip().strip(".,;:!?()[]{}\"'“”‘’").lower()
        if not t:
            return None

        candidates = [t]
        if t.endswith("s") and len(t) > 3:
            candidates.append(t[:-1])
        if t.endswith("es") and len(t) > 4:
            candidates.append(t[:-2])
        if t.endswith("ies") and len(t) > 4:
            candidates.append(t[:-3] + "y")

        for c in candidates:
            hit = self.get(c)
            if hit:
                return hit

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM terms WHERE en LIKE ? ORDER BY hits DESC, LENGTH(en) ASC LIMIT 1",
                (f"%{t}%",),
            ).fetchall()
        return self._row_to_dict(rows[0]) if rows else None

    def search(self, keyword: str, limit: int = 50) -> list[dict]:
        kw = (keyword or "").strip()
        with self._lock:
            if not kw:
                rows = self._conn.execute(
                    "SELECT * FROM terms ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                like = f"%{kw}%"
                rows = self._conn.execute(
                    "SELECT * FROM terms WHERE en LIKE ? OR zh LIKE ? OR definition LIKE ?"
                    " ORDER BY hits DESC LIMIT ?", (like, like, like, limit),
                ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]

    def recent(self, limit: int = 50) -> list[dict]:
        return self.search("", limit)

    # ---------- 按文献删除 ----------

    def _mine(self, row: sqlite3.Row, fp: str, filename: str) -> bool:
        """这条术语是不是这篇文献带进来的"""
        fps = self._load_list(row["source_fps"])
        names = self._load_list(row["sources"])
        if fp and fp in fps:
            return True
        # 老数据只有文件名（没有指纹）：只能按名字认
        return bool(filename) and not fps and filename in names

    def source_impact(self, fp: str, filename: str = "") -> dict:
        """
        先算清楚「删这篇会动到多少术语」，给确认框显示，别让人闭着眼睛点。

        total    这篇带进来的术语条目数
        exclusive 只有这篇用过、删了就没人再用的条数（这些会被真正删掉）
        其余的是多篇共用，删文献时只把这篇从「来源」里摘掉，词条留着。
        """
        total = exclusive = 0
        with self._lock:
            rows = self._conn.execute("SELECT sources, source_fps FROM terms").fetchall()
        for row in rows:
            if not self._mine(row, fp, filename):
                continue
            total += 1
            others_fp = [x for x in self._load_list(row["source_fps"]) if x != fp]
            others_name = [x for x in self._load_list(row["sources"]) if x != filename]
            if not others_fp and not others_name:
                exclusive += 1
        return {"total": total, "exclusive": exclusive}

    def delete_by_source(self, fp: str, filename: str = "") -> dict:
        """
        删掉某篇文献对术语库的贡献。

        只有这篇用过的 → 整条删掉；多篇共用的 → 只把这篇从来源里摘掉。
        这样「删掉一篇水刊」不会把别的文献也靠着的译法一起带走。
        """
        removed = unlinked = 0
        with self._lock:
            rows = self._conn.execute("SELECT * FROM terms").fetchall()
            for row in rows:
                if not self._mine(row, fp, filename):
                    continue
                fps = [x for x in self._load_list(row["source_fps"]) if x != fp]
                names = [x for x in self._load_list(row["sources"]) if x != filename]
                if not fps and not names:
                    self._conn.execute("DELETE FROM terms WHERE en=?", (row["en"],))
                    removed += 1
                else:
                    self._conn.execute(
                        "UPDATE terms SET source_fps=?, sources=?, updated_at=? WHERE en=?",
                        (json.dumps(fps, ensure_ascii=False),
                         json.dumps(names, ensure_ascii=False), _now(), row["en"]),
                    )
                    unlinked += 1
            self._conn.commit()
        return {"removed": removed, "unlinked": unlinked}

    # ---------- 导入导出 ----------

    def export_csv(self, path: str | Path) -> Path:
        """
        导出给使用者看、改、发给同门。
        CSV 用 utf-8-sig，Excel 直接双击打开不会乱码。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            rows = self._conn.execute("SELECT * FROM terms ORDER BY hits DESC").fetchall()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            for r in rows:
                w.writerow([r["display_en"] or r["en"], r["zh"], r["definition"],
                            r["domain"], r["hits"], r["sources"]])
        return path

    def import_csv(self, path: str | Path) -> int:
        """
        从 CSV 合并导入。按英文去重，重复的走合并策略而不是覆盖。
        这样别人积累的术语库可以并进自己的。
        """
        path = Path(path)
        n = 0
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if not row or len(row) < 2:
                    continue
                if i == 0:
                    head = row[0].strip()
                    if head in ("英文", "en", "EN", "English"):
                        continue
                en, zh = row[0].strip(), row[1].strip()
                definition = row[2].strip() if len(row) > 2 else ""
                domain = row[3].strip() if len(row) > 3 else ""
                if self.upsert(en, zh, definition, source="导入", domain=domain):
                    n += 1
        return n

    # ---------- 内部 ----------

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            d["sources"] = json.loads(d.get("sources") or "[]")
        except Exception:
            d["sources"] = []
        # 指纹只给内部用（删文献时认来源），但也解析成列表，免得界面上看到一串 JSON
        d["source_fps"] = self._load_list(d.get("source_fps"))
        return d

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
