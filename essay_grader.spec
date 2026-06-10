# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_all, copy_metadata

# 收集 pyzbar 的子模块
pyzbar_imports = collect_submodules('pyzbar')

# 收集 zxing-cpp 的子模块（含原生库）
zxing_imports = collect_submodules('zxingcpp')

# 收集 PIL 的子模块（条码识别预处理用）
pil_imports = collect_submodules('PIL')

# ---- PaddleOCR 3.x Pipeline 架构 ----
# collect_all 收集模块代码 + 数据文件(yaml/json) + 二进制文件
# collect_submodules 只收 .py，会漏掉 paddlex/configs/pipelines/OCR.yaml 等关键配置
paddleocr_datas, paddleocr_bins, paddleocr_hidden = collect_all('paddleocr')
paddlex_datas, paddlex_bins, paddlex_hidden = collect_all('paddlex')
paddle_datas, paddle_bins, paddle_hidden = collect_all('paddle')

# copy_metadata 的参数是 PyPI 发行包名，不是 import 名
# import paddle → 发行包名 paddlepaddle
paddleocr_datas += copy_metadata('paddleocr')
paddlex_datas += copy_metadata('paddlex')
paddle_datas += copy_metadata('paddlepaddle')

# ---- PaddleOCR ocr-core extra 依赖 ----
# PaddleX 运行时通过 importlib.metadata.version(dep) 逐个检查这些包是否安装。
# collect_all 只收 .py + .so，不会自动收 .dist-info，必须显式 copy_metadata。
_OCR_CORE_DEPS = [
    'imagesize',
    'opencv-contrib-python',
    'pyclipper',
    'pypdfium2',
    'python-bidi',
    'shapely',
]
for _dep in _OCR_CORE_DEPS:
    _d, _b, _h = collect_all(_dep)
    paddleocr_datas += _d + copy_metadata(_dep)
    paddleocr_bins += _b
    paddleocr_hidden += _h

hiddenimports = (
    pyzbar_imports +
    zxing_imports +
    pil_imports +
    paddleocr_hidden +
    paddlex_hidden +
    paddle_hidden +
    ['docx']
)

datas = (
    paddleocr_datas +
    paddlex_datas +
    paddle_datas
)

binaries = (
    paddleocr_bins +
    paddlex_bins +
    paddle_bins
)

a = Analysis(
    ['essay_grader.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['pyi_runtime_hook.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='EssayGrader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='NONE',
)

# --onedir 模式：收集所有文件到目录，避免 onefile 解压导致的
# Paddle/MKL 动态库 SIGILL 问题，且更易于调试和分发
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='EssayGrader',
)
