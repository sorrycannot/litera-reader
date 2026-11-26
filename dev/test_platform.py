# -*- coding: utf-8 -*-
"""
跨平台适配测试（Windows / macOS / Linux）

这些用例在**任何平台**上都能跑：平台分支的取函数/取路径逻辑都留了 platform 参数，
测试直接传 'darwin' / 'win32' / 'linux'，不需要真机也能验证分支对不对。

只跟真机有关的两件事测不了（本机是 Windows）：
  · Chrome 是否真的以 --app 弹出无地址栏窗口
  · Finder 的 open -R 是否真的选中目录
命令拼装可以测，实际效果要靠 Mac 上跑一次。

  python dev/test_platform.py
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev"))

# 隔离 APPDATA，别动使用者真实的 %APPDATA%\Litera
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="lt_plat_"))
os.environ["APPDATA"] = str(_TMP / "appdata")

import main as M                                  # noqa: E402
from app import dialogs as M_dialogs               # noqa: E402  分发层
from app import dialogs_posix as DP                # noqa: E402  AppleScript 后端
from app import paths as P                        # noqa: E402
from app import server as srv                     # noqa: E402

passed = failed = 0


def check(name: str, cond, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}" + (f"  → {detail}" if detail else ""))


def main():
    print("== 1. 浏览器候选路径")
    win = M.browser_candidates("win32")
    mac = M.browser_candidates("darwin")
    lin = M.browser_candidates("linux")

    check("Windows 候选还是原来那 6 条", len(win) == 6, str(win))
    check("Windows 候选里仍有 Edge 与 Chrome",
          any("msedge.exe" in w for w in win) and any("chrome.exe" in w for w in win))
    check("macOS 候选全是 .app 里的可执行文件",
          mac and all("Contents/MacOS/" in m for m in mac), str(mac))
    check("macOS 候选覆盖 Chrome / Edge",
          any("Google Chrome.app" in m for m in mac)
          and any("Microsoft Edge.app" in m for m in mac))
    check("macOS 候选也找 ~/Applications（没管理员权限的装法）",
          any(m.startswith("~/Applications") for m in mac), str(mac))
    check("Linux 候选是绝对路径",
          lin and all(m.startswith("/") for m in lin), str(lin))
    check("Windows 候选用的是 %VAR% 而不是写死盘符",
          all(w.startswith("%") for w in win), str(win))

    print("\n== 2. find_browser 各平台不炸")
    # 不能在别的平台上断言「一定查不到」：GitHub 的 macOS runner 预装了
    # /Applications/Google Chrome.app，这条断言在云端 CI 上必然失败（本机是 Windows，
    # 当初写的时候查不到才过的）。只验证返回值是 None 或真实存在的文件。
    got_mac = M.find_browser("darwin")
    check("darwin 分支能跑通（查到就是真实文件）",
          got_mac is None or os.path.isfile(got_mac), str(got_mac))
    got_lin = M.find_browser("linux")
    check("linux 分支能跑通（查到就是真实文件）",
          got_lin is None or os.path.isfile(got_lin), str(got_lin))
    got = M.find_browser()
    check("当前平台的探测不抛异常", got is None or os.path.isfile(got), str(got))

    print("\n== 3. 浏览器配置目录候选")
    exe_dir = ROOT
    mac_bases = M.profile_base_candidates(exe_dir, "darwin")
    win_bases = M.profile_base_candidates(exe_dir, "win32")
    lin_bases = M.profile_base_candidates(exe_dir, "linux")

    check("三个平台都优先用系统临时目录",
          mac_bases[0] == pathlib.Path(tempfile.gettempdir())
          and win_bases[0] == pathlib.Path(tempfile.gettempdir()))
    check("macOS 有 /tmp 兜底（用户名是中文时 ~ 会被 ASCII 闸门挡掉）",
          pathlib.Path("/tmp") in mac_bases, str(mac_bases))
    check("macOS 不再出现 Windows 的 C:/Temp",
          pathlib.Path("C:/Temp") not in mac_bases, str(mac_bases))
    check("Windows 仍保留 exe 目录与 C:/Temp 兜底",
          exe_dir in win_bases and pathlib.Path("C:/Temp") in win_bases, str(win_bases))
    check("Linux 有 ~/.cache/Litera 兜底",
          pathlib.Path.home() / ".cache" / "Litera" in lin_bases, str(lin_bases))

    # 真正挑一个可写目录：本机（Windows）应当挑出临时目录
    picked = M._pick_profile_base(exe_dir, "win32")
    check("Windows 能挑到一个可写的配置目录", picked is not None, str(picked))
    if picked:
        check("挑中的目录是纯 ASCII（Chromium 的硬要求）", M._ascii_safe(picked), str(picked))

    print("\n== 4. 进程存活检测")
    check("当前进程算活着（本平台分支）", M._pid_alive(os.getpid()))
    check("不存在的 pid 算死了", not M._pid_alive(999999))

    orig_is_win = M.IS_WIN
    M.IS_WIN = False          # 强制走 os.kill(pid, 0) 这条 POSIX 分支
    try:
        check("POSIX 分支：当前进程活着", M._pid_alive(os.getpid()))
        check("POSIX 分支：不存在的 pid 死了", not M._pid_alive(999999))
    finally:
        M.IS_WIN = orig_is_win

    print("\n== 5. 数据目录（指针文件放哪）")
    mac_dir = P.appdata_dir("darwin")
    win_dir = P.appdata_dir("win32")
    lin_dir = P.appdata_dir("linux")
    check("macOS 用 ~/Library/Application Support/Litera",
          mac_dir == pathlib.Path.home() / "Library" / "Application Support" / "Litera",
          str(mac_dir))
    check("Windows 用 %APPDATA%\\Litera",
          win_dir == pathlib.Path(os.environ["APPDATA"]) / "Litera", str(win_dir))
    check("Linux 有 XDG_CONFIG_HOME 时用它",
          pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
          / "Litera" == lin_dir, str(lin_dir))
    check("三个平台的目录名都叫 Litera", {d.name for d in (mac_dir, win_dir, lin_dir)} == {"Litera"})

    print("\n== 6. 打开数据目录的命令")
    calls = []

    class Dummy:
        @staticmethod
        def run(args, **kw):
            calls.append(list(args))
            return None

    orig_run = subprocess.run
    subprocess.run = Dummy.run
    try:
        P.reveal_in_file_manager("/tmp/litera", platform="darwin")
        P.reveal_in_file_manager("/tmp/litera", platform="linux")
    finally:
        subprocess.run = orig_run
    check("macOS 用 open -R", calls and calls[0] == ["open", "-R", "/tmp/litera"], str(calls))
    check("Linux 用 xdg-open",
          len(calls) > 1 and calls[1] == ["xdg-open", "/tmp/litera"], str(calls))

    # Windows 走 os.startfile：这里只验证「不会误走 POSIX 分支」
    started = []
    orig_startfile = getattr(os, "startfile", None)
    if orig_startfile is not None:
        os.startfile = lambda p: started.append(p)
        try:
            P.reveal_in_file_manager("C:/x", platform="win32")
        finally:
            os.startfile = orig_startfile
        check("Windows 用 os.startfile", started == ["C:/x"], str(started))
    else:
        check("Windows 用 os.startfile", True, "非 Windows 解释器，跳过")

    print("\n== 7. 启动日志位置")
    orig_is_mac = M.IS_MAC
    try:
        M.IS_MAC = False
        win_log = M.log_path()
        M.IS_MAC = True
        mac_log = M.log_path()
    finally:
        M.IS_MAC = orig_is_mac
    check("Windows 日志在程序目录", win_log.name == "startup.log" and "Library" not in str(win_log),
          str(win_log))
    check("macOS 日志在 ~/Library/Logs/Litera（.app 内部翻不到）",
          mac_log == pathlib.Path.home() / "Library" / "Logs" / "Litera" / "startup.log",
          str(mac_log))

    print("\n== 8. 界面是否打开的判定（非 Windows 分支）")
    orig = srv.ApiHandler.last_seen
    orig_is_win2 = M.IS_WIN
    M.IS_WIN = False          # 强制走「看服务端请求时间戳」这条分支
    try:
        srv.ApiHandler.last_seen = time.time() + 100
        check("页面已请求过服务 → 判定打开", M.find_app_window(1.0, since=time.time()))

        t0 = time.time()
        srv.ApiHandler.last_seen = t0 - 100
        begin = time.time()
        ok = M.find_app_window(0.6, since=t0)
        cost = time.time() - begin
        check("页面没请求 → 判定失败且按 timeout 退出", (not ok) and 0.5 <= cost < 2.0,
              f"ok={ok} cost={cost:.2f}s")
    finally:
        srv.ApiHandler.last_seen = orig
        M.IS_WIN = orig_is_win2

    print("\n== 9. 浏览器启动参数（--app 窗口）")
    launched = []

    class FakeProc:
        pid = 4242

    orig_popen = subprocess.Popen
    subprocess.Popen = lambda args, **kw: (launched.append(list(args)), FakeProc())[1]
    try:
        base = _TMP / "profiles"
        base.mkdir(parents=True, exist_ok=True)
        M.launch_app_window("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                            "http://127.0.0.1:1234/", base=base)
    finally:
        subprocess.Popen = orig_popen
    args = launched[0] if launched else []
    joined = " ".join(args)
    check("带了 --app=（无地址栏窗口）", "--app=http://127.0.0.1:1234/" in joined, joined)
    check("带了独立的 --user-data-dir（否则会转发给已开的实例）",
          any(a.startswith("--user-data-dir=") for a in args), joined)
    check("配置目录名带进程号", any("litera-profile-" in a for a in args), joined)
    check("首参数就是浏览器可执行文件", args and args[0].endswith("Google Chrome"), joined)
    check("--no-first-run / --no-default-browser-check 都在",
          "--no-first-run" in args and "--no-default-browser-check" in args, joined)

    print("\n== 10. 文件选择对话框（macOS 的 AppleScript 后端）")
    # 分发层：两个平台都必须能拿到这 8 个名字
    for name in ("LAST", "DialogUnavailable", "debug_try", "open_file",
                 "pick_folder", "probe", "save_file", "ui_test"):
        check(f"dialogs 导出 {name}", hasattr(M_dialogs, name))

    check("引号要转义（标题里有引号不会把脚本切断）",
          DP._q('他说"选这里"') == '"他说\\"选这里\\""', DP._q('他说"选这里"'))
    check("取消错误码 -128 认得出", DP._is_cancel("execution error: User canceled. (-128)"))
    check("中文系统的「用户已取消」也认得出", DP._is_cancel("0:12: execution error: 用户已取消。 (-128)"))
    check("真故障不当成取消", not DP._is_cancel("execution error: Can't get window. (-1728)"))
    check("通配符转成扩展名", DP._exts([("PDF 文件", "*.pdf"), ("CSV", "*.csv;*.txt")]) == ["pdf", "csv", "txt"],
          str(DP._exts([("PDF 文件", "*.pdf"), ("CSV", "*.csv;*.txt")])))

    class R:
        def __init__(self, out="", err="", rc=0):
            self.stdout, self.stderr, self.returncode = out, err, rc

    fake = {}

    def fake_run(args, **kw):
        fake["args"] = list(args)
        return fake["reply"]

    orig_run2 = subprocess.run
    subprocess.run = fake_run
    try:
        fake["reply"] = R(out="/Users/me/Documents/\n")
        got = DP.pick_folder("选择数据存放位置", initial_dir=str(_TMP), platform="darwin")
        script = fake.get("args", ["", ""])[-1]
        check("选文件夹用的是 choose folder", "choose folder" in script, script)
        check("带上提示语", 'with prompt "选择数据存放位置"' in script, script)
        check("带上默认位置（目录真实存在时才写）", "default location" in script, script)
        check("返回路径去掉了末尾斜杠", got == "/Users/me/Documents", repr(got))
        check("调的是 osascript", fake["args"][0] == "osascript", str(fake["args"][:2]))

        fake["reply"] = R(out="/")
        check("选了根目录仍返回 /", DP.pick_folder("x", platform="darwin") == "/",
          DP.pick_folder("x", platform="darwin"))

        fake["reply"] = R(out="/Users/me/a.pdf\n")
        got = DP.open_file("选择要精读的文献", [("PDF 文件", "*.pdf")], platform="darwin")
        script = fake.get("args", ["", ""])[-1]
        check("选文件用的是 choose file", "choose file" in script, script)
        check("文件类型传成 of type {\"pdf\"}", 'of type {"pdf"}' in script, script)
        check("返回选中的文件路径", got == "/Users/me/a.pdf", repr(got))

        fake["reply"] = R(out="/Users/me/术语库.csv\n")
        DP.save_file("导出术语库", "术语库.csv", [("CSV", "*.csv")], initial_dir="/Users/me",
                     platform="darwin")
        script = fake.get("args", ["", ""])[-1]
        check("保存用的是 choose file name", "choose file name" in script, script)
        check("带上默认文件名", 'default name "术语库.csv"' in script, script)

        # 用户取消 → None，不能抛异常（抛了前端会弹错误框）
        fake["reply"] = R(err="execution error: User canceled. (-128)", rc=1)
        got = DP.pick_folder("x", platform="darwin")
        check("用户取消返回 None", got is None, repr(got))

        # 真故障 → DialogUnavailable（前端提示改用手动粘贴）
        fake["reply"] = R(err="execution error: boom (-1728)", rc=1)
        raised = False
        try:
            DP.pick_folder("x", platform="darwin")
        except DP.DialogUnavailable:
            raised = True
        check("真故障抛 DialogUnavailable", raised)

        # 目录不存在时不要把 default location 写进去（AppleScript 会直接报错）
        fake["reply"] = R(out="/tmp/")
        DP.pick_folder("x", initial_dir="/no/such/dir/here", platform="darwin")
        script = fake.get("args", ["", ""])[-1]
        check("默认目录不存在就不写 default location", "default location" not in script, script)
    finally:
        subprocess.run = orig_run2

    print(f"\n结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
