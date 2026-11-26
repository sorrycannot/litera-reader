# -*- coding: utf-8 -*-
"""
系统文件/文件夹对话框 —— Windows 实现（原生 API，纯 ctypes，无第三方依赖）

只在 Windows 上被导入（见 dialogs.py 的分发逻辑）；macOS/Linux 走 dialogs_posix.py。


为什么不用 tkinter.filedialog
------------------------------
API 请求跑在 HTTP 工作线程上，而 tkinter 的窗口必须在主线程创建，
跨线程调用会随机崩。Windows 原生对话框没有这个限制。

为什么不用 pywebview 的对话框
------------------------------
已放弃 pywebview（.NET 不支持中文路径），见 server.py 顶部说明。

踩过的两个坑（改代码前务必看）
------------------------------
1. **指针必须显式声明 64 位**。
   ctypes 默认把函数返回值当 C int（32 位）。shell32.SHBrowseForFolderW 返回的是
   64 位 PIDL 指针，不声明 restype 就会被截断成垃圾值：
     - SHGetPathFromIDListW 拿到垃圾 → 返回失败 → 用户选了文件夹却拿不到路径
     - CoTaskMemFree 拿垃圾去释放内存 → 堆损坏，程序可能直接崩
   所以这里的每个函数都写了 argtypes / restype。

2. **对话框没有 owner 就会被浏览器窗口挡在下面**。
   我们的界面是 Edge 应用模式窗口，点按钮时它在前台；对话框若 hwndOwner=None，
   Windows 就把它放到底层，用户只会觉得"点了没反应"。
   现在统一把 owner 设为当前前台窗口（也就是应用窗口），并在对话框出现后
   再抢一次前台，双保险。

  python dev/test_dialog.py     验证对话框能否弹出（不依赖人工点击）
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)

# ---------------- 基础常量 ----------------

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1
RPC_E_CHANGED_MODE = 0x80010106

S_OK = 0
S_FALSE = 1

# IFileDialog 选项
FOS_OVERWRITEPROMPT = 0x00000002
FOS_STRICTFILETYPES = 0x00000004
FOS_NOCHANGEDIR = 0x00000008
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
FOS_ALLNONSTORAGEITEMS = 0x00000080
FOS_ALLOWMULTISELECT = 0x00000200
FOS_PATHMUSTEXIST = 0x00000800
FOS_FILEMUSTEXIST = 0x00001000
FOS_DONTADDTORECENT = 0x02000000

# 老式打开/保存对话框
OFN_EXPLORER = 0x00080000
OFN_FILEMUSTEXIST = 0x00001000
OFN_PATHMUSTEXIST = 0x00000800
OFN_NOCHANGEDIR = 0x00000008
OFN_OVERWRITEPROMPT = 0x00000002
OFN_HIDEREADONLY = 0x00000004

# 老式文件夹选择框
BIF_RETURNONLYFSDIRS = 0x00000001
BIF_EDITBOX = 0x00000010
BIF_NEWDIALOGSTYLE = 0x00000040
BIF_USENEWUI = BIF_EDITBOX | BIF_NEWDIALOGSTYLE

SIGDN_FILESYSPATH = 0x80058000

HRESULT_CANCELLED = 0x800704C7   # ERROR_CANCELLED，用户点了取消


class DialogUnavailable(Exception):
    """现代对话框用不了（COM 起不来、接口调用失败），调用方应退回老接口"""


# 最后一次对话框调用各步骤的真实返回。打包版上排查「弹不出来」全靠它。
LAST: dict = {}


def _rec(key: str, value) -> None:
    LAST[key] = value if isinstance(value, str) else f"0x{int(value) & 0xffffffff:08X}"


# ---------------- 结构体 ----------------


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    """把 "{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}" 转成 GUID 结构"""
    t = text.strip("{}")
    a, b, c, d, e = t.split("-")
    g = GUID()
    g.Data1 = int(a, 16)
    g.Data2 = int(b, 16)
    g.Data3 = int(c, 16)
    rest = d + e
    for i in range(8):
        g.Data4[i] = int(rest[i * 2:i * 2 + 2], 16)
    return g


CLSID_FileOpenDialog = _guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")
IID_IFileDialog = _guid("42f85136-db7e-439c-85f1-e4075d135fc8")
IID_IShellItem = _guid("43826d1e-e718-42ee-bc55-a1e261c37bfe")


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", ctypes.c_void_p),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


# ---------------- 函数签名（防指针截断，别删） ----------------

ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None
ole32.OleInitialize.argtypes = [ctypes.c_void_p]
ole32.OleInitialize.restype = ctypes.c_long
ole32.OleUninitialize.argtypes = []
ole32.OleUninitialize.restype = None

shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
shell32.SHBrowseForFolderW.restype = ctypes.c_void_p          # ← 64 位指针
shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
shell32.ILCreateFromPathW.restype = ctypes.c_void_p           # ← 64 位指针
shell32.SHCreateItemFromParsingName.argtypes = [
    wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID),
    ctypes.POINTER(ctypes.c_void_p),
]
shell32.SHCreateItemFromParsingName.restype = ctypes.c_long

comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
comdlg32.GetSaveFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
comdlg32.GetSaveFileNameW.restype = wintypes.BOOL

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD


# ---------------- COM 虚表调用 ----------------


def _vt(ptr, index: int, restype, *argtypes):
    """
    取 COM 接口指针 ptr 虚表中第 index 个方法，包成可调用对象。
    参数：restype 是返回类型，argtypes 是「除 this 指针外」的参数类型。
    """
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    addr = vtbl[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(addr)


def _release(ptr):
    if ptr:
        try:
            _vt(ptr, 2, ctypes.c_ulong)(ptr)
        except Exception:
            pass


class _Com:
    """在调用线程上初始化 COM（STA），退出时反初始化"""

    def __enter__(self):
        self.hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        # RPC_E_CHANGED_MODE：线程已经是 MTA，继续用（Shell 对话框一般也能跑）
        return self

    def __exit__(self, *exc):
        if self.hr in (S_OK, S_FALSE):
            ole32.CoUninitialize()
        return False


def _on_ui_thread(fn):
    """
    在一个**全新的 STA 线程**上执行 fn，等它结束。

    为什么必须这样（踩过，别再改回去）：
      HTTP 请求跑在 ThreadingHTTPServer 的工作线程里。这些线程的 COM 单元状态
      由 Python 线程池决定，时而 STA 时而 MTA，还带着上一次调用留下的痕迹。
      直接把 IFileDialog::Show 放在这种线程上，会出现「第一次能弹、第二次返回
      0x80004005 (E_FAIL)」这种随机故障 —— 而且 SHBrowseForFolder 作为兜底也
      一起失效（BIF_NEWDIALOGSTYLE 还要求 OleInitialize，不只是 CoInitialize）。

      自己起一个干净的线程 + OleInitialize，行为就可复现了。
    """
    box: dict = {}

    def worker():
        hr = ole32.OleInitialize(None)     # STA + OLE，文件对话框的标准前置条件
        try:
            box["value"] = fn()
        except BaseException as e:          # 包括 DialogUnavailable，交给调用方判断
            box["error"] = e
        finally:
            if hr in (S_OK, S_FALSE):
                ole32.OleUninitialize()

    t = threading.Thread(target=worker, name="dialog-ui", daemon=True)
    t.start()
    t.join()

    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------- 窗口辅助 ----------------


def _foreground_window() -> int | None:
    """
    当前前台窗口，用作对话框的 owner。

    必须校验：如果前台窗口已经销毁（或拿到的是别的进程残留的句柄），
    把无效 HWND 传给 IFileDialog::Show 会直接返回 0x80004005 (E_FAIL)，
    表现就是「点了没反应」。宁可不指定 owner，也不能传个坏的。
    """
    h = user32.GetForegroundWindow()
    if not h:
        return None
    if not user32.IsWindow(h) or not user32.IsWindowVisible(h):
        return None
    cls = ctypes.create_unicode_buffer(64)
    if user32.GetClassNameW(h, cls, 64) > 0 and cls.value == "#32770":
        return None          # 自己弹的对话框，不能当新对话框的 owner
    return int(h)


def _find_dialog_of_this_process() -> int | None:
    """找本进程里可见的标准对话框窗口（类名 #32770）"""
    pid = os.getpid()
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(64)
        if user32.GetClassNameW(hwnd, buf, 64) <= 0:
            return True
        if buf.value != "#32770":
            return True
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            found.append(int(hwnd))
            return False
        return True

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(CB(cb), 0)
    return found[0] if found else None


def _raise_when_shown():
    """
    对话框出现后把它抢到前台。

    为什么需要：owner 已设为前台窗口，正常情况下足够了；
    但用户可能刚好把焦点切走（比如点了别的程序），此时对话框会藏在后面，
    看起来就像"按钮点了没反应"。这里起个短命线程补一刀。
    """
    def worker():
        for _ in range(40):          # 最多等 4 秒
            time.sleep(0.1)
            h = _find_dialog_of_this_process()
            if h:
                try:
                    user32.SetForegroundWindow(h)
                    user32.BringWindowToTop(h)
                except Exception:
                    pass
                return

    threading.Thread(target=worker, daemon=True).start()


def _build_filter(filetypes: list[tuple[str, str]]) -> ctypes.Array:
    """
    拼 Win32 要求的过滤器字符串：显示名\\0通配符\\0...\\0\\0
    用 buffer 而不是 str，避免嵌入的 NUL 被当成字符串结尾。
    """
    parts = []
    for label, pattern in filetypes:
        parts.append(f"{label} ({pattern})")
        parts.append(pattern)
    parts.append("所有文件 (*.*)")
    parts.append("*.*")
    text = "\0".join(parts) + "\0\0"
    return ctypes.create_unicode_buffer(text, len(text) + 1)


# ---------------- 现代接口（IFileDialog） ----------------


def _ifile_dialog(owner, title: str, options: int,
                  filetypes: list[tuple[str, str]] | None,
                  default_name: str = "",
                  initial_dir: str | None = None) -> str | None:
    """
    用 IFileDialog 弹对话框，返回路径；取消/失败返回 None。
    它比老式 GetOpenFileName 好在：能直接选文件夹（FOS_PICKFOLDERS），
    而且原生支持 owner 窗口，不会被挡。
    """
    pfd = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(CLSID_FileOpenDialog), None, CLSCTX_INPROC_SERVER,
        ctypes.byref(IID_IFileDialog), ctypes.byref(pfd),
    )
    if hr != S_OK or not pfd:
        raise DialogUnavailable(f"CoCreateInstance 失败 0x{hr & 0xffffffff:08X}")

    try:
        _vt(pfd, 9, ctypes.c_long, ctypes.c_uint32)(pfd, ctypes.c_uint32(options))
        _vt(pfd, 17, ctypes.c_long, wintypes.LPCWSTR)(pfd, title)

        # 打开时停在指定目录，省得用户从头找
        if initial_dir:
            folder = ctypes.c_void_p()
            hr0 = shell32.SHCreateItemFromParsingName(
                initial_dir, None, ctypes.byref(IID_IShellItem), ctypes.byref(folder)
            )
            if hr0 == S_OK and folder:
                try:
                    _vt(pfd, 12, ctypes.c_long, ctypes.c_void_p)(pfd, folder)   # SetFolder
                finally:
                    _release(folder)

        if filetypes:
            # COMDLG_FILTERSPEC 数组：SetFileTypes(pfd, n, array)
            class FILTERSPEC(ctypes.Structure):
                _fields_ = [("pszName", wintypes.LPCWSTR), ("pszSpec", wintypes.LPCWSTR)]

            arr = (FILTERSPEC * len(filetypes))()
            keep = []                               # 保引用，别让字符串被回收
            for i, (label, pattern) in enumerate(filetypes):
                a = ctypes.create_unicode_buffer(label)
                b = ctypes.create_unicode_buffer(pattern)
                keep.extend([a, b])
                arr[i].pszName = ctypes.cast(a, wintypes.LPCWSTR)
                arr[i].pszSpec = ctypes.cast(b, wintypes.LPCWSTR)
            _vt(pfd, 4, ctypes.c_long, ctypes.c_uint32,
                ctypes.POINTER(FILTERSPEC))(pfd, len(filetypes), arr)
            _vt(pfd, 5, ctypes.c_long, ctypes.c_uint32)(pfd, 1)

        if default_name:
            _vt(pfd, 15, ctypes.c_long, wintypes.LPCWSTR)(pfd, default_name)

        _raise_when_shown()
        # owner 可能刚好在这几百毫秒里失效（用户切走了窗口、应用被关），
        # 无效 HWND 会让 Show 直接 E_FAIL。失败时不带 owner 再试一次。
        hr, hru = 0, 0
        for use_owner in (owner, None):
            hr = _vt(pfd, 3, ctypes.c_long, wintypes.HWND)(pfd, use_owner or None)
            hru = hr & 0xffffffff
            _rec("show", hru)
            if hru == HRESULT_CANCELLED:
                return None                  # 用户取消，不是故障
            if hr >= 0:
                break
        if hru == HRESULT_CANCELLED:
            return None
        if hr < 0:
            raise DialogUnavailable(f"Show 失败 0x{hru:08X}")

        item = ctypes.c_void_p()
        hr = _vt(pfd, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(pfd, ctypes.byref(item))
        if hr != S_OK or not item:
            raise DialogUnavailable(f"GetResult 失败 0x{hr & 0xffffffff:08X}")
        try:
            pw = ctypes.c_wchar_p()
            # IShellItem 虚表：0-2 IUnknown, 3 BindToHandler, 4 GetParent,
            # 5 GetDisplayName —— 别写成 4，那是 GetParent（会往我们的输出指针写垃圾）
            hr = _vt(item, 5, ctypes.c_long, ctypes.c_uint32,
                     ctypes.POINTER(ctypes.c_wchar_p))(item, SIGDN_FILESYSPATH, ctypes.byref(pw))
            if hr != S_OK or not pw.value:
                raise DialogUnavailable(f"GetDisplayName 失败 0x{hr & 0xffffffff:08X}")
            path = pw.value
            ole32.CoTaskMemFree(ctypes.cast(pw, ctypes.c_void_p))
            return path
        finally:
            _release(item)
    finally:
        _release(pfd)


# ---------------- 老式接口（兜底） ----------------


def _browse_folder_legacy(owner, title: str) -> str | None:
    """SHBrowseForFolder：老式树形选择框，作为 IFileDialog 失败时的兜底"""
    display = ctypes.create_unicode_buffer(260)
    bi = BROWSEINFOW()
    bi.hwndOwner = owner or None
    bi.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
    bi.lpszTitle = title
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_USENEWUI

    _raise_when_shown()
    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    _rec("legacy_pidl", 1 if pidl else 0)
    if not pidl:
        _rec("legacy_err", ctypes.get_last_error())
        return None
    try:
        path_buf = ctypes.create_unicode_buffer(1024)
        if shell32.SHGetPathFromIDListW(pidl, path_buf):
            return path_buf.value
        return None
    finally:
        ole32.CoTaskMemFree(pidl)      # pidl 是 CoTaskMemAlloc 分配的


def _open_legacy(owner, title: str, filetypes: list[tuple[str, str]]) -> str | None:
    filt = _build_filter(filetypes)
    buf = ctypes.create_unicode_buffer(1024)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = owner or None
    ofn.lpstrFilter = ctypes.cast(filt, wintypes.LPCWSTR)
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 1024
    ofn.lpstrTitle = title
    ofn.Flags = (OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST
                 | OFN_NOCHANGEDIR)

    _raise_when_shown()
    if comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return buf.value
    return None


def _save_legacy(owner, title: str, default_name: str,
                 filetypes: list[tuple[str, str]]) -> str | None:
    filt = _build_filter(filetypes)
    buf = ctypes.create_unicode_buffer(default_name or "", 1024)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = owner or None
    ofn.lpstrFilter = ctypes.cast(filt, wintypes.LPCWSTR)
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 1024
    ofn.lpstrTitle = title
    ofn.lpstrDefExt = "csv"
    ofn.Flags = (OFN_EXPLORER | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR
                 | OFN_OVERWRITEPROMPT)

    _raise_when_shown()
    if comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
        return buf.value
    return None


def ui_test(timeout_ms: int = 1500) -> dict:
    """
    这个进程到底能不能弹 UI？用 1.5 秒自动关闭的系统消息框验证。
    返回 32000 说明超时自动关了（正常）；返回 1 说明被点了确定。
    """
    MB_OK = 0x0
    MB_ICONINFORMATION = 0x40
    MB_TOPMOST = 0x40000
    try:
        fn = user32.MessageBoxTimeoutW
        fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                       wintypes.UINT, wintypes.WORD, wintypes.DWORD]
        fn.restype = ctypes.c_int
        rc = fn(None, "对话框自检（1.5 秒后自动关闭）", "文献精读助手",
                MB_OK | MB_ICONINFORMATION | MB_TOPMOST, 0, timeout_ms)
        return {"messagebox": rc, "ok": rc in (32000, 1)}
    except Exception as e:
        return {"messagebox": None, "ok": False, "error": repr(e)}


def debug_try(owner_mode: str = "fg", options: int | None = None,
              initial_dir: str | None = None, title: str = "debug") -> dict:
    """
    在真实进程里试弹一次对话框并回报结果。给排查用：把参数逐个打开，
    看哪一个导致 Show 返回 E_FAIL。弹窗需要外部关闭（测试脚本会发 WM_CLOSE）。
    """
    global LAST
    LAST = {}
    owner = _foreground_window() if owner_mode == "fg" else None
    opts = FOS_PICKFOLDERS if options is None else options

    def job():
        try:
            p = _ifile_dialog(owner, title, opts, None, initial_dir=initial_dir)
            return {"path": p}
        except DialogUnavailable as e:
            return {"error": str(e), "fallback": _browse_folder_legacy(owner, title)}

    return {"owner": owner, "options": f"0x{opts:08X}",
            "initial_dir": initial_dir, "result": _on_ui_thread(job),
            "last": dict(LAST)}


def probe() -> dict:
    """
    诊断用：报告每一步的 HRESULT，不弹任何窗口。
    打包版出问题时，把这个结果发出来就能定位是哪一环坏了。
    """
    import sys

    out: dict = {"frozen": bool(getattr(sys, "frozen", False)),
                 "exe": sys.executable}

    with _Com() as c:
        out["coinit"] = f"0x{c.hr & 0xffffffff:08X}"

        pfd = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_FileOpenDialog), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_IFileDialog), ctypes.byref(pfd),
        )
        out["cocreate"] = f"0x{hr & 0xffffffff:08X}"
        if pfd:
            r = _vt(pfd, 9, ctypes.c_long, ctypes.c_uint32)(
                pfd, ctypes.c_uint32(FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM))
            out["setoptions"] = f"0x{r & 0xffffffff:08X}"

            opts = ctypes.c_uint32()
            r = _vt(pfd, 10, ctypes.c_long, ctypes.POINTER(ctypes.c_uint32))(
                pfd, ctypes.byref(opts))
            out["getoptions"] = f"0x{r & 0xffffffff:08X}"
            out["options_value"] = f"0x{opts.value:08X}"

            r = _vt(pfd, 17, ctypes.c_long, wintypes.LPCWSTR)(pfd, "probe")
            out["settitle"] = f"0x{r & 0xffffffff:08X}"
            _release(pfd)

        out["owner"] = _foreground_window()

        # 老接口的前置条件：SHBrowseForFolder 需要 shell32 能取到 pidl
        pidl = shell32.ILCreateFromPathW("C:\\")
        out["pidl"] = str(pidl)
        if pidl:
            ole32.CoTaskMemFree(pidl)

    return out


# ---------------- 对外接口 ----------------
#
# 三个函数的行为约定（API 层据此决定要不要提示用户）：
#   返回 str  → 用户选了，这是路径
#   返回 None → 用户取消（不要紧，别弹错误提示）
#   抛 DialogUnavailable → 对话框压根打不开，调用方应告知用户改用「手动输入路径」

def pick_folder(title: str = "选择文件夹", initial_dir: str | None = None) -> str | None:
    """选择文件夹，返回完整路径；用户取消返回 None"""
    owner = _foreground_window()      # 在当前线程取：此刻前台就是应用窗口

    def job():
        try:
            return _ifile_dialog(
                owner, title,
                FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_DONTADDTORECENT,
                None, initial_dir=initial_dir,
            )
        except DialogUnavailable:
            # 只有真故障才换老接口；用户取消不该再弹第二个框
            return _browse_folder_legacy(owner, title)

    return _on_ui_thread(job)


def open_file(title: str = "选择文件",
              filetypes: list[tuple[str, str]] | None = None,
              initial_dir: str | None = None) -> str | None:
    """打开文件，返回完整路径；用户取消返回 None"""
    filetypes = filetypes or [("所有文件", "*.*")]
    owner = _foreground_window()

    def job():
        try:
            return _ifile_dialog(
                owner, title,
                FOS_FILEMUSTEXIST | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR,
                filetypes, initial_dir=initial_dir,
            )
        except DialogUnavailable:
            return _open_legacy(owner, title, filetypes)

    return _on_ui_thread(job)


def save_file(title: str = "保存文件", default_name: str = "",
              filetypes: list[tuple[str, str]] | None = None,
              initial_dir: str | None = None) -> str | None:
    """保存文件，返回完整路径；用户取消返回 None"""
    filetypes = filetypes or [("所有文件", "*.*")]
    owner = _foreground_window()

    def job():
        try:
            return _ifile_dialog(
                owner, title,
                FOS_OVERWRITEPROMPT | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR,
                filetypes, default_name, initial_dir=initial_dir,
            )
        except DialogUnavailable:
            return _save_legacy(owner, title, default_name, filetypes)

    return _on_ui_thread(job)
