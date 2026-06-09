# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# 收集 rapidocr_onnxruntime 的所有子模块和模型文件
rapidocr_imports = collect_submodules('rapidocr_onnxruntime')
rapidocr_datas = collect_data_files('rapidocr_onnxruntime')

# 收集 onnxruntime 的所有子模块（含原生 .dll / .so）
onnx_imports = collect_submodules('onnxruntime')

# 收集 pyzbar 的子模块
pyzbar_imports = collect_submodules('pyzbar')

# 收集 zxing-cpp 的子模块（含原生库）
zxing_imports = collect_submodules('zxingcpp')

# 收集 PIL 的子模块（条码识别预处理用）
pil_imports = collect_submodules('PIL')

# 收集 paddleocr / paddlepaddle 的子模块
paddleocr_imports = collect_submodules('paddleocr')
paddle_imports = collect_submodules('paddle')

hiddenimports = (
    rapidocr_imports +
    onnx_imports +
    pyzbar_imports +
    zxing_imports +
    pil_imports +
    paddleocr_imports +
    paddle_imports +
    ['python-docx']
)

a = Analysis(
    ['essay_grader.py'],
    pathex=[],
    binaries=[],
    datas=rapidocr_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='EssayGrader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='NONE',
)
