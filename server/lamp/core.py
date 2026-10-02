"""lamp/core.py - 전등 제어 코어.

Gemini, 앱(REST), 알람 등 모든 입구가 이 파일의 함수만 호출한다.
검증(clamp), MQTT publish, 상태/알람 저장이 전부 여기에 있다.

의존성: pip install paho-mqtt apscheduler
"""

import os
import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

# ---------------------------------------------------------------- 설정
BROKER_HOST = "localhost"
BROKER_PORT = 1883
TZ = "Asia/Seoul"

TOPIC_POWER = "lamp/power"
TOPIC_COLOR = "lamp/color"
TOPIC_BRIGHTNESS = "lamp/brightness"

# ~/voice-assistant/lamp_state.json
STATE_FILE = Path(__file__).resolve().parent.parent / "lamp_state.json"

# ---------------------------------------------------------------- 내부 상태
_lock = threading.RLock()
_state = {"on": False, "brightness": 100, "color": [255, 255, 255]}
_alarms = []  # {"id","time","action","repeat"}

_REPEAT_TO_DOW = {
    "daily": "*",
    "weekdays": "mon-fri",
    "weekends": "sat,sun",
}


def _load():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            _state.update(data.get("state", {}))
            _alarms.extend(data.get("alarms", []))
        except (json.JSONDecodeError, OSError):
            pass  # 파일이 깨졌으면 기본값으로 시작


def _save():
    with _lock:
        STATE_FILE.write_text(
            json.dumps({"state": _state, "alarms": _alarms}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------- MQTT
MQTT_USER = os.environ["MQTT_USER"]
MQTT_PASS = os.environ["MQTT_PASS"]

try:
    _client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)  # paho-mqtt 2.x
except AttributeError:
    _client = mqtt.Client()  # paho-mqtt 1.x

_client.username_pw_set(MQTT_USER, MQTT_PASS)


def _on_connect(client, userdata, flags, rc, *args):
    print(f"[mqtt] connect result: {rc}")  # 0 / Success 면 정상


_client.on_connect = _on_connect
_client.connect(BROKER_HOST, BROKER_PORT)
_client.loop_start()

def _publish(topic, payload):
    # retain=True: ESP32가 재부팅해도 마지막 상태를 받아서 복구
    info = _client.publish(topic, payload, retain=True)
    print(f"[mqtt] {topic} <- {payload} (rc={info.rc})")

# ---------------------------------------------------------------- 제어 함수
def set_power(on: bool) -> dict:
    """전등을 켜거나 끈다. on=True면 켜기, False면 끄기."""
    with _lock:
        _state["on"] = bool(on)
        _publish(TOPIC_POWER, "on" if on else "off")
        _save()
        return {"ok": True, "state": dict(_state)}


def set_brightness(level: int) -> dict:
    """밝기를 0~100 (퍼센트)로 설정한다."""
    with _lock:
        _state["brightness"] = max(0, min(100, int(level)))
        _publish(TOPIC_BRIGHTNESS, str(_state["brightness"]))
        _save()
        return {"ok": True, "state": dict(_state)}


def set_color(r: int, g: int, b: int) -> dict:
    """전등 색을 RGB(각 0~255)로 설정한다."""
    with _lock:
        _state["color"] = [max(0, min(255, int(v))) for v in (r, g, b)]
        _publish(TOPIC_COLOR, json.dumps(_state["color"]))
        _save()
        return {"ok": True, "state": dict(_state)}


def get_state() -> dict:
    """현재 전등 상태(켜짐 여부, 밝기, 색)를 반환한다."""
    with _lock:
        return dict(_state)


# ---------------------------------------------------------------- 알람
_scheduler = BackgroundScheduler(timezone=TZ)


def _parse_time(t: str):
    h, m = t.strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError
    return h, m


def _fire(alarm_id: str):
    with _lock:
        alarm = next((a for a in _alarms if a["id"] == alarm_id), None)
        if alarm is None:
            return
        set_power(alarm["action"] == "on")
        if alarm["repeat"] == "once":
            _alarms.remove(alarm)
            _save()


def _schedule(alarm: dict):
    h, m = _parse_time(alarm["time"])
    if alarm["repeat"] == "once":
        now = datetime.now(ZoneInfo(TZ))
        run = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if run <= now:
            run += timedelta(days=1)
        trigger = DateTrigger(run_date=run)
    else:
        trigger = CronTrigger(
            hour=h, minute=m, day_of_week=_REPEAT_TO_DOW[alarm["repeat"]], timezone=TZ
        )
    _scheduler.add_job(_fire, trigger, args=[alarm["id"]], id=alarm["id"], replace_existing=True)


def add_alarm(time: str, action: str, repeat: str = "daily") -> dict:
    """알람을 등록한다.

    time: 24시간제 "HH:MM" (예: "07:30", 오후 9시는 "21:00")
    action: "on"(전등 켜기) 또는 "off"(전등 끄기)
    repeat: "once"(다음 한 번만), "daily"(매일), "weekdays"(평일), "weekends"(주말)
    """
    if action not in ("on", "off"):
        return {"ok": False, "error": "action은 'on' 또는 'off'여야 합니다."}
    if repeat not in ("once", *_REPEAT_TO_DOW):
        return {"ok": False, "error": "repeat은 once/daily/weekdays/weekends 중 하나여야 합니다."}
    try:
        h, m = _parse_time(time)
    except (ValueError, AttributeError):
        return {"ok": False, "error": "time은 'HH:MM' 형식(24시간제)이어야 합니다."}

    alarm = {"id": uuid.uuid4().hex[:8], "time": f"{h:02d}:{m:02d}", "action": action, "repeat": repeat}
    with _lock:
        _alarms.append(alarm)
        _schedule(alarm)
        _save()
    return {"ok": True, "alarm": alarm}


def list_alarms() -> dict:
    """등록된 알람 목록을 반환한다."""
    with _lock:
        return {"alarms": [dict(a) for a in _alarms]}


def remove_alarm(alarm_id: str) -> dict:
    """알람 id로 알람을 삭제한다. id는 list_alarms 결과에서 확인한다."""
    with _lock:
        alarm = next((a for a in _alarms if a["id"] == alarm_id), None)
        if alarm is None:
            return {"ok": False, "error": "해당 id의 알람이 없습니다."}
        _alarms.remove(alarm)
        try:
            _scheduler.remove_job(alarm_id)
        except Exception:
            pass
        _save()
    return {"ok": True}


# ---------------------------------------------------------------- 시작 시 복구
_load()
_scheduler.start()
for _a in list(_alarms):
    try:
        _schedule(_a)
    except Exception:
        _alarms.remove(_a)
