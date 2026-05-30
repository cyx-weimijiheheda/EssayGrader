# -*- coding: utf-8 -*-
"""
高中英语应用文批改工具 — 设置对话框
"""

import os
import json
import base64
import hashlib

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLineEdit, QTextEdit, QCheckBox,
    QRadioButton, QButtonGroup, QPushButton,
    QDialogButtonBox, QMessageBox, QLabel
)
from PySide6.QtCore import Qt

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(CONFIG_DIR, "essay_grader_config.json")

# ==================== 旧格式迁移 ====================

_LEGACY_SECRET = "essay-grader-secret-key-v1"
_LEGACY_KEY = hashlib.sha256(_LEGACY_SECRET.encode()).digest()


def _legacy_decrypt(ciphertext: str) -> str:
    """解密旧版加密的 api_key（仅用于迁移）"""
    if not ciphertext or not ciphertext.startswith("ENC:"):
        return ciphertext
    try:
        encrypted = base64.urlsafe_b64decode(ciphertext[4:].encode())
        decrypted = bytes(e ^ _LEGACY_KEY[i % len(_LEGACY_KEY)] for i, e in enumerate(encrypted))
        return decrypted.decode("utf-8")
    except Exception:
        return ciphertext


def _migrate_config(config: dict) -> dict:
    """将旧版嵌套/加密配置迁移为扁平明文结构"""
    if not config:
        return config
    # 迁移嵌套的 deepseek 配置
    if "deepseek" in config and isinstance(config["deepseek"], dict):
        ds = config.pop("deepseek")
        config.setdefault("deepseek_api_key", _legacy_decrypt(ds.get("api_key", "")))
        config.setdefault("deepseek_model", ds.get("model", "deepseek-chat"))
    # 迁移嵌套的 qwen 配置
    if "qwen" in config and isinstance(config["qwen"], dict):
        qw = config.pop("qwen")
        config.setdefault("qwen_api_key", _legacy_decrypt(qw.get("api_key", "")))
        config.setdefault("qwen_model", qw.get("model", "qwen-vl-max"))
        config.setdefault("qwen_api_base", qw.get("api_base", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
    # 迁移嵌套的 grading_options
    if "grading_options" in config and isinstance(config["grading_options"], dict):
        go = config.pop("grading_options")
        for k, v in go.items():
            config.setdefault(k, v)
    # 解密可能残留的旧加密值
    for key in list(config.keys()):
        if isinstance(config[key], str) and config[key].startswith("ENC:"):
            config[key] = _legacy_decrypt(config[key])
    return config


# ==================== 扁平配置 ====================

DEFAULT_CONFIG = {
    "deepseek_api_key": "",
    "deepseek_model": "deepseek-chat",
    "qwen_api_key": "",
    "qwen_model": "qwen-vl-max",
    "qwen_api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "ocr_method": "qwen",
    "with_score": True,
    "with_comment": True,
    "with_correction": True,
    "ocr_correction": True,
    "skip_duplicates": True,
    "with_polish": True,
    "two_pages_per_student": True,
    "extra_requirements": "",
    "essay_title": "",
    "last_folder": ""
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}
    else:
        config = {}
    config = _migrate_config(config)
    # 用默认值填补缺失的键
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    # 如果迁移后配置有变化，回写为明文新格式
    if config != merged:
        _write_plain(merged)
    return merged


def save_config(config: dict) -> None:
    _write_plain(config)


def _write_plain(config: dict) -> None:
    """明文写入配置文件"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ==================== 设置对话框 ====================

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(500)
        self.config = load_config()
        self.setup_ui()
        self.load_ui_from_config()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # DeepSeek API
        ds_group = QGroupBox("DeepSeek API 配置（批改评分用）")
        ds_layout = QFormLayout(ds_group)

        self.ds_api_key = QLineEdit()
        self.ds_api_key.setEchoMode(QLineEdit.Password)
        self.ds_api_key.setPlaceholderText("sk-...")
        ds_layout.addRow("API Key:", self.ds_api_key)

        self.ds_model = QLineEdit()
        self.ds_model.setPlaceholderText("deepseek-chat")
        ds_layout.addRow("模型名称:", self.ds_model)

        layout.addWidget(ds_group)

        # Qwen API
        qw_group = QGroupBox("阿里云 Qwen API 配置（OCR识别用）")
        qw_layout = QFormLayout(qw_group)

        self.qw_api_key = QLineEdit()
        self.qw_api_key.setEchoMode(QLineEdit.Password)
        self.qw_api_key.setPlaceholderText("sk-...")
        qw_layout.addRow("API Key:", self.qw_api_key)

        self.qw_model = QLineEdit()
        self.qw_model.setPlaceholderText("qwen-vl-max")
        qw_layout.addRow("模型名称:", self.qw_model)

        self.qw_api_base = QLineEdit()
        self.qw_api_base.setPlaceholderText("https://dashscope.aliyuncs.com/compatible-mode/v1")
        qw_layout.addRow("API 端点:", self.qw_api_base)

        layout.addWidget(qw_group)

        # OCR method
        ocr_group = QGroupBox("OCR 识别方式")
        ocr_layout = QVBoxLayout(ocr_group)

        self.ocr_btn_group = QButtonGroup(self)
        self.radio_qwen = QRadioButton("阿里云 Qwen OCR（推荐，需联网）")
        self.radio_rapidocr = QRadioButton("本地 RapidOCR（离线，无需API）")
        self.ocr_btn_group.addButton(self.radio_qwen, 0)
        self.ocr_btn_group.addButton(self.radio_rapidocr, 1)

        ocr_layout.addWidget(self.radio_qwen)
        ocr_layout.addWidget(self.radio_rapidocr)

        layout.addWidget(ocr_group)

        # Grading options
        grading_group = QGroupBox("批改选项")
        grading_layout = QVBoxLayout(grading_group)

        self.chk_score = QCheckBox("出具分数（取消则不返回各项得分）")
        self.chk_comment = QCheckBox("生成精到评语（取消则不返回文字评语）")
        self.chk_correction = QCheckBox("生成改错修正版（取消则仅指出错误，不输出修正全文）")
        self.chk_ocr_correction = QCheckBox("执行 OCR 修正阶段（推荐开启，先修正OCR错误再批改）")
        self.chk_skip_duplicates = QCheckBox("避免重复批改（跳过已批改的相同图片）")
        self.chk_polish = QCheckBox("生成精修升格范文（基于原文和批改结果，生成高分升格版）")
        self.chk_two_pages = QCheckBox("每位学生 DOCX 占满 2 页（不足补空页，方便打印分发）")

        grading_layout.addWidget(self.chk_score)
        grading_layout.addWidget(self.chk_comment)
        grading_layout.addWidget(self.chk_correction)
        grading_layout.addWidget(self.chk_ocr_correction)
        grading_layout.addWidget(self.chk_skip_duplicates)
        grading_layout.addWidget(self.chk_polish)
        grading_layout.addWidget(self.chk_two_pages)

        layout.addWidget(grading_group)

        # Extra requirements
        extra_group = QGroupBox("附加批改要求")
        extra_layout = QVBoxLayout(extra_group)

        self.extra_requirements = QTextEdit()
        placeholder = (
            "在此填写特殊的批改要求，留空则默认填入「无」。\n"
            "例如：\n"
            "- 重点关注时态错误\n"
            "- 注意书信格式是否规范\n"
            "- 检查是否有中式英语表达\n"
            "- 本次作文为建议信，请重点检查建议句型的多样性"
        )
        self.extra_requirements.setPlaceholderText(placeholder)
        self.extra_requirements.setMaximumHeight(120)
        extra_layout.addWidget(self.extra_requirements)

        layout.addWidget(extra_group)

        # Buttons
        btn_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.on_save)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def load_ui_from_config(self):
        self.ds_api_key.setText(self.config.get("deepseek_api_key", ""))
        self.ds_model.setText(self.config.get("deepseek_model", "deepseek-chat"))

        self.qw_api_key.setText(self.config.get("qwen_api_key", ""))
        self.qw_model.setText(self.config.get("qwen_model", "qwen-vl-max"))
        self.qw_api_base.setText(self.config.get("qwen_api_base", ""))

        if self.config.get("ocr_method", "qwen") == "qwen":
            self.radio_qwen.setChecked(True)
        else:
            self.radio_rapidocr.setChecked(True)

        self.chk_score.setChecked(self.config.get("with_score", True))
        self.chk_comment.setChecked(self.config.get("with_comment", True))
        self.chk_correction.setChecked(self.config.get("with_correction", True))
        self.chk_ocr_correction.setChecked(self.config.get("ocr_correction", True))
        self.chk_skip_duplicates.setChecked(self.config.get("skip_duplicates", True))
        self.chk_polish.setChecked(self.config.get("with_polish", True))
        self.chk_two_pages.setChecked(self.config.get("two_pages_per_student", True))

        self.extra_requirements.setPlainText(self.config.get("extra_requirements", ""))

    def on_save(self):
        self.config["deepseek_api_key"] = self.ds_api_key.text().strip()
        self.config["deepseek_model"] = self.ds_model.text().strip() or "deepseek-chat"

        self.config["qwen_api_key"] = self.qw_api_key.text().strip()
        self.config["qwen_model"] = self.qw_model.text().strip() or "qwen-vl-max"
        self.config["qwen_api_base"] = self.qw_api_base.text().strip() or "https://dashscope.aliyuncs.com/compatible-mode/v1"

        self.config["ocr_method"] = "qwen" if self.radio_qwen.isChecked() else "rapidocr"

        self.config["with_score"] = self.chk_score.isChecked()
        self.config["with_comment"] = self.chk_comment.isChecked()
        self.config["with_correction"] = self.chk_correction.isChecked()
        self.config["ocr_correction"] = self.chk_ocr_correction.isChecked()
        self.config["skip_duplicates"] = self.chk_skip_duplicates.isChecked()
        self.config["with_polish"] = self.chk_polish.isChecked()
        self.config["two_pages_per_student"] = self.chk_two_pages.isChecked()

        self.config["extra_requirements"] = self.extra_requirements.toPlainText().strip()

        save_config(self.config)
        self.accept()
