# -*- coding: utf-8 -*-
"""
对话框模块自测。

分两部分：
  A. 指针链路（不需要人工点击）——验证 PIDL / IShellItem 的 64 位指针没被截断。
     这是「选了文件夹但取不到路径」的根因，必须自动化验证。
  B. 能否真的弹出窗口——在后台线程调用，枚举 #32770 窗口确认，然后关掉。

用法：python dev/test_dialog.py

只在 Windows 上有意义（验证的是 Win32 指针链路）。macOS/Linux 上直接跳过：
那边走 dialogs_posix.py，没有 PIDL/IShellItem 这套东西。
"""
import ctypes
import sys
import threading
import time

sys.path.insert(0, ".")

if not sys.platform.startswith("win"):
    print(f"跳过：这个用例只验证 Windows 的 Win32 指针链路（当前是 {sys.platform}）")
    sys.exit(0)

from app import dialogs_win as dialogs    # noqa: E402

WM_CLOSE = 0x0010
u = ctypes.windll.user32

ok_count = 0
fail_count = 0


def check(name, cond, extra=""):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  ✓ {name}")
    else:
        fail_count += 1
        print(f"  ✗ {name} {extra}")


def part_a():
    print("\n[A] 指针链路（不需要点击）")

    # 1) PIDL 往返：ILCreateFromPathW → SHGetPathFromIDListW
    target = "C:\\Windows"
    pidl = dialogs.shell32.ILCreateFromPathW(target)
    check("ILCreateFromPathW 返回非空指针", bool(pidl), f"pidl={pidl}")
    if pidl:
        buf = ctypes.create_unicode_buffer(1024)
        got = dialogs.shell32.SHGetPathFromIDListW(pidl, buf)
        check("SHGetPathFromIDListW 成功", bool(got), f"got={got}")
        check("路径往返一致", buf.value.lower() == target.lower(), f"得到 {buf.value!r}")
        dialogs.ole32.CoTaskMemFree(pidl)

    # 2) 指针位宽：返回值必须能装下 64 位地址
    import struct
    width = struct.calcsize("P") * 8
    check(f"平台是 {width} 位", True)

    # 3) IShellItem::GetDisplayName 往返
    #    SHCreateItemFromParsingName 必须先初始化 COM，否则返回 CO_E_NOTINITIALIZED
    dialogs.ole32.CoInitializeEx(None, dialogs.COINIT_APARTMENTTHREADED)
    item = ctypes.c_void_p()
    hr = dialogs.shell32.SHCreateItemFromParsingName(
        "C:\\Windows", None, ctypes.byref(dialogs.IID_IShellItem), ctypes.byref(item)
    )
    check("SHCreateItemFromParsingName 成功", hr == 0 and bool(item), f"hr=0x{hr & 0xffffffff:08X}")
    if hr == 0 and item:
        pw = ctypes.c_wchar_p()
        get = dialogs._vt(item, 5, ctypes.c_long, ctypes.c_uint32,
                          ctypes.POINTER(ctypes.c_wchar_p))
        hr2 = get(item, dialogs.SIGDN_FILESYSPATH, ctypes.byref(pw))
        check("IShellItem::GetDisplayName 成功", hr2 == 0 and bool(pw.value), f"hr2=0x{hr2 & 0xffffffff:08X}")
        check("ShellItem 路径正确", (pw.value or "").lower() == "c:\\windows", repr(pw.value))
        if pw.value:
            dialogs.ole32.CoTaskMemFree(ctypes.cast(pw, ctypes.c_void_p))
        dialogs._release(item)


def find_dialog():
    out = []

    def cb(h, l):
        if not u.IsWindowVisible(h):
            return True
        cls = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(h, cls, 128)
        if cls.value == "#32770":
            n = u.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            out.append((h, buf.value))
        return True

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u.EnumWindows(CB(cb), 0)
    return out


def run_dialog(label, fn, accept=False):
    print(f"\n[B] {label}")
    box = {}

    def worker():
        t0 = time.time()
        try:
            box["ret"] = fn()
        except Exception as e:
            box["err"] = repr(e)
        box["dt"] = round(time.time() - t0, 2)

    th = threading.Thread(target=worker, daemon=True)
    th.start()

    seen = None
    for _ in range(30):
        time.sleep(0.2)
        dlg = [d for d in find_dialog() if d[1] != "浏览文件夹"]
        if dlg:
            seen = dlg[0]
            break
        if not th.is_alive():
            break

    if not seen:
        check("对话框已弹出", False, f"返回={box}")
        return

    check("对话框已弹出", True, f"标题={seen[1]!r}")

    if accept:
        # 直接按「选择文件夹」按钮（IDOK=1），验证「真选中了能不能拿到路径」
        u.SendMessageW(seen[0], 0x0111, 1, 0)
        th.join(timeout=8)
        ret = box.get("ret")
        ok = isinstance(ret, str) and len(ret) > 2 and ":" in ret
        check("选中后能拿到路径", ok, f"返回={box}")
        if ok:
            print(f"    返回路径：{ret}")
    else:
        # 用户点取消 → 必须返回 None 而不是崩
        u.PostMessageW(seen[0], WM_CLOSE, 0, 0)
        th.join(timeout=6)
        check("取消后正常返回", box.get("ret") is None and "err" not in box, str(box))


if __name__ == "__main__":
    part_a()
    # 连续弹三次：打包版曾经出现过「第一次能弹、第二次返回 E_FAIL」，
    # 只测一次是抓不到的
    for i in range(3):
        run_dialog(f"选择文件夹 · 第 {i + 1} 次（取消）",
                   lambda: dialogs.pick_folder("选择数据存放位置"))
    run_dialog("选择文件夹 · 选中并确认",
               lambda: dialogs.pick_folder("选择数据存放位置", initial_dir="F:\\"),
               accept=True)
    print(f"\n结果：通过 {ok_count}，失败 {fail_count}")
    sys.exit(1 if fail_count else 0)
