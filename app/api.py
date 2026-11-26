# -*- coding: utf-8 -*-
"""
前端与 Python 之间的桥接层

pywebview 的 js_api 机制：前端通过 `pywebview.api.方法名(...)` 调用这里的方法，
返回值会被序列化成 Promise 的 resolve 值。

约定：所有方法都返回 {"ok": bool, "data": ..., "error": str}。
前端不用 try/catch 猜哪里出错，只看 ok 就知道成没成，弹出 error 就能说人话。

长任务（翻译）不走返回值：
  js_api 调用会一直挂着等结果，界面就没法显示进度了。
  所以 start_translate 立刻返回，真正的活在后台线程干，
  进度通过 window.__push(事件名, 数据) 推回前端。
"""

from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

from . import prompts
from .cache import PaperCache
from .cards import CardStore
from .config import Config, ConfigError
from .llm import LLMClient, LLMError
from .paths import (Workspace, detect_cloud_sync, resolve_workspace,
                    reveal_in_file_manager, write_pointer)
from .pdf_reader import Para, parse_pdf, render_page, scan_figures, scan_tables
from .termstore import TermStore
from .translator import Translator, parse_json_block


def _ok(data=None) -> dict:
    return {"ok": True, "data": data}


def _err(msg: str) -> dict:
    return {"ok": False, "error": str(msg)}


class Api:
    def __init__(self):
        self.ws: Workspace | None = None
        self.state = "first_run"
        self.config: Config | None = None
        self.terms: TermStore | None = None
        self.cache: PaperCache | None = None
        self.cards: CardStore | None = None

        self.current: dict | None = None       # 当前打开的文献
        self._translating = False
        self._lock = threading.Lock()
        # 事件队列：后台线程只往这里塞，前端轮询取走。
        # 为什么不用 window.evaluate_js 主动推：pywebview 6 + WebView2 在非 UI 线程
        # 调用会抛 "CoreWebView2 can only be accessed from the UI thread"，
        # 而且不同版本行为不一致。队列 + 轮询跟版本无关，最稳。
        self._events: list[dict] = []
        self._ev_lock = threading.Lock()
        # 已经纠正过标题的文献指纹：卡片列表每次刷新都要读 paper.json 才能拿到标题，
        # 纠过一次就不必再读（标题已写回 cards.db）
        self._card_title_fixed: set[str] = set()

    # ================= 基础设施 =================

    def _push(self, event: str, payload):
        """把事件放进队列，等前端来取"""
        with self._ev_lock:
            # 防止异常情况下队列无限增长
            if len(self._events) > 500:
                self._events = self._events[-200:]
            self._events.append({"event": event, "payload": payload})

    def poll_events(self) -> dict:
        """前端定时调用，取走所有待处理事件"""
        with self._ev_lock:
            items = self._events
            self._events = []
        return _ok(items)

    def _open_workspace(self, root: Path) -> None:
        """切换数据目录：把各组件指向新位置"""
        ws = Workspace(root).ensure()
        self.ws = ws
        self.config = Config(ws.root)
        self.terms = TermStore(ws.terms_db)
        self.cache = PaperCache(ws.papers)
        self.cards = CardStore(ws.cards_db)

    def _ensure_ready(self) -> str | None:
        """返回错误信息；None 表示就绪"""
        if self.ws is None or self.config is None:
            return "数据目录还没准备好"
        return None

    # ================= 启动 =================

    def bootstrap(self) -> dict:
        try:
            ws, state = resolve_workspace()
            self.state = state
            self._open_workspace(ws.root)
            return _ok(self._snapshot())
        except Exception as e:
            traceback.print_exc()
            return _err(f"启动失败：{e}")

    def _snapshot(self) -> dict:
        profiles = []
        active = self.config.active()
        for p in self.config.profiles():
            profiles.append({
                "id": p.id,
                "name": p.name,
                "base_url": p.base_url,
                "model": p.model,
                "masked_key": p.masked_key(),
                "ready": p.is_ready(),
                "active": bool(active and active.id == p.id),
            })
        return {
            "state": self.state,
            "dataDir": str(self.ws.root) if self.ws else "",
            "cloudHint": detect_cloud_sync(self.ws.root) if self.ws else None,
            "profiles": profiles,
            "ui": self.config.ui() if self.config else {},
            "termCount": self.terms.count() if self.terms else 0,
            "cardCount": self.cards.count() if self.cards else 0,
            "library": self.cache.list_all(20) if self.cache else [],
        }

    def refresh(self) -> dict:
        return _ok(self._snapshot())

    # ================= 数据目录 =================

    def pick_folder(self) -> dict:
        """
        弹出系统文件夹选择框。

        三种结果要分清楚，前端的表现完全不同：
          - 选中了        → 返回路径
          - 用户取消      → ok(None)，什么都不提示（以前这里会当成故障）
          - 对话框打不开  → 报错，并提示手动粘贴路径（有兜底输入框）
        """
        from . import dialogs
        try:
            here = str(self.ws.root) if self.ws else None
            path = dialogs.pick_folder("选择数据存放位置", initial_dir=here)
            return _ok(path)
        except dialogs.DialogUnavailable as e:
            return _err(f"系统对话框打不开（{e}）。可以直接在下面粘贴路径。")
        except Exception as e:
            return _err(f"打开文件夹选择框失败：{e}")

    def dialog_probe(self, data: dict | None = None) -> dict:
        """
        诊断系统对话框。data 里给了 owner_mode / options / initial_dir 就实弹一次
        （需要外部关闭窗口），否则只回报各环节状态。
        """
        try:
            from . import dialogs
            data = data or {}
            if data.get("try"):
                return _ok(dialogs.debug_try(
                    owner_mode=data.get("owner_mode", "fg"),
                    options=int(data["options"], 0) if data.get("options") else None,
                    initial_dir=data.get("initial_dir") or None,
                    title=data.get("title") or "debug",
                ))
            out = dialogs.probe()
            out["last_call"] = dict(dialogs.LAST)
            out["ui"] = dialogs.ui_test()
            return _ok(out)
        except Exception as e:
            return _err(f"诊断失败：{e}")

    def set_data_dir(self, path: str, migrate: bool = False) -> dict:
        """
        切换数据目录。
        migrate=True 时把现有数据搬过去（复制 → 校验 → 切指针），
        避免用户换了盘发现术语库"消失"。
        """
        try:
            # 用户可能从资源管理器「复制为路径」粘贴，带着引号和首尾空格
            raw = (path or "").strip().strip('"').strip("'").strip()
            if not raw:
                return _err("路径是空的，先选一个文件夹")

            new_root = Path(raw).expanduser()
            try:
                new_root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return _err(f"这个位置建不了文件夹（{e.strerror or e}）。"
                            "换一个本地目录，比如 D:\\文献数据")
            if not new_root.is_dir():
                return _err(f"这不是文件夹：{new_root}")

            cloud = detect_cloud_sync(new_root)
            if migrate and self.ws and self.ws.root.exists():
                old = self.ws.root
                if old.resolve() != new_root.resolve():
                    for sub in ("papers", "export"):
                        src = old / sub
                        if src.is_dir():
                            import shutil
                            shutil.copytree(src, new_root / sub, dirs_exist_ok=True)
                    for f in ("terms.db", "cards.db"):
                        src = old / f
                        if src.is_file():
                            import shutil
                            shutil.copy2(src, new_root / f)

            write_pointer(new_root)
            self._open_workspace(new_root)
            self.state = "ok"
            snap = self._snapshot()
            snap["cloudHint"] = cloud
            return _ok(snap)
        except Exception as e:
            return _err(f"设置数据目录失败：{e}")

    # ================= 配置档 =================

    def save_profile(self, data: dict) -> dict:
        """新增或更新一套 API 配置。data 里有 id 就是更新，没有就是新增。"""
        try:
            pid = (data or {}).get("id")
            fields = {
                "name": data.get("name", ""),
                "base_url": data.get("base_url", ""),
                "api_key": data.get("api_key", ""),
                "model": data.get("model", ""),
            }
            if pid:
                # 密钥留空表示"不修改"，避免脱敏显示后把真 key 覆盖成空
                if not fields["api_key"]:
                    fields.pop("api_key")
                self.config.update(pid, **fields)
            else:
                self.config.add(fields["name"], fields["base_url"],
                                fields["api_key"], fields["model"])
            return _ok(self._snapshot())
        except ConfigError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"保存配置失败：{e}")

    def delete_profile(self, pid: str) -> dict:
        try:
            self.config.delete(pid)
            return _ok(self._snapshot())
        except ConfigError as e:
            return _err(str(e))

    def activate_profile(self, pid: str) -> dict:
        try:
            self.config.set_active(pid)
            return _ok(self._snapshot())
        except ConfigError as e:
            return _err(str(e))

    def test_profile(self, pid: str = "") -> dict:
        """测试连接。不传 id 就测当前使用的那套。"""
        try:
            prof = self.config.get(pid) if pid else self.config.active()
            if not prof:
                return _err("没有可用的配置")
            client = LLMClient.from_profile(prof)
            ok, msg = client.test()
            return _ok({"success": ok, "message": msg})
        except Exception as e:
            return _err(str(e))

    def test_raw(self, base_url: str, api_key: str, model: str) -> dict:
        """
        直接拿界面上填的三个值测，不需要先保存。
        首次引导里用：让使用者填完立刻知道对不对，而不是保存完才发现填错了。
        """
        try:
            client = LLMClient(base_url, api_key, model, timeout=45, max_retries=0)
            ok, msg = client.test()
            return _ok({"success": ok, "message": msg})
        except Exception as e:
            return _err(str(e))

    def set_ui(self, patch: dict) -> dict:
        try:
            self.config.set_ui(**(patch or {}))
            return _ok(self.config.ui())
        except Exception as e:
            return _err(str(e))

    # ================= 打开文献 =================

    def pick_pdf(self) -> dict:
        from . import dialogs
        try:
            path = dialogs.open_file("选择要精读的文献", [("PDF 文件", "*.pdf")])
            if not path:
                return _ok(None)      # 用户取消
            return self.open_pdf(path)
        except dialogs.DialogUnavailable as e:
            return _err(f"系统对话框打不开（{e}）。重启程序再试一次；还不行就把这条提示发我。")
        except Exception as e:
            return _err(f"选择文件失败：{e}")

    def open_pdf(self, path: str) -> dict:
        """
        打开一篇 PDF。命中缓存就直接返回（不再花钱），否则解析后返回英文段落，
        译文由前端另外触发 translate。
        """
        try:
            p = Path(path)
            if not p.is_file():
                return _err("文件不存在")

            fp = None
            # 先算指纹，命中缓存就省掉解析和翻译
            from .pdf_reader import file_fingerprint
            from . import pdf_reader
            pdf_reader.LAST_TABLE_ERROR = None      # 清掉上一次的，别把旧问题带过来
            fp = file_fingerprint(p)
            cached = self.cache.load(fp) if self.cache else None
            if cached:
                self._attach_source(cached, p)
                if not cached.get("title"):
                    cached["title"] = self.cache.title_of(cached)
                self.current = cached
                self._register_terms(cached.get("terms") or [], cached.get("filename", ""),
                                     cached.get("fingerprint", ""))
                return _ok({"fromCache": True, "paper": cached})

            struct = parse_pdf(p)
            paper = {
                "fingerprint": struct.fingerprint,
                "filename": struct.filename,
                "title": struct.title,
                "pages": struct.pages,
                "columns": struct.columns,
                "source_path": str(p),
                "figures": {str(k): v for k, v in struct.figures.items()},
                "tables": {str(k): v for k, v in struct.tables.items()},
                "formulas": {str(k): v for k, v in struct.formulas.items()},
                "paragraphs": [
                    {**q.to_dict(), "zh": ""}
                    for q in struct.paragraphs
                ],
                "terms": [],
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.current = paper
            warns = []
            if pdf_reader.LAST_TABLE_ERROR:
                warns.append(
                    "表格识别没启动（" + pdf_reader.LAST_TABLE_ERROR + "），"
                    "表格里的字可能会混进正文。这是程序环境问题，把这条告诉开发者。"
                )
            return _ok({"fromCache": False, "paper": paper, "warnings": warns})
        except Exception as e:
            traceback.print_exc()
            return _err(f"解析 PDF 失败：{e}")

    def _attach_source(self, paper: dict, path: Path) -> None:
        """
        把原 PDF 的位置、插图区域补进文献对象。

        老缓存里没有这两样，而「最近打开」是直接读缓存的 ——
        不补的话，凡是在加图片功能之前翻过的文章，再打开就没有图，
        用户会以为是新功能坏了。
        """
        paper["source_path"] = str(path)
        if not paper.get("figures"):
            try:
                paper["figures"] = {str(k): v for k, v in scan_figures(path).items()}
            except Exception:
                paper["figures"] = {}
        if not paper.get("tables"):
            try:
                paper["tables"] = {str(k): v for k, v in scan_tables(path).items()}
            except Exception:
                paper["tables"] = {}

    def page_image(self, fingerprint: str, page: int = 1, index: int = -1,
                   table: int = -1, formula: int = -1) -> bytes | None:
        """
        把某页、页内某张插图、某张表格、某块公式渲染成 PNG 字节，给页面里的 <img> 直接取。

        index >= 0 取插图，table >= 0 取表格，formula >= 0 取显示公式，
        都不给就是整页。

        为什么不放进 JSON 里传：一张图几百 KB，塞进接口返回值会让「打开文献」
        变成几十兆的响应。由浏览器对 /pageimg 发 GET，顺便还能用 HTTP 缓存。
        """
        try:
            src = self._pdf_source(str(fingerprint))
            if not src:
                return None
            page_no, idx, tab, fml = int(page), int(index), int(table), int(formula)
            bbox = None
            figs = (self.current or {}).get("figures") or {}
            tabs = (self.current or {}).get("tables") or {}
            fmls = (self.current or {}).get("formulas") or {}
            if tab >= 0:
                want = tabs.get(str(page_no)) or tabs.get(page_no) or []
                if tab >= len(want):
                    return None
                bbox = want[tab]
            elif fml >= 0:
                want = fmls.get(str(page_no)) or fmls.get(page_no) or []
                if fml >= len(want):
                    return None
                bbox = want[fml]
            elif idx >= 0:
                want = figs.get(str(page_no)) or figs.get(page_no) or []
                if idx >= len(want):
                    return None
                bbox = want[idx]
            # 表格上方通常紧贴表标题，外扩量给小一点，别把标题裁进图里；
            # 公式反而要往外放一点，上下标/分数线容易贴边
            pad = 0.5 if tab >= 0 else (2.5 if fml >= 0 else 3.0)
            # 公式是矢量图，按更高 DPI 渲染：一块公式本身就不大，
            # 130dpi 出来只有两三百像素，放大看上下标会糊
            return render_page(src, page_no, bbox, pad=pad, dpi=220 if fml >= 0 else 130)
        except Exception:
            traceback.print_exc()
            return None

    def _pdf_source(self, fingerprint: str) -> Path | None:
        """这篇文献的原 PDF 还在不在。找不到就只是没有图，正文照读。"""
        raw = ""
        if self.current and self.current.get("fingerprint") == fingerprint:
            raw = self.current.get("source_path") or ""
        if not raw and self.cache:
            raw = (self.cache.load(fingerprint) or {}).get("source_path") or ""
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_file() else None

    def page_count(self, fingerprint: str = "") -> dict:
        """诊断用：这篇有没有原 PDF、有几张插图"""
        fp = fingerprint or (self.current or {}).get("fingerprint", "")
        src = self._pdf_source(fp)
        figs = (self.current or {}).get("figures") or {}
        return _ok({
            "fingerprint": fp,
            "source": str(src) if src else "",
            "pages_with_figures": sorted(int(k) for k in figs),
        })

    # ================= 翻译 =================

    def start_translate(self, force: bool = False) -> dict:
        """
        立刻返回，后台线程干活，进度通过 __push 推给界面。

        force=True 表示「重新翻」：跳过「已经翻好了」的检查，术语表也重抽。
        什么时候用得上：换了模型想重看一遍、或者上次翻的结果不满意。
        不预先清空已有译文 —— 万一这次中途失败，旧译文还在，不至于一片空白。
        """
        if self._translating:
            return _err("正在翻译中，请稍候")
        if not self.current:
            return _err("还没有打开文献")
        if force:
            self.current["terms"] = []
        elif all(q.get("zh") for q in self.current.get("paragraphs", [])):
            return _ok({"message": "这篇已经翻好了"})

        prof = self.config.active() if self.config else None
        if not prof or not prof.is_ready():
            return _err("还没有配置好 API。请到设置里填写接口地址、密钥和模型名，先点「测试连接」。")

        with self._lock:
            self._translating = True

        threading.Thread(target=self._translate_worker, args=(prof,), daemon=True).start()
        return _ok({"started": True})

    def _translate_worker(self, prof):
        try:
            paper = self.current
            fp = paper["fingerprint"]

            struct = _StructView(paper)
            client = LLMClient.from_profile(prof)
            tr = Translator(client)

            self._push("progress", {"stage": "正在提取全文术语，锁定译法…", "percent": 2})

            def on_stage(text):
                self._push("progress", {"stage": text})

            def on_batch(got: dict):
                # 逐批把译文推给界面，读者可以边翻边读
                self._push("batch", {"translations": {str(k): v for k, v in got.items()}})

            result = tr.translate_document(struct, on_batch=on_batch, on_stage=on_stage,
                                           terms=list(paper.get("terms") or []) or None)

            # 回填到当前文献对象，供保存与卡片使用
            for q in paper["paragraphs"]:
                if q["id"] in result.translations:
                    q["zh"] = result.translations[q["id"]]
            paper["terms"] = result.terms

            if self.cache:
                self.cache.save(struct, result.translations, result.terms)
            self._register_terms(result.terms, paper["filename"], paper.get("fingerprint", ""))

            summary = {
                "translated": len(result.translations),
                "missing": len(result.missing_ids),
                "skipped": len(result.skipped_ids),
                "terms": len(result.terms),
                "errors": result.errors[:3],
            }
            self._push("done", summary)

            # 翻完顺手生成文献卡片（需求 D2：读完自动留东西，用户不用额外操作）
            self._make_card(prof, paper)
        except LLMError as e:
            self._push("failed", {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._push("failed", {"error": f"翻译过程出错：{e}"})
        finally:
            with self._lock:
                self._translating = False

    def _register_terms(self, terms: list[dict], filename: str, fingerprint: str = ""):
        if self.terms and terms:
            try:
                # 文件名给人看，指纹用来在删文献时精确认出这些词是哪篇带进来的
                self.terms.upsert_many(terms, source=filename, source_fp=fingerprint)
            except Exception:
                pass

    def _make_card(self, prof, paper):
        try:
            if not self.cards:
                return
            if self.cards.get(paper["fingerprint"]):
                return
            fulltext = "\n\n".join(q["text"] for q in paper["paragraphs"])
            client = LLMClient.from_profile(prof)
            raw = client.chat_json(prompts.build_card_messages(fulltext), max_tokens=1500)
            data = parse_json_block(raw)
            if not isinstance(data, dict):
                return
            title = paper.get("title") or ""
            if not title:
                # 没有真标题时退回第一个 heading（多半是 "Methods"，聊胜于无）
                for q in paper["paragraphs"]:
                    if q.get("kind") == "heading":
                        title = q.get("zh") or q.get("text", "")
                        break
            data["title"] = title
            card = self.cards.upsert(paper["fingerprint"], paper["filename"], data)
            self._push("card", card or {})
        except Exception:
            pass   # 卡片失败不影响阅读，静默

    # ================= 术语 =================

    def lookup_term(self, text: str, context: str = "") -> dict:
        """
        划词查询。
        先查本地术语库 —— 命中就不联网，既快又省钱；
        没命中才带上下文去问模型，解释它"在这句话里的意思"。
        """
        text = (text or "").strip()
        if not text:
            return _err("没有选中内容")

        local = self.terms.lookup(text) if self.terms else None
        if local and (not context or len(text) < 40):
            return _ok({"source": "local", **local})

        prof = self.config.active() if self.config else None
        if not prof or not prof.is_ready():
            if local:
                return _ok({"source": "local", **local})
            return _err("没有配置 API，无法查询生词。已收录的词可以直接在右侧术语栏里找。")

        try:
            client = LLMClient.from_profile(prof)
            prompt = (
                "下面是一段英文学术文献，以及其中被读者选中的一个词或短语。\n"
                "请用中文解释它**在这句话里的具体含义**，不是词典释义。\n\n"
                f"被选中的部分：{text}\n\n所在句子或段落：\n{context[:800] or '（未提供上下文）'}\n\n"
                "只输出 JSON：\n"
                '{"en":"规范化后的英文原词","zh":"这里的中文译法或解释","definition":"说明它在这句话里指什么，一到两句"}'
            )
            raw = client.chat_json([{"role": "user", "content": prompt}], max_tokens=400)
            data = parse_json_block(raw)
            if not isinstance(data, dict):
                return _err("模型没有返回可用的解释，换个词再试")

            en = str(data.get("en") or text).strip()
            zh = str(data.get("zh") or "").strip()
            definition = str(data.get("definition") or "").strip()
            if self.terms and zh:
                # 入库做提炼合并：划词结果会转成通用定义，与已有条目合并而非覆盖
                self.terms.upsert(en, zh, definition,
                                  source=self.current["filename"] if self.current else "")
            return _ok({"source": "model", "en": en, "zh": zh, "definition": definition})
        except LLMError as e:
            if local:
                return _ok({"source": "local", **local})
            return _err(str(e))

    def search_terms(self, keyword: str = "") -> dict:
        try:
            return _ok(self.terms.search(keyword, 200) if self.terms else [])
        except Exception as e:
            return _err(str(e))

    def paper_terms(self) -> dict:
        """当前这篇文献的术语，给右侧栏用"""
        terms = (self.current or {}).get("terms") or []
        return _ok(terms)

    def export_terms(self) -> dict:
        try:
            from . import dialogs
            if not self.terms or self.terms.count() == 0:
                return _err("术语库还是空的，先读几篇文献")
            target = dialogs.save_file("导出术语库", "术语库.csv", [("CSV 文件", "*.csv")])
            if not target:
                return _ok(None)
            return _ok(str(self.terms.export_csv(target)))
        except Exception as e:
            return _err(f"导出失败：{e}")

    def import_terms(self) -> dict:
        try:
            from . import dialogs
            path = dialogs.open_file("导入术语库", [("CSV 文件", "*.csv")])
            if not path:
                return _ok(None)
            n = self.terms.import_csv(path)
            return _ok({"imported": n, "total": self.terms.count()})
        except Exception as e:
            return _err(f"导入失败：{e}")

    # ================= 文献库与卡片 =================

    def list_library(self) -> dict:
        try:
            return _ok(self.cache.list_all(200) if self.cache else [])
        except Exception as e:
            return _err(str(e))

    def open_cached(self, fingerprint: str) -> dict:
        try:
            data = self.cache.load(fingerprint) if self.cache else None
            if not data:
                # 版本升级（段落编号规则变了）之后旧缓存读不了。
                # 但里面存着「原 PDF 在哪」—— 有路径就用新规则重新解析，
                # 不能让使用者因为一次升级而丢掉这篇文献的记录。
                old = self.cache.load_any(fingerprint) if self.cache else None
                src = (old or {}).get("source_path") or ""
                if src and Path(src).is_file():
                    res = self.open_pdf(src)
                    if res.get("ok"):
                        res["data"]["notice"] = (
                            "这篇是按旧版规则切的段，已经用新规则重新解析过了，"
                            "需要重新翻一次（旧译文对不上新的段落编号）。"
                        )
                        res["data"]["needs_translate"] = True
                    return res
                return _err("这篇的缓存不在了，原文也找不到了，重新导入 PDF 吧")
            src = data.get("source_path") or ""
            if src and Path(src).is_file():
                self._attach_source(data, Path(src))
            # 版本对得上的老缓存没有标题字段：读的时候补一次，页头别再显示文件名
            if not data.get("title") and self.cache:
                data["title"] = self.cache.title_of(data)
            self.current = data
            return _ok({"fromCache": True, "paper": data})
        except Exception as e:
            return _err(str(e))

    def list_cards(self, keyword: str = "") -> dict:
        try:
            rows = self.cards.search(keyword, 200) if self.cards else []
            # 早期卡片的标题是拿「第一个 heading」凑的 —— 翻出来是 "Methods"、"Resource"、
            # "ASurveyofAIVirtualCells" 这种。这里用缓存里的真标题纠正一次并写回库。
            for c in rows:
                fp = c.get("fingerprint") or ""
                if not fp or not self.cache or fp in self._card_title_fixed:
                    continue
                data = self.cache.load_any(fp) or {}
                real = (data.get("title") or "") or self.cache.title_of(data)
                if real:
                    if real != (c.get("title") or ""):
                        self.cards.set_title(fp, real)
                        c["title"] = real
                    self._card_title_fixed.add(fp)
            return _ok(rows)
        except Exception as e:
            return _err(str(e))

    # ---------- 删除一篇文献 ----------

    def paper_delete_info(self, fingerprint: str) -> dict:
        """
        删除前先算家底：这篇带进来多少术语、其中多少只有这篇用过。

        为什么要先问一次：删除是不可逆的，而「术语」是跨文献积累的资产 ——
        删文献要不要连术语一起删，得让人看着数字自己决定。
        """
        try:
            data = self.cache.load_any(fingerprint) if self.cache else None
            if not data:
                return _err("这篇的记录已经不在了")
            filename = data.get("filename", "")
            impact = self.terms.source_impact(fingerprint, filename) if self.terms else \
                {"total": 0, "exclusive": 0}
            title = ""
            title = data.get("title") or ""
            if not title:
                # 老缓存没有 title：退回第一个 heading（多半是 "Methods"，聊胜于无）
                for q in data.get("paragraphs") or []:
                    if q.get("kind") == "heading":
                        title = (q.get("zh") or q.get("text") or "")[:120]
                        break
            return _ok({
                "fingerprint": fingerprint,
                "filename": filename,
                "title": title,
                "pages": data.get("pages", 0),
                "translated": sum(1 for q in data.get("paragraphs") or [] if q.get("zh")),
                "terms_total": impact["total"],
                "terms_exclusive": impact["exclusive"],
                "open_now": bool(self.current and self.current.get("fingerprint") == fingerprint),
                "translating": bool(self._translating and self.current
                                    and self.current.get("fingerprint") == fingerprint),
            })
        except Exception as e:
            return _err(str(e))

    def delete_paper(self, fingerprint: str, with_terms: bool = False) -> dict:
        """
        删掉一篇文献在本机的记录。

        with_terms=False：只删译文/插图缓存和文献卡片，术语库原样保留。
        with_terms=True ：连同它带进术语库的词一起删 —— 但只删「只有这篇用过」的那些，
                          多篇共用的词只把这篇从来源里摘掉。
        原 PDF 永远不动（那是你的文件，程序只管自己的缓存）。
        """
        try:
            data = self.cache.load_any(fingerprint) if self.cache else None
            if not data:
                return _err("这篇的记录已经不在了")
            if self._translating and self.current and \
                    self.current.get("fingerprint") == fingerprint:
                return _err("这篇正在翻译，等它翻完再删（免得后台又把它写回来）")

            filename = data.get("filename", "")
            terms_removed = terms_unlinked = 0
            if with_terms and self.terms:
                got = self.terms.delete_by_source(fingerprint, filename)
                terms_removed, terms_unlinked = got["removed"], got["unlinked"]

            deleted = self.cache.delete(fingerprint) if self.cache else False
            if self.cards:
                try:
                    self.cards.delete(fingerprint)
                except Exception:
                    pass
            if self.current and self.current.get("fingerprint") == fingerprint:
                self.current = None
            return _ok({
                "deleted": deleted,
                "filename": filename,
                "terms_removed": terms_removed,
                "terms_unlinked": terms_unlinked,
                "terms_left": self.terms.count() if self.terms else 0,
            })
        except Exception as e:
            traceback.print_exc()
            return _err(f"删除失败：{e}")

    def delete_card(self, fingerprint: str) -> dict:
        try:
            ok = self.cards.delete(fingerprint) if self.cards else False
            return _ok({"deleted": ok})
        except Exception as e:
            return _err(str(e))

    # ================= 杂项 =================

    def open_data_dir(self) -> dict:
        """在文件管理器里打开数据目录，方便用户备份"""
        try:
            if not self.ws:
                return _err("数据目录还没准备好")
            # 三平台各一句（Windows startfile / mac open -R / Linux xdg-open）
            reveal_in_file_manager(self.ws.root)
            return _ok(True)
        except Exception as e:
            return _err(f"打开目录失败：{e}")


class _StructView:
    """
    把缓存里的字典包装成 Translator 需要的对象形状。
    Translator 只用到 fingerprint / paragraphs（每段有 id/text/kind/section/trusted/flags）。
    """

    def __init__(self, paper: dict):
        self.fingerprint = paper["fingerprint"]
        self.filename = paper.get("filename", "")
        # 翻译完成后要回写缓存，标题得原样带回去，否则重开时标题又变成文件名
        self.title = paper.get("title", "")
        self.pages = paper.get("pages", 0)
        self.columns = paper.get("columns", 1)
        self.body_size = 9.0
        # 原 PDF 路径与插图/表格区域：cache.save 要用它们，否则翻完存下来的缓存没有图
        self.path = paper.get("source_path", "")
        self.figures = paper.get("figures") or {}
        self.tables = paper.get("tables") or {}
        self.formulas = paper.get("formulas") or {}
        self.paragraphs = [
            Para(
                id=q["id"], page=q.get("page", 1), text=q["text"],
                size=9.0, kind=q.get("kind", "body"), section=q.get("section", ""),
                flags=list(q.get("flags") or []),
                table_index=q.get("table_index", -1),
                formula_index=q.get("formula_index", -1),
                page_from=q.get("page_from") or q.get("page", 1),
                page_to=q.get("page_to") or q.get("page", 1),
            )
            for q in paper.get("paragraphs", [])
        ]
