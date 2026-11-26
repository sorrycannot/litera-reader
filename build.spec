# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（Windows / macOS 共用）

    Windows:  pyinstaller build.spec        → dist/文献精读助手.exe（单文件）
    macOS:    pyinstaller build.spec        → dist/文献精读助手.app（双击运行）
    Linux:    pyinstaller build.spec        → dist/文献精读助手（无扩展名可执行文件）

PyInstaller **不能交叉编译**：要 Mac 版就得在 Mac 上跑一次，Windows 版在 Windows 上跑。

关键点：

  - datas 必须带 web 目录，否则打开是白屏（找不到 index.html）

  - datas 里剔掉 pymupdf/layout（50MB 的 onnx 模型）。表格检测是自己按坐标写的，
    不用它那套模型 —— 那套跑一页要 1~2 秒，还依赖 onnxruntime + numpy

  - excludes 排掉 numpy / onnxruntime / pymupdf_layout —— 都是版面模型用的，我们用不到

  - 因为界面改用系统浏览器（Edge 应用模式）而不是 pywebview，
    这里没有任何 .NET / pythonnet 依赖，单文件模式可以正常工作。
    之前的 pythonnet 方案在中文路径下会崩，详见 main.py 顶部说明。

  - console=False：双击不弹黑框。排查启动问题时改 True，
    否则报错信息全被吞掉，只看到"闪一下就没了"。

  - macOS 上多包一层 BUNDLE 才是 .app（能双击、有 Dock 图标）；
    只留 EXE 的话产出一个无扩展名的 Unix 可执行文件，双击只会开终端。
"""

import sys

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[('web', 'web')],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'numpy',
        'onnxruntime',
        'pymupdf_layout',
        'pymupdf4llm',
        'tkinter',
        'matplotlib',
        'PIL',
        'webview',
        'clr_loader',
        'pythonnet',
    ],
    noarchive=False,
)

# PyMuPDF 自带的版面分析 / 表格识别模型（onnx）一共 50MB，我们完全用不到 ——
# 表格是按坐标自己判的（见 pdf_reader.page_tables 的注释：它那套模型跑一页要 1~2 秒）。
# 不剔掉的话，发给同学的每个 exe 都白扛 50 兆。
a.datas = [d for d in a.datas
           if "pymupdf/layout" not in d[0].replace("\\", "/")]
a.binaries = [b for b in a.binaries
              if "pymupdf/layout" not in b[0].replace("\\", "/")]

pyz = PYZ(a.pure)

# macOS 的可执行文件放在 .app/Contents/MacOS/ 里，用纯英文名更保险
# （部分签名/公证工具对非 ASCII 的二进制名敏感，虽然这里不做签名）。
EXE_NAME = 'LiteraReader' if sys.platform == 'darwin' else '文献精读助手'

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 压缩会显著提高杀毒软件误报率，关掉
    runtime_tmpdir=None,
    console=False,    # 正式版：双击不弹黑框。排查问题时改 True
    disable_windowed_traceback=False,
    icon=None,
)

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='文献精读助手.app',
        icon=None,
        bundle_identifier='cn.litera.reader',
        info_plist={
            'CFBundleName': '文献精读助手',
            'CFBundleDisplayName': '文献精读助手',
            'CFBundleShortVersionString': '1.0',
            'CFBundleVersion': '1.0',
            # Retina 屏上不糊
            'NSHighResolutionCapable': True,
            # 这是普通前台应用（不是菜单栏小工具），启动后会有 Dock 图标
            'LSApplicationCategoryType': 'public.app-category.productivity',
            # 只读用户自选的目录 + 本地端口，不需要任何隐私权限
            'LSMinimumSystemVersion': '11.0',
        },
    )
