# -*- coding: utf-8 -*-
"""
系统文件/文件夹对话框 —— 平台分发层

两个实现，导入时按平台选一个：

    Windows        dialogs_win.py     原生 API（IFileDialog / SHBrowseForFolder），纯 ctypes
    macOS / Linux  dialogs_posix.py   AppleScript（osascript）/ zenity / kdialog

为什么必须分开：Windows 那份在**模块顶层**就 `ctypes.WinDLL("user32")`，
macOS 上没有 WinDLL，导入即崩 —— 程序连启动都到不了。所以 Windows 代码
整体搬到 dialogs_win.py，这个文件只做转发，不含任何平台 API。

三个函数的约定（api.py 据此决定要不要提示用户）：
    返回 str            用户选了，这是路径
    返回 None           用户取消（别弹错误提示）
    抛 DialogUnavailable 对话框压根打不开 → 提示改用手动粘贴路径
"""

from __future__ import annotations

import sys

if sys.platform.startswith("win"):
    from .dialogs_win import (          # noqa: F401
        LAST, DialogUnavailable, debug_try, open_file, pick_folder, probe,
        save_file, ui_test,
    )
else:
    from .dialogs_posix import (        # noqa: F401
        LAST, DialogUnavailable, debug_try, open_file, pick_folder, probe,
        save_file, ui_test,
    )

__all__ = [
    "LAST", "DialogUnavailable", "debug_try", "open_file", "pick_folder",
    "probe", "save_file", "ui_test",
]
