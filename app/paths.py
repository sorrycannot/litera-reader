# -*- coding: utf-8 -*-
"""
路径与数据目录管理

对应需求清单的约束：
  C3  数据全部本地，不上云、不共享
  C4  程序与数据分离 —— 程序目录只放代码，数据另存；打包分发不会带出别人的 key
  C5  数据目录用户自选，位置本身记在 AppData 的小指针文件里

目录布局（路径分隔符随平台，Windows 是 \\，macOS/Linux 是 /）：
  <配置目录>/Litera/where.json      指针文件，只记一行「数据目录在哪」
    Windows: %APPDATA%\\Litera
    macOS:   ~/Library/Application Support/Litera
    Linux:   ~/.config/Litera
  <用户选的目录>/settings.json      API 配置（最多 5 套）+ 界面偏好
  <用户选的目录>/terms.db           术语库（SQLite）
  <用户选的目录>/papers/           每篇文献的译文缓存
  <用户选的目录>/export/           导出与备份
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "Litera"

# 常见云同步目录的特征字符串（小写比较）
# 数据库放在这些目录里，同步客户端会在写入途中替换文件，导致损坏或冲突副本
CLOUD_HINTS = (
    "onedrive",
    "dropbox",
    "icloud",
    "nutstore",
    "坚果云",
    "baidunetdisk",
    "百度网盘",
    "google drive",
    "google 云端硬盘",
    "wechat files",
    "wechat_files",
    "微云",
    "synology",
    "nextcloud",
    "owncloud",
    "aliyundrive",
    "阿里云盘",
    "quark",
    "夸克",
)


def appdata_dir(platform: str | None = None) -> Path:
    """
    存放「数据目录指针」的小文件所在的目录。

    Windows：%APPDATA%\\Litera
    macOS：  ~/Library/Application Support/Litera
             （mac 上根本没有 %APPDATA%，旧逻辑会落到 ~/.config/Litera ——
              能用，但 Finder 里翻不到，用户想换数据目录时找不到这个指针文件）
    Linux：  $XDG_CONFIG_HOME/Litera，没有就用 ~/.config/Litera

    platform 只给测试用，正常调用取 sys.platform。
    """
    p = sys.platform if platform is None else platform
    if p == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if p.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    else:
        # Linux/BSD 按 XDG 规范来。这里不读 APPDATA ——
        # WSL 里偶尔会带过来一个 Windows 风格的 APPDATA，照抄会写到奇怪的位置。
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / APP_NAME


def pointer_path() -> Path:
    """指针文件：解决「数据目录在哪」这个先有鸡还是先有蛋的问题"""
    return appdata_dir() / "where.json"


def default_data_dir() -> Path:
    """用户没选之前使用的默认位置（本地磁盘，非同步盘）"""
    return appdata_dir() / "data"


def read_data_dir() -> Path | None:
    """读指针。返回 None 表示还没选过（首次启动）。"""
    p = pointer_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # 指针文件损坏不该让程序起不来，当作首次启动处理
        return None
    raw = (data or {}).get("data_dir")
    return Path(raw) if raw else None


def write_pointer(data_dir: Path) -> None:
    """原子写指针，避免断电留下半截文件"""
    p = pointer_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps({"data_dir": str(data_dir), "version": 1}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def reveal_in_file_manager(path: str | Path, platform: str | None = None) -> None:
    """
    在系统文件管理器里定位到这个目录。

    Windows 用 os.startfile（等同双击），macOS 用 `open -R`（Finder 里选中它），
    Linux 用 xdg-open。三个平台各一句，没有第三方依赖。
    """
    p = sys.platform if platform is None else platform
    target = str(path)
    if p.startswith("win"):
        os.startfile(target)            # noqa: S606 平台专用 API
    elif p == "darwin":
        # -R = reveal，在 Finder 里打开父目录并选中这一项（只给路径则直接打开）
        subprocess.run(["open", "-R", target], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["xdg-open", target], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def is_writable(path: Path) -> bool:
    """真实写一次再删，比只看权限位可靠（U 盘写保护、只读盘都能测出来）"""
    try:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def detect_cloud_sync(path: Path) -> str | None:
    """
    判断路径是否落在云同步目录里。
    命中返回特征词，未命中返回 None。
    注意：只做提示，不强制阻止 —— 用户可能就是想这么干。
    """
    s = str(Path(path).resolve()).lower().replace("\\", "/")
    for hint in CLOUD_HINTS:
        if hint.replace(" ", "") in s.replace(" ", ""):
            return hint
    return None


class Workspace:
    """
    数据目录的运行时封装：负责建目录、给出各类文件的固定路径。
    一次启动只建一个实例，全程序共用。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.papers = self.root / "papers"
        self.export = self.root / "export"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def terms_db(self) -> Path:
        return self.root / "terms.db"

    @property
    def cards_db(self) -> Path:
        return self.root / "cards.db"

    def ensure(self) -> "Workspace":
        """建齐所有子目录，幂等"""
        for d in (self.root, self.papers, self.export):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def paper_dir(self, fingerprint: str) -> Path:
        """按 PDF 内容指纹给每篇文献一个独立缓存目录"""
        d = self.papers / fingerprint
        d.mkdir(parents=True, exist_ok=True)
        return d


def resolve_workspace() -> tuple[Workspace, str]:
    """
    启动时调用：确定数据目录并准备好。
    返回 (Workspace, state)，state 取值：
      "first_run"  首次启动，需要引导用户选目录
      "ok"         正常
      "missing"    指针存在但目录没了（U 盘拔了 / 被删了），需要重新选
      "not_writable" 目录在但写不进去
    """
    root = read_data_dir()
    if root is None:
        return Workspace(default_data_dir()).ensure(), "first_run"

    if not root.exists():
        return Workspace(root), "missing"

    if not is_writable(root):
        return Workspace(root), "not_writable"

    return Workspace(root).ensure(), "ok"
