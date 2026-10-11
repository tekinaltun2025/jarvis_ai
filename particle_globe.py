#!/usr/bin/env python3
"""ParticleGlobe — a Three.js-inspired 3D particle sphere rendered with
QOpenGLWidget (no external deps beyond PySide6). Fibonacci-sphere placement
inspired by Prompt-Surfer/obsidian-jarvis-ui's 3D WebGL graph.

The globe idles with a slow rotation; when fed audio levels it pulses and the
particles scatter outward (like the 'drop' nodes blooming with light).
Falls back to a 2D QPainter globe when no OpenGL context is available.
"""
import math
import time

import numpy as np
from PySide6.QtCore import Qt, QTimer, QPointF, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QRadialGradient
from PySide6.QtWidgets import QWidget

try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
    _GL_OK = True
except Exception:
    _GL_OK = False


def fibonacci_sphere(n: int) -> np.ndarray:
    """Evenly distribute n points on a unit sphere (Fibonacci lattice)."""
    i = np.arange(n, dtype=np.float32)
    phi = np.arccos(1 - 2 * (i + 0.5) / n)          # polar angle
    theta = math.pi * (1 + 5 ** 0.5) * i            # golden angle
    x = np.sin(phi) * np.cos(theta)
    y = np.sin(phi) * np.sin(theta)
    z = np.cos(phi)
    return np.stack([x, y, z], axis=1).astype(np.float32)


class ParticleGlobe(QWidget):
    """Rotating 3D particle sphere with audio-reactive bloom (2D projected)."""

    def __init__(self, parent=None, count: int = 260, size: int = 300):
        super().__init__(parent)
        self._base = fibonacci_sphere(count)
        self._jitter = np.random.RandomState(11).uniform(0.92, 1.06, (count, 1)).astype(np.float32)
        self._base = self._base * self._jitter
        self._points = self._base.copy()
        self._yaw = 0.0
        self._pitch = 0.42                     # slight downward tilt
        self._audio = 0.0                      # eased audio level
        self._target_audio = 0.0
        self._depth = np.zeros(count, dtype=np.float32)
        self._proj = (np.zeros(count), np.zeros(count), np.zeros(count))
        self.state = "standby"                 # ReactorWidget API compat
        self.level = 0.0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._timer.start(33)                  # ~30 fps (cheap)

    # ---- API ---------------------------------------------------------
    def set_audio(self, level: float):
        """0..1 mic/playback level — drives bloom pulse."""
        self._target_audio = max(0.0, min(1.0, level))

    def set_active(self, active: bool):
        self._target_audio = 0.25 if active else 0.0
        self.update()

    def set_state(self, state: str):
        """Map agent state to audio level for visual feedback."""
        active = state in ("listening", "speaking", "thinking", "tool")
        self.set_active(active)
        # also update level attribute for MainWindow compatibility
        self.level = self._audio

    # ---- compatibility shim (MainWindow calls ReactorWidget API) ----
    def set_level(self, level: float):
        self.set_audio(level)

    def set_wave(self, wave):
        """Feed a snapshot of audio samples (float32, -1..1) — RMS drives bloom."""
        if wave is None or not len(wave):
            return
        w = np.asarray(wave, dtype=np.float32)
        self.set_audio(float(np.abs(w).mean() * 6.0))

    # ---- loop ---------------------------------------------------------
    def tick(self):
        self._yaw += 0.006 + self._audio * 0.02      # spin faster with audio
        self._audio += (self._target_audio - self._audio) * 0.12
        # project points: rotate yaw, tilt pitch, audio scatters outward
        a = self._yaw
        ca, sa = math.cos(a), math.sin(a)
        cp, sp = math.cos(self._pitch), math.sin(self._pitch)
        p = self._base
        x1 = p[:, 0] * ca + p[:, 2] * sa
        z1 = -p[:, 0] * sa + p[:, 2] * ca
        y1 = p[:, 1] * cp - z1 * sp
        z2 = p[:, 1] * sp + z1 * cp
        scatter = 1.0 + self._audio * 0.35
        x1, y1, z2 = x1 * scatter, y1 * scatter, z2 * scatter
        self._depth = (z2 + 1.0) / 2.0               # 0=back 1=front
        self._proj = (x1, y1, z2)
        self.update()

    # ---- paint ---------------------------------------------------------
    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width(); h = self.height()
        cx, cy = w / 2, h / 2
        R = min(w, h) * 0.38

        # API compat: MainWindow reads reactor.level for gauges/silence checks
        self.level = self._audio

        # soft core glow
        glow = QRadialGradient(cx, cy, R * (1.15 + self._audio * 0.3), cx, cy)
        g0 = QColor(0, 229, 255, int(60 + 70 * self._audio))
        glow.setColorAt(0, g0)
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(glow)); p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), R * 1.25, R * 1.25)

        # particle field: back-to-front, front dots brighter & bigger
        x1, y1, z2 = self._proj
        order = np.argsort(z2)
        size_base = 1.4 + 2.2 * self._audio
        for idx in order:
            zz = z2[idx]
            depth_alpha = 0.16 + 0.74 * max(0.0, min(1.0, zz))
            c = QColor(0, 229, 255, int(depth_alpha * 255))
            rr = size_base * (0.55 + 0.9 * max(0.0, zz))
            p.setBrush(QBrush(c)); p.setPen(Qt.NoPen)
            px = cx + x1[idx] * R
            py = cy - y1[idx] * R
            p.drawEllipse(QPointF(px, py), rr, rr)

        # equator wire (adds 3D legibility)
        p.setPen(QPen(QColor(0, 229, 255, 45), 1)); p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), R, R * 0.28)
        p.setPen(QPen(QColor(0, 229, 255, 30), 1))
        p.drawEllipse(QPointF(cx, cy), R * 1.06, R * 0.34)
        p.end()
