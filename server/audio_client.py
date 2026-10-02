import asyncio
import subprocess
import socket
import threading
import time
import audioop
import struct


# ---------- 대화용 보드 (UDP, 16kHz 모노) ----------
VOICE_IP = "XXX.XX.X.XX"   # esp32_voice.ino
VOICE_PORT = 6000
MAGIC = 0xA55A
CHUNK = 1024               # 512샘플 = 32ms
LEAD = 0.2                 # 처음 200ms는 앞당겨 보내서 ESP32 버퍼를 채움
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
VOICE_VOLUME = 1.0         # 0.0(무음) ~ 1.0(원본). 1.0 넘기면 클리핑

# ---------- 음악용 보드 (TCP, 44.1kHz 스테레오) ----------
MUSIC_IP = "XXX.XX.X.XX"   # esp32_music.ino
MUSIC_PORT = 6001
MUSIC_RATE = 44100
MUSIC_CH = 2
MUSIC_VOLUME = 1.0


_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_seq = 0

_music_stop = threading.Event()
_music_thread = None


# ===================== 대화 =====================
async def _tts_to_file(text, voice, out_path):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate="-5%", pitch="+0Hz")
    await communicate.save(out_path)


def speak(text, voice="ko-KR-SunHiNeural"):
    """대화 응답 → 대화용 보드 (끝날 때까지 기다림)"""
    asyncio.run(_tts_to_file(text, voice, "_tts.mp3"))
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", "_tts.mp3", "-ar", str(SAMPLE_RATE),
         "-ac", "1", "-f", "s16le", "_tts.raw"],
        check=True, capture_output=True, stdin=subprocess.DEVNULL
    )
    _stream_voice("_tts.raw")


def _stream_voice(raw_path):
    global _seq
    sent_sec = 0.0
    start = time.monotonic()
    with open(raw_path, "rb") as f:
        while chunk := f.read(CHUNK):
            if VOICE_VOLUME != 1.0:
                chunk = audioop.mul(chunk, BYTES_PER_SAMPLE, VOICE_VOLUME)
            _udp.sendto(struct.pack("<HH", MAGIC, _seq) + chunk, (VOICE_IP, VOICE_PORT))
            _seq = (_seq + 1) & 0xFFFF
            sent_sec += len(chunk) / BYTES_PER_SAMPLE / SAMPLE_RATE
            wait = start + sent_sec - LEAD - time.monotonic()
            if wait > 0:
                time.sleep(wait)
    _udp.sendto(b"END", (VOICE_IP, VOICE_PORT))


# ===================== 음악 =====================
def _header(rate, ch):
    return struct.pack("<4sIHH", b"AUD1", rate, ch, 0)


def _stream_music(path, stop_event, on_start):
    """ffmpeg 출력을 그대로 TCP로 흘려보냄. 속도 조절은 TCP가 알아서 함."""
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-ar", str(MUSIC_RATE),
         "-ac", str(MUSIC_CH), "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL
    )
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)   # 정지할 때 쌓인 데이터가 적도록
    s.settimeout(5)
    stopped = False
    try:
        s.connect((MUSIC_IP, MUSIC_PORT))
        s.sendall(_header(MUSIC_RATE, MUSIC_CH))
        if on_start:
            on_start()
        while chunk := proc.stdout.read(4096):
            if stop_event.is_set():
                stopped = True
                break
            if MUSIC_VOLUME != 1.0:
                chunk = audioop.mul(chunk, BYTES_PER_SAMPLE, MUSIC_VOLUME)
            s.sendall(chunk)
    finally:
        proc.kill()
        proc.wait()
        if stopped:
            # 남은 전송 데이터를 버리고 즉시 끊음 (RST)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
    if not stopped:
        time.sleep(0.6)   # ESP32 버퍼에 남은 끝부분이 재생될 시간


def play_file(path, on_start=None, on_done=None):
    """노래 → 음악용 보드. 백그라운드로 재생하고 바로 반환.
    이미 재생 중이면 그 곡을 멈추고 새 곡을 재생."""
    global _music_thread
    _stop_thread()
    _music_stop.clear()

    def run():
        try:
            _stream_music(path, _music_stop, on_start)
        except Exception as e:
            print("[music] 재생 실패:", e)
        finally:
            if on_done:
                on_done()

    _music_thread = threading.Thread(target=run, daemon=True)
    _music_thread.start()


def _stop_thread():
    if _music_thread and _music_thread.is_alive():
        _music_stop.set()
        _music_thread.join()


def stop_music():
    """재생 중인 노래를 멈추고, 보드 버퍼에 남은 소리도 바로 비움"""
    _stop_thread()
    try:
        with socket.create_connection((MUSIC_IP, MUSIC_PORT), timeout=2) as s:
            s.sendall(_header(0, 0))
    except OSError:
        pass


def is_music_playing():
    return _music_thread is not None and _music_thread.is_alive()
