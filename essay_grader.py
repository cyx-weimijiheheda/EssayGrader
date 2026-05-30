# -*- coding: utf-8 -*-
"""
高中英语应用文批量批改工具
功能：批量识别文件夹中的手写作文图片，调用DeepSeek API进行批改评分
OCR：支持阿里云Qwen-VL API / 本地RapidOCR双模式
依赖：pip install PySide6 requests rapidocr-onnxruntime python-docx
"""

import sys
import os
import json
import time
import re
import base64
import hashlib
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QListWidget, QListWidgetItem,
    QFileDialog, QMessageBox, QProgressBar, QGroupBox, QFormLayout,
    QLineEdit, QSplitter, QPlainTextEdit
)
from PySide6.QtCore import QThread, Signal, Qt, QMutex, QMutexLocker
from PySide6.QtGui import QFont, QAction

import requests

from prompts import (
    build_ocr_correction_prompt, build_grading_prompt, build_polish_prompt,
    build_ocr_prompt, set_ocr_custom_prompt
)
from settings_dialog import SettingsDialog, load_config as load_config_file, save_config as save_config_file
from export_docx import export_to_docx


# ==================== 工具函数 ====================

def _parse_json_response(content: str) -> dict:
    """从DeepSeek API响应中提取JSON"""
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()
    return json.loads(content)


def render_inline_errors(text: str) -> str:
    """将 [错误:原文→修改|理由] 内联标记转换为高亮HTML"""
    pattern = r'\[错误:(.+?)→(.+?)\|(.+?)\]'

    def replacer(match):
        original = match.group(1)
        correction = match.group(2)
        reason = match.group(3)
        return (
            f'<span style="background-color:#ffe0e0;text-decoration:line-through;'
            f'padding:1px 3px;border-radius:2px;">{original}</span>'
            f' &rarr; '
            f'<span style="background-color:#e0ffe0;'
            f'padding:1px 3px;border-radius:2px;">{correction}</span>'
            f' <small style="color:#888;">({reason})</small>'
        )
    return re.sub(pattern, replacer, text)


# ==================== 已批改缓存 ====================
CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(CACHE_DIR, "graded_cache.json")
LOG_FILE = os.path.join(CACHE_DIR, "last_log.txt")


class GradedCache:
    """记录已批改图片的特征值（SHA256），按学生标识匹配避免重复批改"""

    def __init__(self, cache_path: str = CACHE_PATH):
        self.cache_path = cache_path
        self.entries: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            if "entries" in data:
                self.entries = data["entries"]
            elif data:
                # 旧格式迁移: {folder: {filename: sha}} → 新格式
                self._migrate(data)

    def _migrate(self, old_data: dict):
        for folder, files in old_data.items():
            for filename, sha in files.items():
                self.entries[filename] = {
                    "sha256": sha,
                    "student_name": "",
                    "student_class": "",
                    "folder": folder,
                    "filename": filename
                }
        self.save()

    def save(self):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump({"entries": self.entries}, f, ensure_ascii=False, indent=2)

    def _file_sha256(self, filepath: str) -> str:
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    def check_cache(self, filepath: str) -> dict | None:
        """检查文件是否已批改，返回缓存的批改结果或None"""
        current_sha = self._file_sha256(filepath)
        for entry in self.entries.values():
            if entry.get("sha256") == current_sha and "result" in entry:
                return entry["result"]
        return None

    def mark_graded(self, barcode_data: str, student_name: str, student_class: str,
                    folder: str, filename: str, filepath: str, result: dict):
        """标记为已批改，主键: 考号 > 姓名 > 文件名，同时存储批改结果"""
        sha = self._file_sha256(filepath)
        key = barcode_data or student_name or filename

        self.entries[key] = {
            "sha256": sha,
            "student_name": student_name,
            "student_class": student_class,
            "folder": folder,
            "filename": filename,
            "result": result
        }
        self.save()


# ==================== 批改工作线程 ====================
class GraderWorker(QThread):
    log_updated = Signal(str)
    progress_updated = Signal(int, int)
    result_ready = Signal(str, dict)
    finished = Signal()
    error_occurred = Signal(str)

    def __init__(self, image_folder: str, essay_title: str, config: dict):
        super().__init__()
        self.image_folder = image_folder
        self.essay_title = essay_title
        self.config = config
        self._is_cancelled = False
        self._mutex = QMutex()
        self._rapidocr = None
        self.cache = GradedCache()
        self.skip_duplicates = config.get("skip_duplicates", True)

    def cancel(self):
        with QMutexLocker(self._mutex):
            self._is_cancelled = True

    def is_cancelled(self):
        with QMutexLocker(self._mutex):
            return self._is_cancelled

    # ---- OCR 调度 ----

    def _do_ocr(self, image_path: str) -> dict:
        """根据配置选择OCR方式，返回 {ocr_text, student_name, student_class}"""
        ocr_method = self.config.get("ocr_method", "qwen")
        if ocr_method == "qwen":
            return self._qwen_ocr_image(image_path)
        elif ocr_method == "ollama":
            return self._ollama_ocr_image(image_path)
        else:
            text = self._rapidocr_image(image_path)
            return {"ocr_text": text, "student_name": "", "student_class": ""}

    # ---- Qwen OCR ----

    def _qwen_ocr_image(self, image_path: str) -> dict:
        """使用阿里云Qwen-VL API进行OCR识别，返回 {ocr_text, student_name, student_class}"""
        api_key = self.config.get("qwen_api_key", "")
        api_base = self.config.get("qwen_api_base", "").rstrip("/")
        model = self.config.get("qwen_model", "qwen-vl-max")

        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".bmp": "image/bmp"}
        mime_type = mime_map.get(ext, "image/jpeg")
        data_uri = f"data:{mime_type};base64,{image_data}"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": build_ocr_prompt("qwen", model)}
                    ]
                }
            ],
            "max_tokens": 2000
        }

        api_url = f"{api_base}/chat/completions"
        response = requests.post(api_url, headers=headers, json=payload, timeout=60)
        if not response.ok:
            detail = ""
            try:
                err = response.json()
                detail = err.get("error", {}).get("message", "") or err.get("message", "") or str(err)
            except Exception:
                detail = response.text[:200]
            raise Exception(f"Qwen API 返回 {response.status_code}: {detail}")
        result = response.json()
        content = result["choices"][0]["message"]["content"].strip()

        # 解析 Qwen 返回的 JSON
        try:
            data = _parse_json_response(content)
            return {
                "ocr_text": data.get("recognized_text", content),
                "student_name": data.get("student_name", ""),
                "student_class": data.get("student_class", "")
            }
        except (json.JSONDecodeError, KeyError):
            # 兼容旧格式（纯文本）
            return {
                "ocr_text": content,
                "student_name": "",
                "student_class": ""
            }

    # ---- Ollama OCR ----

    def _ollama_ocr_image(self, image_path: str) -> dict:
        """使用本地Ollama VL模型进行OCR识别"""
        host = self.config.get("ollama_host", "localhost")
        port = self.config.get("ollama_port", "11434")
        model = self.config.get("ollama_model", "qwen2.5vl:7b")

        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".bmp": "image/bmp"}
        mime_type = mime_map.get(ext, "image/jpeg")
        data_uri = f"data:{mime_type};base64,{image_data}"

        headers = {"Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": build_ocr_prompt("ollama", model)}
                    ]
                }
            ],
            "max_tokens": 2000
        }

        api_url = f"http://{host}:{port}/v1/chat/completions"
        response = requests.post(api_url, headers=headers, json=payload, timeout=120)
        if not response.ok:
            detail = ""
            try:
                err = response.json()
                detail = err.get("error", {}).get("message", "")
                if not detail:
                    detail = err.get("error", str(err))
            except Exception:
                detail = response.text[:200]
            raise Exception(f"Ollama API 返回 {response.status_code}: {detail}")
        result = response.json()
        content = result["choices"][0]["message"]["content"].strip()

        try:
            data = _parse_json_response(content)
            return {
                "ocr_text": data.get("recognized_text", content),
                "student_name": data.get("student_name", ""),
                "student_class": data.get("student_class", "")
            }
        except (json.JSONDecodeError, KeyError):
            return {
                "ocr_text": content,
                "student_name": "",
                "student_class": ""
            }

    # ---- RapidOCR ----

    def _init_rapidocr(self):
        """延迟初始化RapidOCR"""
        if self._rapidocr is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                self._rapidocr = RapidOCR()
            except Exception as e:
                raise Exception(f"RapidOCR初始化失败: {str(e)}")

    def _rapidocr_image(self, image_path: str) -> str:
        """使用RapidOCR本地识别单张图片"""
        self._init_rapidocr()
        result, _ = self._rapidocr(image_path)
        if not result:
            return ""
        text_lines = [line[1] for line in result]
        return "\n".join(text_lines)

    def _scan_barcode(self, image_path: str) -> str:
        """扫描图片中的条形码/二维码，pyzbar 多预处理变体 + zxing-cpp 兜底"""
        from PIL import Image, ImageEnhance, ImageOps

        def _decode_pyzbar(img) -> list:
            try:
                from pyzbar.pyzbar import decode
                return decode(img)
            except Exception:
                return []

        def _decode_zxing(img) -> list:
            try:
                import zxingcpp
                return zxingcpp.read_barcodes(
                    img, formats=zxingcpp.BarcodeFormat.LinearCodes,
                    try_rotate=True, try_downscale=True, try_invert=True
                )
            except Exception:
                return []

        img = Image.open(image_path)

        # 收集所有预处理变体
        variants = [img, img.convert("L")]
        try:
            gray = img.convert("L")
            variants.append(ImageEnhance.Contrast(gray).enhance(2.0))
            variants.append(ImageEnhance.Contrast(gray).enhance(3.0))
            variants.append(ImageOps.autocontrast(gray))
            variants.append(gray.point(lambda p: 255 if p > 128 else 0, "1"))
            variants.append(gray.point(lambda p: 255 if p > 100 else 0, "1"))
        except Exception:
            pass

        # pyzbar 主识别（去重）
        seen = set()
        for v in variants:
            for barcode in _decode_pyzbar(v):
                try:
                    text = barcode.data.decode("utf-8").strip()
                except Exception:
                    text = str(barcode.data).strip()
                key = (barcode.type, text)
                if key not in seen and text:
                    seen.add(key)
                    return text

        # zxing-cpp 兜底
        for v in variants:
            for result in _decode_zxing(v):
                text = result.text.strip()
                if text:
                    return text

        return ""

    # ---- DeepSeek API 调用 ----

    def _call_deepseek(self, system_prompt: str, user_content: str, max_tokens: int = 2000) -> dict:
        """通用DeepSeek API调用"""
        headers = {
            "Authorization": f"Bearer {self.config.get('deepseek_api_key', '')}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.config.get("deepseek_model", "deepseek-chat"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens
        }
        api_url = "https://api.deepseek.com/v1/chat/completions"
        response = requests.post(api_url, headers=headers, json=payload, timeout=90)
        response.raise_for_status()
        result = response.json()
        content = result["choices"][0]["message"]["content"]
        return _parse_json_response(content)

    def _call_ocr_correction(self, ocr_text: str) -> dict:
        """第一阶段：OCR修正"""
        extra_req = self.config.get("extra_requirements", "") or "无"

        system_prompt = build_ocr_correction_prompt()
        user_content = f"""【作文题目】
{self.essay_title}

【附加要点】
{extra_req}

【OCR原始识别文本】：
{ocr_text}"""

        return self._call_deepseek(system_prompt, user_content, max_tokens=2000)

    def _call_grading(self, text_to_grade: str) -> dict:
        """第二阶段：批改评分"""
        extra_req = self.config.get("extra_requirements", "") or "无"
        system_prompt = build_grading_prompt(
            with_score=self.config.get("with_score", True),
            with_comment=self.config.get("with_comment", True),
            with_correction=self.config.get("with_correction", True),
            extra_requirements=extra_req
        )
        user_content = f"""【作文题目】
{self.essay_title}

【附加要点】
{extra_req}

【学生作文文本】：
{text_to_grade}"""

        return self._call_deepseek(system_prompt, user_content, max_tokens=3000)

    def _call_polish(self, original_text: str, grading_result: dict) -> dict:
        """精修升格阶段：基于原文和批改结果生成升格范文"""
        system_prompt = build_polish_prompt()
        errors = grading_result.get("errors", [])
        comment = grading_result.get("comment", "")
        score = grading_result.get("total_score", 0)

        error_summary = "\n".join(f"- {e}" for e in errors) if errors else "无明显错误"

        user_content = f"""【作文题目】
{self.essay_title}

【学生原文】
{original_text}

【当前得分】
{score}/15

【批改发现的错误】
{error_summary}

【教师评语】
{comment}"""

        return self._call_deepseek(system_prompt, user_content, max_tokens=2000)

    # ---- 错误结果构造 ----

    def _make_error_result(self, error_msg: str, ocr_text: str = "") -> dict:
        return {
            "ocr_corrected_text": ocr_text,
            "total_score": 0,
            "content_score": 0,
            "language_score": 0,
            "structure_score": 0,
            "format_score": 0,
            "errors": [error_msg],
            "ocr_suspicions": [],
            "comment": error_msg,
            "corrected_version": ocr_text,
            "polished_version": "",
            "learning_version": "",
            "student_name": "",
            "student_class": "",
            "barcode_data": ""
        }

    # ---- 主流程 ----

    def run(self):
        # RapidOCR模式下预先初始化
        if self.config.get("ocr_method", "qwen") == "rapidocr":
            try:
                self._init_rapidocr()
            except Exception as e:
                self.error_occurred.emit(str(e))
                self.finished.emit()
                return

        # 获取图片文件
        image_extensions = {'.jpg', '.jpeg', '.png', '.bmp'}
        image_files = []
        for f in os.listdir(self.image_folder):
            ext = os.path.splitext(f)[1].lower()
            if ext in image_extensions:
                image_files.append(f)

        if not image_files:
            self.error_occurred.emit("未找到图片文件（支持jpg/jpeg/png/bmp）")
            self.finished.emit()
            return

        total = len(image_files)
        ocr_method = self.config.get("ocr_method", "qwen")
        ocr_method_label = {"qwen": "Qwen", "ollama": "Ollama", "rapidocr": "RapidOCR"}.get(ocr_method, ocr_method)
        self.log_updated.emit(f"找到 {total} 张图片，OCR方式: {ocr_method_label}，开始批改...")

        do_ocr_correction = self.config.get("ocr_correction", True)

        for idx, filename in enumerate(image_files):
            if self.is_cancelled():
                self.log_updated.emit("批改任务已取消")
                break

            self.progress_updated.emit(idx + 1, total)
            image_path = os.path.join(self.image_folder, filename)

            self.log_updated.emit(f"[{idx + 1}/{total}] {filename}")

            # ---- 步骤1：SHA256缓存检查（纯本地，不调API） ----
            if self.skip_duplicates:
                cached_result = self.cache.check_cache(image_path)
                if cached_result is not None:
                    self.log_updated.emit(f"  - 命中缓存，跳过（得分: {cached_result.get('total_score', 0)}/15）")
                    self.result_ready.emit(filename, cached_result)
                    continue

            # ---- 步骤2：条形码扫描 ----
            barcode_data = ""
            if self.config.get("detect_barcode", True):
                barcode_data = self._scan_barcode(image_path)
                if barcode_data:
                    self.log_updated.emit(f"  - 条形码识别: {barcode_data}")
                else:
                    self.log_updated.emit("  - 未检测到条形码")

            # ---- 步骤3：OCR识别 ----
            try:
                self.log_updated.emit("  - OCR识别中...")
                ocr_result = self._do_ocr(image_path)
                ocr_text = ocr_result.get("ocr_text", "")
                student_name = ocr_result.get("student_name", "") if self.config.get("detect_name", True) else ""
                student_class = ocr_result.get("student_class", "") if self.config.get("detect_class", True) else ""
                if not ocr_text:
                    self.log_updated.emit("  - 警告: OCR识别为空")
                else:
                    self.log_updated.emit(f"  - OCR完成，共{len(ocr_text)}字符")
                if student_name:
                    self.log_updated.emit(f"  - 识别姓名: {student_name}")
                if student_class:
                    self.log_updated.emit(f"  - 识别班级: {student_class}")
            except Exception as e:
                self.log_updated.emit(f"  - OCR失败: {e}")
                self.result_ready.emit(filename, self._make_error_result(f"OCR识别失败: {str(e)}"))
                continue

            # ---- 步骤4：OCR修正（可选） ----
            if do_ocr_correction:
                try:
                    self.log_updated.emit("  - DeepSeek OCR修正中...")
                    correction_result = self._call_ocr_correction(ocr_text)
                    text_for_grading = correction_result.get("ocr_corrected_text", ocr_text)
                    self.log_updated.emit(f"  - OCR修正完成，共{len(text_for_grading)}字符")
                    time.sleep(0.5)
                except Exception as e:
                    self.log_updated.emit(f"  - OCR修正失败: {e}，使用原始OCR文本继续")
                    text_for_grading = ocr_text
                    correction_result = {"ocr_corrected_text": ocr_text}
            else:
                text_for_grading = ocr_text
                correction_result = {"ocr_corrected_text": ocr_text}
                self.log_updated.emit("  - 跳过OCR修正阶段")

            # ---- 步骤5：批改评分 ----
            try:
                self.log_updated.emit("  - DeepSeek批改评分中...")
                grading_result = self._call_grading(text_for_grading)
                total_score = grading_result.get("total_score", 0)
                self.log_updated.emit(f"  - 批改完成，得分: {total_score}/15")
            except Exception as e:
                self.log_updated.emit(f"  - 批改失败: {e}")
                error_result = self._make_error_result(f"DeepSeek批改失败: {str(e)}", text_for_grading)
                error_result["student_name"] = student_name
                error_result["student_class"] = student_class
                error_result["barcode_data"] = barcode_data
                self.result_ready.emit(filename, error_result)
                continue

            # ---- 步骤6：精修升格范文（可选） ----
            polished_version = ""
            learning_version = ""
            if self.config.get("with_polish", True):
                try:
                    self.log_updated.emit("  - DeepSeek精修升格中...")
                    polish_result = self._call_polish(text_for_grading, grading_result)
                    polished_version = polish_result.get("polished_version", "")
                    learning_version = polish_result.get("learning_version", "")
                    self.log_updated.emit(f"  - 升格范文生成完成，共{len(polished_version)}字符")
                    if learning_version:
                        self.log_updated.emit(f"  - 检测到主题偏离，已额外生成学习版范文（{len(learning_version)}字符）")
                    time.sleep(0.5)
                except Exception as e:
                    self.log_updated.emit(f"  - 升格范文生成失败: {e}")

            # ---- 合并结果 ----
            combined = {
                "ocr_corrected_text": correction_result.get("ocr_corrected_text", ocr_text),
                "total_score": grading_result.get("total_score", 0),
                "content_score": grading_result.get("content_score", 0),
                "language_score": grading_result.get("language_score", 0),
                "structure_score": grading_result.get("structure_score", 0),
                "format_score": grading_result.get("format_score", 0),
                "errors": grading_result.get("errors", []),
                "ocr_suspicions": grading_result.get("ocr_suspicions", []),
                "comment": grading_result.get("comment", ""),
                "corrected_version": grading_result.get("corrected_version", ""),
                "polished_version": polished_version,
                "learning_version": learning_version,
                "student_name": student_name,
                "student_class": student_class,
                "barcode_data": barcode_data
            }
            self.result_ready.emit(filename, combined)
            self.cache.mark_graded(barcode_data, student_name, student_class,
                                   self.image_folder, filename, image_path, combined)
            time.sleep(0.5)

        self.finished.emit()


# ==================== 主窗口 ====================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("高中英语应用文批量批改工具")
        self.setMinimumSize(1000, 700)

        self.worker = None
        self.all_results = []
        self._config = None

        self.setup_ui()
        self.load_main_config()

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # ---- 菜单栏 ----
        menubar = self.menuBar()
        settings_menu = menubar.addMenu("文件")
        settings_action = QAction("设置", self)
        settings_action.triggered.connect(self.open_settings)
        settings_menu.addAction(settings_action)

        # ---- 批改参数 ----
        params_group = QGroupBox("批改参数")
        params_layout = QFormLayout(params_group)

        self.essay_title = QTextEdit()
        self.essay_title.setPlaceholderText("例如：假定你是李华，给你的英国笔友Chris写一封邮件...")
        self.essay_title.setMaximumHeight(80)
        params_layout.addRow("作文题目:", self.essay_title)

        self.folder_path = QLineEdit()
        self.folder_path.setPlaceholderText("请选择包含作文图片的文件夹")
        folder_layout = QHBoxLayout()
        folder_layout.addWidget(self.folder_path)
        self.browse_btn = QPushButton("浏览文件夹")
        self.browse_btn.clicked.connect(self.select_folder)
        folder_layout.addWidget(self.browse_btn)
        params_layout.addRow("图片文件夹:", folder_layout)

        main_layout.addWidget(params_group)

        # ---- 主内容区 ----
        splitter = QSplitter(Qt.Horizontal)

        # 左侧：结果列表
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("批改结果列表:"))
        self.file_list = QListWidget()
        self.file_list.currentRowChanged.connect(self.on_list_selected)
        left_layout.addWidget(self.file_list)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        left_layout.addWidget(self.progress_bar)

        splitter.addWidget(left_widget)

        # 右侧：详情 + 日志
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setFont(QFont("Microsoft YaHei", 10))
        right_layout.addWidget(QLabel("批改详情:"))
        right_layout.addWidget(self.result_text)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        self.log_text.setFont(QFont("Consolas", 9))
        right_layout.addWidget(QLabel("运行日志:"))
        right_layout.addWidget(self.log_text)

        splitter.addWidget(right_widget)
        splitter.setSizes([300, 700])

        main_layout.addWidget(splitter)

        # ---- 底部按钮 ----
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始批改")
        self.start_btn.clicked.connect(self.start_grading)
        self.start_btn.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold;"
        )

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.cancel_grading)
        self.cancel_btn.setEnabled(False)

        self.export_btn = QPushButton("导出JSON")
        self.export_btn.clicked.connect(self.export_results)

        self.export_docx_btn = QPushButton("导出DOCX")
        self.export_docx_btn.clicked.connect(self.export_docx)

        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.export_btn)
        btn_layout.addWidget(self.export_docx_btn)
        btn_layout.addStretch()
        main_layout.addLayout(btn_layout)

    # ---- 配置管理 ----

    def _current_config(self) -> dict:
        """获取当前完整配置，合并主窗口的临时字段"""
        config = load_config_file()
        config["essay_title"] = self.essay_title.toPlainText()
        config["last_folder"] = self.folder_path.text()
        return config

    def load_main_config(self):
        """加载配置到主窗口"""
        config = load_config_file()
        self.essay_title.setPlainText(config.get("essay_title", ""))
        self.folder_path.setText(config.get("last_folder", ""))
        set_ocr_custom_prompt(config.get("ocr_custom_prompt", ""))

    def save_main_fields(self):
        """保存主窗口字段到配置文件"""
        config = load_config_file()
        config["essay_title"] = self.essay_title.toPlainText()
        config["last_folder"] = self.folder_path.text()
        save_config_file(config)

    # ---- 操作 ----

    def open_settings(self):
        """打开设置对话框"""
        dialog = SettingsDialog(self)
        if dialog.exec():
            self.load_main_config()
            self.append_log("设置已更新")

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择包含作文图片的文件夹")
        if folder:
            self.folder_path.setText(folder)
            self.save_main_fields()

    def start_grading(self):
        folder = self.folder_path.text()
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "错误", "请选择有效的图片文件夹")
            return

        config = self._current_config()

        # 根据OCR方式校验
        ocr_method = config.get("ocr_method", "qwen")
        if ocr_method == "qwen":
            if not config.get("qwen_api_key", ""):
                QMessageBox.warning(self, "错误",
                    "请在 文件→设置 中配置阿里云Qwen API Key，\n"
                    "或切换到其他OCR模式")
                return
        # ollama / rapidocr 不需要API Key

        if not config.get("deepseek_api_key", ""):
            QMessageBox.warning(self, "错误", "请在 文件→设置 中配置DeepSeek API Key")
            return
        if not self.essay_title.toPlainText():
            QMessageBox.warning(self, "错误", "请输入作文题目")
            return

        self.save_main_fields()

        # 初始化日志文件（覆盖写入会话头）
        try:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"=== 批改任务开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                f.write(f"=== 文件夹: {folder} ===\n")
                f.write(f"=== 题目: {self.essay_title.toPlainText()} ===\n\n")
        except Exception:
            pass

        # 清空旧数据
        self.file_list.clear()
        self.result_text.clear()
        self.all_results.clear()

        self.worker = GraderWorker(folder, self.essay_title.toPlainText(), config)
        self.worker.log_updated.connect(self.append_log)
        self.worker.progress_updated.connect(self.update_progress)
        self.worker.result_ready.connect(self.on_result_ready)
        self.worker.finished.connect(self.on_finished)
        self.worker.error_occurred.connect(self.on_error)

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        self.worker.start()

    def cancel_grading(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.append_log("正在取消批改任务...")

    def append_log(self, msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {msg}"
        self.log_text.appendPlainText(formatted)
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(formatted + "\n")
        except Exception:
            pass

    def update_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.progress_bar.setFormat(f"{current}/{total}")

    def on_result_ready(self, filename, result):
        self.all_results.append({"filename": filename, "result": result})
        total_score = result.get("total_score", 0)
        student_name = result.get("student_name", "")
        label = f"{filename}"
        if student_name:
            label += f"  [{student_name}]"
        label += f"  {total_score}/15"
        item = QListWidgetItem(label)
        self.file_list.addItem(item)

    def on_list_selected(self, index):
        if 0 <= index < len(self.all_results):
            self.display_result(self.all_results[index]["result"])

    def display_result(self, result):
        total = result.get("total_score", 0)
        content = result.get("content_score", 0)
        language = result.get("language_score", 0)
        structure = result.get("structure_score", 0)
        fmt = result.get("format_score", 0)
        errors = result.get("errors", [])
        ocr_suspicions = result.get("ocr_suspicions", [])
        comment = result.get("comment", "")
        corrected = result.get("corrected_version", "")
        ocr_corrected = result.get("ocr_corrected_text", "")
        polished = result.get("polished_version", "")
        student_name = result.get("student_name", "")
        student_class = result.get("student_class", "")
        barcode_data = result.get("barcode_data", "")

        # 学生信息行
        info_parts = []
        if student_name:
            info_parts.append(f"姓名：{student_name}")
        if student_class:
            info_parts.append(f"班级：{student_class}")
        if barcode_data:
            info_parts.append(f"条码：{barcode_data}")
        info_html = ""
        if info_parts:
            info_html = "<p><b>" + "&nbsp;&nbsp;|&nbsp;&nbsp;".join(info_parts) + "</b></p><hr>"

        html = f"""
        <h3>评分详情</h3>
        {info_html}
        <table border="0" cellpadding="5">
        <tr><td><b>总分：</b></td><td><font color="red" size="5">{total}</font> / 15</td></tr>
        <tr><td>内容要点：</td><td>{content}/5</td></tr>
        <tr><td>语言质量：</td><td>{language}/5</td></tr>
        <tr><td>篇章结构：</td><td>{structure}/3</td></tr>
        <tr><td>格式与语体：</td><td>{fmt}/2</td></tr>
        </table>
        """

        if errors:
            html += "<h4>错误分析：</h4><ul>"
            for err in errors:
                html += f"<li>{err}</li>"
            html += "</ul>"

        if ocr_suspicions:
            html += "<h4>OCR可疑识别：</h4><ul>"
            for sus in ocr_suspicions:
                html += f"<li>{sus}</li>"
            html += "</ul>"

        if comment:
            html += f"<h4>评语：</h4><p style='line-height:1.8;'>{comment}</p>"

        if ocr_corrected:
            html += "<h4>OCR修正版：</h4>"
            html += f"<pre style='background-color:#f5f5f5; padding:10px; border-radius:5px; white-space:pre-wrap;'>{ocr_corrected}</pre>"

        if corrected:
            rendered = render_inline_errors(corrected)
            html += "<h4>修正版（含错误标注）：</h4>"
            html += f"<div style='background-color:#fafafa; padding:12px; border-radius:5px; line-height:2.2;'>{rendered}</div>"
        elif errors:
            html += "<p><i>（本次未生成改错修正版）</i></p>"

        if polished:
            html += "<h4>精修升格范文：</h4>"
            html += f"<div style='background-color:#f0f7ff; padding:12px; border-radius:5px; line-height:2.0; border-left:4px solid #4A90D9;'>{polished}</div>"

        learning = result.get("learning_version", "")
        if learning:
            html += "<h4>学习版范文（主题偏离，直接回应题目要求）：</h4>"
            html += f"<div style='background-color:#fff7e0; padding:12px; border-radius:5px; line-height:2.0; border-left:4px solid #E6A817;'>{learning}</div>"

        self.result_text.setHtml(html)

    def on_finished(self):
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        if self.all_results:
            self.append_log(f"全部批改完成！共 {len(self.all_results)} 篇作文")

    def on_error(self, error_msg):
        self.append_log(f"错误: {error_msg}")
        QMessageBox.critical(self, "错误", error_msg)
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setVisible(False)

    def export_results(self):
        if not self.all_results:
            QMessageBox.warning(self, "警告", "没有可导出的结果")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "保存批改结果",
            f"essay_grading_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            "JSON文件 (*.json)"
        )
        if not file_path:
            return

        export_data = {
            "export_time": datetime.now().isoformat(),
            "essay_title": self.essay_title.toPlainText(),
            "total_count": len(self.all_results),
            "results": self.all_results
        }
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(export_data, f, ensure_ascii=False, indent=2)
            QMessageBox.information(self, "成功", f"结果已保存到:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存失败: {str(e)}")

    def export_docx(self):
        """导出为 DOCX 文件"""
        if not self.all_results:
            QMessageBox.warning(self, "警告", "没有可导出的结果")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出批改结果为DOCX",
            f"essay_grading_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx",
            "Word文档 (*.docx)"
        )
        if not file_path:
            return

        try:
            config = self._current_config()
            export_to_docx(self.all_results, self.essay_title.toPlainText(), file_path,
                           two_pages_per_student=config.get("two_pages_per_student", True))
            QMessageBox.information(self, "成功", f"结果已保存到:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"导出DOCX失败: {str(e)}\n请确认已安装 python-docx")

    def closeEvent(self, event):
        self.save_main_fields()
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "确认", "批改任务正在进行中，确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.worker.cancel()
                self.worker.wait(3000)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


# ==================== 程序入口 ====================
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
