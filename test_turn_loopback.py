"""End-to-end loopback test: Piper TTS audio -> WS voice server -> Whisper STT -> Hermes turn.

Proves the 'stop listening and send text to the model' path works:
  start -> PCM chunks -> stop -> transcript event -> agent runs -> done event.
"""
import json
import sys
import time
import wave
import io
import os

import numpy as np
import websocket  # websocket-client

SAMPLE_RATE = 16000
CACHE = os.path.expanduser("~/.cache/piper")
MODEL = f"{CACHE}/tr_TR_99eren99.onnx"
CONFIG = f"{CACHE}/tr_TR_99eren99.json"
WS_URL = "ws://127.0.0.1:8765/ws"

# 1) Synthesize a Turkish command with the installed piper voice
import piper
voice = piper.PiperVoice.load(MODEL, config_path=CONFIG)
buf = io.BytesIO()
with wave.open(buf, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(22050)
    voice.synthesize_wav("Merhaba, hava nasıl bugün?", w)
buf.seek(0)
with wave.open(buf) as w:
    sr = w.getframerate()
    frames = w.readframes(w.getnframes())
pcm22 = np.frombuffer(frames, dtype=np.int16)

# 2) Resample 22050 -> 16000
if sr != SAMPLE_RATE:
    dur = len(pcm22) / sr
    n_out = int(dur * SAMPLE_RATE)
    x_old = np.linspace(0, dur, len(pcm22), endpoint=False)
    x_new = np.linspace(0, dur, n_out, endpoint=False)
    pcm16 = np.interp(x_new, x_old, pcm22.astype(np.float64)).astype(np.int16)
else:
    pcm16 = pcm22
print(f"[test] generated {len(pcm16)/SAMPLE_RATE:.1f}s of Turkish speech ({sr}Hz -> {SAMPLE_RATE}Hz)")

# 3) Feed it through the voice server exactly like the Qt client does
ws = websocket.WebSocket()
ws.connect(WS_URL, timeout=10, suppress_origin=True)
print("[test] connected")

def wait_for(pred, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ws.settimeout(2)
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        if isinstance(raw, bytes):
            ev = {"type": "_audio", "bytes": len(raw)}
            print(f"[test] <- audio chunk ({len(raw)} bytes)")
        else:
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            t = ev.get("type")
            if t in ("partial_transcript", "transcript", "agent_status", "done",
                     "error", "status", "run_started", "approval_request",
                     "sentence_start", "sentence_end", "live", "usage"):
                print(f"[test] <- {t}: " + json.dumps(
                    {k: v for k, v in ev.items() if k != 'audio'}, ensure_ascii=False)[:220])
            else:
                print(f"[test] <- {t}")
        if pred(ev):
            return ev
    return None

ws.send(json.dumps({"type": "start", "sample_rate": SAMPLE_RATE,
                    "format": "pcm_s16le", "channels": 1, "conversation": "jarvis-main"}))
CHUNK = 3200  # 100 ms
for i in range(0, len(pcm16), CHUNK):
    ws.send_binary(pcm16[i:i+CHUNK].tobytes())  # binary opcode, like the Qt client
    time.sleep(0.02)
ws.send(json.dumps({"type": "stop"}))
print("[test] audio sent, stop sent")

done = wait_for(lambda ev: ev.get("type") in ("done", "error"), timeout=240)
if done and done.get("type") == "done":
    print("[PASS] full turn completed: STT -> Hermes -> TTS -> done")
    sys.exit(0)
print(f"[FAIL] turn ended with: {done}")
sys.exit(1)
