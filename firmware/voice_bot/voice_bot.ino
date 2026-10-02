// ESP32-WROOM-32E 음성 비서 펌웨어 (마이크 + 스피커 한 보드)
// Arduino ESP32 core 3.x / 라이브러리: PubSubClient
//
// 스피커: Pi audio_client.py → UDP 6000 → MAX98357A 재생
// 마이크: INMP441 → 음성 감지(VAD) → UDP 5005로 START / PCM / END → Pi mic_receiver.py
// 재생 중(+끝난 뒤 300ms)에는 녹음을 시작하지 않아서 자기 목소리를 명령으로 인식하지 않음

#include <WiFi.h>
#include <WiFiUdp.h>
#include <PubSubClient.h>
#include <ESP_I2S.h>
#include <freertos/stream_buffer.h>

// ===================== 설정 (여기만 수정) =====================
#include "secrets.h"   // Wi-Fi · MQTT 계정, 라즈베리파이 IP (secrets.example.h 참고)


#define SPK_UDP_PORT  6000    // audio_client.py 의 PORT
#define MIC_UDP_PORT  5005    // mic_receiver.py 의 MIC_UDP_PORT
#define MQTT_PORT     1883
#define T_VOLUME      "speaker/volume"   // 0~100
#define T_MUSIC       "music/state"      // playing / stopped (assistant.py가 발행)

#define VAD_RMS       1000    // 평소 역치 (테스트 스케치와 같은 스케일)
#define VAD_RMS_MUSIC 3000    // 음악 재생 중 역치 (음악 보드 소리를 무시하도록 높게)
#define SILENCE_MS    800     // 이만큼 조용하면 END
#define MAX_REC_MS    10000   // 최대 녹음 길이
#define GATE_MS       300     // 재생 끝난 뒤 녹음 금지 시간

// ===================== 핀 =====================
#define SPK_BCLK 26
#define SPK_LRC  25
#define SPK_DIN  22
#define MIC_SCK  32
#define MIC_WS   33
#define MIC_SD   34

#define RATE       16000
#define N          256                     // 256샘플 = 16ms
#define PRE_BLOCKS 19                      // 19 x 16ms ≈ 300ms 프리롤
#define RING_BYTES 32000                   // 스피커 버퍼 1초
#define PREBUF     (RATE * 2 * 150 / 1000) // 150ms 모이면 재생 시작

// ===================== 전역 =====================
I2SClass spk, mic;
WiFiUDP udpSpk, udpMic;
WiFiClient net;
PubSubClient mqtt(net);
StreamBufferHandle_t ring;

volatile uint8_t  volume = 70;
volatile uint32_t lastPlayMs = 0;
volatile bool     playing = false;
volatile bool     musicOn = false;   // 음악 보드가 재생 중인지 (MQTT로 받음)

// ===================== 스피커 =====================
void playerTask(void*) {
  static int16_t buf[N];
  for (;;) {
    if (!playing && xStreamBufferBytesAvailable(ring) >= PREBUF) playing = true;

    if (playing) {
      size_t n = xStreamBufferReceive(ring, buf, sizeof(buf), pdMS_TO_TICKS(50));
      if (n == 0) { playing = false; continue; }      // 스트림 끝 또는 끊김
      uint8_t v = volume;
      for (size_t i = 0; i < n / 2; i++) buf[i] = (int32_t)buf[i] * v / 100;
      spk.write((uint8_t*)buf, n);
      lastPlayMs = millis();
    } else {
      memset(buf, 0, sizeof(buf));                    // 대기 중엔 무음 출력
      spk.write((uint8_t*)buf, sizeof(buf));
    }
  }
}

void handleSpeakerUdp() {
  static uint8_t pkt[1500];
  int sz;
  while ((sz = udpSpk.parsePacket()) > 0) {
    int len = udpSpk.read(pkt, sizeof(pkt));
    if (len <= 0) continue;
    if ((len == 5 && !memcmp(pkt, "START", 5)) || (len == 3 && !memcmp(pkt, "END", 3))) continue;

    uint8_t* p = pkt;
    if (len >= 4 && pkt[0] == 0x5A && pkt[1] == 0xA5) { p += 4; len -= 4; }  // MAGIC+seq 헤더 제거
    xStreamBufferSend(ring, p, len, 0);               // 가득 차면 버림
  }
}

// ===================== 마이크 =====================
void sendMic(const void* data, size_t len) {
  if (WiFi.status() != WL_CONNECTED) return;
  udpMic.beginPacket(PI_IP, MIC_UDP_PORT);
  udpMic.write((const uint8_t*)data, len);
  udpMic.endPacket();
}

void micTask(void*) {
  static int32_t raw[N];
  static int16_t pre[PRE_BLOCKS][N];
  int preIdx = 0;
  float dc = 0;
  bool recording = false;
  int loudCount = 0;
  uint32_t recStart = 0, lastLoud = 0;
  uint32_t bootMs = millis();

  for (;;) {
    size_t got = mic.readBytes((char*)raw, sizeof(raw));
    int n = got / 4;
    if (n < N) continue;

    // 24bit → 16bit, DC 제거, rms 계산
    int16_t* blk = pre[preIdx];
    double sumSq = 0;
    for (int i = 0; i < N; i++) {
      float x = (float)(raw[i] >> 14);
      dc += (x - dc) / 1024.0f;
      int v = constrain((int)(x - dc), -32768, 32767);
      blk[i] = v;
      sumSq += (double)v * v;
    }
    int rms = (int)sqrt(sumSq / N);
    preIdx = (preIdx + 1) % PRE_BLOCKS;

    uint32_t now = millis();
    if (now - bootMs < 1500) continue;                // 부팅 직후 DC 안정화 대기

    bool gated = playing || (now - lastPlayMs < GATE_MS);
    int  th = musicOn ? VAD_RMS_MUSIC : VAD_RMS;

    if (!recording) {
      loudCount = (rms > th && !gated) ? loudCount + 1 : 0;
      if (loudCount >= 2) {                           // 32ms 연속이면 말 시작
        sendMic("START", 5);
        for (int k = 0; k < PRE_BLOCKS; k++)          // 오래된 블록부터 프리롤 전송
          sendMic(pre[(preIdx + k) % PRE_BLOCKS], N * 2);
        recording = true;
        recStart = lastLoud = now;
        loudCount = 0;
        Serial.printf("[MIC] START rms=%d th=%d\n", rms, th);
      }
    } else {
      sendMic(blk, N * 2);
      if (rms > th) lastLoud = now;
      if (now - lastLoud > SILENCE_MS || now - recStart > MAX_REC_MS) {
        sendMic("END", 3);
        recording = false;
        Serial.printf("[MIC] END %lums\n", (unsigned long)(now - recStart));
      }
    }
  }
}

// ===================== MQTT =====================
void onMqtt(char* topic, byte* payload, unsigned int len) {
  char s[8] = {0};
  memcpy(s, payload, min(len, (unsigned int)7));
  if (!strcmp(topic, T_VOLUME)) {
    volume = constrain(atoi(s), 0, 100);
    Serial.printf("[MQTT] volume=%d\n", volume);
  } else if (!strcmp(topic, T_MUSIC)) {
    musicOn = !strcmp(s, "playing");
    Serial.printf("[MQTT] music=%s\n", musicOn ? "playing" : "stopped");
  }
}

void mqttReconnect() {
  static uint32_t last = 0;
  if (mqtt.connected() || WiFi.status() != WL_CONNECTED) return;
  if (millis() - last < 3000) return;
  last = millis();
  String id = "esp32-voice-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  if (mqtt.connect(id.c_str(), MQTT_USER, MQTT_PASS)) {
    mqtt.subscribe(T_VOLUME);
    mqtt.subscribe(T_MUSIC);
    Serial.println("[MQTT] connected");
  }
}

// ===================== setup / loop =====================
void setup() {
  Serial.begin(115200);
  delay(300);

  ring = xStreamBufferCreate(RING_BYTES, 4);

  spk.setPins(SPK_BCLK, SPK_LRC, SPK_DIN);
  if (!spk.begin(I2S_MODE_STD, RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO))
    Serial.println("[ERR] speaker I2S");
  mic.setPins(MIC_SCK, MIC_WS, -1, MIC_SD);
  if (!mic.begin(I2S_MODE_STD, RATE, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO, I2S_STD_SLOT_LEFT))
    Serial.println("[ERR] mic I2S");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) delay(200);
  Serial.printf("[WiFi] %s IP=%s\n", WiFi.status() == WL_CONNECTED ? "ok" : "FAIL",
                WiFi.localIP().toString().c_str());

  udpSpk.begin(SPK_UDP_PORT);
  mqtt.setServer(PI_IP, MQTT_PORT);
  mqtt.setCallback(onMqtt);

  xTaskCreatePinnedToCore(playerTask, "player", 4096, nullptr, 6, nullptr, 1);
  xTaskCreatePinnedToCore(micTask,    "mic",    8192, nullptr, 5, nullptr, 1);
}

void loop() {
  handleSpeakerUdp();
  mqttReconnect();
  mqtt.loop();
  delay(1);
}
