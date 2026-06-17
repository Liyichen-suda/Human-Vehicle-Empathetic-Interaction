# 人车共情闭环系统

摄像头情绪检测 → RL 策略推理 → MQTT 下发 → 设备端执行

## 目录结构

```
In-vehicle-visualization-system-main/
├── requirements.txt
├── README.md
│
├── integrated_system/              # 主应用
│   ├── app.py                      # 入口
│   ├── config.yaml
│   ├── paths.py                    # 路径常量
│   ├── perception/                 # 情绪检测
│   │   ├── emotion_detector.py
│   │   └── emotion_smoothing.py
│   ├── control/                    # RL → MQTT 管道
│   │   └── pipeline.py
│   ├── voice/                      # 语音/文本命令
│   │   ├── voice_controller.py
│   │   └── voice_offline.py
│   ├── learning/                   # 在线学习
│   │   └── online_learner.py
│   ├── mqtt/                       # 设备状态订阅
│   │   └── mqtt_status_listener.py
│   ├── infra/                      # 进程/信号/端口
│   │   └── service_lifecycle.py
│   ├── web/templates/index.html    # Web 界面
│   ├── scripts/download_vosk_model.py
│   └── models/
│       ├── emotion/                # enet_b0_8_best_vgaf.onnx
│       └── vosk-model-*/           # 语音模型
│
└── center_control/                 # RL 模型 + MQTT 控制中心
    ├── device_control_center.py
    ├── config/
    ├── core/
    ├── model/v3.1.py
    ├── devices/
    ├── simulators/                 # 本地设备模拟（run_all_devices.py）
    ├── assets/music/
    └── logs/checkpoints/           # UserState_fixed.pth
```

## 启动（一条命令）

```bash
# 项目根目录
python run.py
```

Windows 也可双击 **`start.bat`**。

自动完成：启动 5 个设备模拟器 → 启动 Flask Web（http://127.0.0.1:5000）  
按 **Ctrl+C** 会同时停止设备和主应用。

**前置条件**：MQTT Broker 已运行在 `localhost:1883`（如 Mosquitto）。

### 首次准备

```bash
pip install -r requirements.txt
# 情绪模型 -> integrated_system/models/emotion/enet_b0_8_best_vgaf.onnx
# RL 权重   -> center_control/logs/checkpoints/UserState_fixed.pth
# 语音模型  -> cd integrated_system && python scripts/download_vosk_model.py
```

### 单独启动（调试用）

```bash
cd center_control/simulators && python run_all_devices.py
cd integrated_system && python app.py
```

## 模块说明（integrated_system）

| 目录 | 职责 |
|------|------|
| `perception/` | 摄像头人脸检测、情绪识别、多帧平滑 |
| `control/` | 情绪 → 9 维状态 → RL 推理 → MQTT 下发 |
| `voice/` | 离线语音识别 + 文本/语音命令解析 |
| `learning/` | 动作后情绪反馈、在线策略微调 |
| `mqtt/` | 订阅设备状态 topic，供 Web 展示 |
| `infra/` | PID 文件、信号处理、端口检测 |
| `web/templates/` | Flask 前端页面 |
| `models/` | 情绪 ONNX、Vosk 语音模型 |
| `paths.py` | 全局路径常量（各模块统一引用） |
```
