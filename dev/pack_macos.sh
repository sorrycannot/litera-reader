#!/usr/bin/env bash
# 在 macOS 上打包「文献精读助手」
#
# 用法（在项目根目录或任意位置都行）：
#     bash dev/pack_macos.sh            # 打包
#     bash dev/pack_macos.sh --run      # 打包并立刻打开
#
# 产出：dist/文献精读助手.app
#
# 为什么必须在 Mac 上跑：PyInstaller 不支持交叉编译，Windows 那边打不出 .app。
#
# 前置条件：
#   1. 已经装了 Xcode 命令行工具（装 Python 依赖时偶尔要用）
#         xcode-select --install
#   2. Python 3.10+ —— 系统自带的 /usr/bin/python3 通常是 3.9，**不够**。
#      用 Homebrew 或 python.org 的 3.12（脚本里会检查，版本不够直接拦下来）
#   3. 首次运行要联网装依赖（pymupdf / pyinstaller）
#
# Apple Silicon 注意：用 arm64 的 Python 打包出来才是原生 arm64。
#   如果 `uname -m` 打印 x86_64 而机器是 M 系列，说明在用 Rosetta 的 Python，
#   打出来的包要靠 Rosetta 才能跑（能跑，但慢）。换 arm64 的解释器重新打包即可。

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# 允许用 PYTHON=python3.12 指定解释器（新版 Python 可能不叫 python3）
PYTHON="${PYTHON:-python3}"

echo "== 环境"
echo "   目录: $ROOT"
echo "   架构: $(uname -m)"
echo "   Python: $("$PYTHON" -V 2>&1)"

# 版本必须 ≥ 3.10。
#   macOS 自带的 /usr/bin/python3 是 3.9（Xcode 命令行工具那份），
#   而代码里用了 `str | None` 这种注解、PyMuPDF 2.x 也要求 3.10+ ——
#   3.9 会在导入阶段就报 TypeError，提示信息还很难懂，所以先在这里拦住。
PY_OK=$("$PYTHON" -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)')
if [ "$PY_OK" != "1" ]; then
  cat >&2 <<'MSG'

!! 需要 Python 3.10 或更高版本。
   macOS 自带的 python3 往往是 3.9，不够用。任选一种装新版：

     brew install python@3.12          # 有 Homebrew 的话最省事
     或从 https://www.python.org/downloads/macos/ 下安装包

   装好后确认：python3 -V  （应打印 3.12.x 之类）
   如果新版装在别的名字下（比如 python3.12），这样跑：
     PYTHON=python3.12 bash dev/pack_macos.sh

MSG
  exit 1
fi

# 1) 独立虚拟环境：别把依赖装进系统 Python（macOS 自带的 3.9 不允许）
if [ ! -d ".venv-mac" ]; then
  echo "== 建虚拟环境 .venv-mac（$("$PYTHON" -V 2>&1)）"
  "$PYTHON" -m venv .venv-mac
fi
# shellcheck disable=SC1091
source .venv-mac/bin/activate

echo "== 装依赖"
python -m pip install --quiet --upgrade pip
if ! python -m pip install --quiet "pymupdf>=2.0,<3" "pyinstaller>=6.0"; then
  cat >&2 <<'MSG'

!! 依赖安装失败。
   常见原因：
     · 网络问题（PyPI 慢）：换国内源重试
         python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "pymupdf>=2.0,<3" pyinstaller
     · 用了 Rosetta 的 Python 而包里没有对应轮子：换 arm64 的 Python 重来
     · 上面提示 "No matching distribution"：多半是 Python 版本太低（需 3.10+）

MSG
  exit 1
fi

python - <<'PY'
import platform, pymupdf, PyInstaller
print(f"   pymupdf {pymupdf.__version__} / PyInstaller {PyInstaller.__version__} / {platform.machine()}")
PY

# 2) 打包前先跑解析测试，规则改动后的回归别等到用户手里才发现
echo "== 自检（解析 + 平台分支）"
python dev/test_parse.py | tail -1
python dev/test_platform.py | tail -1

# 3) 打包
echo "== 打包"
rm -rf build dist
python -m PyInstaller build.spec --noconfirm

APP="dist/文献精读助手.app"
if [ ! -x "$APP/Contents/MacOS/LiteraReader" ]; then
  echo "!! 打包失败：$APP 里没有可执行文件" >&2
  exit 1
fi

# 4) 去掉隔离属性（本机打的包本来没有；从网上下载解压的会有，
#    带着它双击会提示「无法打开，因为 Apple 无法检查其是否包含恶意软件」）
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo
echo "== 完成"
du -sh "$APP" | awk '{print "   体积: "$1}'
echo "   路径: $ROOT/$APP"
echo
echo "双击运行；第一次可能被 Gatekeeper 拦一次，两种解法："
echo "   · 右键点图标 → 打开 → 再点「打开」"
echo "   · 或执行：xattr -dr com.apple.quarantine \"$ROOT/$APP\""
echo
echo "启动日志在 ~/Library/Logs/Litera/startup.log（出问题先看它）"

if [ "${1:-}" = "--run" ]; then
  open "$APP"
fi
