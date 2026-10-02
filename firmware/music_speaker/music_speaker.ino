// ESP32-WROOM-32E 음악용 스피커 펌웨어 (MAX98357A)
// Arduino ESP32 core 3.x / 라이브러리: PubSubClient, ESP32-A2DP
// 보드 설정: Partition Scheme = Huge APP (3MB No OTA)
//
// WiFi 모드: Pi audio_client.py → TCP 6001 → 재생 (44.1kHz 스테레오)
//   - 연결마다 12바이트 헤더: "AUD1" + rate(uint32) + channels(uint16) + 0(uint16)
//   - 새 연결이 들어오면 이전 곡을 즉시 멈추고 버퍼를 비움
//   - channels=0 헤더는 "정지" 명령
// 블루투스 모드: 폰에서 "ESP32-Music"으로 연결
// 모드 전환: BOOT 버튼 1초, 또는 MQTT music/mode 에 "bt" / "wifi" (재부팅됨)

#include <WiFi.h>
#include <PubSubClient.h>
#include <Preferences.h>
#include <ESP_I2S.h>
#include <BluetoothA2DPSink.h>
#include <esp_bt.h>
#include <freertos/stream_buffer.h>

// ===================== 설정 (여기만 수정) =====================
#include "secrets.h"   // Wi-Fi · MQTT 계정, 라즈베리파이 IP (secrets.example.h 참고)

#define TCP_PORT   6001
#define MQTT_PORT  1883
#define T_VOLUME   "music/volume"   // 0~100
#define T_MODE     "music/mode"     // "bt" 또는 "wifi"
#define BT_NAME    "ESP32-Music"

#define PREBUF_MS  300              

// ===================== 핀 =====================
#define SPK_BCLK 26
#define SPK_LRC  25
#define SPK_DIN  22
#define PIN_BOOT 0

// ===================== 전역 =====================
I2SClass spk;
BluetoothA2DPSink a2dp;
Preferences prefs;
WiFiServer server(TCP_PORT);
WiFiClient net;
PubSubClient mqtt(net);
StreamBufferHandle_t ring;
size_t ringBytes = 0;

bool btMode = false;
volatile uint8_t  volume = 70;
volatile uint32_t curRate = 44100, reqRate = 44100;
volatile uint16_t curCh = 2, reqCh = 2;
volatile bool cfgReq = false;      // 샘플레이트/채널 변경 요청
volatile bool flushReq = false;    // 버퍼 비우기 요청
volatile bool streamOpen = false;  // 곡이 아직 들어오는 중인지
volatile uint32_t underruns = 0;

bool i2sStart(uint32_t rate, uint16_t ch) {
  return spk.begin(I2S_MODE_STD, rate, I2S_DATA_BIT_WIDTH_16BIT,
                   ch == 2 ? I2S_SLOT_MODE_STEREO : I2S_SLOT_MODE_MONO);
}

// ===================== 재생 =====================
void playerTask(void*) {
  static int16_t buf[512];                 // 1024바이트 = 스테레오 256프레임
  bool playing = false;
  for (;;) {
    if (cfgReq) {
      xStreamBufferReset(ring);
      playing = false;
      flushReq = false;
      if (reqRate != curRate || reqCh != curCh) {
        spk.end();
        curRate = reqRate;
        curCh = reqCh;
        if (!i2sStart(curRate, curCh)) Serial.println("[ERR] I2S reconfig");
        Serial.printf("[I2S] %u Hz, %u ch\n", (unsigned)curRate, (unsigned)curCh);
      }
      cfgReq = false;
    }
    if (flushReq) {
      xStreamBufferReset(ring);
      playing = false;
      flushReq = false;
    }

    size_t avail = xStreamBufferBytesAvailable(ring);
    size_t prebuf = min((size_t)(curRate * curCh * 2 * PREBUF_MS / 1000), ringBytes / 2);
    if (!playing && (avail >= prebuf || (!streamOpen && avail > 0))) playing = true;

    if (playing) {
      size_t n = xStreamBufferReceive(ring, buf, sizeof(buf), pdMS_TO_TICKS(50));
      if (n == 0) {
        playing = false;
        if (streamOpen) underruns++;       // 곡 중간에 버퍼가 비었음
        continue;
      }
      uint8_t v = volume;
      for (size_t i = 0; i < n / 2; i++) buf[i] = (int32_t)buf[i] * v / 100;
      if (spk.write((uint8_t*)buf, n) == 0) vTaskDelay(1);   // I2S 이상 시 다른 태스크를 막지 않도록
    } else {
      memset(buf, 0, sizeof(buf));
      if (spk.write((uint8_t*)buf, 256 * curCh * 2) == 0) vTaskDelay(1);   // 무음 256프레임
    }
  }
}

// ===================== WiFi: TCP 수신 =====================
void tcpTask(void*) {
  static uint8_t tmp[4096 + 4];
  uint8_t hdr[12];
  int hdrGot = 0;
  uint8_t carry[4];
  int carryN = 0;
  WiFiClient cli;

  for (;;) {
    if (server.hasClient()) {                     // 새 연결 = 새 곡 또는 정지
      if (cli) cli.stop();
      cli = server.accept();
      cli.setNoDelay(true);
      hdrGot = 0;
      carryN = 0;
      streamOpen = false;
      flushReq = true;
    }
    if (!cli) { vTaskDelay(pdMS_TO_TICKS(10)); continue; }

    int av = cli.available();
    if (av > 0) {
      if (hdrGot < 12) {                          // 헤더 먼저 받기
        int r = cli.read(hdr + hdrGot, 12 - hdrGot);
        if (r > 0) hdrGot += r;
        if (hdrGot == 12) {
          uint32_t rate; uint16_t ch;
          memcpy(&rate, hdr + 4, 4);
          memcpy(&ch, hdr + 8, 2);
          if (memcmp(hdr, "AUD1", 4) != 0) {
            Serial.println("[TCP] bad header");
            cli.stop();
          } else if (ch == 0) {
            Serial.println("[TCP] stop");
            flushReq = true;
            cli.stop();
          } else {
            reqRate = rate;
            reqCh = ch;
            cfgReq = true;
            while (cfgReq) vTaskDelay(1);         // 재생 태스크가 설정을 마칠 때까지 대기
            streamOpen = true;
            Serial.printf("[TCP] stream %u Hz %u ch\n", (unsigned)rate, (unsigned)ch);
          }
        }
        continue;
      }

      size_t space = xStreamBufferSpacesAvailable(ring);
      if (space < 1024) { vTaskDelay(pdMS_TO_TICKS(5)); continue; }   // 가득 참 → TCP가 Pi를 기다리게 함

      size_t want = min((size_t)av, min((size_t)4096, space - 8));
      memcpy(tmp, carry, carryN);
      int r = cli.read(tmp + carryN, want);
      if (r <= 0) continue;
      size_t total = carryN + r;
      size_t aligned = total & ~(size_t)3;        // 스테레오 프레임(4바이트) 단위로만 넣기
      if (aligned) xStreamBufferSend(ring, tmp, aligned, 0);
      carryN = total - aligned;
      memcpy(carry, tmp + aligned, carryN);
    } else if (!cli.connected()) {
      cli.stop();
      streamOpen = false;
      Serial.println("[TCP] closed");
    } else {
      vTaskDelay(pdMS_TO_TICKS(2));
    }
  }
}

// ===================== 블루투스 =====================
void onBtData(const uint8_t* data, uint32_t len) {
  xStreamBufferSend(ring, data, len & ~3u, 0);
}
void onBtRate(uint16_t rate) {
  reqRate = rate;
  reqCh = 2;
  cfgReq = true;
}
void onBtVolume(int v) {                          // 폰 볼륨 (0~127)
  volume = v * 100 / 127;
}

// ===================== 모드 전환 / MQTT =====================
void switchMode(bool toBt) {
  Serial.printf("[MODE] -> %s, 재부팅\n", toBt ? "bt" : "wifi");
  prefs.putBool("bt", toBt);
  delay(200);
  ESP.restart();
}

void onMqtt(char* topic, byte* payload, unsigned int len) {
  char s[8] = {0};
  memcpy(s, payload, min(len, (unsigned int)7));
  if (!strcmp(topic, T_VOLUME)) {
    volume = constrain(atoi(s), 0, 100);
    prefs.putUChar("vol", volume);
    Serial.printf("[MQTT] volume=%d\n", volume);
  } else if (!strcmp(topic, T_MODE) && !strcmp(s, "bt")) {
    switchMode(true);
  }
}

void mqttReconnect() {
  static uint32_t last = 0;
  if (mqtt.connected() || WiFi.status() != WL_CONNECTED) return;
  if (millis() - last < 3000) return;
  last = millis();
  String id = "esp32-music-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  if (mqtt.connect(id.c_str(), MQTT_USER, MQTT_PASS)) {
    mqtt.subscribe(T_VOLUME);
    mqtt.subscribe(T_MODE);
    Serial.println("[MQTT] connected");
  }
}

// ===================== setup / loop =====================
void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(PIN_BOOT, INPUT_PULLUP);

  spk.setPins(SPK_BCLK, SPK_LRC, SPK_DIN);             // I2S 핀 지정 (빠뜨리면 I2S가 시작 안 됨)

  prefs.begin("music", false);
  btMode = prefs.getBool("bt", false);
  volume = prefs.getUChar("vol", 70);

  if (btMode) {
    esp_bt_controller_mem_release(ESP_BT_MODE_BLE);    // BLE 메모리 반납 (클래식 BT만 사용)
    if (!i2sStart(curRate, curCh)) Serial.println("[ERR] I2S start");
    ringBytes = 32 * 1024;
    ring = xStreamBufferCreate(ringBytes, 4);
    streamOpen = true;
    a2dp.set_stream_reader(onBtData, false);           // false: 라이브러리가 직접 출력하지 않음
    a2dp.set_sample_rate_callback(onBtRate);
    a2dp.set_on_volumechange(onBtVolume);
    a2dp.start(BT_NAME);
    Serial.printf("[MODE] bt  name=%s  heap=%u\n", BT_NAME, (unsigned)ESP.getFreeHeap());
  } else {
    esp_bt_controller_mem_release(ESP_BT_MODE_BTDM);   // BT 메모리 전부 반납
    if (!i2sStart(curRate, curCh)) Serial.println("[ERR] I2S start");
    ringBytes = 96 * 1024;                             // 44.1kHz 스테레오 약 0.55초
    ring = xStreamBufferCreate(ringBytes, 4);
    if (!ring) {
      ringBytes = 48 * 1024;
      ring = xStreamBufferCreate(ringBytes, 4);
    }

    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) delay(200);
    Serial.printf("[WiFi] %s IP=%s\n", WiFi.status() == WL_CONNECTED ? "ok" : "FAIL",
                  WiFi.localIP().toString().c_str());

    server.begin();
    server.setNoDelay(true);
    mqtt.setServer(PI_IP, MQTT_PORT);
    mqtt.setCallback(onMqtt);
    xTaskCreatePinnedToCore(tcpTask, "tcp", 6144, nullptr, 4, nullptr, 1);
    Serial.printf("[MODE] wifi  ring=%uKB  heap=%u\n", (unsigned)(ringBytes / 1024),
                  (unsigned)ESP.getFreeHeap());
  }

  xTaskCreatePinnedToCore(playerTask, "player", 4096, nullptr, 6, nullptr, 1);
}

void loop() {
  if (!btMode) {
    mqttReconnect();
    mqtt.loop();
  }

  // BOOT 버튼 1초 → 모드 전환
  static uint32_t pressStart = 0;
  if (digitalRead(PIN_BOOT) == LOW) {
    if (!pressStart) pressStart = millis();
    else if (millis() - pressStart > 1000) switchMode(!btMode);
  } else {
    pressStart = 0;
  }

  // 5초마다 상태 출력 (끊김 진단용)
  static uint32_t tStat = 0;
  if (millis() - tStat > 5000) {
    tStat = millis();
    Serial.printf("[STAT] buf=%u/%u underruns=%u heap=%u\n",
                  (unsigned)xStreamBufferBytesAvailable(ring), (unsigned)ringBytes,
                  (unsigned)underruns, (unsigned)ESP.getFreeHeap());
  }
  delay(5);
}
