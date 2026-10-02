"""lamp/gemini_tools.py - Gemini가 core 함수를 호출하게 연결하는 얇은 층.

의존성: pip install google-genai
환경변수: GEMINI_API_KEY (모델을 바꾸려면 GEMINI_MODEL)

google-genai SDK는 파이썬 함수를 tools에 넘기면
타입힌트/docstring으로 함수 선언을 만들고, Gemini가 호출을 요청하면
실제 함수를 자동 실행한 뒤 결과를 다시 Gemini에 넘겨 최종 답변을 만든다.
"""
import os

from google import genai
from google.genai import types

from . import core

# 기존 main.py에서 쓰는 모델명으로 바꿔주세요.
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

TOOLS = [
    core.set_power,
    core.set_brightness,
    core.set_color,
    core.get_state,
    core.add_alarm,
    core.list_alarms,
    core.remove_alarm,
]

SYSTEM = """너는 사용자의 방 전등을 제어하는 비서다. 반드시 제공된 함수로 전등을 제어한다.
- 색 이름(주황, 따뜻한 색, 차분한 파랑 등)은 적절한 RGB 값으로 직접 변환해서 set_color를 호출한다.
- 꺼져 있는 상태에서 색/밝기 변경을 요청받으면 set_power(True)도 함께 호출한다.
- "좀 더 밝게/어둡게" 같은 상대적 요청은 get_state로 현재 밝기를 확인한 뒤 조절한다.
- 알람 시간은 24시간제 "HH:MM"으로 변환한다. 알람 삭제는 list_alarms로 id를 찾은 뒤 remove_alarm을 호출한다.
- 함수가 ok=False를 반환하면 이유를 사용자에게 짧게 알려준다.
- 답변은 한두 문장으로 짧게, 한국어로 한다."""

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def ask(text: str) -> str:
    """텍스트 명령을 Gemini에 보내고, 필요한 전등 함수를 실행한 뒤 최종 답변 문자열을 반환."""
    resp = _client.models.generate_content(
        model=MODEL,
        contents=text,
        config=types.GenerateContentConfig(system_instruction=SYSTEM, tools=TOOLS),
    )
    return resp.text


if __name__ == "__main__":
    # 텍스트로 먼저 테스트: ~/voice-assistant 에서 `python -m lamp.gemini_tools`
    print("전등 명령을 입력하세요 (종료: Ctrl+C)")
    while True:
        print("→", ask(input("> ")))
