# -*- coding: utf-8 -*-
"""
文献精读助手 · 程序入口

启动一个只监听本机的 HTTP 服务，然后用 Edge/Chrome 的**应用模式窗口**打开界面。
应用模式窗口没有地址栏、没有标签页、没有地址提示，看起来就是个独立软件。

为什么不用 pywebview（踩了很久才定下来）
------------------------------------
pywebview 的 Windows 后端依赖 pythonnet → .NET Framework，而 .NET 加载程序集
**不支持含中文的路径**。国内用户几乎必然把程序放在中文目录里，一启动就报
"Failed to resolve Python.Runtime.Loader.Initialize"。实测同一份 exe：
放在 C:\\lt_test 能跑，放在"文献阅读脚本"目录就崩。

换成标准库 http.server + 系统已有浏览器，完全不碰 .NET：
  - 中文路径无障碍
  - 打包体积小（不需要任何 GUI 框架）
  - 启动快

  python main.py            正常启动
  python main.py --demo     用内置样例数据预览阅读页（不连后端）

支持平台
----------------------------
Windows / macOS / Linux 同一份代码，差异都收在下面几处：
  · 浏览器路径：macOS 要钻进 .app/Contents/MacOS/ 里那个可执行文件
  · 配置目录：macOS 用 /var/folders/... 的临时目录，Linux 用 /tmp
  · 进程存活检测：Windows 走 OpenProcess，POSIX 走 os.kill(pid, 0)
  · 界面是否打开：Windows 枚举窗口标题；macOS/Linux 看页面有没有请求过服务
    （mac 查窗口标题要「辅助功能」授权，装个阅读器不值得让用户去点系统设置）
macOS 上没有 Chromium 内核浏览器时会退回默认浏览器（Safari），
此时是普通标签页而不是无地址栏的独立窗口 —— 功能一样，只是不像个软件。
"""

import os
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.api import Api                    # noqa: E402
from app import server as srv              # noqa: E402

# 界面关掉之后，多久没有心跳就认为程序该退出了（秒）
#
# 别调小。前端每 25 秒发一次心跳，这个值只决定「关掉窗口后多久进程才退」。
# 原来设 90 秒，结果把「开着窗口读文献」也判成了关窗口 ——
# 静置一分半，服务自己退出，窗口变成空壳，点什么都是 Failed to fetch。
# 浏览器最小化时定时器会被节流到约 1 分钟一次，所以留三倍余量。
IDLE_TIMEOUT = 180

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# 等界面窗口出现的时限。macOS 上第一次用全新配置目录启动 Chrome 会做一次初始化，
# 比 Windows 慢不少（实测 8~15 秒），给够余量。
WINDOW_TIMEOUT = 20.0 if IS_MAC else 12.0

# 只找 Chromium 内核的浏览器：--app 模式（无地址栏窗口）和 --user-data-dir（独立实例）
# 都是它的特性。Safari 两样都没有，叫起来只能开普通标签页，
# 所以找不到 Chromium 时退回 webbrowser.open()（见 main 末尾）。
BROWSER_CANDIDATES_WIN = [
    r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
    r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
]

# macOS：不能直接执行 .app 目录，要钻进 Contents/MacOS/ 里那个可执行文件。
# 除了 /Applications，也找 ~/Applications —— 用户没管理员权限时会把浏览器装在这里。
BROWSER_CANDIDATES_MAC = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "~/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]

BROWSER_CANDIDATES_LINUX = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/microsoft-edge",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/opt/google/chrome/chrome",
]


def base_dir() -> Path:
    """打包后资源在 _MEIPASS；开发时就是脚本所在目录"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return Path(__file__).resolve().parent


def log_path() -> Path:
    """
    启动日志落在哪。

    Windows：exe 同目录的 startup.log —— 用户双击的就是那个文件夹，随手就能发过来。
    macOS：  ~/Library/Logs/Litera/startup.log
             .app 的 Contents/MacOS 是包内部，写进去用户翻不到；
             而且往包内容里写文件会让签名校验更难看。
    """
    if IS_MAC:
        return Path.home() / "Library" / "Logs" / "Litera" / "startup.log"
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / "startup.log"


def log(msg: str):
    """
    启动过程日志（打包后没有控制台，出问题只能靠它排查）。
    开发时写到项目根目录。
    """
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def browser_candidates(platform: str | None = None) -> list[str]:
    """按平台给候选浏览器路径。platform 可传值，方便测试各分支。"""
    p = sys.platform if platform is None else platform
    if p.startswith("win"):
        return BROWSER_CANDIDATES_WIN
    if p == "darwin":
        return BROWSER_CANDIDATES_MAC
    return BROWSER_CANDIDATES_LINUX


def find_browser(platform: str | None = None) -> str | None:
    for raw in browser_candidates(platform):
        # Windows 用 %PROGRAMFILES% 这类变量，macOS/Linux 用 ~；
        # 两个都展开一遍，谁命中算谁（不命中的展开是空操作）。
        p = os.path.expanduser(os.path.expandvars(raw))
        if os.path.isfile(p):
            return p
    return None


def _ascii_safe(p: Path) -> bool:
    """
    路径能只用 ASCII 表示吗。

    Chromium 对 --user-data-dir 里的非 ASCII 字符处理不可靠：实测把配置目录
    放在「文献阅读脚本」这种中文路径下，窗口能出来但页面是**一片白板**，
    标题停在 URL 上 —— 看起来像程序坏了，其实是浏览器没渲染。
    所以配置目录一律挑 ASCII 路径。
    """
    try:
        str(p).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def profile_base_candidates(exe_dir: Path, platform: str | None = None) -> list[Path]:
    """
    浏览器配置目录的候选位置（按优先级）。

    macOS 的临时目录是 /var/folders/xx/…/T（纯 ASCII），最合用；
    用户名是中文时 ~/Library/Caches 会含中文，被下面的 ASCII 闸门挡掉，
    还有 /tmp 兜底。Linux 同理走 /tmp。
    """
    p = sys.platform if platform is None else platform
    out = [Path(tempfile.gettempdir())]
    if p.startswith("win"):
        out += [
            Path(os.environ.get("LOCALAPPDATA", "") or "") / "Temp" / "Litera",
            exe_dir,
            Path.home(),
            Path("C:/Temp"),
        ]
    elif p == "darwin":
        out += [Path("/tmp"), Path.home() / "Library" / "Caches" / "Litera", Path.home()]
    else:
        out += [Path("/tmp"), Path.home() / ".cache" / "Litera", Path.home()]
    return out


def _pick_profile_base(exe_dir: Path, platform: str | None = None) -> Path | None:
    """
    给浏览器配置目录挑一个「能写 + 纯 ASCII」的位置。
    优先级：系统临时目录 → 平台自己的缓存目录 → 程序目录 → 用户目录。
    """
    candidates = profile_base_candidates(exe_dir, platform)
    usable = [c for c in candidates
              if str(c) not in ("", ".") and _ascii_safe(c)]
    for c in candidates:
        if str(c) in ("", ".") or _ascii_safe(c):
            continue
        log(f"跳过含中文的配置目录候选：{c}")
    return _writable_dir(usable)


def _writable_dir(candidates: list[Path]) -> Path | None:
    """找第一个真的能写进去的目录（只看存在不够，得实际写一次）"""
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            probe = c / ".write_test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            return c
        except Exception:
            continue
    return None


def _use_scratch_cwd(base: Path | None) -> None:
    """
    把「当前工作目录」挪到临时目录。

    为什么：输入法（搜狗实测）会往当前工作目录写 debug.log 这类内部日志。
    用户双击 exe 时 CWD 就是程序所在文件夹，于是 dist 里会莫名多出一个 debug.log ——
    自己看着无害，但把文件夹发给别人时，对方看到「debug.log」很容易怀疑程序在记录什么。

    挪到临时目录后，第三方往 CWD 写什么都不影响程序目录的干净。
    注意：本程序自己读写文件全部用绝对路径，chdir 不影响功能。
    """
    if not base:
        return
    # 兜底情况下 base 可能退化成程序目录本身，那就别在里面建 run/ 文件夹了
    exe_dir = (Path(sys.executable).parent if getattr(sys, "frozen", False)
               else Path(__file__).parent).resolve()
    try:
        if base.resolve() == exe_dir:
            return
    except Exception:
        pass

    scratch = base / "run"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        os.chdir(scratch)
        log(f"工作目录切到临时目录：{scratch}")
    except Exception as e:
        log(f"切换工作目录失败（忽略）：{e}")


_PROFILE_DIR: Path | None = None


def _pid_alive(pid: int) -> bool:
    """进程还活着吗（只查，不碰）"""
    if not IS_WIN:
        # POSIX：信号 0 不发信号，只做「能不能找到这个进程」的检查。
        # 权限不足（别人的进程）会抛 PermissionError —— 那也说明它活着。
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False
        return True

    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def cleanup_stale_profiles(base: Path) -> int:
    """
    清掉以前运行留下的浏览器配置目录。

    每次启动用一个独立配置目录（否则 Edge 会把命令转给旧实例，窗口打不开），
    代价是程序被强制结束时目录会留下来 —— 一个就有三百多兆。
    这些都是我们自己建在临时目录里的，删掉安全；别人正用着的（进程还活着）不动。
    """
    import shutil
    removed = 0
    try:
        for d in base.glob("litera-profile-*"):
            if not d.is_dir():
                continue
            # 目录名形如 litera-profile-12345 / litera-profile-12345-retry，
            # 取其中第一段数字作为进程号
            tail = d.name[len("litera-profile-"):].split("-")[0]
            if tail.isdigit():
                pid = int(tail)
                if pid == os.getpid() or _pid_alive(pid):
                    continue
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    except Exception:
        pass
    return removed


def launch_app_window(browser: str, url: str, fresh: bool = False,
                      base: Path | None = None):
    """
    用 --app 模式打开：没有地址栏和标签页。

    --user-data-dir 必须给，否则用户已经开着浏览器时新参数会被忽略，
    变成在现有窗口里开个标签页，应用模式就失效了。

    但**不能复用同一个目录**：Edge 发现那个配置目录已有实例在跑时，
    会把新命令转发给旧实例，`--app` 参数被丢掉，结果是"程序启动了但窗口不出现"，
    或者弹出一个指向旧地址的空白页。所以每次启动都用带进程号的独立目录，
    退出时清理。

    fresh=True 表示「上一次启动没成功，换一个全新目录再来」。
    """
    global _PROFILE_DIR
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    if base is None:
        base = _pick_profile_base(exe_dir)

    profile = None
    if base:
        n = cleanup_stale_profiles(base)
        if n:
            log(f"清理了 {n} 个残留的浏览器配置目录")
        name = f"litera-profile-{os.getpid()}" + ("-retry" if fresh else "")
        profile = base / name
        try:
            profile.mkdir(parents=True, exist_ok=True)
            _PROFILE_DIR = profile
        except Exception as e:
            log(f"创建配置目录失败：{e}")
            profile = None
    log(f"浏览器配置目录：{profile}")

    args = [
        browser,
        f"--app={url}",
        "--window-size=1500,940",
        "--no-first-run",
        "--no-default-browser-check",
        # 关掉翻译浮条、媒体路由和 Edge 自带侧边栏（Copilot 那一栏会从右边挤出来）
        "--disable-features=Translate,MediaRouter,msEdgeSidebarV2,msCopilot,EdgeCollections",
    ]
    if profile:
        args.insert(3, f"--user-data-dir={profile}")
    else:
        log("⚠ 找不到可用的纯英文配置目录，浏览器将使用默认配置（可能开不出独立窗口）")

    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"已启动浏览器 pid={proc.pid}")
        return proc
    except Exception as e:
        log(f"启动浏览器失败：{e}")
        return None


def cleanup_profile():
    """退出时清掉临时的浏览器配置目录，别在硬盘上留垃圾"""
    if _PROFILE_DIR and _PROFILE_DIR.exists():
        import shutil
        shutil.rmtree(_PROFILE_DIR, ignore_errors=True)


def _wait_page_seen(since: float, timeout: float) -> bool:
    """
    等界面真的连上服务（macOS / Linux 用）。

    为什么 Windows 那边是枚举窗口标题、这边换一套：Win32 的 EnumWindows 是现成的，
    macOS 要查窗口标题得走 AppleScript，会弹「辅助功能」授权框 ——
    用户装个阅读器还要去系统设置里勾权限，太重了。

    换个体征：前端一打开就会请求接口并开始发心跳，服务端 last_seen 会往前走。
    它动了，就说明页面确实加载并跑起来了（起不来的白板页面不会发请求）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if srv.ApiHandler.last_seen > since:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def find_app_window(timeout: float = 12.0, since: float = 0.0) -> bool:
    """
    等界面窗口真正加载出来。

    为什么要等：Edge 偶尔会先给一个空白窗口，页面没加载成功（标题停在 URL 上），
    用户看到的就是"白板"。这种事无法完全避免，但可以检测出来重开一次。
    Windows 上的判定标准是窗口标题——页面加载成功后 <title>文献精读助手</title> 会生效。
    macOS 上改用「页面是否已经请求过服务」判断，见 _wait_page_seen。

    since：启动浏览器的时刻。非 Windows 平台靠它区分「这次的请求」和「之前的请求」。
    """
    if not IS_WIN:
        return _wait_page_seen(since or time.time(), timeout)

    import ctypes

    u = ctypes.windll.user32
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []

        def cb(h, _):
            if not u.IsWindowVisible(h):
                return True
            n = u.GetWindowTextLengthW(h)
            if n:
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(h, b, n + 1)
                if "文献精读助手" in b.value:
                    found.append(h)
                    return False
            return True

        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        u.EnumWindows(CB(cb), 0)
        if found:
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    web = base_dir() / "web"
    index = web / "index.html"
    if not index.is_file():
        print(f"找不到界面文件：{index}")
        return 1

    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    base = _pick_profile_base(exe_dir)
    # 先挪开工作目录：输入法等第三方会往 CWD 写日志，别让它污染程序目录
    _use_scratch_cwd(base)

    api = Api()
    server, port = srv.start_server(api, web, timeout_seconds=IDLE_TIMEOUT)
    url = f"http://127.0.0.1:{port}/"
    if "--demo" in sys.argv:
        url += "#demo"      # 用内置样例数据预览阅读页，不连后端
    log(f"服务启动 {url} web={web}")

    browser = find_browser()
    log(f"找到浏览器：{browser}（平台 {sys.platform}）")
    if browser:
        # t0 必须取在启动之前：macOS/Linux 靠「请求时间戳有没有往前走」判断
        # 界面是否真的加载了，起点选晚了会把上一次的请求当成这一次的。
        # 再和 last_seen 取 max：服务刚起来时 last_seen 就被设成了启动时刻，
        # 万一和 t0 撞在同一微秒，就会立刻误判成「界面已打开」。
        t0 = max(time.time(), srv.ApiHandler.last_seen)
        proc = launch_app_window(browser, url, base=base)
        if proc and not find_app_window(WINDOW_TIMEOUT, since=t0):
            # 空白窗口：把这次启动的浏览器关掉，换一个新配置目录重来一次
            log("界面没加载出来，重开一次")
            try:
                proc.terminate()
            except Exception:
                pass
            time.sleep(1.5)
            launch_app_window(browser, url, fresh=True, base=base)
    else:
        log("没找到 Chrome/Edge，退回系统默认浏览器（会是普通标签页，不是独立窗口）")
        webbrowser.open(url)

    print(f"界面已打开：{url}")
    print(f"关闭窗口后程序会自动退出（最长等 {IDLE_TIMEOUT} 秒）")

    try:
        # 靠心跳判断界面还在不在：页面关掉就不再 ping，超时即退出
        while True:
            time.sleep(2)
            if time.time() - srv.ApiHandler.last_seen > IDLE_TIMEOUT:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        cleanup_profile()
    return 0


if __name__ == "__main__":
    sys.exit(main())
