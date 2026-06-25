#include <Adafruit_NeoPixel.h>
#include <WiFi.h>
#include <WebServer.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <time.h>
#include <stdarg.h>
#include <cstring>
#include <math.h>

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

// ====================== 模式 / 灯光 / 喷洒策略 ======================
enum LightMode : uint8_t {
  LIGHT_OFF = 0,
  LIGHT_SOLID,
  LIGHT_BREATHE,
  LIGHT_PULSE,
  LIGHT_WAVE,
  LIGHT_RAINBOW,
  LIGHT_GRADIENT,
};

// 间歇=开阀-关阀循环；持续=长喷短停(>80%%占空比)；慢间歇=冥想类
enum SprayPattern : uint8_t {
  SPRAY_INTERMITTENT = 0,
  SPRAY_CONTINUOUS,
  SPRAY_SLOW,
};

struct AromaModeDef {
  const char *actionId;
  const char *scentLabel;
  const char *channelCode;
  int pin;
  uint8_t r, g, b;
  LightMode lightMode;
  bool scentEnabled;
  SprayPattern sprayPattern;
  uint16_t sessionSec;     // 模式总时长(秒)
  uint16_t burstMs;        // 启动爆发喷(毫秒, 0=无)
  uint16_t sprayOnMs;      // 单次开阀
  uint16_t sprayOffMs;     // 单次关阀间隔
  uint8_t baseSpeed;       // 速度滑条基准(50=模式默认)
  const char *sprayDesc;   // 策略说明
};

/*
  通道: PH青柠 PL白玉兰 NH洋甘菊薰衣草 NL薄荷
  sessionSec/burstMs/sprayOnMs/sprayOffMs 均为模式内置参数
*/
const AromaModeDef MODE_TABLE[] = {
  {"关闭", "none", "OFF", -1, 0,0,0, LIGHT_OFF, false, SPRAY_INTERMITTENT, 0, 0, 0, 0, 50, "停止"},
  // --- 控制中心三模式 (加强爆发+可感知间歇) ---
  {"平静", "chamomile", "NH", scent_NH, 150,80,255, LIGHT_BREATHE, true,
   SPRAY_INTERMITTENT, 90, 6000, 3500, 6500, 50, "90s|爆发6s+间歇3.5s/6.5s|NH洋甘菊"},
  {"舒缓", "magnolia", "PL", scent_PL, 255,210,140, LIGHT_BREATHE, true,
   SPRAY_INTERMITTENT, 75, 7000, 4000, 5000, 55, "75s|爆发7s+间歇4s/5s|PL白玉兰"},
  {"提神", "mint", "NL", scent_NL, 80,220,255, LIGHT_PULSE, true,
   SPRAY_CONTINUOUS, 50, 8000, 9000, 1500, 70, "50s|爆发8s+持续9s/1.5s|NL薄荷"},
  // --- 扩展模式 ---
  {"专注", "lime_basil", "PH", scent_PH, 0,200,140, LIGHT_SOLID, true,
   SPRAY_INTERMITTENT, 60, 5000, 2800, 4200, 50, "60s|爆发5s+间歇2.8s/4.2s|PH青柠"},
  {"共情", "magnolia", "PL", scent_PL, 255,200,150, LIGHT_GRADIENT, true,
   SPRAY_INTERMITTENT, 70, 5500, 3200, 5800, 45, "70s|爆发5.5s+间歇3.2s/5.8s|PL"},
  {"振奋", "citrus", "PH", scent_PH, 255,160,40, LIGHT_RAINBOW, true,
   SPRAY_CONTINUOUS, 45, 9000, 10000, 1200, 75, "45s|爆发9s+持续10s/1.2s|PH"},
  {"冥想", "sandalwood", "NH", scent_NH, 120,60,180, LIGHT_BREATHE, true,
   SPRAY_SLOW, 180, 4000, 2200, 14000, 30, "180s|爆发4s+慢间歇2.2s/14s|NH"},
  {"净化", "eucalyptus", "NL", scent_NL, 60,180,255, LIGHT_WAVE, true,
   SPRAY_INTERMITTENT, 55, 6000, 4500, 4000, 60, "55s|爆发6s+间歇4.5s/4s|NL"},
  {"浪漫", "rose", "PL", scent_PL, 255,120,180, LIGHT_PULSE, true,
   SPRAY_INTERMITTENT, 80, 5500, 3500, 7500, 45, "80s|爆发5.5s+间歇3.5s/7.5s|PL"},
  {"深度放松", "deep_lavender", "NH", scent_NH, 100,50,160, LIGHT_BREATHE, true,
   SPRAY_SLOW, 120, 8000, 3000, 12000, 35, "120s|爆发8s+慢间歇3s/12s|NH"},
  {"派对", "mix", "ALL", -2, 255,255,255, LIGHT_RAINBOW, true,
   SPRAY_CONTINUOUS, 40, 5000, 7000, 2000, 80, "40s|四通道轮换+持续喷|ALL"},
  {"夜灯", "none", "OFF", -1, 255,180,80, LIGHT_BREATHE, false,
   SPRAY_INTERMITTENT, 0, 0, 0, 0, 50, "仅灯光"},
};
const size_t MODE_COUNT = sizeof(MODE_TABLE) / sizeof(MODE_TABLE[0]);

enum SprayPhase { PHASE_IDLE, PHASE_BURST, PHASE_ON, PHASE_OFF };

struct ActiveTask {
  bool running = false;
  bool continuous = false;
  int pin = -1;
  bool allChannels = false;
  uint8_t speed = 50;
  uint16_t onMs = 2000;
  uint16_t offMs = 4000;
  uint16_t burstMs = 0;
  SprayPattern sprayPattern = SPRAY_INTERMITTENT;
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
void applyModeSprayTiming(const AromaModeDef *mode, uint8_t userSpeed, ActiveTask &out);
const char *sprayPatternName(SprayPattern p);
void stopTask(const char *message);
bool startMode(const String &action, uint8_t speed, uint16_t durationSec, bool force, const String &source, bool continuous = false);
bool startChannel(const String &channel, uint8_t speed, uint16_t durationSec, bool force, const String &source);
void sprayPinOn();
void rotatePartyChannel();
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

const char *sprayPatternName(SprayPattern p) {
  switch (p) {
    case SPRAY_CONTINUOUS: return "continuous";
    case SPRAY_SLOW: return "slow_intermittent";
    default: return "intermittent";
  }
}

void applyModeSprayTiming(const AromaModeDef *mode, uint8_t userSpeed, ActiveTask &out) {
  if (!mode) return;
  out.sprayPattern = mode->sprayPattern;
  out.burstMs = mode->burstMs;
  out.onMs = mode->sprayOnMs;
  out.offMs = mode->sprayOffMs;

  if (mode->sprayPattern == SPRAY_CONTINUOUS) {
    out.onMs = max(out.onMs, (uint16_t)8000);
    out.offMs = min(out.offMs, (uint16_t)2000);
  } else if (mode->sprayPattern == SPRAY_SLOW) {
    out.offMs = max(out.offMs, (uint16_t)10000);
  }

  uint8_t ref = mode->baseSpeed > 0 ? mode->baseSpeed : 50;
  uint8_t spd = userSpeed > 0 ? clampSpeed(userSpeed) : ref;
  float factor = (float)spd / (float)ref;
  factor = constrain(factor, 0.6f, 1.8f);

  out.onMs = (uint16_t)constrain((int)(out.onMs * factor), 1000, 15000);
  out.offMs = (uint16_t)constrain((int)(out.offMs / factor), 500, 20000);
  if (out.burstMs > 0) {
    out.burstMs = (uint16_t)constrain((int)(out.burstMs * factor), 3000, 15000);
  }
  out.speed = spd;
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
    const char *phaseStr = "idle";
    if (task.phase == PHASE_BURST) phaseStr = "burst";
    else if (task.phase == PHASE_ON) phaseStr = "spray_on";
    else if (task.phase == PHASE_OFF) phaseStr = "spray_off";
    doc["phase"] = phaseStr;

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
    params["spray_pattern"] = sprayPatternName(task.sprayPattern);
    params["burst_ms"] = task.burstMs;
    params["spray_on_ms"] = task.onMs;
    params["spray_off_ms"] = task.offMs;
    params["spray_desc"] = task.scentEnabled ? "mode_profile" : "light_only";

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
  applyModeSprayTiming(mode, speed > 0 ? speed : 0, task);
  task.lightMode = mode->lightMode;
  task.r = mode->r;
  task.g = mode->g;
  task.b = mode->b;
  task.scentEnabled = mode->scentEnabled;
  task.startMs = millis();
  uint16_t dur = durationSec > 0 ? durationSec : mode->sessionSec;
  task.endMs = continuous ? 0 : task.startMs + (unsigned long)dur * 1000UL;
  if (task.scentEnabled && task.burstMs > 0) task.phase = PHASE_BURST;
  else if (task.scentEnabled) task.phase = PHASE_ON;
  else task.phase = PHASE_IDLE;
  task.phaseStartMs = millis();

  if (strcmp(mode->channelCode, "ALL") == 0) {
    task.allChannels = true;
    task.pin = scent_PH;
  } else {
    task.pin = mode->pin;
    task.allChannels = false;
  }

  if (task.scentEnabled) {
    if (task.allChannels) {
      turnAllOff();
      task.pin = scent_PH;
      digitalWrite(scent_PH, HIGH);
    } else if (task.pin >= 0) {
      sprayPinOn();
    }
  }

  currentAction = action;
  currentStatus = "executing";
  currentRuntime = "执行中";
  currentMessage = String("模式 ") + action + " 运行中";
  readyForCommand = false;

  logFmt("任务", "模式=%s %s 通道=%s 爆发=%ums 开=%u 关=%u 总=%us",
         action.c_str(), mode->sprayDesc, task.channelCode,
         task.burstMs, task.onMs, task.offMs, dur);
  markStatusDirty();
  publishStatus(true, true);
  return true;
}

bool startChannel(const String &channel, uint8_t speed, uint16_t durationSec, bool force, const String &source) {
  String action = channel;
  if (action.startsWith("通道")) action = action.substring(strlen("通道"));
  action.toUpperCase();

  int pin = -1;
  if (action == "PH") pin = scent_PH;
  else if (action == "PL") pin = scent_PL;
  else if (action == "NH") pin = scent_NH;
  else if (action == "NL") pin = scent_NL;
  else return false;

  lastSource = source;
  (void)force;
  turnAllOff();
  task = ActiveTask();
  task.running = true;
  task.pin = pin;
  task.channelCode = action.c_str();
  task.scentLabel = "custom";
  task.speed = clampSpeed(speed > 0 ? speed : 55);
  task.burstMs = 5000;
  task.onMs = 3500;
  task.offMs = 4500;
  task.sprayPattern = SPRAY_INTERMITTENT;
  task.lightMode = LIGHT_SOLID;
  task.r = 180;
  task.g = 180;
  task.b = 180;
  task.scentEnabled = true;
  task.startMs = millis();
  task.endMs = task.startMs + (unsigned long)(durationSec > 0 ? durationSec : 45) * 1000UL;
  task.phase = PHASE_BURST;
  task.phaseStartMs = millis();
  digitalWrite(task.pin, HIGH);
  currentAction = String("通道") + action;
  currentStatus = "executing";
  currentRuntime = "执行中";
  currentMessage = String("通道 ") + action + " 爆发5s+间歇3.5s/4.5s";
  readyForCommand = false;
  markStatusDirty();
  publishStatus(true, true);
  return true;
}

void sprayPinOn() {
  if (task.allChannels) {
    digitalWrite(scent_PH, HIGH);
    digitalWrite(scent_PL, HIGH);
    digitalWrite(scent_NH, HIGH);
    digitalWrite(scent_NL, HIGH);
  } else if (task.pin >= 0) {
    digitalWrite(task.pin, HIGH);
  }
}

void rotatePartyChannel() {
  static int partyIdx = 0;
  turnAllOff();
  const int pins[4] = {scent_PH, scent_PL, scent_NH, scent_NL};
  task.pin = pins[partyIdx % 4];
  partyIdx++;
  digitalWrite(task.pin, HIGH);
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

  if (!task.scentEnabled || task.pin < -1) return;

  unsigned long elapsed = now - task.phaseStartMs;

  if (task.phase == PHASE_BURST) {
    if (elapsed >= task.burstMs) {
      if (task.allChannels) turnAllOff();
      else digitalWrite(task.pin, LOW);
      task.phase = (task.sprayPattern == SPRAY_CONTINUOUS) ? PHASE_ON : PHASE_OFF;
      task.phaseStartMs = now;
      logLine("喷香", "爆发结束，进入常規循环");
      markStatusDirty();
    }
    return;
  }

  if (task.phase == PHASE_ON) {
    if (elapsed >= task.onMs) {
      if (task.allChannels) {
        rotatePartyChannel();
        task.phaseStartMs = now;
      } else {
        digitalWrite(task.pin, LOW);
        task.phase = PHASE_OFF;
        task.phaseStartMs = now;
      }
      markStatusDirty();
    }
  } else if (task.phase == PHASE_OFF) {
    if (elapsed >= task.offMs) {
      if (task.allChannels) rotatePartyChannel();
      else sprayPinOn();
      task.phase = PHASE_ON;
      task.phaseStartMs = now;
      markStatusDirty();
    }
  }
  publishStatus(false, false);
}

void updateLightEffects() {
  if (!task.running) {
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
    m["session_sec"] = MODE_TABLE[i].sessionSec;
    m["burst_ms"] = MODE_TABLE[i].burstMs;
    m["spray_on_ms"] = MODE_TABLE[i].sprayOnMs;
    m["spray_off_ms"] = MODE_TABLE[i].sprayOffMs;
    m["spray_pattern"] = sprayPatternName(MODE_TABLE[i].sprayPattern);
    m["spray_desc"] = MODE_TABLE[i].sprayDesc;
    m["base_speed"] = MODE_TABLE[i].baseSpeed;
    m["light_mode"] = (int)MODE_TABLE[i].lightMode;
    m["scent_enabled"] = MODE_TABLE[i].scentEnabled;
  }
  JsonArray ch = doc.createNestedArray("channels");
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
