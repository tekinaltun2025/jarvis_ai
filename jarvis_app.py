#!/usr/bin/env python3
"""JARVIS — native Ubuntu desktop app (PySide6).

Replaces the web HUD: same voice-server protocol (WS start/stop + PCM chunks),
native Qt window, microphone via sounddevice (PipeWire-native, no browser).
Turkish UI. Piper TTS output plays locally through the default sink.
"""
import json
import os
import sys
import time
import queue
import struct

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QPointF, QRectF  # noqa: F401
from PySide6.QtGui import (QColor, QFont, QPainter, QPen, QRadialGradient, QBrush,
                           QPolygonF)
from PySide6.QtWidgets import (
    QApplication, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QScrollArea, QVBoxLayout, QWidget, QFrame, QSizePolicy,
)

import numpy as np
import sounddevice as sd
import websocket  # websocket-client (sync, simple)

try:
    from particle_globe import ParticleGlobe as _ParticleGlobe
    _GLOBE_OK = True
except Exception:
    _ParticleGlobe = None
    _GLOBE_OK = False
try:
    from setup_wizard import SetupWizard
    _WIZARD_OK = True
except Exception:
    _WIZARD_OK = False

# ---------------------------------------------------------------- config
SERVER_WS = os.environ.get("JARVIS_WS", "ws://127.0.0.1:8765/ws")
HERMES_API = os.environ.get("JARVIS_API", "http://127.0.0.1:8642")
CONV = "jarvis-main"
SAMPLE_RATE = 16000
CHUNK_FRAMES = 1280  # 80 ms

# --- always-listening / wake word ---
# wake word: "bilgisayar" (+ whisper'ın ürettiği yazım varyantları)
WAKE_WORDS = ("bilgisayar", "bilgi sayar", "bilgisayr", "bilgisayr", "birgisayar",
              "bilgisayara", "bilgisayarı", "bilgisayarı", "bilgisayarlar",
              "bilgi sayara", "bilgisaayar", "bilgisayarlar", "pilgisayar",
              "bilgisayor", "bilgisayir")
WAKE_WORDS_EXACT = {"bilgisayar", "bilgi sayar", "birgisayar", "bilgisayara",
                    "bilgisayarı", "bilgisayarı", "pilgisayar", "bilgisayor",
                    "bilgisayir", "bilgisaayar"}
WAKE_ENERGY_GATE = 300        # RMS int16 below this = silence, skip everything
WAKE_VAD_THRESHOLD = 0.5      # silero speech probability
WAKE_WINDOW_SEC = 5.0         # rolling window fed to whisper
WAKE_STRIDE_SEC = 1.5         # transcribe at most every N s while speech present
WAKE_MATCH_PREFIX = 40        # chars of transcript prefix scanned for the wake word
WAKE_COOLDOWN_SEC = 1.5       # don't retrigger immediately after a wake
SILENCE_END_SEC = 1.2         # end-of-speech silence for the command window
COMMAND_MAX_SEC = 12.0        # hard cap on one spoken command

STATE_STYLE = {
    "standby":   {"speed": 0.15, "color": "#00e5ff", "glow": 18, "pulse": 0.0},
    "listening": {"speed": 0.50, "color": "#19f0d8", "glow": 30, "pulse": 1.0},
    "thinking":  {"speed": 1.60, "color": "#ffb300", "glow": 26, "pulse": 0.4},
    "tool":      {"speed": 2.40, "color": "#ffb300", "glow": 32, "pulse": 0.6},
    "speaking":  {"speed": 0.70, "color": "#00e5ff", "glow": 34, "pulse": 0.8},
    "error":     {"speed": 0.05, "color": "#ff4d5e", "glow": 22, "pulse": 0.0},
}
STATE_TEXT = {
    "standby": "BEKLEMEDE", "listening": "DİNLİYOR", "thinking": "İŞLENİYOR",
    "tool": "ÇALIŞIYOR", "speaking": "KONUŞUYOR", "error": "HATA",
}


def hud_token() -> str:
    env = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env):
        with open(env) as f:
            for line in f:
                if line.startswith("JARVIS_HUD_TOKEN="):
                    return line.split("=", 1)[1].strip()
    return ""


# ---------------------------------------------------------------- hud décor
class HudBackground(QWidget):
    """Full-window Stark-style HUD décor painted UNDER the layout:
    circuit traces, corner brackets, hex dots, scanlines, side rails."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, False)

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        t = time.time()

        # scanlines
        p.setPen(QPen(QColor(0, 229, 255, 7), 1))
        for y in range(0, h, 4):
            p.drawLine(0, y, w, y)

        cyan = QColor(0, 229, 255)
        dim = QColor(0, 229, 255, 38)

        # corner brackets
        p.setPen(QPen(cyan, 2))
        L = 26; M = 14
        for (x0, y0, dx, dy) in ((M, M, 1, 1), (w - M, M, -1, 1),
                                 (M, h - M, 1, -1), (w - M, h - M, -1, -1)):
            p.drawLine(x0, y0, x0 + dx * L, y0)
            p.drawLine(x0, y0, x0, y0 + dy * L)

        # side rails (vertical lines with nodes) — like the reference's outer rails
        p.setPen(QPen(dim, 2))
        for x in (M + 2, w - M - 2):
            p.drawLine(x, h * 0.18, x, h * 0.82)
        p.setPen(QPen(cyan, 2))
        for x in (M + 2, w - M - 2):
            for fy in (0.18, 0.5, 0.82):
                p.drawEllipse(QPointF(x, h * fy), 3, 3)

        # circuit traces: a few fixed polylines with node dots + moving pulse
        pts_sets = [
            [(w*0.06, h*0.30), (w*0.14, h*0.30), (w*0.17, h*0.36), (w*0.24, h*0.36)],
            [(w*0.94, h*0.26), (w*0.86, h*0.26), (w*0.83, h*0.33), (w*0.76, h*0.33)],
            [(w*0.06, h*0.70), (w*0.15, h*0.70), (w*0.18, h*0.64), (w*0.26, h*0.64)],
            [(w*0.94, h*0.74), (w*0.85, h*0.74), (w*0.82, h*0.67), (w*0.74, h*0.67)],
        ]
        p.setPen(QPen(dim, 1))
        for pts in pts_sets:
            prev = pts[0]
            for pt in pts[1:]:
                p.drawLine(int(prev[0]), int(prev[1]), int(pt[0]), int(pt[1]))
                prev = pt
            p.setBrush(QBrush(dim)); p.setPen(QPen(Qt.NoPen))
            p.drawEllipse(QPointF(pts[-1][0], pts[-1][1]), 2.5, 2.5)
            p.setPen(QPen(dim, 1))
        # travelling pulse dots along the first two traces
        for i, pts in enumerate(pts_sets[:2]):
            ph = (t * 0.22 + i * 0.5) % 1.0
            seg = int(ph * (len(pts) - 1)); frac = ph * (len(pts) - 1) - seg
            (x1, y1), (x2, y2) = pts[seg], pts[min(seg + 1, len(pts) - 1)]
            px, py = x1 + (x2 - x1) * frac, y1 + (y2 - y1) * frac
            pc = QColor(0, 229, 255, 170)
            p.setBrush(QBrush(pc)); p.setPen(QPen(Qt.NoPen))
            p.drawEllipse(QPointF(px, py), 2.4, 2.4)
            p.setPen(QPen(QColor(0, 229, 255, 40), 1))

        # scattered hex particles (static positions, twinkle by alpha)
        rng = np.random.RandomState(7)
        for _ in range(26):
            x, y = rng.uniform(w*0.05, w*0.95), rng.uniform(h*0.08, h*0.92)
            a = 25 + 25 * np.sin(t*2 + x)
            c = QColor(0, 229, 255, max(0, int(a)))
            p.setBrush(QBrush(c)); p.setPen(QPen(Qt.NoPen))
            p.drawEllipse(QPointF(x, y), 1.3, 1.3)
        p.end()


class GaugeWidget(QWidget):
    """Circular gauge like the reference: dial arc + big centered label."""
    def __init__(self, label: str, size: int = 116, parent=None):
        super().__init__(parent)
        self.label = label
        self.value_text = "--"
        self.sub_text = ""
        self._frac = 0.35
        self.setFixedSize(size, size)

    def set_value(self, text: str, frac: float, sub: str = ""):
        self.value_text = text
        self._frac = max(0.0, min(1.0, frac))
        self.sub_text = sub
        self.update()

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width(); cx = w/2; cy = h = self.height()/2
        r = w/2 - 6
        cyan = QColor(0, 229, 255)

        # back arc
        back = QColor(0, 229, 255, 45)
        p.setPen(QPen(back, 5)); p.setBrush(QBrush())
        p.drawArc(QRectF(cx-r, cy-r, 2*r, 2*r), 30*16, 300*16)
        # value arc
        val = QColor(0, 229, 255)
        p.setPen(QPen(val, 5))
        span = int(-300 * 16 * self._frac)
        p.drawArc(QRectF(cx-r, cy-r, 2*r, 2*r), 120*16, span)
        # inner thin ring
        p.setPen(QPen(QColor(0, 229, 255, 70), 1))
        p.drawEllipse(QPointF(cx, cy), r*0.72, r*0.72)

        # texts
        p.setPen(QPen(cyan))
        f = QFont("DejaVu Sans", int(w*0.16), QFont.Bold)
        f.setLetterSpacing(QFont.PercentageSpacing, 100)
        p.setFont(f)
        p.drawText(QRectF(0, cy - r*0.34, w, r*0.5), Qt.AlignCenter, self.value_text)
        p.setFont(QFont("DejaVu Sans", 8))
        p.setPen(QPen(QColor(159, 216, 232, 200)))
        p.drawText(QRectF(0, cy + r*0.10, w, 18), Qt.AlignCenter, self.label)
        if self.sub_text:
            p.setPen(QPen(QColor(77, 125, 146)))
            p.drawText(QRectF(0, cy + r*0.34, w, 16), Qt.AlignCenter, self.sub_text)
        p.end()


# ---------------------------------------------------------------- reactor
class ReactorWidget(QWidget):
    """Compact, symmetric arc-reactor widget.

    Idle: thin rotating rings + tick marks.
    Listening/speaking: the ring itself undulates like a sound wave, driven by
    live audio samples (mic while listening, playback while speaking), mirrored
    for perfect left-right symmetry.
    """

    HALF = 64  # waveform bins; mirrored → symmetric ring

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "standby"
        self.level = 0.0
        self.rot = 0.0
        self._fps = 60
        self._env = np.zeros(self.HALF, dtype=np.float32)  # per-bin envelope
        self._amp = 0.0                                    # eased wave amplitude
        self.setFixedSize(300, 300)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._timer.start(16)  # ~60 fps

    def set_active_fps(self, active: bool):
        """60 fps while listening/speaking, 24 fps idle (CPU saver)."""
        new = 16 if active else 42
        if new != self._fps:
            self._fps = new
            self._timer.setInterval(new)

    def set_state(self, state: str):
        self.state = state if state in STATE_STYLE else "standby"
        self.update()

    def set_level(self, level: float):
        self.level = max(0.0, min(1.0, level))

    def set_wave(self, wave):
        """Feed a snapshot of audio samples (float32, -1..1)."""
        if wave is None or not len(wave):
            return
        w = np.asarray(wave, dtype=np.float32)
        idx = np.linspace(0, len(w) - 1, self.HALF)
        mag = np.interp(idx, np.arange(len(w)), np.abs(w))
        # fast attack, slow decay → lively but smooth undulation
        self._env = np.maximum(self._env * 0.72, mag)
        self.update()

    def tick(self):
        s = STATE_STYLE[self.state]
        self.rot += s["speed"]
        target = 0.14 if self.state in ("listening", "speaking") else 0.0
        self._amp += (target - self._amp) * 0.18   # ease in/out of wave mode
        self._env *= 0.90                          # gentle inter-frame falloff
        self.set_active_fps(self.state in ("listening", "speaking", "thinking", "tool"))
        if self.isVisible() and not self.window().isMinimized():
            self.update()

    def paintEvent(self, ev):  # noqa: N802
        s = STATE_STYLE[self.state]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        base = min(w, h) * 0.34
        color = QColor(s["color"])

        # core glow (breathes with level + wave)
        glow_r = base * (0.46 + 0.10 * self.level + 0.08 * self._amp)
        grad = QRadialGradient(cx, cy, glow_r, cx, cy)
        c0 = QColor(color); c0.setAlpha(95)
        grad.setColorAt(0, c0)
        grad.setColorAt(1, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(grad)); p.setPen(QPen(Qt.NoPen))
        p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        # outer faint boundary ring — the "widget glass"
        oc = QColor(color); oc.setAlphaF(0.10)
        p.setPen(QPen(oc, 1)); p.setBrush(QBrush())
        p.drawEllipse(QPointF(cx, cy), base * 1.36, base * 1.36)

        # --- waveform ring (undulates with audio) or idle rings ---
        if self._amp > 0.004:
            env = self._env
            N = self.HALF * 2
            outer, inner = [], []
            for i in range(N):
                ang = -np.pi / 2 + i / N * 2 * np.pi
                k = i if i < self.HALF else N - 1 - i      # mirror → symmetric
                m = float(env[k]) * self._amp
                ro = base * (1.02 + m * 3.0)
                ri = base * 0.78 * (1.0 + m * 2.0)
                cosa, sina = np.cos(ang), np.sin(ang)
                outer.append(QPointF(cx + cosa * ro, cy + sina * ro))
                inner.append(QPointF(cx + cosa * ri, cy + sina * ri))
            pc = QColor(color); pc.setAlphaF(0.95)
            p.setPen(QPen(pc, 2.2)); p.setBrush(QBrush())
            p.drawPolygon(QPolygonF(outer))
            pc2 = QColor(color); pc2.setAlphaF(0.35)
            p.setPen(QPen(pc2, 1.2))
            p.drawPolygon(QPolygonF(inner))
        else:
            def ring(radius, width, alpha, dash=None, offset=0.0):
                pen = QPen(color, width)
                c = QColor(color); c.setAlphaF(alpha)
                pen.setColor(c)
                if dash:
                    pen.setDashPattern(dash)
                    pen.setDashOffset(offset)
                p.setPen(pen); p.setBrush(QBrush())
                p.drawEllipse(QPointF(cx, cy), radius, radius)

            ring(base, 2, 0.9)
            ring(base * .84, 1.2, 0.4, dash=[3.0, 1.4], offset=self.rot * 2)
            ring(base * .66, 3.5, 0.22, dash=[0.3, 0.8], offset=-self.rot * 3)

        # tick marks (always, subtle)
        p.setPen(QPen(Qt.NoPen))
        for i in range(60):
            a = (i / 60) * 2 * np.pi + self.rot / 180
            r1, r2 = base * 1.10, base * (1.16 if i % 5 == 0 else 1.13)
            c = QColor(color); c.setAlphaF(0.7 if i % 5 == 0 else 0.25)
            p.setBrush(QBrush(c))
            x1, y1 = cx + np.cos(a) * r1, cy + np.sin(a) * r1
            x2, y2 = cx + np.cos(a) * r2, cy + np.sin(a) * r2
            p.drawLine(int(x1), int(y1), int(x2), int(y2))

        # center dot
        dot_r = 3.5 + 5.0 * self.level
        dc = QColor(color); dc.setAlphaF(0.9)
        p.setBrush(QBrush(dc)); p.setPen(QPen(Qt.NoPen))
        p.drawEllipse(QPointF(cx, cy), dot_r, dot_r)
        p.end()


# ---------------------------------------------------------------- mic
class MicThread(QThread):
    """Capture 16 kHz mono int16 from the default source; emit chunks + level."""
    chunk_ready = Signal(bytes)
    level_ready = Signal(float)
    wave_ready = Signal(object)   # float32 snapshot for the reactor waveform
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._capturing = False
        self._stream = None
        self.wake_engine = None   # set by MainWindow after both are created

    def start_capture(self):
        self._capturing = True

    def stop_capture(self):
        self._capturing = False

    def run(self):
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=CHUNK_FRAMES)
            self._stream.start()
            self._running = True
        except Exception as exc:
            self.error.emit(f"mikrofon açılamadı: {exc}")
            return
        wake = self.wake_engine
        while self._running:
            data, overflow = self._stream.read(CHUNK_FRAMES)
            if overflow:
                pass
            arr = np.frombuffer(data, dtype=np.int16)
            if arr.size:
                s = 0
                for i in range(0, arr.size, 8):
                    s += abs(int(arr[i]))
                level = min(1.0, (s / max(1, arr.size // 8)) / 9000)
                self.level_ready.emit(level)
                self.wave_ready.emit(arr.astype(np.float32) / 32768.0)
            if wake is not None:
                wake.feed(arr)
            if self._capturing and arr.size:
                self.chunk_ready.emit(arr.tobytes())
        try:
            self._stream.stop(); self._stream.close()
        except Exception:
            pass

    def stop(self):
        self._running = False
        self.wait(2000)


# ---------------------------------------------------------------- playback
class WakeWordEngine(QThread):
    """Always-listening wake-word detector.

    Stages per 80 ms chunk:
      0. energy gate  — skip all silence (near-zero CPU)
      1. silero VAD   — skip non-speech audio
      2. whisper small(tr) on a rolling 5 s window, at most every 1.5 s
      3. fuzzy match 'jarvis' variants in the transcript prefix
    Emits wake_detected(the tail audio already spoken after the wake word).
    """
    wake_detected = Signal(bytes)   # audio tail (post-wake) to seed the command
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled = True
        self.suppressed = False          # True while JARVIS is talking (echo guard)
        self.last_speech_prob = 0.0      # latest VAD probability (for silence checks)
        self._buf: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=800)
        self._running = True

    def feed(self, arr: np.ndarray):
        if not self.enabled or self.suppressed:
            return
        try:
            self._buf.put_nowait(arr)
        except queue.Full:
            try:
                self._buf.get_nowait()   # drop oldest
                self._buf.put_nowait(arr)
            except Exception:
                pass

    def reset(self):
        """Drop buffered audio (e.g. when a manual capture starts).

        Also resets the VAD probability: while suppressed the run loop gets
        no data and last_speech_prob would freeze at the last spoken value,
        which made _auto_silence_check's vad_quiet condition unreachable and
        the command window hang open until the 12 s hard cap.
        """
        with self._buf.mutex:
            self._buf.queue.clear()
        self.last_speech_prob = 0.0

    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _rms(arr: np.ndarray) -> float:
        if not arr.size:
            return 0.0
        return float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))

    @staticmethod
    def _match_wake(text: str) -> bool:
        t = (text or "").lower().replace("ı", "i").replace("İ", "i")
        import difflib
        # "bilgi sayar" gibi iki kelimeli varyant: alt çizgiye çevirip tek token yap
        joined = " ".join(t.replace(",", " ").replace(".", " ").split())
        for two in ("bilgi sayar", "bilgi sayara", "bilgi sayaro"):
            if two in joined:
                return True
        for token in joined.split():
            if len(token) < 4:
                continue
            if token in WAKE_WORDS_EXACT:
                return True
            # fuzzy: yalnız 'b/p'-başlayan tokenlar (yanlış pozitif engeli)
            for w in WAKE_WORDS:
                if " " in w:
                    continue
                if token[0] in "bp" and difflib.SequenceMatcher(None, token, w).ratio() >= 0.8:
                    return True
        return False

    def _drain_to_array(self) -> np.ndarray:
        parts = []
        while True:
            try:
                parts.append(self._buf.get_nowait())
            except queue.Empty:
                break
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)

    # ---- main loop ------------------------------------------------------
    def run(self):
        from faster_whisper import WhisperModel
        import silero_vad, torch
        model = WhisperModel("small", device="cpu", compute_type="int8")
        vad = silero_vad.load_silero_vad()
        window = np.zeros(0, dtype=np.int16)
        last_transcribe = 0.0
        cooldown = 0.0
        self.status.emit("wake motoru hazır")

        while self._running:
            time.sleep(0.02)
            if cooldown > 0:
                cooldown -= 0.02
            arr = self._drain_to_array()
            if not arr.size:
                continue
            window = np.concatenate([window, arr])
            max_win = int(WAKE_WINDOW_SEC * SAMPLE_RATE)
            if len(window) > max_win:
                window = window[-max_win:]

            if self._rms(arr) < WAKE_ENERGY_GATE or cooldown > 0:
                self.last_speech_prob = 0.0
                continue

            # VAD on the latest chunk (silero needs 512-sample frames @16k)
            try:
                probs = []
                for off in range(0, arr.size - 511, 512):
                    fr = arr[off:off + 512].astype(np.float32) / 32768.0
                    probs.append(vad(torch.from_numpy(fr), SAMPLE_RATE).item())
                prob = max(probs) if probs else 1.0
            except Exception:
                prob = 1.0
            self.last_speech_prob = prob
            if prob < WAKE_VAD_THRESHOLD:
                continue

            # throttle whisper: at most once per WAKE_STRIDE_SEC
            now = time.time()
            if now - last_transcribe < WAKE_STRIDE_SEC:
                continue
            last_transcribe = now

            x = window.astype(np.float32) / 32768.0
            try:
                segs, info = model.transcribe(x, language="tr", beam_size=1,
                                              without_timestamps=True)
                text = " ".join(s.text for s in segs).strip()
            except Exception:
                continue
            if text and self._match_wake(text[:WAKE_MATCH_PREFIX]):
                tail = window[int(-1.2 * SAMPLE_RATE):]  # seed with last 1.2 s
                window = np.zeros(0, dtype=np.int16)
                cooldown = WAKE_COOLDOWN_SEC              # don't retrigger
                self.wake_detected.emit(tail.tobytes())

    def stop(self):
        self._running = False
        self.wait(2000)


# ---------------------------------------------------------------- playback
class PlaybackThread(QThread):
    """Play 16 kHz int16 chunks queued from the WebSocket."""
    finished_playing = Signal()
    wave_ready = Signal(object)   # float32 snapshot of what's being played

    def __init__(self, parent=None):
        super().__init__(parent)
        self._q: "queue.Queue[bytes]" = queue.Queue()
        self._running = True
        self._stream = None

    def enqueue(self, data: bytes):
        self._q.put(data)

    def clear(self):  # barge-in
        with self._q.mutex:
            self._q.queue.clear()

    def run(self):
        try:
            self._stream = sd.OutputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=CHUNK_FRAMES)
            self._stream.start()
        except Exception:
            return
        while self._running:
            try:
                data = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            self.wave_ready.emit(arr.astype(np.float32) / 32768.0)
            self._stream.write(arr)
        try:
            self._stream.stop(); self._stream.close()
        except Exception:
            pass

    def stop(self):
        self._running = False
        self.wait(2000)


# ---------------------------------------------------------------- ws client
class WsClient(QThread):
    """WebSocket to the voice server; JSON events -> Qt signals."""
    connected = Signal()
    disconnected = Signal()
    status_msg = Signal(str)
    transcript = Signal(str)
    partial = Signal(str)
    agent_status = Signal(str)
    run_started = Signal(str)
    done = Signal(dict)
    error_msg = Signal(str)
    approval = Signal(dict)
    audio_chunk = Signal(bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ws = None
        self._running = True
        self._token = hud_token()
        self._outq: "queue.Queue[tuple]" = queue.Queue()  # thread-safe sends

    def send_json(self, obj: dict):
        self._outq.put(("json", json.dumps(obj)))

    def send_audio(self, data: bytes):
        self._outq.put(("bin", data))

    def _flush_out(self, ws):
        """Send queued frames from the WS thread only (concurrent-send safe)."""
        while True:
            try:
                kind, payload = self._outq.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "json":
                    ws.send(payload)
                else:
                    ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                # Re-raise send errors to trigger connection reset and reconnect
                raise RuntimeError(f"Failed to send audio: {e}") from e

    def run(self):
        import websocket
        while self._running:
            try:
                ws = websocket.WebSocket()
                # suppress Origin: the server allows origin-less (native) clients
                # without a token; websocket-client otherwise sends one
                ws.connect(SERVER_WS, timeout=10, suppress_origin=True)
                self._ws = ws
                self.connected.emit()
                ws.settimeout(0.2)
                idle = 0.0
                while self._running:
                    self._flush_out(ws)
                    try:
                        frame = ws.recv()
                        idle = 0.0
                    except websocket.WebSocketTimeoutException:
                        idle += 0.2
                        continue
                    except Exception as e:
                        print(f"WsClient inner loop exception: {e}", flush=True)
                        break
                        self.audio_chunk.emit(bytes(frame))
                        continue
                    try:
                        ev = json.loads(frame)
                    except Exception:
                        continue
                    etype = ev.get("type")
                    if etype == "status":
                        self.status_msg.emit(ev.get("message", ""))
                    elif etype == "transcript":
                        self.transcript.emit(ev.get("text", ""))
                    elif etype == "partial_transcript":
                        self.partial.emit(ev.get("text", ""))
                    elif etype == "run_started":
                        self.run_started.emit(ev.get("run_id", ""))
                    elif etype == "agent_status":
                        self.agent_status.emit(ev.get("state", ""))
                    elif etype == "approval_request":
                        self.approval.emit(ev)
                    elif etype == "error":
                        self.error_msg.emit(ev.get("message", "bilinmeyen hata"))
                    elif etype == "done":
                        self.done.emit(ev.get("timing", {}))
                try:
                    ws.close()
                except Exception:
                    pass
            except Exception:
                pass
            self._ws = None
            self.disconnected.emit()
            for _ in range(30):  # 3 s bekle, çıkabilirse
                if not self._running:
                    return
                time.sleep(0.1)

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self.wait(2000)


# ---------------------------------------------------------------- UI helpers
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("J.A.R.V.I.S — Hermes Sınır Ağı Arabirimi")
        # restore last window geometry
        st = self._load_state()
        geo = st.get("geometry")
        if geo and len(geo) == 4:
            self.move(geo[0], geo[1]); self.resize(geo[2], min(geo[3], 640))
        else:
            self.resize(1180, 640)
        self.state = "standby"
        self.turns = 0
        self.capturing = False
        self.live_msg = None

        central = QWidget(); central.setObjectName("centralWidget")
        self.setCentralWidget(central)

        # Stark-style HUD décor painted under everything
        self.hud_bg = HudBackground(central)

        grid = QGridLayout(central)
        grid.setContentsMargins(26, 20, 26, 16); grid.setSpacing(6)

        # ================= COMPACT SYMMETRIC LAYOUT =================
        # Header: logo | state chips (ws·mic·api) | clock
        header = QWidget()
        hh = QHBoxLayout(header)
        hh.setContentsMargins(4, 0, 4, 0); hh.setSpacing(14)
        logo = QLabel("J.A.R.V.I.S"); logo.setObjectName("logo")
        hh.addWidget(logo)
        hh.addStretch(1)
        self.conn_state = QLabel("BAĞLANTI YOK"); self.conn_state.setObjectName("sub")
        hh.addWidget(self.conn_state)
        self.clock = QLabel("--:--:--"); self.clock.setObjectName("clock")
        hh.addWidget(self.clock)
        grid.addWidget(header, 0, 0, 1, 2)

        # ---- LEFT rail: voice controls + system meters merged ----
        left = QVBoxLayout()
        left.setSpacing(4)
        # status dots — ws & mic
        self.ws_dot = QLabel("●"); self.ws_dot.setObjectName("dotOff")
        self.ws_state = QLabel("çevrimdışı")
        row = QHBoxLayout(); row.addWidget(self.ws_dot); row.addWidget(self.ws_state)
        row.addStretch(); left.addLayout(row)
        self.mic_dot = QLabel("●"); self.mic_dot.setObjectName("dotOff")
        self.mic_state = QLabel("beklemede")
        row2 = QHBoxLayout(); row2.addWidget(self.mic_dot); row2.addWidget(self.mic_state)
        row2.addStretch(); left.addLayout(row2)
        # mic level bar
        self.level_bar = QFrame(); self.level_bar.setObjectName("levelTrack")
        self.level_bar.setFixedHeight(6)
        self.level_fill = QFrame(self.level_bar); self.level_fill.setObjectName("levelFill")
        self.level_fill.setGeometry(0, 0, 0, 6)
        left.addWidget(self.level_bar)
        # voice buttons stacked compactly
        self.talk_btn = QPushButton("SES BAŞLAT"); self.talk_btn.setObjectName("btn")
        self.stop_btn = QPushButton("■ DURDUR"); self.stop_btn.setObjectName("btnDanger")
        self.stop_btn.setVisible(False)
        left.addWidget(self.talk_btn)
        left.addWidget(self.stop_btn)
        self.always_btn = QPushButton("SÜREKLİ DİNLEME: AÇIK")
        self.always_btn.setObjectName("btnAmber")
        self.always_btn.setCheckable(True); self.always_btn.setChecked(True)
        self.always_btn.clicked.connect(self.toggle_always)
        left.addWidget(self.always_btn)
        # session metrics — live values only
        left.addWidget(self._hud_rule("OTURUM METRİKLERİ"))
        self.m_turns = QLabel("0"); self.m_first = QLabel("—"); self.m_total = QLabel("—")
        self.m_brain = QLabel("hermes-agent")
        for label, val in (("Turlar", self.m_turns),
                           ("Konuşma→ses", self.m_first),
                           ("Toplam", self.m_total),
                           ("Beyin", self.m_brain)):
            r = QHBoxLayout(); l = QLabel(label); l.setObjectName("dim")
            r.addWidget(l); r.addStretch(); r.addWidget(val)
            left.addLayout(r)
        # ---- system section (moved from the right rail) ----
        left.addWidget(self._hud_rule("SİSTEM"))
        self.api_dot = QLabel("●"); self.api_dot.setObjectName("dotOff")
        self.api_state = QLabel("—")
        r = QHBoxLayout(); r.addWidget(self.api_dot); r.addWidget(self.api_state)
        r.addStretch(); left.addLayout(r)
        self.h_cpu = QLabel("—"); self.h_mem = QLabel("—")
        self.sys_cpu_bar = QFrame(); self.sys_cpu_bar.setObjectName("levelTrack")
        self.sys_cpu_bar.setFixedHeight(6)
        self.sys_cpu_fill = QFrame(self.sys_cpu_bar); self.sys_cpu_fill.setObjectName("levelFill")
        self.sys_cpu_fill.setGeometry(0, 0, 0, 6)
        self.sys_mem_bar = QFrame(); self.sys_mem_bar.setObjectName("levelTrack")
        self.sys_mem_bar.setFixedHeight(6)
        self.sys_mem_fill = QFrame(self.sys_mem_bar); self.sys_mem_fill.setObjectName("levelFill")
        self.sys_mem_fill.setGeometry(0, 0, 0, 6)
        for label, val, bar in (("CPU", self.h_cpu, self.sys_cpu_bar),
                                ("Bellek", self.h_mem, self.sys_mem_bar)):
            r = QHBoxLayout(); l = QLabel(label); l.setObjectName("dim")
            r.addWidget(l); r.addStretch(); r.addWidget(val)
            left.addLayout(r)
            left.addWidget(bar)
        # pipeline info — single source of truth, live-updated
        left.addWidget(self._hud_rule("HAT YAPISI"))
        self.p_stt = QLabel("whisper small (tr)")
        self.p_tts = QLabel("piper tr_TR_99eren99")
        for label, val in (("STT", self.p_stt),
                           ("TTS", self.p_tts),
                           ("Yedek", QLabel("claude-haiku"))):
            r = QHBoxLayout(); l = QLabel(label); l.setObjectName("dim")
            r.addWidget(l); r.addStretch(); r.addWidget(val)
            left.addLayout(r)
        # actions
        left.addWidget(self._hud_rule("EYLEMLER"))
        act_row = QHBoxLayout()
        self.voice_btn = QPushButton("SESİ DEĞİŞTİR"); self.voice_btn.setObjectName("btn")
        self.voice_btn.setToolTip("Piper Türkçe ses değiştir (fahrettin ↔ dfki)")
        self.voice_btn.clicked.connect(self.cycle_voice)
        self.wizard_btn = QPushButton("SİHİRBAZ"); self.wizard_btn.setObjectName("btn")
        self.wizard_btn.setToolTip("Sistem denetimi + LM Studio + wake word sihirbazı")
        self.wizard_btn.clicked.connect(self.open_wizard)
        act_row.addWidget(self.voice_btn); act_row.addWidget(self.wizard_btn)
        left.addLayout(act_row)
        # session scope
        left.addWidget(self._hud_rule("OTURUM"))
        for label, val in (("Konuşma", CONV), ("Hafıza kapsamı", "jarvis:user:main")):
            r = QHBoxLayout(); l = QLabel(label); l.setObjectName("dim")
            w = QLabel(val); w.setObjectName("val")
            r.addWidget(l); r.addStretch(); r.addWidget(w)
            left.addLayout(r)
        left.addStretch()
        lw = QWidget(); lw.setLayout(left); lw.setObjectName("col")
        grid.addWidget(lw, 1, 0)
        grid.setColumnStretch(0, 1)   # left rail narrow
        grid.setColumnStretch(1, 3)   # center column dominant

        # ---- CENTER: reactor over compact chat (now the right 3/4) ----
        center = QVBoxLayout()
        center.setSpacing(4)
        if _GLOBE_OK and _ParticleGlobe is not None:
            self.reactor = _ParticleGlobe(size=200)
        else:
            self.reactor = ReactorWidget()
        self.state_label = QLabel("BEKLEMEDE"); self.state_label.setObjectName("coreState")
        self.state_label.setAlignment(Qt.AlignCenter)
        wrap = QWidget(); wrap.setLayout(QVBoxLayout())
        wrap.layout().setContentsMargins(0, 0, 0, 0)
        wrap.layout().addWidget(self.reactor, alignment=Qt.AlignHCenter)
        self.state_hint = QLabel("'Bilgisayar' deyin · boşluk = konuş")
        self.state_hint.setObjectName("dim"); self.state_hint.setAlignment(Qt.AlignCenter)
        wrap.layout().addWidget(self.state_hint)
        # status text floats over the ring
        self.state_label.setParent(self.reactor)
        self.state_label.setGeometry(0, self.reactor.height() // 2 - 20,
                                     self.reactor.width(), 40)
        center.addWidget(wrap, stretch=5)

        feed_holder = QWidget()
        fv = QVBoxLayout(feed_holder); fv.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMaximumHeight(150)          # narrow chat history
        self.feed_inner = QWidget(); self.feed_inner.setObjectName("feed")
        self.feed_v = QVBoxLayout(self.feed_inner)
        self.feed_v.setContentsMargins(4, 4, 4, 4); self.feed_v.setSpacing(6)
        self.feed_v.addStretch()
        scroll.setWidget(self.feed_inner)
        self.feed_scroll = scroll          # keep a handle for auto-scroll
        fv.addWidget(scroll)
        center.addWidget(feed_holder, stretch=2)

        chat_row = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("Hermes'e yaz... (Boşluk = ses)")
        self.chat_input.returnPressed.connect(self.send_chat)
        self.send_btn = QPushButton("GÖNDER"); self.send_btn.setObjectName("btn")
        self.send_btn.clicked.connect(self.send_chat)
        self.clear_btn = QPushButton("🗑"); self.clear_btn.setObjectName("btnDanger")
        self.clear_btn.setFixedWidth(34)
        self.clear_btn.setToolTip("Konuşma geçmişini sil (Hermes oturumunu /new ile sıfırlar)")
        self.clear_btn.clicked.connect(self.clear_history)
        chat_row.addWidget(self.chat_input, stretch=1)
        chat_row.addWidget(self.send_btn)
        chat_row.addWidget(self.clear_btn)
        center.addLayout(chat_row)
        cw = QWidget(); cw.setLayout(center); cw.setObjectName("col")
        grid.addWidget(cw, 1, 1)

        # ---- RIGHT rail removed: all meters merged into the left rail ----

        # footer — plain HUD strip (STARK INDUSTRIES vibe)
        footer = QWidget()
        fh = QHBoxLayout(footer); fh.setContentsMargins(14, 2, 14, 2)
        self.foot_msg = QLabel("HAZIR"); self.foot_msg.setObjectName("dim")
        ver = QLabel("S T A R K   I N D U S T R I E S   ·   NOUS HERMES"); ver.setObjectName("dim")
        fh.addWidget(self.foot_msg); fh.addStretch(); fh.addWidget(ver)
        grid.addWidget(footer, 2, 0, 1, 2)

        # threads --------------------------------------------------------
        self.ws = WsClient(self)
        self.ws.connected.connect(self.on_ws_connected)
        self.ws.disconnected.connect(self.on_ws_disconnected)
        self.ws.status_msg.connect(lambda m: self.add_msg("sys", m))
        self.ws.transcript.connect(self.on_transcript)
        self.ws.partial.connect(self.on_partial)
        self.ws.agent_status.connect(self.on_agent_status)
        self.ws.approval.connect(self.show_approval)
        self.ws.run_started.connect(lambda rid: self.stop_btn.setVisible(True))
        self.ws.done.connect(self.on_done)
        self.ws.error_msg.connect(self.on_error)
        self.ws.audio_chunk.connect(self.on_audio)
        self.ws.start()

        self.mic = MicThread(self)
        self.mic.chunk_ready.connect(self.on_mic_chunk)
        self.mic.level_ready.connect(self.on_level)
        self.mic.wave_ready.connect(self.reactor.set_wave)
        self.mic.error.connect(self.on_error)
        self.mic.start()

        self.playback = PlaybackThread(self)
        self.playback.wave_ready.connect(self.reactor.set_wave)
        self.playback.start()

        # always-listening wake word engine
        self.wake = WakeWordEngine(self)
        self.wake.wake_detected.connect(self.on_wake)
        self.wake.status.connect(lambda m: self.add_msg("sys", m))
        self.wake.start()
        self.mic.wake_engine = self.wake     # mic feeds the engine
        self.auto_listen = True              # continuous listening mode ON

        self.talk_btn.clicked.connect(self.toggle_talk)
        self.stop_btn.clicked.connect(self.stop_run)
        self.reactor.mousePressEvent = lambda ev: self.toggle_talk()

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.tick_clock)
        self.clock_timer.start(500)
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._auto_silence_check)
        self.auto_timer.start(100)
        self.api_timer = QTimer(self)
        self.api_timer.timeout.connect(self.refresh_api)
        self.api_timer.start(15000)
        self.refresh_api()
        self._load_history()
        self.add_msg("sys", "Bilgisayar HUD çevrimiçi. 'Bilgisayar' diyerek veya Boşluk tuşuyla konuşun.")

    # ----------------------------------------------------------- helpers
    # ---- persistent state (window geometry + chat history) ----
    STATE_PATH = os.path.expanduser("~/.config/jarvis/ui_state.json")
    CHAT_PATH = os.path.expanduser("~/.config/jarvis/chat_history.jsonl")

    @classmethod
    def _load_state(cls) -> dict:
        try:
            with open(cls.STATE_PATH) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.STATE_PATH), exist_ok=True)
            g = self.geometry()
            with open(self.STATE_PATH, "w") as f:
                json.dump({"geometry": [g.x(), g.y(), g.width(), g.height()],
                           "always_listen": self.auto_listen}, f)
        except Exception:
            pass

    def _persist_msg(self, cls: str, text: str):
        try:
            os.makedirs(os.path.dirname(self.CHAT_PATH), exist_ok=True)
            with open(self.CHAT_PATH, "a") as f:
                f.write(json.dumps({"t": time.time(), "cls": cls, "text": text},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _load_history(self, limit: int = 50):
        try:
            with open(self.CHAT_PATH) as f:
                lines = f.readlines()[-limit:]
        except Exception:
            return
        for line in lines:
            try:
                d = json.loads(line)
                cls = d.get("cls", "sys")
                if cls in ("you", "jarvis", "sys"):
                    self.add_msg(cls, str(d.get("text", "")), persist=False)
            except Exception:
                continue

    def _hud_rule(self, title: str) -> QWidget:
        """HUD section rule: glow line with a small caption, Stark style."""
        w = QWidget(); h = QHBoxLayout(w)
        h.setContentsMargins(0, 6, 0, 2); h.setSpacing(8)
        cap = QLabel(title); cap.setObjectName("ruleCap")
        line = QFrame(); line.setObjectName("ruleLine"); line.setFixedHeight(2)
        h.addWidget(cap); h.addWidget(line, stretch=1)
        return w

    def tick_clock(self):
        self.clock.setText(time.strftime("%H:%M:%S"))
        # keep overlay label centered in reactor
        self.state_label.setFixedWidth(self.reactor.width())
        self.state_label.move(0, self.reactor.height() // 2 - 20)

    def resizeEvent(self, ev):  # noqa: N802
        super().resizeEvent(ev)
        self.state_label.setFixedWidth(self.reactor.width())
        self.state_label.move(0, self.reactor.height() // 2 - 20)

    def set_state(self, state: str):
        self.state = state if state in STATE_TEXT else "standby"
        self.reactor.set_state(self.state)
        self.state_label.setText(STATE_TEXT[self.state])
        if self.state == "listening":
            self.state_hint.setText("GÖNDERMEK İÇİN TEKRAR TIKLAYIN / BOŞLUK")
        elif self.state == "standby":
            self.state_hint.setText("HALKA TIKLA VEYA BOŞLUK TUŞUNA BAS")

    def add_msg(self, cls: str, text: str, persist: bool = True):
        ts = time.strftime("%H:%M")
        lbl = QLabel(f"{text}" if cls == "sys" else f"<span style='color:#4d7d92;font-size:10px'>{ts}</span><br>{text}")
        lbl.setObjectName("msg" + cls.capitalize())
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._stick_to_bottom()
        self.feed_v.insertWidget(self.feed_v.count() - 1, lbl)
        # no cap on kept messages — scroll up to see the full history
        if persist and cls in ("you", "jarvis", "sys") and text:
            self._persist_msg(cls, text)
        QTimer.singleShot(10, self._maybe_scroll_after_insert)
        return lbl

    def scroll_feed_bottom(self):
        # Follow only when the user is (near) the bottom. If they scrolled up
        # to read history, keep their position and don't yank the view.
        sb = self.feed_scroll.verticalScrollBar()
        at_bottom = sb.maximum() - sb.value() <= 40
        if at_bottom:
            sb.setValue(sb.maximum())

    def _stick_to_bottom(self):
        """Call BEFORE inserting a message: if the view is at the bottom,
        remember it so the post-insert scroll actually follows the new msg."""
        sb = self.feed_scroll.verticalScrollBar()
        self._follow = sb.maximum() - sb.value() <= 40

    def _maybe_scroll_after_insert(self):
        if getattr(self, "_follow", True):
            sb = self.feed_scroll.verticalScrollBar()
            sb.setValue(sb.maximum())

    def add_activity(self, text: str):
        # activity list removed in the compact redesign — mirror into feed as sys
        self.add_msg("sys", text, persist=False)

    # ----------------------------------------------------------- events
    def on_ws_connected(self):
        self.ws_dot.setObjectName("dotOn"); self.ws_dot.setStyleSheet("")
        self._restyle(self.ws_dot)
        self.ws_state.setText("çevrimiçi")
        self.conn_state.setText("BAĞLANTI AKTİF")
        self.foot_msg.setText("SES SUNUCUSU BAĞLANDI")

    def on_ws_disconnected(self):
        self.ws_dot.setObjectName("dotOff"); self._restyle(self.ws_dot)
        self.ws_state.setText("çevrimdışı")
        self.conn_state.setText("BAĞLANTI YOK")
        self.set_state("standby")

    def _restyle(self, lbl):
        lbl.style().unpolish(lbl); lbl.style().polish(lbl)

    def on_transcript(self, text: str):
        self.clear_live()
        if text:
            self.add_msg("you", text)

    def on_partial(self, text: str):
        if not text:
            return
        if self.live_msg is not None:
            try:
                self.live_msg.text()  # probe: deleted C++ object raises
            except RuntimeError:
                self.live_msg = None
        if not self.live_msg:
            self.live_msg = self.add_msg("you", "", persist=False)
        self.live_msg.setText(text)
        self._stick_to_bottom()
        QTimer.singleShot(10, self._maybe_scroll_after_insert)

    def clear_live(self):
        if self.live_msg is not None:
            try:
                self.live_msg.deleteLater()
            except RuntimeError:
                pass
            self.live_msg = None

    def on_agent_status(self, state: str, tool: str = "", preview: str = ""):
        if state == "thinking":
            self.set_state("thinking"); self.stop_btn.setVisible(True)
        elif state == "tool_use":
            self.set_state("tool")
            self.add_activity(f"▸ {tool or 'tool'}" + (f" — {preview[:60]}" if preview else ""))
        elif state == "speaking":
            self.set_state("speaking")
        elif state == "stopped":
            self.set_state("standby"); self.stop_btn.setVisible(False)
            self.playback.clear()
            self.add_msg("sys", "çalıştırma durduruldu")

    # ---- approval dialog (agent permission requests) ----
    def show_approval(self, ev: dict):
        data = ev.get("data") or {}
        run_id = ev.get("run_id") or ""
        approval_id = data.get("approval_id") or data.get("id") or ""
        desc = data.get("preview") or data.get("command") or data.get("description") \
               or json.dumps(data, ensure_ascii=False)[:300]
        self.add_msg("sys", "⚠ ONAY GEREKLİ")
        card = QWidget(); card.setObjectName("apprCard")
        cv = QVBoxLayout(card); cv.setContentsMargins(10, 8, 10, 8)
        pre = QLabel(desc); pre.setObjectName("apprDesc"); pre.setWordWrap(True)
        cv.addWidget(pre)
        row = QHBoxLayout()
        allow = QPushButton("İZİN VER"); allow.setObjectName("btn")
        deny = QPushButton("REDDET"); deny.setObjectName("btnDanger")
        row.addStretch(); row.addWidget(allow); row.addWidget(deny)
        cv.addLayout(row)
        self.feed_v.insertWidget(self.feed_v.count() - 1, card)

        def decide(decision: str):
            self.ws.send_json({"type": "approval_decision", "run_id": run_id,
                               "approval_id": approval_id, "decision": decision})
            card.setParent(None); card.deleteLater()
            self.add_msg("sys", "onay gönderildi: " + ("İZİN" if decision == "allow" else "RED"))
        allow.clicked.connect(lambda: decide("allow"))
        deny.clicked.connect(lambda: decide("deny"))
        QTimer.singleShot(10, self.scroll_feed_bottom)

    def on_done(self, timing: dict):
        self.turns += 1
        self.m_turns.setText(str(self.turns))
        first = timing.get("end_of_speech_to_first_audio_seconds")
        total = timing.get("total_turn_seconds")
        if first is not None:
            self.m_first.setText(f"{first:.2f}s")
        if total is not None:
            self.m_total.setText(f"{total:.2f}s")
        self.m_brain.setText(timing.get("llm_provider") or "—")
        rt = timing.get("response_text")
        if rt:
            self.add_msg("jarvis", rt)
        self.stop_btn.setVisible(False)
        self.clear_live()
        # release the echo guard ~0.4 s after the answer (and playback) end
        QTimer.singleShot(400, self._release_echo_guard)
        QTimer.singleShot(400, lambda: self.state != "listening" and self.set_state("standby"))

    def _release_echo_guard(self):
        # wait until the playback queue is drained, then re-open the mic gate
        def reopen():
            if self.playback._q.qsize() == 0:
                self.wake.suppressed = False
                self.add_msg("sys", "dinlemeye devam — 'Bilgisayar' diyebilirsiniz")
            else:
                QTimer.singleShot(400, reopen)
        QTimer.singleShot(1200, reopen)

    def on_error(self, message: str):
        self.clear_live()
        self.add_msg("sys", "hata: " + message)
        self.set_state("standby")
        self.stop_btn.setVisible(False)
        # a failed turn must not leave the wake engine suppressed forever
        self.wake.suppressed = False

    def on_audio(self, data: bytes):
        self.playback.enqueue(data)
        if self.state in ("thinking", "tool"):
            self.set_state("speaking")

    def on_mic_chunk(self, data: bytes):
        if self.capturing:
            self.ws.send_audio(data)

    def on_level(self, level: float):
        self.reactor.set_level(level)
        w = int(self.level_bar.width() * level)
        self.level_fill.setGeometry(0, 0, w, 6)

    def open_wizard(self):
        """Launch the setup wizard (isair/jarvis-inspired)."""
        if not _WIZARD_OK:
            self.add_msg("sys", "sihirbaz modülü yüklenemedi")
            return
        dlg = SetupWizard(self)
        dlg.resize(620, 520)
        dlg.exec()
        self.refresh_api()

    def cycle_voice(self):
        """Switch the Piper voice server-side via config edit + reload."""
        import threading
        cfg_path = "/home/tekinaltun/jarvis_ai/server/config/server.yaml"
        def work():
            try:
                with open(cfg_path) as f:
                    content = f.read()
                if "tr_TR-fahrettin-medium" in content:
                    new = content.replace("tr_TR-fahrettin-medium", "tr_TR-dfki-medium")
                    label = "DFKİ"
                else:
                    new = content.replace("tr_TR-dfki-medium", "tr_TR-fahrettin-medium")
                    label = "FAHRETTİN"
                with open(cfg_path, "w") as f:
                    f.write(new)
                QTimer.singleShot(0, lambda: (self.voice_btn.setText(label),
                                              self.add_msg("sys", f"TTS sesi: {label}")))
            except Exception as exc:
                QTimer.singleShot(0, lambda: self.add_msg("sys", f"ses değiştirilemedi: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def toggle_talk(self):
        if not self.ws.isRunning():
            self.add_msg("sys", "ses sunucusu çevrimdışı")
            return
        if self.capturing:
            self.capturing = False
            self.ws.send_json({"type": "stop"})
            self.mic.stop_capture()
            self.talk_btn.setText("SES BAŞLAT")
            self.mic_state.setText("hazır")
            self.level_fill.setGeometry(0, 0, 0, 6)
            self.set_state("thinking")
            return
        self.playback.clear()  # barge-in
        self.wake.reset()      # stale wake audio can't false-trigger later
        self.ws.send_json({"type": "start", "sample_rate": SAMPLE_RATE,
                           "format": "pcm_s16le", "channels": 1,
                           "conversation": CONV})
        self.mic.start_capture()
        self.capturing = True
        self.talk_btn.setText("■ DURDUR & GÖNDER")
        self.mic_state.setText("CANLI")
        self.mic_dot.setObjectName("dotOn"); self._restyle(self.mic_dot)
        self.set_state("listening")

    def stop_run(self):
        self.playback.clear()
        self.ws.send_json({"type": "stop_run"})
        self.stop_btn.setVisible(False)

    # ------------------------------------------- always-listening mode
    def toggle_always(self, checked: bool):
        self.auto_listen = checked
        self.wake.enabled = checked
        self.always_btn.setText("SÜREKLİ DİNLEME: " + ("AÇIK" if checked else "KAPALI"))
        self.add_msg("sys", "sürekli dinleme " + ("açıldı — 'Bilgisayar' diyerek konuşun"
                                                    if checked else "kapatıldı"))

    def on_wake(self, tail: bytes):
        """'Bilgisayar' heard → start a command turn, seeding the WS with the tail."""
        if not self.auto_listen or self.capturing:
            return
        self.playback.clear()  # barge-in: stop TTS mid-sentence
        self.wake.reset()               # drop stale audio
        self.wake.suppressed = True     # command window: engine off entirely
        self.ws.send_json({"type": "start", "sample_rate": SAMPLE_RATE,
                           "format": "pcm_s16le", "channels": 1,
                           "conversation": CONV})
        if tail:
            self.ws.send_audio(tail)         # seed: audio after the wake word
        self.mic.start_capture()
        self.capturing = True
        self._wake_started_at = time.time()
        self._wake_silence = 0.0
        self.talk_btn.setText("■ DURDUR & GÖNDER")
        self.mic_state.setText("CANLI")
        self.mic_dot.setObjectName("dotOn"); self._restyle(self.mic_dot)
        self.set_state("listening")
        self.add_msg("sys", "🔔 'Bilgisayar' algılandı — dinliyorum…")

    def _auto_silence_check(self):
        """End the auto-captured command after silence or the hard cap.

        Silence = mic level low AND wake-VAD probability low (if available).
        """
        if not (self.auto_listen and self.capturing and getattr(self, "_wake_started_at", None)):
            return
        level = self.reactor.level
        now = time.time()
        vad_quiet = getattr(self.wake, "last_speech_prob", 1.0) < 0.3
        if level < 0.06 and vad_quiet:
            self._wake_silence = getattr(self, "_wake_silence", 0.0) + 0.1
        else:
            self._wake_silence = 0.0
        elapsed = now - self._wake_started_at
        if self._wake_silence >= SILENCE_END_SEC or elapsed >= COMMAND_MAX_SEC:
            self._wake_silence = 0.0
            self._wake_started_at = None
            # send the command exactly like toggle_talk's stop path
            self.capturing = False
            self.ws.send_json({"type": "stop"})
            self.mic.stop_capture()
            self.wake.reset()
            self.talk_btn.setText("SES BAŞLAT")
            self.mic_state.setText("hazır")
            self.level_fill.setGeometry(0, 0, 0, 6)
            self.set_state("thinking")

    def send_chat(self):
        text = self.chat_input.text().strip()
        if not text:
            return
        self.chat_input.clear()
        self.add_msg("you", text)
        if text.lower().startswith("/new"):
            self.clear_feed()
            self.add_msg("sys", "konuşma sıfırlama isteği gönderildi")
        self.set_state("thinking")
        import threading
        # Voice server's /api/chat over plain HTTP (same host as the WS):
        def work():
            try:
                import requests
                base = SERVER_WS.replace("ws://", "http://").replace("/ws", "")
                r = requests.post(f"{base}/api/chat",
                                  json={"input": text, "conversation": CONV}, timeout=240)
                j = r.json()
                if r.ok:
                    QTimer.singleShot(0, lambda: (self.add_msg("jarvis", j.get("text") or "(yanıt yok)"),
                                                  self.set_state("standby")))
                else:
                    QTimer.singleShot(0, lambda: (self.add_msg("sys", f"sohbet hatası: {j.get('error', r.status_code)}"),
                                                  self.set_state("standby")))
            except Exception as exc:
                QTimer.singleShot(0, lambda: (self.add_msg("sys", f"sohbet hatası: {exc}"),
                                              self.set_state("standby")))
        threading.Thread(target=work, daemon=True).start()

    def clear_feed(self):
        while self.feed_v.count() > 1:
            item = self.feed_v.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.add_msg("sys", "sohbet temizlendi (geçmiş Hermes'te kalır — /new ile sıfırla)")

    def clear_history(self):
        """Clear the on-screen feed AND the persisted chat file + Hermes session."""
        from PySide6.QtWidgets import QMessageBox
        btns = QMessageBox.StandardButton
        ret = QMessageBox.question(
            self, "Geçmişi Sil",
            "Konuşma geçmişi ekrandan ve kayıt dosyasından silinecek.\n"
            "Hermes oturumu da /new ile sıfırlanacak.\n\nEmin misiniz?",
            btns.Yes | btns.No, btns.No)
        if ret != btns.Yes:
            return
        # 1) wipe the persisted jsonl
        try:
            if os.path.exists(self.CHAT_PATH):
                os.remove(self.CHAT_PATH)
        except Exception as exc:
            self.add_msg("sys", f"geçmiş dosyası silinemedi: {exc}")
        # 2) wipe the on-screen feed
        while self.feed_v.count() > 1:
            item = self.feed_v.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()
        # 3) reset the Hermes session in a worker thread (non-blocking)
        import threading
        def work():
            try:
                import requests
                base = SERVER_WS.replace("ws://", "http://").replace("/ws", "")
                r = requests.post(f"{base}/api/chat",
                                  json={"input": "/new", "conversation": CONV},
                                  timeout=30)
                ok = r.ok
            except Exception:
                ok = False
            QTimer.singleShot(0, lambda: self.add_msg(
                "sys", "oturum sıfırlandı — geçmiş tamamen silindi" if ok
                else "oturum sıfırlanamadı (sunucuya ulaşılıyor mu?)"))
        threading.Thread(target=work, daemon=True).start()

    def refresh_api(self):
        import threading
        def work():
            try:
                import requests
                r = requests.get(f"{HERMES_API}/health", timeout=5)
                ok = r.status_code == 200
            except Exception:
                ok = False
            # system gauges via /proc (fast, no deps)
            try:
                with open("/proc/loadavg") as f:
                    load1 = float(f.read().split()[0])
                cpu_frac = min(1.0, load1 / (os.cpu_count() or 1))
            except Exception:
                cpu_frac = 0.0
            try:
                with open("/proc/meminfo") as f:
                    mi = {l.split(":")[0]: int(l.split()[1]) for l in f if ":" in l}
                mem_frac = 1 - mi["MemAvailable"] / mi["MemTotal"]
            except Exception:
                mem_frac = 0.0
            def ui():
                self.api_dot.setObjectName("dotOn" if ok else "dotOff")
                self._restyle(self.api_dot)
                self.api_state.setText("çevrimiçi" if ok else "çevrimdışı")
                # system meters: text + fill bars (compact redesign)
                self.h_cpu.setText(f"{cpu_frac*100:.0f}%")
                self.h_mem.setText(f"{mem_frac*100:.0f}%")
                for bar, fill, frac in ((self.sys_cpu_bar, self.sys_cpu_fill, cpu_frac),
                                        (self.sys_mem_bar, self.sys_mem_fill, mem_frac)):
                    w = int(bar.width() * frac)
                    fill.setGeometry(0, 0, w, 6)
                # live pipeline labels (server.yaml is the single source of truth)
                try:
                    import yaml
                    with open("/home/tekinaltun/jarvis_ai/server/config/server.yaml") as f:
                        vcfg = yaml.safe_load(f).get("voice") or {}
                    self.p_tts.setText(f"piper {vcfg.get('voice_id', '—')}")
                    self.p_stt.setText(
                        f"whisper {vcfg.get('model', 'small')}" if (vcfg.get('model')) else "whisper small (tr)")
                except Exception:
                    pass
            QTimer.singleShot(0, ui)
        threading.Thread(target=work, daemon=True).start()

    def keyPressEvent(self, ev):  # noqa: N802
        if ev.key() == Qt.Key_Space and not self.chat_input.hasFocus():
            ev.accept(); self.toggle_talk(); return
        if ev.key() == Qt.Key_Escape:
            self.stop_run(); return
        super().keyPressEvent(ev)

    def closeEvent(self, ev):  # noqa: N802
        self._save_state()
        self.wake.stop()
        self.ws.stop(); self.mic.stop(); self.playback.stop()
        super().closeEvent(ev)


STYLESHEET = """
#centralWidget { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
    stop:0 #04101d, stop:0.5 #030a13, stop:1 #02060c); }
#logo { font-family: 'Orbitron','DejaVu Sans'; font-size: 16px; letter-spacing: 6px;
        color: #00e5ff; }
#sub { font-size: 11px; letter-spacing: 3px; color: #4d7d92; }
#clock { font-size: 15px; color: #19f0d8; }
#ruleCap { font-size: 9px; letter-spacing: 3px; color: #00e5ff; }
#ruleLine { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 #00e5ff88, stop:1 #00e5ff10); border: none; }
#dim { color: #4d7d92; font-size: 12px; }
#val { color: #9fd8e8; font-size: 13px; font-weight: 600; }
#dotOn { color: #19f0d8; } #dotOff { color: #ff4d5e; }
#levelTrack { background: #0a2435; border-radius: 3px; border: 1px solid #0e2c40; }
#levelFill { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 #0a7f96, stop:1 #00e5ff); border-radius: 3px; }
QPushButton#btn { background: #0a2435aa; color: #00e5ff; border: 1px solid #0a7f96;
                  padding: 8px 14px; font-size: 11px; letter-spacing: 2px; }
QPushButton#btn:hover { background: #0e3349; }
QPushButton#btnDanger { background: #240a0eaa; color: #ff4d5e; border: 1px solid #7a2a33;
                        padding: 8px 14px; font-size: 11px; }
#btnAmber { background: #241c0aaa; color: #ffb300; border: 1px solid #7a5a1a;
           padding: 8px 14px; font-size: 11px; letter-spacing: 2px; }
#msgYou { background: #0a243566; border: 1px solid #1a4a5e; border-right: 3px solid #19f0d8;
          color: #cfeefb; padding: 7px 11px; font-size: 13.5px; }
#msgJarvis { background: #08152366; border: 1px solid #0e2c40; border-left: 3px solid #00e5ff;
             color: #9fd8e8; padding: 7px 11px; font-size: 13.5px; }
#msgSys { color: #4d7d92; font-size: 11.5px; }
#apprCard { background: #241c0acc; border: 1px solid #ffb300aa; }
#apprDesc { color: #ffd98a; font-size: 12.5px; }
#coreState { font-size: 14px; letter-spacing: 6px; color: #00e5ff;
             background: transparent; }
QScrollArea { background: transparent; border: none; }
QWidget#feed { background: transparent; }
QLineEdit { background: #06101caa; border: 1px solid #0e2c40; color: #cfeefb;
            padding: 9px 12px; font-size: 14px; border-radius: 2px; }
QLineEdit:focus { border-color: #0a7f96; }
QWidget { background: transparent; color: #9fd8e8; font-family: 'Rajdhani','DejaVu Sans'; }
QLabel { background: transparent; }
QFrame { background: transparent; }
QScrollArea { background: transparent; }
QWidget#feed { background: transparent; }
QScrollBar:vertical { background: #06101c66; width: 7px; }
QScrollBar::handle:vertical { background: #0e2c40; border-radius: 3px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "gnome")
    app = QApplication(sys.argv)
    app.setApplicationName("JARVIS")
    app.setQuitOnLastWindowClosed(False)   # tray keeps it alive
    app.setStyleSheet(STYLESHEET)

    # rotating log for debugging
    log_path = "/tmp/jarvis-app.log"
    import logging
    try:
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger("jarvis").addHandler(handler)
        logging.getLogger("jarvis").setLevel(logging.INFO)
    except Exception:
        pass

    win = MainWindow()
    win.show()

    # ---- system tray ----
    tray = None
    try:
        from PySide6.QtGui import QIcon, QPixmap
        from PySide6.QtWidgets import QSystemTrayIcon, QMenu
        from PySide6.QtGui import QAction

        pm = QPixmap(24, 24); pm.fill(QColor(0, 0, 0, 0))
        pt = QPainter(pm)
        pt.setRenderHint(QPainter.Antialiasing)
        pt.setPen(QPen(QColor(0, 229, 255), 2)); pt.setBrush(QBrush())
        pt.drawEllipse(3, 3, 18, 18)
        pt.setBrush(QBrush(QColor(0, 229, 255))); pt.drawEllipse(10, 10, 4, 4)
        pt.end()
        icon = QIcon(pm)

        tray = QSystemTrayIcon(icon, app)
        menu = QMenu()
        act_show = QAction("Pencereyi Göster", app)
        act_quit = QAction("Çıkış", app)
        menu.addAction(act_show); menu.addSeparator(); menu.addAction(act_quit)
        tray.setContextMenu(menu)
        tray.setToolTip("J.A.R.V.I.S — 'Bilgisayar' diyerek konuşun")

        def toggle_window():
            if win.isVisible():
                win.hide()
            else:
                win.showNormal(); win.activateWindow()
        act_show.triggered.connect(toggle_window)

        def quit_all():
            win._save_state()
            win.wake.stop(); win.ws.stop(); win.mic.stop(); win.playback.stop()
            app.quit()
        act_quit.triggered.connect(quit_all)

        # closing the window just hides to tray
        win._hide_on_close = True
        tray.activated.connect(lambda reason: toggle_window() if reason == QSystemTrayIcon.Trigger else None)
        tray.show()
    except Exception as exc:
        logging.getLogger("jarvis").warning("tray unavailable: %s", exc)

    def close_event_override(ev):
        if getattr(win, "_hide_on_close", False) and tray:
            ev.ignore(); win.hide()
            win._save_state()
        else:
            win._save_state()
            win.wake.stop(); win.ws.stop(); win.mic.stop(); win.playback.stop()
            ev.accept()
    win.closeEvent = close_event_override

    rc = app.exec()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
