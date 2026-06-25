/*
 * 复制本文件为 config.h 并填写你的 WiFi / MQTT 参数。
 * config.h 已被 .gitignore 忽略，避免泄露密码。
 */
#pragma once

// WiFi
#define WIFI_SSID "test"
#define WIFI_PASSWORD "66666666"

// MQTT Broker（运行 Mosquitto 的电脑局域网 IP，不是 localhost）
#define MQTT_BROKER "192.168.117.94"
#define MQTT_PORT 1883
#define MQTT_USER ""
#define MQTT_PASS ""

// 与 center_control/config/runtime.yaml 保持一致
#define TOPIC_PREFIX "cabin"
#define DEVICE_NAME "aroma"

// ESP32 本地 Web API 端口（配合 final/aroma_control.html）
#define WEB_PORT 80
