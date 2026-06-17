"""摄像头人脸检测与情绪识别模块。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch
from emotiefflib.facial_analysis import EmotiEffLibRecognizer
from facenet_pytorch import MTCNN
from PIL import Image, ImageDraw, ImageFont

EMOTION_MAP = {
    "happy": "开心",
    "happiness": "开心",
    "sad": "悲伤",
    "sadness": "悲伤",
    "angry": "愤怒",
    "anger": "愤怒",
    "fear": "恐惧",
    "surprise": "惊讶",
    "neutral": "平静",
    "disgust": "厌恶",
    "contempt": "轻蔑",
}

DEFAULT_EMOTION_MODEL = "enet_b0_8_best_vgaf"
EMOTIEFFLIB_CACHE = Path.home() / ".emotiefflib"


from paths import APP_DIR, ROOT


def _find_local_onnx(configured: str | None) -> Path | None:
    """查找本地 ONNX 模型文件。"""
    if configured:
        path = Path(configured)
        if path.is_file() and path.suffix.lower() == ".onnx":
            return path

    for path in (
        APP_DIR / "models" / "emotion" / f"{DEFAULT_EMOTION_MODEL}.onnx",
        ROOT / f"{DEFAULT_EMOTION_MODEL}.onnx",
        EMOTIEFFLIB_CACHE / f"{DEFAULT_EMOTION_MODEL}.onnx",
    ):
        if path.is_file():
            return path
    return None


def _prepare_emotion_model_name(configured: str | None) -> str:
    """
    准备 EmotiEffLib 所需的模型名（不含路径、不含 .onnx 后缀）。

    EmotiEffLib 的 get_model_path_onnx 会对 model_name 再追加 .onnx；
    若传入完整路径会导致 .onnx.onnx 并触发错误的在线下载。
    因此：本地有文件时先复制到 ~/.emotiefflib/，再返回标准模型名。
    """
    local_onnx = _find_local_onnx(configured)
    if local_onnx is not None:
        EMOTIEFFLIB_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file = EMOTIEFFLIB_CACHE / f"{DEFAULT_EMOTION_MODEL}.onnx"
        if not cache_file.exists() or cache_file.stat().st_size != local_onnx.stat().st_size:
            shutil.copy2(local_onnx, cache_file)
            print(f"[EmotionDetector] 已缓存情绪模型: {cache_file}")
        return DEFAULT_EMOTION_MODEL

    cache_file = EMOTIEFFLIB_CACHE / f"{DEFAULT_EMOTION_MODEL}.onnx"
    if cache_file.is_file():
        return DEFAULT_EMOTION_MODEL

    raise FileNotFoundError(
        "未找到情绪识别 ONNX 模型。请将 enet_b0_8_best_vgaf.onnx 放到以下任一位置后重试：\n"
        f"  1) {APP_DIR / 'models' / 'emotion' / f'{DEFAULT_EMOTION_MODEL}.onnx'}\n"
        f"  2) {EMOTIEFFLIB_CACHE / f'{DEFAULT_EMOTION_MODEL}.onnx'}\n"
        "或在 integrated_system/config.yaml 的 emotion.model_path 中指定完整路径。\n"
        "手动下载地址:\n"
        "  https://github.com/sb-ai-lab/EmotiEffLib/raw/main/models/affectnet_emotions/onnx/enet_b0_8_best_vgaf.onnx"
    )


def get_font(size: int = 20) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_paths = [
        "simhei.ttf",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


class EmotionDetector:
    """封装 MTCNN 人脸检测 + EmotiEffLib 情绪识别。"""

    def __init__(
        self,
        camera_index: int = 0,
        emotion_model_path: str | None = None,
        min_face_prob: float = 0.9,
    ):
        self.camera_index = camera_index
        self.min_face_prob = min_face_prob
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self.cap = cv2.VideoCapture(camera_index)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        if not self.cap.isOpened() and camera_index == 0:
            self.cap = cv2.VideoCapture(1)
            self.camera_index = 1

        if not self.cap.isOpened():
            raise RuntimeError("无法打开摄像头，请检查设备连接")

        self.mtcnn = MTCNN(
            keep_all=True,
            post_process=False,
            min_face_size=40,
            device=self.device,
        )

        model_name = _prepare_emotion_model_name(emotion_model_path or os.getenv("EMOTION_MODEL_PATH"))
        self.fer = EmotiEffLibRecognizer(engine="onnx", model_name=model_name, device=self.device)
        self.detected_probs: dict[str, float] = {}

    def release(self) -> None:
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def is_active(self) -> bool:
        return self.cap is not None and self.cap.isOpened()

    def annotate_frame(
        self,
        frame_bgr: np.ndarray,
        overlay_lines: list[str] | None = None,
    ) -> np.ndarray:
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        bboxes, probs = self.mtcnn.detect(frame_rgb)
        pil_img = Image.fromarray(frame_rgb)
        draw = ImageDraw.Draw(pil_img)
        font_large = get_font(30)
        font_small = get_font(20)

        if bboxes is not None and probs is not None:
            for idx, box in enumerate(bboxes):
                if probs[idx] < self.min_face_prob:
                    continue

                x1, y1, x2, y2 = box.astype(int)
                h, w, _ = frame_rgb.shape
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(w, x2), min(h, y2)

                face_rgb = frame_rgb[y1:y2, x1:x2, :]
                if face_rgb.size == 0:
                    continue

                emotions, scores = self.fer.predict_emotions([face_rgb], logits=True)
                emotion_top_eng = emotions[0]
                scores_tensor = torch.from_numpy(scores)
                probs_tensor = torch.softmax(scores_tensor, dim=1)
                probs_array = probs_tensor[0].numpy()

                emotion_probs: dict[str, float] = {}
                for i, prob in enumerate(probs_array):
                    emotion_eng = self.fer.idx_to_emotion_class[i]
                    emotion_cn = EMOTION_MAP.get(emotion_eng.lower(), emotion_eng)
                    emotion_probs[emotion_cn] = float(prob * 100)

                self.detected_probs = emotion_probs

                draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=3)
                emotion_top_cn = EMOTION_MAP.get(emotion_top_eng.lower(), emotion_top_eng)
                draw.text((x1, y1 - 35), emotion_top_cn, font=font_large, fill=(0, 255, 0))

                line_height = 25
                for e_i, prob_val in enumerate(probs_array):
                    emotion_eng = self.fer.idx_to_emotion_class[e_i]
                    emotion_cn = EMOTION_MAP.get(emotion_eng.lower(), emotion_eng)
                    text_str = f"{emotion_cn}: {prob_val * 100:.1f}%"
                    draw.text((x1, y2 + 10 + e_i * line_height), text_str, font=font_small, fill=(50, 50, 255))
        else:
            draw.text((10, 50), "未检测到人脸", font=font_large, fill=(255, 0, 0))

        if overlay_lines:
            y = 10
            for line in overlay_lines:
                draw.text((10, y), line, font=font_small, fill=(255, 255, 0))
                y += 28

        return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    def read_and_process(self, overlay_lines: list[str] | None = None) -> tuple[bool, np.ndarray | None]:
        if not self.is_active():
            return False, None
        success, frame = self.cap.read()
        if not success:
            return False, None
        annotated = self.annotate_frame(frame, overlay_lines=overlay_lines)
        return True, annotated

    def get_latest_probs(self) -> dict[str, float]:
        return dict(self.detected_probs)

    def get_status(self) -> dict[str, Any]:
        return {
            "camera_index": self.camera_index,
            "camera_opened": self.is_active(),
            "device": self.device,
            "has_emotion_data": bool(self.detected_probs),
        }
