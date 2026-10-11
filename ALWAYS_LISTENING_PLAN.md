# JARVIS Sürekli Dinleme Modu — Plan

## Hedef
Push-to-talk yerine **always-listening**: uygulama arka planda sürekli dinler,
sadece **"Jarvis"** wake word'ü duyduğunda aktifleşir. Diğer konuşmalar yok sayılır.

## Mimari (mevcut native app'e ekleme — server.py DEĞİŞMEZ)

```
MicThread (zaten sürekli çalışıyor, 16kHz chunk üretiyor)
   │ her 80ms'lik chunk
   ▼
WakeWordEngine (YENİ QThread, app içinde)
   ├─ Aşama 0: enerji kapısı — RMS < eşik ise tamamen atla (CPU ~%0)
   ├─ Aşama 1: silero VAD — konuşma değilse atla (hafif, ~1ms)
   ├─ Aşama 2: rolling buffer (5s) dolunca faster-whisper small TR ile transcribe
   │           (yaw: sadece VAD konuşmaAlgılandığında; ~0.6s CPU)
   └─ Aşama 3: transcript'te "jarvis|cervis|carvis|javis|yarıvis" fuzzy match
        │ bulundu
        ▼
   "wake_detected" sinyali → MainWindow
        ├─ TTS çalmayı durdur (barge-in)
        ├─ start komutu gönder → mevcut konuşma akışı (listening state)
        ├─ 2.5 s konuşma penceresi: wake'den sonrası buffer'daki audio
        │   zaten start'a dahil edilir (server chunk'ları toplamaya başlar)
        └─ konuşma bitince (sessizlik 1.2s veya 12s üst sınır) → stop gönder
```

## Kararlar
- **Wake word doğrulama: Whisper (STT tabanlı)**. openWakeWord/Porcupine gibi
  özel motorlar İngilizce optimize; Türkçe aksanla "Jarvis" için Whisper+fuzzy
  daha güvenilir. CPU maliyeti kabul edilebilir (VAD kapısı sayesinde saniyede
  en fazla 1 transcribe, konuşma yoksa sıfır).
- **Konuşma sonu algılama**: VAD sessizlik sayacı (1.2s) veya 12s üst sınır.
- **Koşu sırasında**: wake bekleme thread'i çalışmaya devam eder ama TTS çalarken
  kendi sesini duymasın diye playback sırasında mic penceresi kapanır
  (echo sorunu). Playback bitince 300ms sonra tekrar açılır.
- **Kapatma**: arayüzde "SÜREKLİ DİNLEME" toggle (default ON). Off → eski
  push-to-talk davranışı.

## Dosyalar
1. `jarvis_app.py` — WakeWordEngine sınıfı + MainWindow entegrasyonu + toggle UI
2. `systemd user unit` — server + app otomatik başlatma (isteğe bağlı)

## Doğrulama
1. Wake word testi: /tmp'ye kaydedilmiş "Jarvis" içerikli sesle engine ünitesi testi
2. Fuzzy match ünite testi (metin listesi)
3. Uygulama açıkken "Jarvis" deyince listening'e geçtiğinin gözlemlenmesi
