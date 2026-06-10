# -*- coding: utf-8 -*-
"""
PyInstaller 运行时 Hook：PaddleOCR / PaddleX 单文件打包兼容。

patch get_dep_version：importlib.metadata 找不到 .dist-info 时
fallback 到 importlib.util.find_spec。

注：原生库搜索路径（LD_LIBRARY_PATH / PATH）不在此设置，
由启动脚本 run.sh / run.bat 在进程启动前设置，确保 dlopen/LoadLibrary 可靠生效。
"""

import importlib.util
from paddlex.utils import deps

_original_get_dep_version = deps.get_dep_version


def _patched_get_dep_version(dep):
    version = _original_get_dep_version(dep)
    if version is not None:
        return version
    try:
        if importlib.util.find_spec(dep) is not None:
            return "0.0.0-pyinstaller"
    except (ImportError, ValueError, ModuleNotFoundError):
        pass
    return None


deps.get_dep_version = _patched_get_dep_version

for _fn in [deps.is_dep_available, deps.is_extra_available]:
    if hasattr(_fn, "cache_clear"):
        _fn.cache_clear()
