# JARVIS Native Ubuntu Uygulaması — Plan

## Hedef
HUD'u web tarayıcısından çıkarmak: PySide6 (Qt6) ile **native Ubuntu masaüstü uygulaması**.
Aynı arka plan mimarisi korunur (voice server 8765 WS), ön yüz Qt penceresi olur.

## Mimari

```
┌─────────────────────────────────────────────────┐
│  jarvis_app.py (PySide6 native GUI)             │
│  ├─ Ana pencere: koyu tema, holografik stil     │
│  ├─ Reactor widget: QPainter halka animasyonu   │
│  ├─ Mikrofon: sounddevice (native PulseWire)    │
│  ├─ Ses çalma: sounddevice playback             │
│  ├─ WebSocket istemcisi → ws://127.0.0.1:8765/ws│
│  ├─ Sohbet akışı + aktivite paneli              │
│  └─ Durum etiketleri (Türkçe)                   │
└──────────────────┬──────────────────────────────┘
                   │ WebSocket (JSON + binary PCM)
┌──────────────────▼──────────────────────────────┐
│  server.py (mevcut, değişiklik yok)             │
│  ├─ STT: faster-whisper small (tr)              │
│  ├─ LLM: Hermes Agent (8642)                    │
│  └─ TTS: Piper tr_TR-fahrettin-medium           │
└─────────────────────────────────────────────────┘
```

## Neden PySide6 + sounddevice
- **Native**: Qt widget'ları gerçek pencere, GPU hızlandırmalı, tarayıcı/HTTPS gerekmez
- **Mikrofon**: sounddevice PortAudio üzerinden PipeWire'a native bağlanır — secure-context/sertifika sorunları tamamen ortadan kalkar
- **WebEngine YOK**: QWebEngineView kullanmıyoruz (o da bir Chromium); saf QWidget + QPainter
- Mevcut sunucu protokolü birebir kullanılır: `start/stop` JSON + binary PCM16 chunk'lar

## Bileşenler

### 1. jarvis_app.py — ana uygulama (tek dosya)
- `ReactorWidget(QWidget)`: paintEvent ile 4 halka + tik işaretleri + çekirdek parıltısı (HTML'deki canvas mantığının Qt karşılığı), durum renkleri: standby cyan, listening teal, thinking amber, speaking cyan
- `MicThread(QThread)`: sounddevice InputStream 16kHz mono int16, 80ms chunk'lar → WS binary send; ses seviyesi sinyali (Qt Signal)
- `WsClient(QThread)`: websockets bağlantı; JSON mesajlar → Qt Signal (status, transcript, partial, agent_status, done, error, approval); binary → playback kuyruğu
- `PlaybackThread(QThread)`: sounddevice OutputStream 16kHz, kuyruktan int16 chunk çalar (barge-in desteği: stopPlayback kuyruğu boşaltır)
- `MainWindow`: 3 kolonlu grid:
  - Sol: SES BAĞLANTISI (socket/mikrofon durumu, seviye çubuğu, SES BAŞLAT/DURDUR), AJAN AKTİVİTESİ, TUR METRİKLERİ
  - Orta: reactor + durum + sohbet akışı + giriş satırı
  - Sağ: MODEL YÜKLEMESİ (bilgi), TANILAMA (API durumu — /api/usage üzerinden), OTURUM
- Klavye: Boşluk = konuşma toggle, Enter = gönder
- Auth: token'ı `~/.hermes/.env`'den okur (JARVIS_HUD_TOKEN), PIN diyaloğu gerekirse

### 2. jarvis.desktop — masaüstü başlatıcısı
~/.local/share/applications/ altına; uygulamayı menüden/dock'tan başlatabilir

### 3. start-jarvis-app.sh — servis + uygulama başlatıcı
Voice server çalışmıyorsa başlatır, sonra GUI'yi açar

## Değişiklik yapılmayacaklar
- server.py, config, Piper, STT — hepsi olduğu gibi kalır
- Web HUD (index.html) — yerinde durur (yedek arayüz)

## Doğrulama planı
1. `python -c "import jarvis_app"` — sözdizimi/içe aktarma
2. Uygulamayı başlat → pencere açılıyor mu (DISPLAY=:0)
3. WS bağlantısı: server logunda bağlantı
4. Mikrofon: sounddevice 16kHz yakalama testi (pw-record benzeri)
5. TTS çalma: sunucudan gelen binary chunk'lar OutputStream ile duyulur
