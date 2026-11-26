# -*- coding: utf-8 -*-
"""
系统文件/文件夹对话框 —— macOS / Linux 实现

macOS 用系统自带的 `osascript` 调 AppleScript 的 choose folder / choose file /
choose file name，弹出来的就是原生对话框（和 Finder 里「打开」一模一样），
而且**零第三方依赖** —— 不用 PyObjC（装了要拖进二十多兆的包），也不用 tkinter
（必须在主线程建窗口，而我们的请求跑在 HTTP 工作线程上，跨线程会随机崩）。

Linux 先看有没有 zenity / kdialog（GNOME、KDE 自带），有就用；都没有就抛
DialogUnavailable，前端会提示「直接粘贴路径」——那里本来就有输入框兜底。

和 Windows 版一致的约定：
    返回 str            用户选了，这是路径
    返回 None           用户取消（不要提示错误）
    抛 DialogUnavailable 对话框压根打不开
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


class DialogUnavailable(Exception):
    """对话框起不来，调用方应提示用户改用手动输入路径"""


# 最后一次调用的原始返回，排查「弹不出来」时看它
LAST: dict = {}


def _rec(key: str, value) -> None:
    LAST[key] = value


def _q(s: str) -> str:
    """把字符串转成 AppleScript 的字面量（转义反斜杠和引号）"""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_cancel(stderr: str) -> bool:
    """
    用户点了取消吗。

    AppleScript 取消会返回 -128（中文系统上那句话是「用户已取消。」），
    所以先认错误码，再看文字 —— 别只看英文 "User canceled"，
    否则中文系统上用户一取消就被当成故障弹错误框。
    """
    s = (stderr or "").lower()
    return "-128" in s or "canceled" in s or "cancelled" in s or "取消" in s


def _osa(script: str, timeout: float = 900.0) -> str | None:
    """跑一段 AppleScript；取消返回 None，真故障抛 DialogUnavailable"""
    _rec("backend", "osascript")
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise DialogUnavailable(f"系统里没有 osascript：{e}") from e
    except subprocess.TimeoutExpired as e:
        raise DialogUnavailable("等待选择超时（太久没有操作）") from e

    _rec("returncode", str(r.returncode))
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        if _is_cancel(err):
            _rec("error", "用户取消")
            return None
        _rec("error", err or f"退出码 {r.returncode}")
        raise DialogUnavailable(err or f"osascript 退出码 {r.returncode}")
    _rec("error", "")
    return (r.stdout or "").strip()


# ---------------- 文件类型 ----------------


def _exts(filetypes: list[tuple[str, str]] | None) -> list[str]:
    """[('PDF 文件', '*.pdf')] → ['pdf']（AppleScript 的 of type 只认扩展名/UTI）"""
    out: list[str] = []
    for _label, pat in filetypes or []:
        for part in str(pat).replace(";", " ").split():
            part = part.strip()
            if part.startswith("*.") and len(part) > 2:
                ext = part[2:].lower()
                if ext not in out:
                    out.append(ext)
    return out


# ---------------- macOS ----------------


def _mac_pick_folder(title: str, initial_dir: str | None) -> str | None:
    parts = [f"choose folder with prompt {_q(title)}"]
    if initial_dir and Path(initial_dir).is_dir():
        parts.append(f"default location (POSIX file {_q(str(initial_dir))})")
    out = _osa(f"set p to POSIX path of ({' '.join(parts)})\nreturn p")
    if out is None:
        return None
    # choose folder 返回的 POSIX path 末尾带 /，去掉；根目录 "/" 保留
    return out.rstrip("/") or "/"


def _mac_open_file(title: str, filetypes, initial_dir: str | None) -> str | None:
    parts = [f"choose file with prompt {_q(title)}"]
    exts = _exts(filetypes)
    if exts:
        parts.append("of type {" + ", ".join(_q(e) for e in exts) + "}")
    if initial_dir and Path(initial_dir).is_dir():
        parts.append(f"default location (POSIX file {_q(str(initial_dir))})")
    out = _osa(f"set f to ({' '.join(parts)})\nreturn POSIX path of f")
    return out or None


def _mac_save_file(title: str, default_name: str, filetypes, initial_dir: str | None) -> str | None:
    # choose file name：选一个「还不存在」的路径，正好是保存对话框的语义
    parts = [f"choose file name with prompt {_q(title)}"]
    if default_name:
        parts.append(f"default name {_q(default_name)}")
    if initial_dir and Path(initial_dir).is_dir():
        parts.append(f"default location (POSIX file {_q(str(initial_dir))})")
    out = _osa(f"set f to ({' '.join(parts)})\nreturn POSIX path of f")
    return out or None


# ---------------- Linux（zenity / kdialog） ----------------


def _gui_tool() -> str | None:
    for name in ("zenity", "kdialog"):
        if shutil.which(name):
            return name
    return None


def _linux_run(args: list[str]) -> str | None:
    _rec("backend", args[0])
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=900.0)
    except FileNotFoundError as e:
        raise DialogUnavailable(f"找不到 {args[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise DialogUnavailable("等待选择超时") from e
    _rec("returncode", str(r.returncode))
    if r.returncode != 0:
        # zenity 取消返回 1；kdialog 取消返回 1
        _rec("error", "用户取消" if r.returncode == 1 else (r.stderr or "").strip())
        return None
    return (r.stdout or "").strip() or None


def _linux_pick_folder(title: str, initial_dir: str | None) -> str | None:
    tool = _gui_tool()
    if not tool:
        raise DialogUnavailable("没找到 zenity / kdialog，无法弹出文件选择框")
    if tool == "zenity":
        args = ["zenity", "--file-selection", "--directory", f"--title={title}"]
        if initial_dir:
            args.append(f"--filename={initial_dir.rstrip('/')}/")
        return _linux_run(args)
    args = ["kdialog", "--getexistingdirectory", initial_dir or str(Path.home()), "--title", title]
    return _linux_run(args)


def _linux_open_file(title: str, filetypes, initial_dir: str | None) -> str | None:
    tool = _gui_tool()
    if not tool:
        raise DialogUnavailable("没找到 zenity / kdialog，无法弹出文件选择框")
    if tool == "zenity":
        args = ["zenity", "--file-selection", f"--title={title}"]
        exts = _exts(filetypes)
        if exts:
            # zenity 的过滤器语法：--file-filter=描述 | *.pdf
            args.append("--file-filter=" + " | ".join(f"*.{e}" for e in exts))
        if initial_dir:
            args.append(f"--filename={initial_dir.rstrip('/')}/")
        return _linux_run(args)
    args = ["kdialog", "--getopenfilename", initial_dir or str(Path.home()), "--title", title]
    return _linux_run(args)


def _linux_save_file(title: str, default_name: str, filetypes, initial_dir: str | None) -> str | None:
    tool = _gui_tool()
    if not tool:
        raise DialogUnavailable("没找到 zenity / kdialog，无法弹出文件选择框")
    start = initial_dir or str(Path.home())
    if default_name:
        start = str(Path(start) / default_name)
    if tool == "zenity":
        args = ["zenity", "--file-selection", "--save", "--confirm-overwrite",
                f"--title={title}", f"--filename={start}"]
        return _linux_run(args)
    args = ["kdialog", "--getsavefilename", start, "--title", title]
    return _linux_run(args)


# ---------------- 对外接口 ----------------


def pick_folder(title: str = "选择文件夹", initial_dir: str | None = None,
                platform: str | None = None) -> str | None:
    """选择文件夹，返回完整路径；用户取消返回 None（platform 只给测试用）"""
    if (sys.platform if platform is None else platform) == "darwin":
        return _mac_pick_folder(title, initial_dir)
    return _linux_pick_folder(title, initial_dir)


def open_file(title: str = "选择文件",
              filetypes: list[tuple[str, str]] | None = None,
              initial_dir: str | None = None,
              platform: str | None = None) -> str | None:
    """打开文件，返回完整路径；用户取消返回 None（platform 只给测试用）"""
    if (sys.platform if platform is None else platform) == "darwin":
        return _mac_open_file(title, filetypes, initial_dir)
    return _linux_open_file(title, filetypes, initial_dir)


def save_file(title: str = "保存文件", default_name: str = "",
              filetypes: list[tuple[str, str]] | None = None,
              initial_dir: str | None = None,
              platform: str | None = None) -> str | None:
    """保存文件，返回完整路径；用户取消返回 None（platform 只给测试用）"""
    if (sys.platform if platform is None else platform) == "darwin":
        return _mac_save_file(title, default_name, filetypes, initial_dir)
    return _linux_save_file(title, default_name, filetypes, initial_dir)


# ---------------- 诊断（前端「对话框体检」按钮用） ----------------


def probe() -> dict:
    """回报这套后端能不能用，不弹窗"""
    out: dict = {"platform": sys.platform}
    if sys.platform == "darwin":
        out["backend"] = "osascript"
        try:
            r = subprocess.run(["osascript", "-e", 'return "ok"'],
                               capture_output=True, text=True, timeout=15)
            out["osascript"] = (r.stdout or "").strip() or (r.stderr or "").strip()
            out["returncode"] = r.returncode
        except Exception as e:
            out["osascript_error"] = str(e)
    else:
        tool = _gui_tool()
        out["backend"] = tool or "none"
        out["gui_tool"] = tool
    return out


def ui_test(timeout_ms: int = 1500) -> dict:
    """轻量自检：只确认命令存在，不真的弹窗（弹窗会卡住 HTTP 线程）"""
    if sys.platform == "darwin":
        ok = shutil.which("osascript") is not None
        return {"ok": ok, "note": "osascript 存在" if ok else "找不到 osascript"}
    tool = _gui_tool()
    return {"ok": tool is not None, "note": f"用 {tool}" if tool else "zenity/kdialog 都没有"}


def debug_try(owner_mode: str = "fg", options: int | None = None,
              initial_dir: str | None = None, title: str = "debug") -> dict:
    """实弹一次（会真的弹窗），用于确认「点了没反应」到底是哪一步坏了"""
    try:
        picked = pick_folder(title, initial_dir=initial_dir)
        return {"ok": True, "picked": picked, "backend": LAST.get("backend"),
                "owner_mode": owner_mode, "options": options}
    except DialogUnavailable as e:
        return {"ok": False, "error": str(e), "backend": LAST.get("backend")}
