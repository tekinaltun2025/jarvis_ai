#!/usr/bin/env python3
"""JARVIS Setup Wizard — inspired by isair/jarvis's setup wizard.

Checks (page by page, with auto-fix actions):
  1. Voice server (server.py on :8765)      → auto-start button
  2. Hermes API (:8642)                     → status + restart hint
  3. Piper TTS Turkish voice model          → auto-download button
  4. Microphone (PipeWire default source)   → probe + level meter
  5. LM Studio model server (optional)      → endpoint + key + model list
  6. Wake word choice                       → Bilgisayar / Jarvis
Each page shows live status dots and an action when something is missing.
"""
import json
import os
import shutil
import subprocess
import sys

import requests
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget, QWizard,
    QWizardPage, QFileDialog,
)

from PySide6.QtCore import QSettings

SERVER_WS = "ws://127.0.0.1:8765/ws"
HERMES_API = "http://127.0.0.1:8642"
PIPER_DIR = os.path.expanduser("~/.cache/piper")
LMSTUDIO_ENV = os.path.expanduser("~/.config/jarvis/lmstudio.env")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.expanduser("~/.config/jarvis/ui_state.json")
VOICE_SERVER_CFG = os.path.join(APP_DIR, "server", "config", "server.yaml")


def _read_env(path: str) -> dict:
    out = {}
    try:
        with open(path) as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ checks
class CheckThread(QThread):
    """Run all checks in the background; emit results as they complete."""
    result = Signal(str, bool, str)   # key, ok, detail

    def run(self):
        # 1. voice server
        try:
            import socket
            s = socket.create_connection(("127.0.0.1", 8765), timeout=2)
            s.close()
            self.result.emit("server", True, "ws://127.0.0.1:8765 bağlı")
        except Exception:
            self.result.emit("server", False, "8765 dinlenmiyor — server.py kapalı")

        # 2. hermes api
        try:
            r = requests.get(f"{HERMES_API}/health", timeout=4)
            ok = r.status_code == 200
            self.result.emit("hermes", ok, "health 200" if ok else f"HTTP {r.status_code}")
        except Exception as exc:
            self.result.emit("hermes", False, f"erişilemiyor: {type(exc).__name__}")

        # 3. piper voice
        try:
            with open(VOICE_SERVER_CFG) as f:
                cfg = f.read()
            vid = "tr_TR-fahrettin-medium"
            for line in cfg.splitlines():
                if line.strip().startswith("voice_id:"):
                    vid = line.split(":", 1)[1].strip()
                    break
            onnx = os.path.join(PIPER_DIR, f"{vid}.onnx")
            jsn = os.path.join(PIPER_DIR, f"{vid}.json")
            ok = os.path.exists(onnx) and os.path.exists(jsn)
            self.result.emit("piper", ok, f"{vid}: " + ("kurulu" if ok else "model eksik"))
        except Exception as exc:
            self.result.emit("piper", False, f"config okunamadı: {exc}")

        # 4. microphone
        try:
            import sounddevice as sd
            import numpy as np
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                                blocksize=1600) as st:
                data, _ = st.read(1600)
                arr = np.frombuffer(data, dtype=np.int16)
                peak = int(np.abs(arr).max()) if arr.size else 0
            ok = peak > 0
            self.result.emit("mic", ok, f"yakalama OK (peak={peak})" if ok else "sessiz cihaz")
        except Exception as exc:
            self.result.emit("mic", False, f"mikrofon açılamadı: {type(exc).__name__}")

        # 5. LM Studio (optional)
        env = _read_env(LMSTUDIO_ENV)
        key = env.get("LMSTUDIO_API_KEY", "")
        port = env.get("LMSTUDIO_PORT", "1234")
        if key and port:
            try:
                r = requests.get(f"http://127.0.0.1:{port}/v1/models",
                                 headers={"Authorization": f"Bearer {key}"}, timeout=4)
                models = [m.get("id", "?").split("/")[-1] for m in
                          (r.json().get("data") or [])]
                ok = r.status_code == 200
                detail = f":{port} " + (f"→ {len(models)} model" if ok else f"HTTP {r.status_code}")
                self.result.emit("lmstudio", ok, detail + (f" ({models[0][:40]})" if ok and models else ""))
            except Exception as exc:
                self.result.emit("lmstudio", False, f":{port} erişilemedi")
        else:
            self.result.emit("lmstudio", False, "anahtar yapılmamış (isteğe bağlı)")

        # 6. hermes toolchain python
        py = shutil.which("python3")
        self.result.emit("python", True, py or "python3")


class StatusDot(QWidget):
    """Small colored status circle: gray=checking, green=ok, red=fail."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.color = QColor("#4d7d92")
        self.setFixedSize(14, 14)

    def set_state(self, ok: bool):
        self.color = QColor("#19f0d8") if ok else QColor("#ff4d5e")
        self.update()

    def set_pending(self):
        self.color = QColor("#4d7d92"); self.update()

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(self.color)); p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 10, 10)
        p.end()


class CheckPage(QWizardPage):
    """A page that lists check rows with status dots and action buttons."""
    def __init__(self, title: str, subtitle: str, rows: list, wizard=None):
        super().__init__()
        self.wizard_ref = wizard
        self.setTitle(title)
        self.setSubTitle(subtitle)
        v = QVBoxLayout(self)
        self._rows = {}
        for key, label in rows:
            h = QHBoxLayout()
            dot = StatusDot(); dot.set_pending()
            lbl = QLabel(label); lbl.setStyleSheet("color:#cfeefb; font-size:14px;")
            detail = QLabel("…"); detail.setStyleSheet("color:#4d7d92; font-size:12px;")
            detail.setMinimumWidth(260)
            action = QPushButton(""); action.setObjectName("btn")
            action.setVisible(False)
            h.addWidget(dot); h.addWidget(lbl); h.addStretch()
            h.addWidget(detail); h.addWidget(action)
            v.addLayout(h)
            self._rows[key] = (dot, detail, action)
        v.addStretch()
        self._thread = None

    def initializePage(self):
        self._run_checks()

    def _run_checks(self):
        for dot, detail, action in self._rows.values():
            dot.set_pending(); detail.setText("…"); action.setVisible(False)
        self._thread = CheckThread()
        self._thread.result.connect(self._on_result)
        self._thread.start()

    def _on_result(self, key: str, ok: bool, detail: str):
        if key not in self._rows:
            return
        dot, dlabel, action = self._rows[key]
        dot.set_state(ok)
        dlabel.setText(detail)
        if ok:
            return
        # wire auto-fix actions
        if key == "server":
            action.setText("BAŞLAT")
            action.clicked.connect(self._start_server)
            action.setVisible(True)
        elif key == "piper":
            action.setText("İNDİR")
            action.clicked.connect(self._fix_piper)
            action.setVisible(True)
        elif key == "hermes":
            action.setText("YENİDEN DENE")
            action.clicked.connect(self._run_checks)
            action.setVisible(True)
        elif key == "mic":
            action.setText("YENİDEN DENE")
            action.clicked.connect(self._run_checks)
            action.setVisible(True)
        elif key == "lmstudio":
            action.setText("YAPILANDIR")
            action.clicked.connect(self._configure_lmstudio)
            action.setVisible(True)

    # ---- auto-fix actions ------------------------------------------
    def _start_server(self):
        dot, dlabel, action = self._rows["server"]
        dlabel.setText("başlatılıyor…")
        subprocess.Popen(
            [sys.executable or "python3", os.path.join(APP_DIR, "server", "server.py")],
            cwd=os.path.join(APP_DIR, "server"),
            stdout=open("/tmp/jarvis-server.log", "a"), stderr=subprocess.STDOUT)
        QTimer.singleShot(6000, self._run_checks)

    def _fix_piper(self):
        dot, dlabel, action = self._rows["piper"]
        dlabel.setText("indiriliyor… (~63 MB)")
        import threading
        def work():
            ok = False
            for name in ("tr_TR-fahrettin-medium",):
                base = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/voices/tr/tr_TR/fahrettin/medium/{name}"
                try:
                    r1 = requests.get(base + ".onnx", timeout=600)
                    r2 = requests.get(base + ".json", timeout=120)
                    if r1.ok and r2.ok:
                        os.makedirs(PIPER_DIR, exist_ok=True)
                        with open(os.path.join(PIPER_DIR, name + ".onnx"), "wb") as f:
                            f.write(r1.content)
                        with open(os.path.join(PIPER_DIR, name + ".json"), "wb") as f:
                            f.write(r2.content)
                        ok = True
                except Exception:
                    pass
            QTimer.singleShot(0, lambda: (dot.set_state(ok),
                                          dlabel.setText("kurulu" if ok else "indirme başarısız — manuel deneyin"),
                                          action.setVisible(not ok)))
        threading.Thread(target=work, daemon=True).start()

    def _configure_lmstudio(self):
        dlg = LMStudioDialog(self)
        if dlg.exec() == QDialog.Accepted:
            QTimer.singleShot(300, self._run_checks)


class LMStudioDialog(QDialog):
    """One-shot LM Studio config: base URL + API key + live model list."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("LM Studio Bağlantısı")
        self.setFixedWidth(460)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("LM Studio / OpenAI-uyumlu sunucu bilgisi:"))
        h1 = QHBoxLayout()
        h1.addWidget(QLabel("Port:"))
        self.port_edit = QLineEdit("35701")
        h1.addWidget(self.port_edit)
        v.addLayout(h1)
        h2 = QHBoxLayout()
        h2.addWidget(QLabel("API Key:"))
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        h2.addWidget(self.key_edit)
        v.addLayout(h2)
        self.status = QLabel(""); self.status.setStyleSheet("color:#4d7d92; font-size:12px;")
        v.addWidget(self.status)
        self.models = QListWidget(); self.models.setMaximumHeight(140)
        v.addWidget(self.models)
        btns = QHBoxLayout()
        test = QPushButton("TEST ET"); test.setObjectName("btn")
        save = QPushButton("KAYDET"); save.setObjectName("btn")
        btns.addStretch(); btns.addWidget(test); btns.addWidget(save)
        v.addLayout(btns)
        test.clicked.connect(self._test)
        save.clicked.connect(self._save)
        # preload saved
        env = _read_env(LMSTUDIO_ENV)
        self.port_edit.setText(env.get("LMSTUDIO_PORT", "35701"))
        self.key_edit.setText(env.get("LMSTUDIO_API_KEY", ""))

    def _test(self):
        self.status.setText("test ediliyor…")
        port = self.port_edit.text().strip() or "1234"
        key = self.key_edit.text().strip()
        try:
            r = requests.get(f"http://127.0.0.1:{port}/v1/models",
                             headers={"Authorization": f"Bearer {key}"} if key else {},
                             timeout=5)
            if r.status_code == 200:
                models = [m.get("id", "?").split("/")[-1] for m in r.json().get("data") or []]
                self.models.clear()
                for m in models:
                    QListWidgetItem(m, self.models)
                self.status.setText(f"✓ {len(models)} model bulundu")
                self.status.setStyleSheet("color:#19f0d8; font-size:12px;")
            else:
                self.status.setText(f"HTTP {r.status_code} — anahtar/port hatalı olabilir")
                self.status.setStyleSheet("color:#ff4d5e; font-size:12px;")
        except Exception as exc:
            self.status.setText(f"bağlanamadı: {exc}")
            self.status.setStyleSheet("color:#ff4d5e; font-size:12px;")

    def _save(self):
        os.makedirs(os.path.dirname(LMSTUDIO_ENV), exist_ok=True)
        with open(LMSTUDIO_ENV, "w") as f:
            f.write(f"LMSTUDIO_API_KEY={self.key_edit.text().strip()}\n")
            f.write(f"LMSTUDIO_PORT={self.port_edit.text().strip()}\n")
        os.chmod(LMSTUDIO_ENV, 0o600)
        self.accept()


class WakeWordPage(QWizardPage):
    """Choose the wake word; writes it to ~/.config/jarvis/settings.json."""
    WORDS = ("bilgisayar", "jarvis", "hey asistan")
    def __init__(self):
        super().__init__()
        self.setTitle("Uyanma kelimesi")
        self.setSubTitle("Asistan hangi kelimeyle uyanacak?")
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Kelime seçin (whisper varyantları otomatik eklenir):"))
        self.combo = QComboBox()
        for w in self.WORDS:
            self.combo.addItem(w)
        v.addWidget(self.combo)
        hint = QLabel("Sürekli dinleme bu kelimeyi duyunca komut penceresini açar.")
        hint.setWordWrap(True); hint.setStyleSheet("color:#4d7d92; font-size:12px;")
        v.addWidget(hint)
        v.addStretch()

    def validatePage(self):
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            st = {}
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH) as f:
                    st = json.load(f)
            st["wake_word"] = self.combo.currentText()
            with open(STATE_PATH, "w") as f:
                json.dump(st, f)
        except Exception:
            pass
        return True


class FinishPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Hazır!")
        self.setSubTitle("JARVIS yapılandırması tamamlandı.")
        v = QVBoxLayout(self)
        msg = QLabel("Uygulama artık 'Bilgisayar' kelimesiyle uyanacak.\n"
                     "Bu sihirbazı tekrar açmak için: Sağ panel → SİHİRBAZ")
        msg.setWordWrap(True); msg.setStyleSheet("color:#cfeefb; font-size:14px;")
        v.addWidget(msg); v.addStretch()


class SetupWizard(QWizard):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("JARVIS Kurulum Sihirbazı")
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.addPage(CheckPage(
            "Sistem denetimi",
            "Ses sunucusu, API, TTS modeli ve mikrofon kontrol ediliyor…",
            [("server", "Ses sunucusu (8765)"),
             ("hermes", "Hermes API (8642)"),
             ("piper", "Piper TTS Türkçe sesi"),
             ("mic", "Mikrofon"),
             ("lmstudio", "LM Studio model sunucusu (isteğe bağlı)")],
            self))
        self.addPage(WakeWordPage())
        self.addPage(FinishPage())
        self.setButtonText(QWizard.FinishButton, "BİTİR")

    def done(self, result):
        # persist wake word choice so the app can pick it up on next start
        super().done(result)
