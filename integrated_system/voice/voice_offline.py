"""离线语音识别（Whisper 主引擎 + Vosk 备用，无需联网）。"""

from __future__ import annotations

import io
import json
import logging
import re
import wave
from pathlib import Path
from typing import Any, Optional, Tuple

import numpy as np

from paths import APP_DIR

logger = logging.getLogger(__name__)

_MODEL = None
_MODEL_PATH: Optional[Path] = None
_WHISPER = None
_WHISPER_KEY: Optional[str] = None


def default_model_dir(app_dir: Path | None = None) -> Path:
    base = app_dir or APP_DIR
    return base / "models" / "vosk-model-small-cn-0.22"


def resolve_model_path(configured: str | None = None, app_dir: Path | None = None) -> Path:
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = ((app_dir or APP_DIR) / path).resolve()
        return path
    return default_model_dir(app_dir)


def model_ready(model_path: Path) -> bool:
    return model_path.is_dir() and any(model_path.glob("**/final.mdl"))


def get_model(model_path: Path):
    global _MODEL, _MODEL_PATH
    if _MODEL is not None and _MODEL_PATH == model_path:
        return _MODEL
    if not model_ready(model_path):
        raise RuntimeError(
            f"未找到 Vosk 中文模型目录: {model_path}\n"
            "请运行: python scripts/download_vosk_model.py"
        )
    from vosk import Model

    _MODEL = Model(str(model_path))
    _MODEL_PATH = model_path
    return _MODEL


def get_whisper(model_name: str = "base", compute_type: str = "int8"):
    global _WHISPER, _WHISPER_KEY
    key = f"{model_name}:{compute_type}"
    if _WHISPER is not None and _WHISPER_KEY == key:
        return _WHISPER
    from faster_whisper import WhisperModel

    _WHISPER = WhisperModel(model_name, device="cpu", compute_type=compute_type)
    _WHISPER_KEY = key
    return _WHISPER


def _resample_float32(samples: np.ndarray, orig_rate: int, target_rate: int = 16000) -> np.ndarray:
    if orig_rate == target_rate or samples.size == 0:
        return samples
    new_len = max(1, int(len(samples) * target_rate / orig_rate))
    indices = np.linspace(0, len(samples) - 1, new_len)
    return np.interp(indices, np.arange(len(samples)), samples).astype(np.float32)


def _resample_pcm16(pcm: bytes, orig_rate: int, target_rate: int = 16000) -> bytes:
    if orig_rate == target_rate:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    resampled = _resample_float32(samples, orig_rate, target_rate)
    return np.clip(resampled, -32767, 32767).astype(np.int16).tobytes()


def extract_pcm16_16k(wav_bytes: bytes) -> Tuple[bytes, float]:
    """解析 WAV → 16kHz mono int16 PCM，返回 (pcm, duration_s)。"""
    if not wav_bytes or len(wav_bytes) < 44:
        raise RuntimeError("录音数据为空或过短，请重新录制")
    if wav_bytes[:4] != b"RIFF":
        raise RuntimeError("音频格式无效（需要 WAV），请刷新页面后重试")

    try:
        wf = wave.open(io.BytesIO(wav_bytes), "rb")
    except wave.Error as exc:
        raise RuntimeError(f"无法解析 WAV 文件: {exc}") from exc

    with wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        if sample_width != 2:
            raise RuntimeError(f"仅支持 16-bit PCM WAV，当前 sampwidth={sample_width}")

        pcm = wf.readframes(wf.getnframes())
        duration_s = wf.getnframes() / max(sample_rate, 1)

        if channels == 2:
            stereo = np.frombuffer(pcm, dtype=np.int16).reshape(-1, 2)
            pcm = stereo.mean(axis=1).astype(np.int16).tobytes()
        elif channels != 1:
            raise RuntimeError(f"仅支持 mono/stereo WAV，当前 channels={channels}")

        pcm = _resample_pcm16(pcm, sample_rate, 16000)
        pcm = _normalize_pcm16(pcm)

    return pcm, duration_s


def _wav_audio_stats(wav_bytes: bytes) -> dict[str, float]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        rate = wf.getframerate()
    if not pcm:
        return {"peak": 0.0, "rms": 0.0, "duration_s": 0.0}
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(samples * samples)))
    return {"peak": peak, "rms": rms, "duration_s": len(samples) / max(rate, 1)}


def _normalize_pcm16(pcm: bytes, target_peak: float = 0.75) -> bytes:
    """音量归一化；过高峰值先限幅，避免失真导致识别失败。"""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return pcm
    peak = float(np.max(np.abs(samples)))
    if peak < 200:
        return pcm
    # 已削波：限幅到 75% 再归一化
    if peak >= 32000:
        samples = np.clip(samples, -24500, 24500)
        peak = float(np.max(np.abs(samples)))
    scale = (target_peak * 32767.0) / peak
    scaled = np.clip(samples * scale, -32767, 32767).astype(np.int16)
    return scaled.tobytes()


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip())


def _soft_limit_pcm16(pcm: bytes) -> bytes:
    """修复削波失真（音量 100% 时常见）。"""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return pcm
    peak = float(np.max(np.abs(samples)))
    if peak <= 28000:
        return pcm
    scale = 22000.0 / peak
    return np.clip(samples * scale, -32767, 32767).astype(np.int16).tobytes()


def _run_vosk(pcm: bytes, model, grammar: Optional[list[str]] = None) -> str:
    from vosk import KaldiRecognizer

    grammar_json = json.dumps(grammar, ensure_ascii=False) if grammar else None
    if grammar_json:
        recognizer = KaldiRecognizer(model, 16000, grammar_json)
    else:
        recognizer = KaldiRecognizer(model, 16000)
    recognizer.SetWords(False)

    # 短音频：整段送入识别率更高
    recognizer.AcceptWaveform(pcm)

    final = json.loads(recognizer.FinalResult())
    text = (final.get("text") or "").strip()
    if not text:
        text = json.loads(recognizer.PartialResult()).get("partial", "").strip()
    return _clean_text(text)


def _run_vosk_multipass(pcm: bytes, model, grammar: Optional[list[str]] = None) -> str:
    variants = [
        pcm,
        _soft_limit_pcm16(pcm),
        _normalize_pcm16(pcm),
        _normalize_pcm16(_soft_limit_pcm16(pcm)),
    ]
    seen: set[bytes] = set()
    for variant in variants:
        key = variant[:64]
        if key in seen:
            continue
        seen.add(key)
        text = _run_vosk(variant, model, None)
        if text:
            return text
    if grammar:
        return _run_vosk(_normalize_pcm16(_soft_limit_pcm16(pcm)), model, grammar)
    return ""


def _run_whisper(pcm16: bytes, model_name: str = "base", compute_type: str = "int8") -> str:
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    model = get_whisper(model_name, compute_type)
    segments, _ = model.transcribe(
        samples,
        language="zh",
        beam_size=3,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    parts = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
    return _clean_text("".join(parts))


def transcribe_wav_bytes(
    wav_bytes: bytes,
    model_path: Path,
    grammar: Optional[list[str]] = None,
    stt_engine: str = "whisper",
    whisper_model: str = "base",
    whisper_compute_type: str = "int8",
) -> Tuple[str, str]:
    """WAV → 文本。默认 Whisper，失败时回退 Vosk。"""
    pcm, _ = extract_pcm16_16k(wav_bytes)
    errors: list[str] = []

    if stt_engine == "whisper":
        try:
            text = _run_whisper(pcm, whisper_model, whisper_compute_type)
            if text:
                return text, f"whisper-{whisper_model}"
        except ImportError:
            errors.append("未安装 faster-whisper，请运行: pip install faster-whisper")
        except Exception as exc:
            logger.warning("Whisper 识别失败: %s", exc)
            errors.append(f"Whisper: {exc}")

    try:
        model = get_model(model_path)
        text = _run_vosk_multipass(pcm, model, grammar)
        if text:
            return text, "vosk"
    except Exception as exc:
        errors.append(f"Vosk: {exc}")

    if errors:
        logger.warning("语音识别全部失败: %s", "; ".join(errors))
    return "", "none"


def diagnose_offline_voice(
    model_path: Path,
    stt_engine: str = "whisper",
    whisper_model: str = "base",
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "engine": stt_engine,
        "offline": True,
        "model_path": str(model_path),
        "whisper_model": whisper_model,
        "whisper_installed": False,
        "model_ready": model_ready(model_path),
        "vosk_installed": False,
        "recommended_mode": "browser_wav_upload",
        "issues": [],
        "hints": [
            "浏览器录音 → 服务端 Whisper 离线识别 → 强制控制设备",
            "可说完整命令：播放稳定音乐、空调降温、关闭所有",
        ],
    }
    try:
        import faster_whisper  # noqa: F401

        info["whisper_installed"] = True
    except ImportError:
        info["issues"].append("推荐安装: pip install faster-whisper")

    try:
        import vosk  # noqa: F401

        info["vosk_installed"] = True
    except ImportError:
        info["issues"].append("备用引擎 vosk 未安装: pip install vosk")

    if stt_engine == "whisper" and info["whisper_installed"]:
        try:
            get_whisper(whisper_model, "int8")
            info["whisper_loaded"] = True
        except Exception as exc:
            info["issues"].append(f"Whisper 模型加载失败: {exc}")

    if info["model_ready"]:
        try:
            get_model(model_path)
            info["vosk_loaded"] = True
        except Exception as exc:
            info["issues"].append(f"Vosk 模型: {exc}")

    return info
