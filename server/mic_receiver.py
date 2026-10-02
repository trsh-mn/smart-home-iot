import socket
import threading
import wave
import time
import queue

MIC_UDP_PORT = 5005
SAMPLE_RATE = 16000

audio_queue = queue.Queue()   # 완성된 WAV 경로를 메인 루프에 전달

def mic_udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", MIC_UDP_PORT))
    print(f"[mic] UDP 리스너 시작 (포트 {MIC_UDP_PORT})")

    buffer = bytearray()
    recording = False

    while True:
        data, addr = sock.recvfrom(4096)

        if data == b"START":
            buffer = bytearray()
            recording = True
            print("[mic] 녹음 시작 감지")

        elif data == b"END":
            if recording and len(buffer) > 0:
                filename = f"/tmp/mic_input_{int(time.time())}.wav"
                with wave.open(filename, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)   # 16비트
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(bytes(buffer))
                print(f"[mic] 저장 완료: {filename} ({len(buffer)} bytes)")
                audio_queue.put(filename)
            recording = False

        elif recording:
            buffer.extend(data)

