# Generates final.ino — run: python gen_firmware.py
from pathlib import Path

INO = Path(__file__).resolve().parent / "final.ino"

CONTENT = r'''#include <Adafruit_NeoPixel.h>
#include <WiFi.h>
#include <WebServer.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include <stdarg.h>
#include <cstring>

/*
  ESP32 四通道智能香薰机 v2
  - MQTT: cabin/aroma/control | cabin/aroma/status
  - HTTP: :80/api/status | /api/modes | /api/control | /api/stop
  - 网页: final/aroma_control.html（填入本机 IP 即可控制）
*/

#if __has_include("config.h")
#include "config.h"
#else
#define WIFI_SSID "test"
#define WIFI_PASSWORD "66666666"
#define MQTT_BROKER "192.168.117.94"
#define MQTT_PORT 1883
#define MQTT_USER ""
#define MQTT_PASS ""
#define TOPIC_PREFIX "cabin"
#define DEVICE_NAME "aroma"
#endif

#ifndef WEB_PORT
#define WEB_PORT 80
#endif

// ====================== 硬件引脚 ======================
const int scent_PH = 25;
const int scent_PL = 26;
const int scent_NH = 27;
const int scent_NL = 14;
const int LED_PIN = 13;
const int NUM_PIXELS = 16;

Adafruit_NeoPixel strip(NUM_PIXELS, LED_PIN, NEO_GRB + NEO_KHZ800);
WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
WebServer webServer(WEB_PORT);

// ====================== 模式 / 灯光 ======================
enum LightMode : uint8_t {
  LIGHT_OFF = 0,
  LIGHT_SOLID,
  LIGHT_BREATHE,
  LIGHT_PULSE,
  LIGHT_WAVE,
  LIGHT_RAINBOW,
  LIGHT_GRADIENT,
};

struct AromaModeDef {
  const char *actionId;
  const char *scentLabel;
  const char *channelCode;
  int pin;
  uint8_t defaultSpeed;
  uint16_t defaultDurationSec;
  uint8_t r, g, b;
  LightMode lightMode;
  bool scentEnabled;
};

const AromaModeDef MODE_TABLE[] = {
  {"关闭", "none", "OFF", -1, 0, 0, 0, 0, 0, LIGHT_OFF, false},
  {"平静", "chamomile", "NH", scent_NH, 28, 60, 150, 80, 255, LIGHT_BREATHE, true},
  {"舒缓", "lavender", "NH", scent_NH, 42, 45, 140, 70, 220, LIGHT_BREATHE, true},
  {"提神", "mint", "NL", scent_NL, 88, 35, 80, 220, 255, LIGHT_PULSE, true},
  {"专注", "lime_basil", "PH", scent_PH, 55, 50, 0, 200, 140, LIGHT_SOLID, true},
  {"共情", "magnolia", "PL", scent_PL, 38, 55, 255, 200, 150, LIGHT_GRADIENT, true},
  {"振奋", "citrus", "PH", scent_PH, 92, 40, 255, 160, 40, LIGHT_RAINBOW, true},
  {"冥想", "sandalwood", "NH", scent_NH, 18, 120, 120, 60, 180, LIGHT_BREATHE, true},
  {"净化", "eucalyptus", "NL", scent_NL, 62, 40, 60, 180, 255, LIGHT_WAVE, true},
  {"浪漫", "rose", "PL", scent_PL, 48, 50, 255, 120, 180, LIGHT_PULSE, true},
  {"深度放松", "deep_lavender", "NH", scent_NH, 22, 90, 100, 50, 160, LIGHT_BREATHE, true},
  {"派对", "mix", "ALL", -2, 75, 30, 255, 255, 255, LIGHT_RAINBOW, true},
  {"夜灯", "none", "OFF", -1, 0, 0, 255, 180, 80, LIGHT_BREATHE, false},
};
const size_t MODE_COUNT = sizeof(MODE_TABLE) / sizeof(MODE_TABLE[0]);

enum SprayPhase { PHASE_IDLE, PHASE_ON, PHASE_OFF };

struct ActiveTask {
  bool running = false;
  bool continuous = false;
  int pin = -1;
  bool allChannels = false;
  uint8_t speed = 50;
  uint16_t onMs = 2000;
  uint16_t offMs = 4000;
  unsigned long startMs = 0;
  unsigned long endMs = 0;
  SprayPhase phase = PHASE_IDLE;
  unsigned long phaseStartMs = 0;
  LightMode lightMode = LIGHT_OFF;
  uint8_t r = 0, g = 0, b = 0;
  const char *actionId = "关闭";
  const char *scentLabel = "none";
  const char *channelCode = "OFF";
  bool scentEnabled = false;
};

ActiveTask task;
String currentAction = "关闭";
String currentRuntime = "在线";
String currentMessage = "设备已上线，等待控制命令";
String currentStatus = "online";
String lastSource = "boot";
bool readyForCommand = true;
bool statusDirty = true;

unsigned long lastBreathUpdate = 0;
unsigned long lastHeartbeatMs = 0;
unsigned long lastMqttRetryMs = 0;
unsigned long lastLightUpdateMs = 0;
unsigned long lastStatusPublishMs = 0;
int breatheBrightness = 20;
int breatheDirection = 1;
uint16_t lightAnimStep = 0;

char controlTopic[64];
char statusTopic[64];
bool ntpReady = false;
bool wifiWasConnected = false;
uint8_t heartbeatLogCounter = 0;

// ====================== 日志 ======================
void logLine(const char *tag, const char *msg) {
  Serial.printf("[%s] %s\n", tag, msg);
}

void logFmt(const char *tag, const char *fmt, ...) {
  Serial.printf("[%s] ", tag);
  char buf[180];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  Serial.println(buf);
}

const char *mqttStateText(int state) {
  switch (state) {
    case -4: return "连接超时";
    case -3: return "连接丢失";
    case -2: return "连接失败";
    case -1: return "已断开";
    case 1: return "错误协议版本";
    case 2: return "客户端 ID 被拒绝";
    case 3: return "Broker 不可用";
    case 4: return "用户名或密码错误";
    case 5: return "未授权";
    default: return "未知错误";
  }
}

// ====================== 前向声明 ======================
void turnAllOff();
void markStatusDirty();
void publishStatus(bool logOutput = false, bool forceNow = false);
void ensureMqtt();
bool connectWiFi();
void setupTime();
String isoTimestamp();
const AromaModeDef *findModeByAction(const String &action);
void speedToTiming(uint8_t speed, uint16_t &onMs, uint16_t &offMs);
void stopTask(const char *message);
bool startMode(const String &action, uint8_t speed, uint16_t durationSec, bool force, const String &source, bool continuous = false);
bool startChannel(const String &channel, uint8_t speed, uint16_t durationSec, bool force, const String &source);
void updateSprayMachine();
void updateLightEffects();
void applySolid(uint8_t r, uint8_t g, uint8_t b);
void handleControlJson(JsonDocument &doc, const String &source);
void setupWebServer();
void addCorsHeaders();

// ====================== 工具 ======================
void markStatusDirty() {
  statusDirty = true;
}

uint8_t clampSpeed(int v) {
  if (v < 1) return 1;
  if (v > 100) return 100;
  return (uint8_t)v;
}

void speedToTiming(uint8_t speed, uint16_t &onMs, uint16_t &offMs) {
  speed = clampSpeed(speed);
  onMs = map(speed, 1, 100, 600, 5500);
  offMs = map(speed, 1, 100, 14000, 700);
}

const AromaModeDef *findModeByAction(const String &action) {
  for (size_t i = 0; i < MODE_COUNT; i++) {
    if (action == MODE_TABLE[i].actionId) return &MODE_TABLE[i];
  }
  return nullptr;
}

float taskProgressPct() {
  if (!task.running || task.continuous) return task.running ? 50.0f : 0.0f;
  unsigned long now = millis();
  if (now >= task.endMs) return 100.0f;
  unsigned long total = task.endMs - task.startMs;
  if (total == 0) return 0.0f;
  return constrain(100.0f * (now - task.startMs) / total, 0.0f, 100.0f);
}

float taskElapsedSec() {
  if (!task.running) return 0.0f;
  return (millis() - task.startMs) / 1000.0f;
}

float taskRemainingSec() {
  if (!task.running) return 0.0f;
  if (task.continuous) return -1.0f;
  if (millis() >= task.endMs) return 0.0f;
  return (task.endMs - millis()) / 1000.0f;
}

void fillStatusJson(JsonDocument &doc) {
  doc["device"] = DEVICE_NAME;
  doc["online"] = true;
  doc["hardware"] = true;
  doc["busy"] = task.running;
  doc["ready_for_command"] = readyForCommand;
  doc["action"] = currentAction;
  doc["status"] = currentStatus;
  doc["runtime"] = currentRuntime;
  doc["message"] = currentMessage;
  doc["source"] = lastSource;
  doc["progress_pct"] = taskProgressPct();
  doc["phase"] = task.phase == PHASE_ON ? "spray_on" : (task.phase == PHASE_OFF ? "spray_off" : "idle");

  JsonObject params = doc.createNestedObject("params");
  params["mode"] = currentAction;
  params["channel"] = task.channelCode;
  params["scent"] = task.scentLabel;
  params["speed"] = task.speed;
  params["continuous"] = task.continuous;
  params["duration_s"] = task.continuous ? 0 : max(0, (int)((task.endMs - task.startMs) / 1000));
  params["elapsed_s"] = serialized(taskElapsedSec(), 1);
  params["remaining_s"] = task.continuous ? -1 : serialized(taskRemainingSec(), 1);
  params["light_mode"] = (int)task.lightMode;
  JsonArray rgb = params.createNestedArray("rgb");
  rgb.add(task.r);
  rgb.add(task.g);
  rgb.add(task.b);
  params["spray_on_ms"] = task.onMs;
  params["spray_off_ms"] = task.offMs;

  String ts = isoTimestamp();
  if (ts.length() > 0) doc["timestamp"] = ts;
}

// ArduinoJson 6 helper for float in nested - use direct assignment
// Fix: serialized() may not exist in v6 - use roundf
// Replace params elapsed with float cast in publish

void publishStatus(bool logOutput, bool forceNow) {
  unsigned long now = millis();
  if (!forceNow && !statusDirty && (now - lastStatusPublishMs) < 2000) return;

  if (mqttClient.connected()) {
    StaticJsonDocument<768> doc;
    doc["device"] = DEVICE_NAME;
    doc["online"] = true;
    doc["hardware"] = true;
    doc["busy"] = task.running;
    doc["ready_for_command"] = readyForCommand;
    doc["action"] = currentAction;
    doc["status"] = currentStatus;
    doc["runtime"] = currentRuntime;
    doc["message"] = currentMessage;
    doc["source"] = lastSource;
    doc["progress_pct"] = taskProgressPct();
    doc["phase"] = task.phase == PHASE_ON ? "spray_on" : (task.phase == PHASE_OFF ? "spray_off" : "idle");

    JsonObject params = doc.createNestedObject("params");
    params["mode"] = currentAction;
    params["channel"] = task.channelCode;
    params["scent"] = task.scentLabel;
    params["speed"] = task.speed;
    params["continuous"] = task.continuous;
    if (!task.continuous && task.running)
      params["duration_s"] = (task.endMs - task.startMs) / 1000;
    else
      params["duration_s"] = 0;
    params["elapsed_s"] = taskElapsedSec();
    params["remaining_s"] = task.continuous ? -1.0f : taskRemainingSec();
    params["light_mode"] = (int)task.lightMode;
    JsonArray rgb = params.createNestedArray("rgb");
    rgb.add(task.r);
    rgb.add(task.g);
    rgb.add(task.b);
    params["spray_on_ms"] = task.onMs;
    params["spray_off_ms"] = task.offMs;

    String ts = isoTimestamp();
    if (ts.length() > 0) doc["timestamp"] = ts;

    char payload[768];
    size_t n = serializeJson(doc, payload, sizeof(payload));
    mqttClient.publish(statusTopic, (const uint8_t *)payload, n, false);
    lastStatusPublishMs = now;
    statusDirty = false;
    if (logOutput) {
      logFmt("MQTT", "状态上报 %s", payload);
    }
  } else if (logOutput) {
    logLine("MQTT", "状态未上报：MQTT 未连接");
  }
}

// ====================== 喷雾 / 灯光 ======================
void turnAllOff() {
  digitalWrite(scent_PH, LOW);
  digitalWrite(scent_PL, LOW);
  digitalWrite(scent_NH, LOW);
  digitalWrite(scent_NL, LOW);
}

void applySolid(uint8_t r, uint8_t g, uint8_t b) {
  for (int i = 0; i < NUM_PIXELS; i++) strip.setPixelColor(i, strip.Color(r, g, b));
  strip.show();
}

void stopTask(const char *message) {
  logFmt("任务", "停止: %s", message);
  turnAllOff();
  task = ActiveTask();
  currentMessage = message;
  currentStatus = "idle";
  currentRuntime = "在线";
  readyForCommand = true;
  markStatusDirty();
  publishStatus(true, true);
}

bool startMode(const String &action, uint8_t speed, uint16_t durationSec, bool force, const String &source, bool continuous) {
  lastSource = source;
  const AromaModeDef *mode = findModeByAction(action);
  if (!mode) return false;

  if (action == "关闭") {
    stopTask("香薰已关闭");
    currentAction = "关闭";
    currentStatus = "stopped";
    currentRuntime = "已关闭";
    markStatusDirty();
    publishStatus(true, true);
    return true;
  }

  if (task.running && !force && action == currentAction) {
    currentMessage = "相同模式执行中";
    publishStatus(false, true);
    return true;
  }

  turnAllOff();
  task = ActiveTask();
  task.running = true;
  task.continuous = continuous;
  task.actionId = mode->actionId;
  task.scentLabel = mode->scentLabel;
  task.channelCode = mode->channelCode;
  task.speed = clampSpeed(speed > 0 ? speed : mode->defaultSpeed);
  speedToTiming(task.speed, task.onMs, task.offMs);
  task.lightMode = mode->lightMode;
  task.r = mode->r;
  task.g = mode->g;
  task.b = mode->b;
  task.scentEnabled = mode->scentEnabled;
  task.startMs = millis();
  uint16_t dur = durationSec > 0 ? durationSec : mode->defaultDurationSec;
  task.endMs = continuous ? 0 : task.startMs + (unsigned long)dur * 1000UL;
  task.phase = task.scentEnabled ? PHASE_ON : PHASE_IDLE;
  task.phaseStartMs = millis();

  if (strcmp(mode->channelCode, "ALL") == 0) {
    task.allChannels = true;
    task.pin = scent_PH;
  } else {
    task.pin = mode->pin;
    task.allChannels = false;
  }

  if (task.scentEnabled && task.pin >= 0) {
    if (task.allChannels) {
      digitalWrite(scent_PH, HIGH);
      digitalWrite(scent_PL, HIGH);
      digitalWrite(scent_NH, HIGH);
      digitalWrite(scent_NL, HIGH);
    } else {
      digitalWrite(task.pin, HIGH);
    }
  }

  currentAction = action;
  currentStatus = "executing";
  currentRuntime = "执行中";
  currentMessage = String("模式 ") + action + " 运行中";
  readyForCommand = false;

  logFmt("任务", "启动模式=%s 通道=%s 速度=%d 时长=%us 连续=%d",
         action.c_str(), task.channelCode, task.speed, dur, continuous);
  markStatusDirty();
  publishStatus(true, true);
  return true;
}

bool startChannel(const String &channel, uint8_t speed, uint16_t durationSec, bool force, const String &source) {
  String action = channel;
  if (action.startsWith("通道")) action = action.substring(strlen("通道"));
  action.toUpperCase();
  AromaModeDef custom = {"定制", "custom", action.c_str(), -1, speed, durationSec, 200, 200, 200, LIGHT_SOLID, true};
  if (action == "PH") custom.pin = scent_PH;
  else if (action == "PL") custom.pin = scent_PL;
  else if (action == "NH") custom.pin = scent_NH;
  else if (action == "NL") custom.pin = scent_NL;
  else return false;

  // Temporarily use startMode logic via fake table lookup - inline:
  lastSource = source;
  if (task.running && !force) { /* allow switch */ }
  turnAllOff();
  task = ActiveTask();
  task.running = true;
  task.pin = custom.pin;
  task.channelCode = custom.channelCode;
  task.scentLabel = "custom";
  task.speed = clampSpeed(speed);
  speedToTiming(task.speed, task.onMs, task.offMs);
  task.lightMode = LIGHT_SOLID;
  task.r = 180; task.g = 180; task.b = 180;
  task.scentEnabled = true;
  task.startMs = millis();
  task.endMs = task.startMs + (unsigned long)(durationSec > 0 ? durationSec : 30) * 1000UL;
  task.phase = PHASE_ON;
  task.phaseStartMs = millis();
  digitalWrite(task.pin, HIGH);
  currentAction = String("通道") + action;
  currentStatus = "executing";
  currentRuntime = "执行中";
  currentMessage = String("通道 ") + action + " 喷雾";
  readyForCommand = false;
  markStatusDirty();
  publishStatus(true, true);
  return true;
}

void updateSprayMachine() {
  if (!task.running) return;
  unsigned long now = millis();

  if (!task.continuous && task.endMs > 0 && now >= task.endMs) {
    String doneAction = currentAction;
    stopTask("模式完成");
    currentAction = doneAction;
    currentStatus = "done";
    currentRuntime = "运行中";
    currentMessage = String("模式 ") + doneAction + " 已完成";
    readyForCommand = true;
    markStatusDirty();
    publishStatus(true, true);
    return;
  }

  if (!task.scentEnabled || task.pin < 0) return;

  unsigned long elapsed = now - task.phaseStartMs;
  if (task.phase == PHASE_ON) {
    if (elapsed >= task.onMs) {
      if (task.allChannels) turnAllOff();
      else digitalWrite(task.pin, LOW);
      task.phase = PHASE_OFF;
      task.phaseStartMs = now;
      markStatusDirty();
    }
  } else if (task.phase == PHASE_OFF) {
    if (elapsed >= task.offMs) {
      if (task.allChannels) {
        digitalWrite(scent_PH, HIGH);
        digitalWrite(scent_PL, HIGH);
        digitalWrite(scent_NH, HIGH);
        digitalWrite(scent_NL, HIGH);
      } else digitalWrite(task.pin, HIGH);
      task.phase = PHASE_ON;
      task.phaseStartMs = now;
      markStatusDirty();
    }
  }
  publishStatus(false, false);
}

void updateLightEffects() {
  if (!task.running && currentAction == "关闭") {
    if (millis() - lastBreathUpdate > 25) {
      lastBreathUpdate = millis();
      breatheBrightness += breatheDirection;
      if (breatheBrightness >= 70) breatheDirection = -1;
      if (breatheBrightness <= 8) breatheDirection = 1;
      for (int i = 0; i < NUM_PIXELS; i++)
        strip.setPixelColor(i, strip.Color(breatheBrightness, breatheBrightness * 0.35, 0));
      strip.show();
    }
    return;
  }
  if (!task.running) return;
  if (millis() - lastLightUpdateMs < 30) return;
  lastLightUpdateMs = millis();
  lightAnimStep++;

  switch (task.lightMode) {
    case LIGHT_SOLID:
      applySolid(task.r, task.g, task.b);
      break;
    case LIGHT_BREATHE: {
      breatheBrightness += breatheDirection;
      if (breatheBrightness >= 90) breatheDirection = -1;
      if (breatheBrightness <= 15) breatheDirection = 1;
      uint8_t br = breatheBrightness;
      applySolid(task.r * br / 255, task.g * br / 255, task.b * br / 255);
      break;
    }
    case LIGHT_PULSE: {
      uint8_t p = (lightAnimStep % 40 < 20) ? 255 : 80;
      applySolid(task.r * p / 255, task.g * p / 255, task.b * p / 255);
      break;
    }
    case LIGHT_WAVE: {
      for (int i = 0; i < NUM_PIXELS; i++) {
        uint8_t w = (sin((i + lightAnimStep * 0.2) * 0.5) + 1) * 127;
        strip.setPixelColor(i, strip.Color(task.r * w / 255, task.g * w / 255, task.b * w / 255));
      }
      strip.show();
      break;
    }
    case LIGHT_RAINBOW: {
      for (int i = 0; i < NUM_PIXELS; i++) {
        strip.setPixelColor(i, strip.ColorHSV((lightAnimStep * 256 + i * 8000) & 65535, 255, 180));
      }
      strip.show();
      break;
    }
    case LIGHT_GRADIENT: {
      for (int i = 0; i < NUM_PIXELS; i++) {
        float t = (float)i / NUM_PIXELS;
        strip.setPixelColor(i, strip.Color(task.r * (1 - t) + task.b * t, task.g, task.b * (1 - t) + task.r * t));
      }
      strip.show();
      break;
    }
    default:
      break;
  }
}

// ====================== 命令处理 ======================
void handleControlJson(JsonDocument &doc, const String &source) {
  const char *action = doc["action"] | "";
  bool force = doc["force"] | false;
  int speed = doc["speed"] | 0;
  int duration = doc["duration_s"] | doc["duration"] | 0;
  bool continuous = doc["continuous"] | false;

  if (strlen(action) == 0) {
    logLine("命令", "缺少 action");
    return;
  }

  String act = String(action);
  if (act.startsWith("通道")) {
    startChannel(act, clampSpeed(speed > 0 ? speed : 50), duration > 0 ? duration : 30, force, source);
    return;
  }

  if (!startMode(act, clampSpeed(speed), duration, force, source, continuous)) {
    currentAction = act;
    currentStatus = "unknown";
    currentRuntime = "未知";
    currentMessage = String("未知动作: ") + act;
    logFmt("命令", "未知动作 %s", act.c_str());
    markStatusDirty();
    publishStatus(true, true);
  }
}

void handleMqttAction(const String &action, bool force, const String &source) {
  StaticJsonDocument<256> doc;
  doc["action"] = action;
  doc["force"] = force;
  handleControlJson(doc, source);
}

// ====================== HTTP API ======================
void addCorsHeaders() {
  webServer.sendHeader("Access-Control-Allow-Origin", "*");
  webServer.sendHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  webServer.sendHeader("Access-Control-Allow-Headers", "Content-Type");
}

void handleApiStatus() {
  addCorsHeaders();
  StaticJsonDocument<768> doc;
  doc["device"] = DEVICE_NAME;
  doc["online"] = true;
  doc["hardware"] = true;
  doc["busy"] = task.running;
  doc["ready_for_command"] = readyForCommand;
  doc["action"] = currentAction;
  doc["status"] = currentStatus;
  doc["runtime"] = currentRuntime;
  doc["message"] = currentMessage;
  doc["ip"] = WiFi.localIP().toString();
  doc["rssi"] = WiFi.RSSI();
  doc["progress_pct"] = taskProgressPct();
  JsonObject params = doc.createNestedObject("params");
  params["speed"] = task.speed;
  params["channel"] = task.channelCode;
  params["scent"] = task.scentLabel;
  params["elapsed_s"] = taskElapsedSec();
  params["remaining_s"] = task.continuous ? -1.0f : taskRemainingSec();
  String out;
  serializeJson(doc, out);
  webServer.send(200, "application/json", out);
}

void handleApiModes() {
  addCorsHeaders();
  StaticJsonDocument<2048> doc;
  JsonArray arr = doc.createNestedArray("modes");
  for (size_t i = 0; i < MODE_COUNT; i++) {
    JsonObject m = arr.createNestedObject();
    m["action"] = MODE_TABLE[i].actionId;
    m["scent"] = MODE_TABLE[i].scentLabel;
    m["channel"] = MODE_TABLE[i].channelCode;
    m["default_speed"] = MODE_TABLE[i].defaultSpeed;
    m["default_duration_s"] = MODE_TABLE[i].defaultDurationSec;
    m["light_mode"] = (int)MODE_TABLE[i].lightMode;
    m["scent_enabled"] = MODE_TABLE[i].scentEnabled;
  }
  doc["channels"] = JsonArray();
  JsonArray ch = doc["channels"].to<JsonArray>();
  ch.add("PH"); ch.add("PL"); ch.add("NH"); ch.add("NL");
  String out;
  serializeJson(doc, out);
  webServer.send(200, "application/json", out);
}

void handleApiControl() {
  addCorsHeaders();
  if (webServer.method() == HTTP_OPTIONS) {
    webServer.send(204);
    return;
  }
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, webServer.arg("plain"));
  if (err) {
    webServer.send(400, "application/json", "{\"error\":\"invalid json\"}");
    return;
  }
  handleControlJson(doc, "web");
  StaticJsonDocument<128> resp;
  resp["ok"] = true;
  resp["action"] = currentAction;
  String out;
  serializeJson(resp, out);
  webServer.send(200, "application/json", out);
}

void handleApiStop() {
  addCorsHeaders();
  stopTask("网页手动停止");
  currentAction = "关闭";
  currentStatus = "stopped";
  currentRuntime = "已关闭";
  publishStatus(true, true);
  webServer.send(200, "application/json", "{\"ok\":true}");
}

void handleOptions() {
  addCorsHeaders();
  webServer.send(204);
}

void setupWebServer() {
  webServer.on("/api/status", HTTP_GET, handleApiStatus);
  webServer.on("/api/modes", HTTP_GET, handleApiModes);
  webServer.on("/api/control", HTTP_POST, handleApiControl);
  webServer.on("/api/control", HTTP_OPTIONS, handleOptions);
  webServer.on("/api/stop", HTTP_POST, handleApiStop);
  webServer.on("/api/stop", HTTP_OPTIONS, handleOptions);
  webServer.onNotFound([]() {
    addCorsHeaders();
    webServer.send(200, "text/plain", "Aroma ESP32 API. Open final/aroma_control.html");
  });
  webServer.begin();
  logFmt("Web", "HTTP API 已启动 http://%s:%d/api/status", WiFi.localIP().toString().c_str(), WEB_PORT);
}

// ====================== WiFi / MQTT ======================
void printBootConfig() {
  Serial.println("\n========================================");
  Serial.println("  ESP32 智能香薰机 v2");
  Serial.println("========================================");
  logFmt("系统", "WiFi=%s MQTT=%s:%d", WIFI_SSID, MQTT_BROKER, MQTT_PORT);
  logFmt("系统", "control=%s status=%s", controlTopic, statusTopic);
}

bool connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  logFmt("WiFi", "连接 %s ...", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    logLine("WiFi", "成功连接 WiFi");
    logFmt("WiFi", "IP 地址: %s", WiFi.localIP().toString().c_str());
    logFmt("WiFi", "信号: %d dBm", WiFi.RSSI());
    wifiWasConnected = true;
    return true;
  }
  logLine("WiFi", "连接失败");
  return false;
}

bool testBrokerTcpPort() {
  WiFiClient probe;
  probe.setTimeout(5);
  if (probe.connect(MQTT_BROKER, MQTT_PORT, 5000)) {
    probe.stop();
    return true;
  }
  logLine("网络", "TCP 1883 不可达，检查防火墙");
  return false;
}

void ensureMqtt() {
  if (WiFi.status() != WL_CONNECTED || mqttClient.connected()) return;
  if (!testBrokerTcpPort()) return;
  String clientId = String("aroma-esp32-") + String((uint32_t)ESP.getEfuseMac(), HEX);
  bool ok = strlen(MQTT_USER) > 0
    ? mqttClient.connect(clientId.c_str(), MQTT_USER, MQTT_PASS)
    : mqttClient.connect(clientId.c_str());
  if (ok) {
    mqttClient.subscribe(controlTopic, 1);
    logLine("MQTT", "成功连接并订阅 control");
    publishStatus(true, true);
  } else {
    logFmt("MQTT", "失败 rc=%d %s", mqttClient.state(), mqttStateText(mqttClient.state()));
  }
}

void setupTime() {
  configTime(8 * 3600, 0, "ntp.aliyun.com", "pool.ntp.org");
  struct tm timeinfo;
  if (getLocalTime(&timeinfo, 3000)) ntpReady = true;
}

String isoTimestamp() {
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo, 50)) return "";
  char buf[32];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &timeinfo);
  return String(buf);
}

// ====================== setup / loop ======================
void setup() {
  Serial.begin(115200);
  delay(400);
  pinMode(scent_PH, OUTPUT);
  pinMode(scent_PL, OUTPUT);
  pinMode(scent_NH, OUTPUT);
  pinMode(scent_NL, OUTPUT);
  turnAllOff();
  strip.begin();
  strip.show();

  snprintf(controlTopic, sizeof(controlTopic), "%s/%s/control", TOPIC_PREFIX, DEVICE_NAME);
  snprintf(statusTopic, sizeof(statusTopic), "%s/%s/status", TOPIC_PREFIX, DEVICE_NAME);

  mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
  mqttClient.setBufferSize(768);
  mqttClient.setSocketTimeout(15);
  mqttClient.setCallback([](char *topic, byte *payload, unsigned int length) {
    StaticJsonDocument<512> doc;
    if (deserializeJson(doc, payload, length)) return;
    logFmt("MQTT", "收到 action=%s", doc["action"].as<const char *>());
    handleControlJson(doc, "mqtt");
  });

  printBootConfig();
  if (connectWiFi()) {
    setupTime();
    setupWebServer();
    ensureMqtt();
  }
  logLine("系统", "就绪");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    wifiWasConnected = false;
    if (millis() - lastMqttRetryMs > 5000) {
      lastMqttRetryMs = millis();
      connectWiFi();
    }
  } else {
    if (!wifiWasConnected) {
      wifiWasConnected = true;
      setupWebServer();
      ensureMqtt();
    }
    if (!mqttClient.connected() && millis() - lastMqttRetryMs > 3000) {
      lastMqttRetryMs = millis();
      ensureMqtt();
    } else {
      mqttClient.loop();
    }
    if (millis() - lastHeartbeatMs > 3000) {
      lastHeartbeatMs = millis();
      heartbeatLogCounter++;
      publishStatus(heartbeatLogCounter % 10 == 0, false);
    }
  }

  webServer.handleClient();
  updateSprayMachine();
  updateLightEffects();

  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.equalsIgnoreCase("STOP")) {
      handleMqttAction("关闭", true, "serial");
    } else if (line.length() > 0) {
      StaticJsonDocument<128> doc;
      doc["action"] = line;
      doc["force"] = true;
      handleControlJson(doc, "serial");
    }
  }
}
'''

# Fix ArduinoJson v6 - remove broken fillStatusJson references (already removed from final)
# Fix handleApiModes channels - JsonArray syntax wrong for v6
CONTENT = CONTENT.replace(
    '  doc["channels"] = JsonArray();\n  JsonArray ch = doc["channels"].to<JsonArray>();\n  ch.add("PH"); ch.add("PL"); ch.add("NH"); ch.add("NL");',
    '  JsonArray ch = doc.createNestedArray("channels");\n  ch.add("PH"); ch.add("PL"); ch.add("NH"); ch.add("NL");',
)

# Remove dead fillStatusJson function block if present
start = CONTENT.find('void fillStatusJson')
if start != -1:
    end = CONTENT.find('void publishStatus(bool logOutput, bool forceNow)', start)
    if end != -1:
        CONTENT = CONTENT[:start] + CONTENT[end:]

# Remove duplicate publishStatus stub comment lines
CONTENT = CONTENT.replace(
    '// ArduinoJson 6 helper for float in nested - use direct assignment\n// Fix: serialized() may not exist in v6 - use roundf\n// Replace params elapsed with float cast in publish\n\n',
    '',
)

INO.write_text(CONTENT, encoding='utf-8')
print('Wrote', INO, 'bytes', INO.stat().st_size)
