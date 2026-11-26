# -*- coding: utf-8 -*-
"""
真实场景验证：应用窗口在前台时，点「选择文件夹」对话框会不会被挡在后面。

之前的问题：
  - 对话框 hwndOwner=None → Windows 把它放到底层 → 用户以为按钮没反应
  - SHBrowseForFolderW 返回的 64 位指针被 ctypes 当 int 截断 → 选完拿不到路径

这个脚本模拟前端点击（直接 POST /api/pick_folder），检查：
  1. 对话框确实出现
  2. 它拿到了前台（或至少可见，不被应用窗口完全压住）
  3. 取消后接口返回 ok=True / data=None（前端据此静默，不报错）
"""
import ctypes
import json
import sys
import threading
import time
import urllib.request

WM_CLOSE = 0x0010
u = ctypes.windll.user32


def post(url, payload=None, timeout=30):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def windows_of(title_part):
    out = []

    def cb(h, l):
        if not u.IsWindowVisible(h):
            return True
        n = u.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            if title_part in b.value:
                out.append((h, b.value))
        return True

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u.EnumWindows(CB(cb), 0)
    return out


def find_post_dialog():
    """
    找本进程弹出的标准对话框。这里不过滤标题，因为 IFileDialog 的窗口标题
    就是 SetTitle 设的「选择数据存放位置」。
    """
    out = []

    def cb(h, l):
        if not u.IsWindowVisible(h):
            return True
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(h, cls, 64)
        if cls.value != "#32770":
            return True
        n = u.GetWindowTextLengthW(h)
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        if "选择数据存放位置" in b.value:
            out.append((h, b.value))
            return False
        return True

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u.EnumWindows(CB(cb), 0)
    return out[0] if out else None


def main(port):
    url = f"http://127.0.0.1:{port}"
    print(f"服务：{url}")

    # 1) 把应用窗口放到前台，营造真实条件
    app = windows_of("文献精读助手")
    if not app:
        print("✗ 找不到应用窗口，先启动 main.py")
        return 1
    ah = app[0][0]
    u.ShowWindow(ah, 9)
    u.SetForegroundWindow(ah)
    time.sleep(0.8)
    print(f"应用窗口 {ah} 已置于前台，前台窗口={u.GetForegroundWindow()}")

    # 2) 后台发请求（会一直阻塞到对话框关闭）
    box = {}

    def worker():
        try:
            box["resp"] = post(url + "/api/pick_folder")
        except Exception as e:
            box["err"] = repr(e)

    th = threading.Thread(target=worker, daemon=True)
    th.start()

    # 3) 等对话框出现
    dlg = None
    for _ in range(40):
        time.sleep(0.25)
        dlg = find_post_dialog()
        if dlg:
            break
    if not dlg:
        print("✗ 对话框没有出现（接口可能直接失败）")
        th.join(3)
        print("   接口返回：", box)
        return 1

    h, title = dlg
    fg = u.GetForegroundWindow()
    print(f"✓ 对话框出现 hwnd={h} 标题={title!r}")
    print(f"  {'✓' if fg == h else '✗'} 是否前台：{fg == h}（前台 hwnd={fg}）")

    # 4) 用户点取消
    u.PostMessageW(h, WM_CLOSE, 0, 0)
    th.join(8)
    resp = box.get("resp") or {}
    ok = resp.get("ok") is True and resp.get("data") is None
    print(f"  {'✓' if ok else '✗'} 取消后返回：{resp or box}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 0))
