"""
人车共情闭环系统 — 主入口

流程: 摄像头情绪检测 -> 9维状态 -> RL 推理 -> MQTT 下发 -> 设备端执行

依赖:
  - integrated_system: Flask 主应用（情绪检测、语音、Web UI）
  - center_control: RL 模型 + MQTT 控制中心 + 设备模拟器
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

# 允许从项目根目录执行: python integrated_system/app.py
_APP_ROOT = Path(__file__).resolve().parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

import cv2
import yaml
from flask import Flask, Response, jsonify, render_template, request

from paths import APP_DIR, LOGS_DIR, ROOT, WEB_TEMPLATES

from perception import EmotionDetector, EmotionSmoother
from mqtt import DeviceStatusListener
from control import EmotionControlPipeline, format_prediction_for_frontend
from infra import (
    PidFile,
    check_port_available,
    install_signal_handlers,
    register_cleanup,
    shutdown_all,
    shutdown_event,
)


def load_config() -> dict:
    config_path = APP_DIR / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CFG = load_config()
EMOTION_CFG = CFG.get("emotion", {})
CONTROL_CFG = CFG.get("control", {})
PIPELINE_CFG = CFG.get("pipeline", {})
ONLINE_CFG = CFG.get("online_learning", {})
VOICE_CFG = CFG.get("voice", {})
SERVER_CFG = CFG.get("server", {})

PREDICTION_INTERVAL = float(PIPELINE_CFG.get("prediction_interval", 5.0))
AUTO_PREDICT = bool(PIPELINE_CFG.get("auto_predict", False))

app = Flask(__name__, template_folder=str(WEB_TEMPLATES))

# 全局状态
detected_probs: dict[str, float] = {}
last_prediction: dict | None = None
prediction_history: list[dict] = []
emotion_buffer: queue.Queue = queue.Queue(maxsize=100)
emotion_smoother = EmotionSmoother(
    window_size=int(EMOTION_CFG.get("smooth_window", 8)),
    min_samples=int(EMOTION_CFG.get("smooth_min_samples", 2)),
)
last_prediction_time = datetime.now() - timedelta(seconds=PREDICTION_INTERVAL)
overlay_lines: list[str] = ["系统初始化中..."]

detector: EmotionDetector | None = None
pipeline: EmotionControlPipeline | None = None
status_listener: DeviceStatusListener | None = None


def _cleanup_resources(_reason: str) -> None:
    global detector, pipeline, status_listener

    if detector is not None:
        detector.release()
        detector = None
    if pipeline is not None:
        try:
            pipeline.stop_online_learning()
            pipeline.disconnect_mqtt()
        except Exception:
            pass
        pipeline = None
    if status_listener is not None:
        try:
            status_listener.stop()
        except Exception:
            pass
        status_listener = None
    try:
        cv2.destroyAllWindows()
    except Exception:
        pass


def _update_overlay_auto_predict() -> None:
    global overlay_lines
    if not overlay_lines:
        return
    auto_line = f"自动预测: {'开' if AUTO_PREDICT else '关(仅手动)'}"
    overlay_lines = [auto_line if "自动预测" in line else line for line in overlay_lines]
    if not any("自动预测" in line for line in overlay_lines):
        overlay_lines.append(auto_line)


def _init_services() -> None:
    global detector, pipeline, status_listener, overlay_lines

    center_root = ROOT / CONTROL_CFG.get("center_control_root", "center_control")
    pipeline = EmotionControlPipeline(
        center_control_root=center_root,
        default_fatigue=float(CONTROL_CFG.get("default_fatigue", 50.0)),
        sample_mode=bool(CONTROL_CFG.get("sample_mode", False)),
        publish_mqtt=bool(CONTROL_CFG.get("publish_mqtt", True)),
        online_learning_cfg=ONLINE_CFG,
    )

    mqtt_ok = pipeline.connect_mqtt()

    def _current_emotion() -> dict[str, float] | None:
        smoothed = emotion_smoother.get_smoothed()
        return smoothed or (dict(detected_probs) if detected_probs else None)

    def _current_fatigue() -> float:
        return float(CONTROL_CFG.get("default_fatigue", 50.0))

    pipeline.start_online_learning(_current_emotion, _current_fatigue)

    status_listener = DeviceStatusListener(center_control_root=center_root)
    status_ok = status_listener.start()

    overlay_lines = [
        f"MQTT: {'已连接' if mqtt_ok else '未连接'}",
        f"设备状态订阅: {'已连接' if status_ok else '未连接'}",
        f"自动预测: {'开' if AUTO_PREDICT else '关(仅手动)'}",
        f"在线学习: {'开' if ONLINE_CFG.get('enabled', True) else '关'}",
        f"预测间隔: {PREDICTION_INTERVAL}s",
    ]

    detector = EmotionDetector(
        camera_index=int(EMOTION_CFG.get("camera_index", 0)),
        emotion_model_path=EMOTION_CFG.get("model_path") or None,
        min_face_prob=float(EMOTION_CFG.get("min_face_prob", 0.9)),
    )


def _run_prediction(emotion_probs: dict[str, float] | None = None) -> dict | None:
    global last_prediction, last_prediction_time

    if pipeline is None:
        return None

    probs = emotion_probs or emotion_smoother.get_smoothed() or detected_probs
    if not probs:
        return None

    try:
        result = pipeline.run_from_emotion(probs)
        device_status = status_listener.get_status() if status_listener else {}
        formatted = format_prediction_for_frontend(result, emotion_input=probs, device_status=device_status)
        last_prediction = formatted
        last_prediction_time = datetime.now()

        prediction_history.append(formatted)
        if len(prediction_history) > 20:
            prediction_history.pop(0)

        print(f"\n[{formatted['timestamp']}] 推理完成 (MQTT 已下发，等待设备执行):")
        for dev, info in formatted["predicted_actions"].items():
            mqtt_status = formatted.get("mqtt_result", {}).get(dev, {}).get("status", "-")
            dev_runtime = info.get("runtime", "-")
            print(f"  {dev}: {info['action']} (mqtt={mqtt_status}, 设备={dev_runtime})")

        return formatted
    except Exception as exc:
        print(f"[App] 预测失败: {exc}")
        import traceback

        traceback.print_exc()
        return None


def prediction_scheduler() -> None:
    global last_prediction_time

    while not shutdown_event.is_set():
        if AUTO_PREDICT:
            time_since = (datetime.now() - last_prediction_time).total_seconds()
            if time_since >= PREDICTION_INTERVAL:
                latest = None
                smoothed = emotion_smoother.get_smoothed()
                if smoothed:
                    latest = smoothed
                else:
                    items = list(emotion_buffer.queue)
                    if items:
                        latest = items[-1]
                    elif detected_probs:
                        latest = detected_probs

                if latest:
                    _run_prediction(latest)

        if shutdown_event.wait(0.1):
            break


def gen_frames():
    global detected_probs, overlay_lines

    while not shutdown_event.is_set():
        if detector is None or not detector.is_active():
            break

        lines = list(overlay_lines)
        time_since = (datetime.now() - last_prediction_time).total_seconds()
        time_remaining = max(0.0, PREDICTION_INTERVAL - time_since)
        lines.insert(0, f"下次预测: {time_remaining:.1f}s")

        if last_prediction:
            lines.append("最新动作:")
            for dev, info in last_prediction.get("predicted_actions", {}).items():
                lines.append(f"  {dev}: {info['action']}")

        success, frame = detector.read_and_process(overlay_lines=lines)
        if not success or frame is None:
            break

        probs = detector.get_latest_probs()
        if probs:
            detected_probs = probs
            emotion_smoother.push(probs)
            try:
                emotion_buffer.put_nowait(probs)
            except queue.Full:
                try:
                    emotion_buffer.get_nowait()
                    emotion_buffer.put_nowait(probs)
                except queue.Empty:
                    pass

        ret, buffer = cv2.imencode(".jpg", frame)
        yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/video_feed")
def video_feed():
    return Response(gen_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/emotion_probs")
def get_emotion_probs():
    if detected_probs:
        return jsonify(detected_probs)
    return jsonify({"status": "No emotion data available"})


@app.route("/latest_prediction")
def get_latest_prediction():
    if last_prediction:
        # 合并最新设备 MQTT 状态
        if status_listener:
            device_status = status_listener.get_status()
            merged = dict(last_prediction)
            predicted = dict(merged.get("predicted_actions", {}))
            for dev, info in predicted.items():
                ds = device_status.get(dev, {})
                if ds:
                    info["runtime"] = ds.get("runtime", info.get("runtime", ""))
                    info["exec_status"] = ds.get("status", info.get("exec_status", ""))
                    info["message"] = ds.get("message", info.get("message", ""))
                    info["selected_track"] = ds.get("selected_track", info.get("selected_track"))
                    info["device_online"] = ds.get("online") if ds else None
            merged["predicted_actions"] = predicted
            merged["device_status"] = device_status
            return jsonify(merged)
        return jsonify(last_prediction)

    has_emotion = bool(detected_probs or emotion_smoother.get_smoothed())
    camera_on = detector is not None and detector.is_active()
    reasons = []
    if not AUTO_PREDICT:
        reasons.append("自动预测已关闭，请点击「手动预测」")
    elif not has_emotion:
        reasons.append("尚未检测到人脸情绪，请开启摄像头并对准面部")
    elif not camera_on:
        reasons.append("摄像头未开启，请点击「开启」按钮")
    else:
        reasons.append(f"等待首次推理（间隔 {PREDICTION_INTERVAL}s）")

    return jsonify(
        {
            "status": "No prediction available yet",
            "auto_predict": AUTO_PREDICT,
            "has_emotion_data": has_emotion,
            "camera_active": camera_on,
            "emotion_samples": emotion_smoother.sample_count,
            "prediction_interval": PREDICTION_INTERVAL,
            "hint": "；".join(reasons),
        }
    )


@app.route("/prediction_history")
def get_prediction_history():
    return jsonify({"history": prediction_history[-10:], "count": len(prediction_history)})


@app.route("/mqtt_status")
def mqtt_status():
    if pipeline is None:
        return jsonify({"connected": False, "publish_enabled": False})
    return jsonify(
        {
            "connected": pipeline.mqtt_connected,
            "publish_enabled": pipeline.publish_mqtt,
        }
    )


@app.route("/config", methods=["GET", "POST"])
def config():
    global PREDICTION_INTERVAL, AUTO_PREDICT

    if request.method == "POST":
        try:
            data = request.json or {}
            if "interval" in data:
                new_interval = float(data["interval"])
                if 1 <= new_interval <= 60:
                    PREDICTION_INTERVAL = new_interval
                    return jsonify(
                        {
                            "success": True,
                            "message": f"预测间隔已更新为 {PREDICTION_INTERVAL} 秒",
                            "interval": PREDICTION_INTERVAL,
                            "prediction_interval": PREDICTION_INTERVAL,
                            "auto_predict": AUTO_PREDICT,
                        }
                    )
                return jsonify({"success": False, "message": "预测间隔必须在1-60秒之间"}), 400
            if "auto_predict" in data:
                AUTO_PREDICT = bool(data["auto_predict"])
                _update_overlay_auto_predict()
                return jsonify(
                    {
                        "success": True,
                        "message": f"自动预测已{'开启' if AUTO_PREDICT else '关闭'}",
                        "auto_predict": AUTO_PREDICT,
                        "prediction_interval": PREDICTION_INTERVAL,
                    }
                )
        except Exception as exc:
            return jsonify({"success": False, "message": f"配置错误: {exc}"}), 400

    device_count = len(pipeline.get_devices_info()) if pipeline else 0
    emotion_count = len(pipeline.get_emotion_keys()) if pipeline else 8
    return jsonify(
        {
            "prediction_interval": PREDICTION_INTERVAL,
            "auto_predict": AUTO_PREDICT,
            "device_count": device_count,
            "emotion_count": emotion_count,
            "mqtt_connected": pipeline.mqtt_connected if pipeline else False,
        }
    )


@app.route("/manual_predict", methods=["POST"])
def manual_predict():
    try:
        data = request.json or {}
        emotion_probs = data.get("emotion_probs") or detected_probs
        if not emotion_probs:
            return jsonify({"success": False, "message": "没有可用的情绪数据"}), 400

        prediction = _run_prediction(emotion_probs)
        if prediction:
            return jsonify({"success": True, "message": "手动预测完成", "prediction": prediction})
        return jsonify({"success": False, "message": "预测失败，请检查模型权重是否就绪"}), 500
    except Exception as exc:
        return jsonify({"success": False, "message": f"预测错误: {exc}"}), 500


@app.route("/api/execute_action", methods=["POST"])
def execute_action():
    if pipeline is None:
        return jsonify({"success": False, "message": "控制管道未初始化"}), 500

    data = request.json or {}
    device = data.get("device")
    action = data.get("action")
    if not device or not action:
        return jsonify({"success": False, "message": "缺少 device 或 action 参数"}), 400

    try:
        result = pipeline.run_direct_action(device, action, source="manual", force=bool(data.get("force")))
        return jsonify({
            "success": True,
            "message": f"MQTT 已下发 {device} -> {action}，等待设备执行",
            "mqtt_result": result,
        })
    except RuntimeError as exc:
        return jsonify({"success": False, "message": str(exc)}), 503


@app.route("/api/close_all_devices", methods=["POST"])
def close_all_devices():
    """通过 MQTT 关闭全部设备（停止音乐播放等）。"""
    if pipeline is None:
        return jsonify({"success": False, "message": "控制管道未初始化"}), 500
    try:
        actions = {dev: "关闭" for dev in pipeline.center.engine.device_names}
        sent = pipeline.run_direct_actions(actions, source="manual", force=True)
        sent_count = sum(1 for v in sent.values() if v.get("status") == "sent")
        return jsonify(
            {
                "success": True,
                "message": f"已关闭 {sent_count}/{len(actions)} 个设备",
                "mqtt_result": sent,
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/publish_latest", methods=["POST"])
def publish_latest():
    """强制将最新预测动作下发到全部设备（跳过冷却、设备端强制打断）。"""
    if pipeline is None or not last_prediction:
        return jsonify({"success": False, "message": "无可用预测结果，请先开启视频并完成预测"}), 400

    actions = {
        dev: info["action"]
        for dev, info in last_prediction.get("predicted_actions", {}).items()
        if info.get("action")
    }
    if not actions:
        return jsonify({"success": False, "message": "预测结果中没有可下发的动作"}), 400

    try:
        sent = pipeline.run_direct_actions(actions, source="manual_force", force=True)
        sent_count = sum(1 for v in sent.values() if v.get("status") == "sent")
        return jsonify(
            {
                "success": True,
                "message": f"已强制下发 {sent_count}/{len(actions)} 个设备动作",
                "actions": actions,
                "mqtt_result": sent,
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/device_status")
def device_status():
    if status_listener is None:
        return jsonify({"runtime": {}, "listener_connected": False, "online_count": 0})

    runtime = status_listener.get_status()
    online_count = sum(1 for s in runtime.values() if s.get("online"))
    return jsonify(
        {
            "runtime": runtime,
            "listener_connected": status_listener.connected,
            "online_count": online_count,
            "device_count": 5,
        }
    )


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """优雅停止服务（仅本机调试）。"""
    remote = request.remote_addr or ""
    if remote not in ("127.0.0.1", "::1"):
        return jsonify({"success": False, "message": "仅允许本机调用"}), 403

    def _delayed_exit() -> None:
        time.sleep(0.3)
        shutdown_all(reason="api")
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return jsonify({"success": True, "message": "服务正在关闭..."})


@app.route("/devices")
def get_devices():
    if pipeline is None:
        return jsonify({})
    return jsonify(pipeline.get_devices_info())


@app.route("/emotions")
def get_emotions():
    if pipeline is None:
        return jsonify({"emotions": []})
    return jsonify({"emotions": pipeline.get_emotion_keys()})


@app.route("/api/voice/help")
def voice_help():
    from voice.voice_controller import get_command_help

    return jsonify(get_command_help())


@app.route("/api/voice/diagnose")
def voice_diagnose():
    from voice.voice_controller import diagnose_voice_setup
    from voice.voice_offline import resolve_model_path

    model_path = resolve_model_path(VOICE_CFG.get("vosk_model_path"), APP_DIR)
    return jsonify(diagnose_voice_setup(model_path, VOICE_CFG))


def _voice_model_path() -> Path:
    from voice.voice_offline import resolve_model_path

    return resolve_model_path(VOICE_CFG.get("vosk_model_path"), APP_DIR)


@app.route("/api/voice/text", methods=["POST"])
def voice_text_command():
    if pipeline is None:
        return jsonify({"success": False, "message": "控制管道未初始化"}), 500

    data = request.json or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"success": False, "message": "缺少 text 参数"}), 400

    try:
        result = pipeline.run_voice_command(text)
        status = 200 if result.get("success") else 400
        return jsonify(result), status
    except RuntimeError as exc:
        return jsonify({"success": False, "message": str(exc)}), 503


def _read_voice_audio_bytes() -> bytes:
    """从 multipart 文件或 JSON base64 读取 WAV 字节。"""
    if request.files and "audio" in request.files:
        data = request.files["audio"].read()
        if data:
            return data

    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        import base64

        b64 = payload.get("audio_base64") or payload.get("audio")
        if b64:
            s = str(b64)
            if "," in s:
                s = s.split(",", 1)[1]
            return base64.b64decode(s)

    raise ValueError("缺少音频数据（WAV）")


@app.route("/api/voice/listen", methods=["POST"])
def voice_listen_command():
    """接收浏览器上传的 WAV，离线识别后强制控制设备。"""
    if pipeline is None:
        return jsonify({"success": False, "message": "控制管道未初始化"}), 500
    if not VOICE_CFG.get("enabled", True):
        return jsonify({"success": False, "message": "语音功能已禁用"}), 403

    try:
        wav_bytes = _read_voice_audio_bytes()
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    debug_wav = LOGS_DIR / "last_voice.wav"
    try:
        debug_wav.parent.mkdir(parents=True, exist_ok=True)
        debug_wav.write_bytes(wav_bytes)
    except Exception:
        pass

    if len(wav_bytes) < 44 or wav_bytes[:4] != b"RIFF":
        return jsonify(
            {
                "success": False,
                "message": "上传的不是有效 WAV 文件，请刷新页面后重新录音",
                "hint": f"收到 {len(wav_bytes)} 字节，文件头={wav_bytes[:4]!r}",
            }
        ), 400

    try:
        from voice.voice_controller import transcribe_audio_wav
        from voice.voice_offline import _wav_audio_stats

        stats = _wav_audio_stats(wav_bytes)
        text, engine = transcribe_audio_wav(wav_bytes, _voice_model_path(), VOICE_CFG)
        result = pipeline.run_voice_command(text)
        result["recognized_text"] = text
        result["recognition_engine"] = engine
        result["audio_peak"] = stats["peak"]
        result["audio_rms"] = stats["rms"]
        result["audio_duration_s"] = stats["duration_s"]
        if stats["peak"] < 0.008:
            result["success"] = False
            result["message"] = (
                f"录音几乎无声（峰值 {stats['peak']*100:.1f}%），"
                "请检查 Windows 默认输入设备与麦克风音量"
            )
            return jsonify(result), 400
        status = 200 if result.get("success") else 400
        return jsonify(result), status
    except RuntimeError as exc:
        stats = {}
        try:
            from voice.voice_offline import _wav_audio_stats
            stats = _wav_audio_stats(wav_bytes)
        except Exception:
            pass
        payload = {"success": False, "message": str(exc), "error_type": "runtime"}
        if stats:
            payload.update(
                audio_peak=stats.get("peak"),
                audio_rms=stats.get("rms"),
                audio_duration_s=stats.get("duration_s"),
            )
        return jsonify(payload), 400
    except Exception as exc:
        return jsonify(
            {
                "success": False,
                "message": f"离线识别失败: {exc}",
                "error_type": "unknown",
                "hint": "请确认已安装 vosk 并下载中文模型: python scripts/download_vosk_model.py",
            }
        ), 500


@app.route("/api/online_learning/stats")
def online_learning_stats():
    if pipeline is None:
        return jsonify({"enabled": False})
    return jsonify(pipeline.get_online_learning_stats())


def main() -> None:
    print("=" * 60)
    print("  人车共情闭环系统")
    print("  情绪检测 -> RL 推理 -> MQTT -> 设备端执行")
    print("=" * 60)

    install_signal_handlers()
    register_cleanup(_cleanup_resources)

    pid_file = PidFile(LOGS_DIR / "app.pid", service_name="integrated_system/app.py")
    pid_file.acquire()

    host = SERVER_CFG.get("host", "0.0.0.0")
    port = int(SERVER_CFG.get("port", 5000))

    if not check_port_available(host, port):
        print(f"错误: 端口 {port} 已被占用，可能已有 app.py 在运行")
        print(f"请检查: netstat -ano | findstr \":{port}\"")
        sys.exit(1)

    _init_services()

    scheduler = threading.Thread(target=prediction_scheduler, daemon=True, name="prediction-scheduler")
    scheduler.start()

    print(f"\n预测间隔: {PREDICTION_INTERVAL} 秒")
    print(f"自动 MQTT 下发: {'开启' if AUTO_PREDICT else '关闭（需手动预测或点击执行）'}")
    print(f"MQTT: {'已连接' if pipeline and pipeline.mqtt_connected else '未连接'}")
    print(f"PID: {pid_file.pid}  (日志: {LOGS_DIR / 'app.pid'})")
    print(f"\n访问: http://127.0.0.1:{port}")
    print("停止服务: 在本终端按 Ctrl+C\n")

    try:
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        shutdown_all(reason="keyboard")
    finally:
        shutdown_all(reason="main-finally")


if __name__ == "__main__":
    main()
