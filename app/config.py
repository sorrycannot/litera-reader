# -*- coding: utf-8 -*-
"""
API 配置与界面偏好

对应需求 4.4：
  - 最多 5 套配置，每套含：名称 / 接口地址 / 密钥 / 模型名
  - 可增、可删、可切换当前使用哪套
  - 接口地址不写死，兼容官方与各类中转、校园接口
  - 密钥不硬编码进程序，只存在使用者自己的数据目录里（约束 C6）

为什么地址、模型、密钥要分开存：
  每个人手上的 key 来源不同。有人用 DeepSeek 官方，有人用中转站，
  有人用实验室统一发的接口。三样都是变量，写死任何一样都会让工具在别人手上失效。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_PROFILES = 5

# 新建配置时的默认值：使用者通常只需要填密钥，地址和模型可以直接用
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

DEFAULT_UI = {
    "show_zh": True,        # 译文默认展开（用户明确要求不用点就能看到中文）
    "font_size": 16,
    "sidebar_open": True,
    "zh_only": False,       # 「只看中文」开关
}


class ConfigError(Exception):
    """配置内容不合法"""


class ProfileLimitError(ConfigError):
    """配置套数超过上限"""


@dataclass
class Profile:
    id: str
    name: str
    base_url: str
    api_key: str
    model: str

    def masked_key(self) -> str:
        """脱敏显示，防止截图或录屏时泄露"""
        k = (self.api_key or "").strip()
        if not k:
            return "未填写"
        if len(k) <= 8:
            return "*" * len(k)
        return f"{k[:4]}****{k[-4:]}"

    def is_ready(self) -> bool:
        """三样齐全才算可用"""
        return bool((self.api_key or "").strip()) and bool((self.base_url or "").strip()) and bool(
            (self.model or "").strip()
        )


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _norm_base_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        raise ConfigError("接口地址不能为空")
    if not (u.startswith("http://") or u.startswith("https://")):
        raise ConfigError("接口地址要以 http:// 或 https:// 开头")
    return u


class Config:
    """settings.json 的读写封装。整个程序只用一个实例。"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "settings.json"
        self._data = self._load()
        # 这里**故意不预置任何配置**。
        # 以前会塞一套「DeepSeek 官方」，结果是使用者一打开就看到一套自己没建过、
        # 名字也不是自己起的空配置，既困惑又容易误以为已经配好了。
        # 现在改成空着，由使用者自己新建并命名。

    # ---------- 存取 ----------

    def _load(self) -> dict:
        if self.path.is_file():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    d.setdefault("profiles", [])
                    d.setdefault("ui", dict(DEFAULT_UI))
                    # 补齐新增的界面项，老配置文件也能平滑升级
                    for k, v in DEFAULT_UI.items():
                        d["ui"].setdefault(k, v)
                    return d
            except Exception:
                # 配置文件坏了不要静默清空 —— 先留一份残骸，用户还有救回来的机会
                try:
                    self.path.replace(self.path.with_name("settings.broken.json"))
                except Exception:
                    pass
        return {"version": 1, "profiles": [], "active_profile_id": None, "ui": dict(DEFAULT_UI)}

    def save(self) -> None:
        """原子写：先写临时文件再替换，断电不会留下半截配置"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _seed_if_empty(self) -> None:
        """已废弃：不再预置默认配置。

        留着这个空实现只是为了让旧代码/测试若还调用它不会炸。
        真正的行为是「没有配置就是没有」，由界面上「新建一套」创建。
        """
        return

    # ---------- 配置档 ----------

    def profiles(self) -> list[Profile]:
        return [Profile(**p) for p in self._data.get("profiles", [])]

    def get(self, pid: str) -> Profile | None:
        for p in self.profiles():
            if p.id == pid:
                return p
        return None

    def count(self) -> int:
        return len(self._data.get("profiles", []))

    def active(self) -> Profile | None:
        pid = self._data.get("active_profile_id")
        p = self.get(pid) if pid else None
        if p is None and self.count():
            # 当前指向的配置被删了，退回到第一套
            p = self.profiles()[0]
            self._data["active_profile_id"] = p.id
            self.save()
        return p

    def set_active(self, pid: str) -> Profile:
        p = self.get(pid)
        if p is None:
            raise ConfigError("这套配置不存在")
        self._data["active_profile_id"] = pid
        self.save()
        return p

    def add(self, name: str, base_url: str, api_key: str, model: str) -> Profile:
        if self.count() >= MAX_PROFILES:
            raise ProfileLimitError(f"最多保存 {MAX_PROFILES} 套配置，请先删掉一套再新建")
        clean_name = (name or "").strip()
        if not clean_name:
            # 名字是使用者自己认这套配置的唯一线索，替他想一个只会在列表里造成混乱
            raise ConfigError("给这套配置起个名字，比如「百炼免费额度」")
        p = Profile(
            id=_new_id(),
            name=clean_name,
            base_url=_norm_base_url(base_url),
            api_key=(api_key or "").strip(),
            model=(model or "").strip() or DEFAULT_MODEL,
        )
        self._data["profiles"].append(asdict(p))
        # 新建的立即成为当前使用项，符合直觉
        self._data["active_profile_id"] = p.id
        self.save()
        return p

    def update(self, pid: str, **fields) -> Profile:
        items = self._data.get("profiles", [])
        for i, raw in enumerate(items):
            if raw.get("id") != pid:
                continue
            if "base_url" in fields:
                fields["base_url"] = _norm_base_url(fields["base_url"])
            if "model" in fields and not (fields["model"] or "").strip():
                raise ConfigError("模型名不能为空")
            for k in ("name", "api_key", "model"):
                if k in fields and fields[k] is not None:
                    fields[k] = str(fields[k]).strip()
            raw.update({k: v for k, v in fields.items() if k in raw})
            items[i] = raw
            self._data["profiles"] = items
            self.save()
            return Profile(**raw)
        raise ConfigError("这套配置不存在")

    def delete(self, pid: str) -> None:
        items = [p for p in self._data.get("profiles", []) if p.get("id") != pid]
        if len(items) == len(self._data.get("profiles", [])):
            raise ConfigError("这套配置不存在")
        # 允许删到空：使用者可能就是想重新开始。空着由界面引导新建，
        # 而不是强迫他留着一条不想用的配置。
        self._data["profiles"] = items
        if self._data.get("active_profile_id") == pid:
            self._data["active_profile_id"] = items[0]["id"] if items else None
        self.save()

    # ---------- 界面偏好 ----------

    def ui(self) -> dict:
        return dict(self._data.get("ui", DEFAULT_UI))

    def set_ui(self, **kwargs) -> dict:
        ui = self._data.setdefault("ui", dict(DEFAULT_UI))
        for k, v in kwargs.items():
            if k in DEFAULT_UI:
                ui[k] = v
        self.save()
        return dict(ui)
