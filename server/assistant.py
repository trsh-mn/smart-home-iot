import os, json
from datetime import datetime
from zoneinfo import ZoneInfo
from google import genai
from google.genai import types
from audio_client import speak, play_file, stop_music, is_music_playing
import paho.mqtt.client as mqtt
import threading
from mic_receiver import mic_udp_listener, audio_queue
import sys
 
from lamp import core  # 전등 제어 코어 (MQTT_USER / MQTT_PASS 환경변수 필요)
 
 
client = genai.Client()
 
MUSIC_DIR = os.path.expanduser("~/voice-assistant/music")
 
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_STATE = "device/state"   # 비서 상태 (idle / thinking / speaking)
MQTT_TOPIC_MUSIC = "music/state"    # 음악 상태 (playing / stopped) → 대화용 보드가 역치 조절
 
mqtt_client = mqtt.Client()
# 브로커가 allow_anonymous false 이므로 계정 인증 필요
mqtt_client.username_pw_set(os.environ["MQTT_USER"], os.environ["MQTT_PASS"])
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
mqtt_client.loop_start()
 
current_song = None
 
 
def set_state(state: str):
    mqtt_client.publish(MQTT_TOPIC_STATE, state)
    print(f"[state] {state}")
 
 
def set_music_state(state: str):
    # retain=True: 보드가 재접속해도 현재 음악 상태를 바로 받음
    mqtt_client.publish(MQTT_TOPIC_MUSIC, state, retain=True)
    print(f"[music] {state}")
 
 
# Gemini가 lamp 액션에서 호출할 수 있는 함수 (화이트리스트)
LAMP_FUNCS = {
    f.__name__: f
    for f in (
        core.set_power,
        core.set_brightness,
        core.set_color,
        core.add_alarm,
        core.remove_alarm,
    )
}
 
 
def run_lamp_calls(calls):
    results = []
    for c in calls:
        fn = LAMP_FUNCS.get(c.get("fn"))
        if fn is None:
            results.append({"ok": False, "error": f"알 수 없는 기능: {c.get('fn')}"})
            continue
        try:
            results.append(fn(**c.get("args", {})))
        except Exception as e:
            results.append({"ok": False, "error": str(e)})
    return results
 
 
def build_system_prompt():
    songs = os.listdir(MUSIC_DIR) if os.path.isdir(MUSIC_DIR) else []
    music_now = f"재생 중 ({current_song})" if is_music_playing() else "재생 안 함"
 
    now = datetime.now(ZoneInfo(core.TZ)).strftime("%Y-%m-%d %H:%M")
    st = core.get_state()
    power = "켜짐" if st["on"] else "꺼짐"
    alarms = core.list_alarms()["alarms"]
    alarm_text = (
        ", ".join(f"{a['id']}: {a['time']} {a['action']} ({a['repeat']})" for a in alarms)
        if alarms
        else "없음"
    )
 
    return f"""너는 음성 비서야. 사용자 입력에 대해 반드시 아래 JSON 형식 중 하나로만 답해.
 
{{"action": "speak", "text": "..."}}
{{"action": "play_music", "song": "파일명.mp3"}}
{{"action": "stop_music", "text": "멈춘 뒤 말해줄 짧은 한 문장 (생략 가능)"}}
{{"action": "lamp", "calls": [{{"fn": "함수이름", "args": {{...}}}}, ...], "text": "실행 후 말해줄 짧은 한 문장"}}
{{"action": "none"}}
 
사용 가능한 노래 목록: {", ".join(songs) if songs else "없음"}
현재 음악: {music_now}
 
음악 규칙:
- 음악을 끄거나 멈춰 달라는 요청은 stop_music으로 답한다.
- 음악 재생 중에 다른 곡을 요청하면 play_music으로 답한다 (이전 곡은 자동으로 멈춘다).
 
[전등 제어]
현재 시각: {now} ({core.TZ})
현재 전등 상태: {power}, 밝기 {st["brightness"]}%, 색 RGB {st["color"]}
등록된 알람: {alarm_text}
 
lamp의 calls는 순서대로 실행되고, 사용할 수 있는 함수는 다음과 같아.
- set_power {{"on": true 또는 false}}
- set_brightness {{"level": 0~100}}
- set_color {{"r": 0~255, "g": 0~255, "b": 0~255}}
- add_alarm {{"time": "HH:MM"(24시간제), "action": "on" 또는 "off", "repeat": "once" | "daily" | "weekdays" | "weekends"}}
- remove_alarm {{"alarm_id": "위 알람 목록의 id"}}
 
전등 규칙:
- 색 이름(주황, 따뜻한 색, 차분한 파랑 등)은 적절한 RGB 값으로 직접 변환한다.
- 꺼져 있는 상태에서 색/밝기 변경을 요청받으면 set_power true도 함께 넣는다.
- "좀 더 밝게/어둡게" 같은 상대적 요청은 현재 밝기를 기준으로 계산한다.
- "10분 뒤" 같은 상대 시간은 현재 시각으로 계산해 HH:MM으로 바꾸고 repeat는 "once"로 한다.
- 전등 상태나 알람을 묻는 질문은 위 정보를 보고 speak로 답한다.
 
[공통 규칙]
- 오디오에 비서에게 하는 사람의 말이 없고 음악, 노래 가사, TV 소리, 잡음만 있으면 반드시 none으로 답한다.
- text는 음성으로 읽히므로 짧고 자연스러운 한국어 한 문장으로 쓴다.
"""
 
 
def say(text):
    set_state("speaking")
    speak(text)
    set_state("idle")
 
 
def start_music(song, path):
    global current_song
 
    def on_start():
        set_music_state("playing")
 
    def on_done():
        set_music_state("stopped")
 
    current_song = song
    play_file(path, on_start=on_start, on_done=on_done)
 
 
def process_response(response):
    """Gemini 응답을 파싱하고 액션을 실행 — text/audio 입력 둘 다 여기서 공유"""
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError:
        print("파싱 실패:", response.text)
        set_state("idle")
        return
 
    action = data.get("action")
 
    if action == "speak":
        say(data.get("text", ""))
 
    elif action == "play_music":
        song = data.get("song", "")
        path = os.path.join(MUSIC_DIR, song)
        if os.path.exists(path):
            start_music(song, path)   # 백그라운드 재생, 바로 반환
            set_state("idle")
        else:
            say(f"{song} 파일을 못 찾았어요")
 
    elif action == "stop_music":
        stop_music()
        if data.get("text"):
            say(data["text"])
        else:
            set_state("idle")
 
    elif action == "lamp":
        results = run_lamp_calls(data.get("calls", []))
        failed = [r for r in results if not r.get("ok")]
        print("[lamp]", data.get("calls"), "->", results)
        if failed:
            say("전등 제어에 실패했어요. " + failed[0].get("error", ""))
        else:
            say(data.get("text", "완료했어요"))
 
    elif action == "none":
        print("[none] 사람 말 없음, 무시")
        set_state("idle")
 
    else:
        print("알 수 없는 액션:", data)
        set_state("idle")
 
 
def handle(user_input: str):
    set_state("thinking")
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=user_input,
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
            response_mime_type="application/json",
        ),
    )
    process_response(response)
 
 
def handle_audio(wav_path: str):
    set_state("thinking")
    with open(wav_path, "rb") as f:
        audio_bytes = f.read()
 
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=[types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")],
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
            response_mime_type="application/json",
        ),
    )
    process_response(response)
 
 
if __name__ == "__main__":
    print("비서 시작 (종료: Ctrl+C)")
    set_state("idle")
    set_music_state("stopped")
 
    text_mode = "--text" in sys.argv
 
    if text_mode:
        try:
            while True:
                text = input("입력: ").strip()
                if text.lower() in ("exit", "quit"):
                    break
                if text:
                    handle(text)
        except KeyboardInterrupt:
            pass
    else:
        threading.Thread(target=mic_udp_listener, daemon=True).start()
        try:
            while True:
                wav_path = audio_queue.get()
                handle_audio(wav_path)
        except KeyboardInterrupt:
            pass
 
    stop_music()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()