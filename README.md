# Voice AI — Otonom Sesli Resepsiyon Asistanı

Türkiye'nin ilk **Voice AI Hackathon**'unda, **Freya AI** ve **fal.ai** iş birliğiyle düzenlenen etkinlik kapsamında geliştirdiğimiz, 7/24 çalışan ve gerçek zamanlı takvim sorgulayıp randevu oluşturabilen **otonom yapay zekâ sesli resepsiyon asistanı** projesidir.

Ekip arkadaşlarım **Reyhan Nur Tanrıtanır** ve **Uğur Emir Azı** ile birlikte bu vizyoner etkinliği başarıyla tamamladık; hem teknik hem ürün vizyonu açısından oldukça öğretici ve heyecan verici bir deneyim yaşadık. Voice-first bir geleceğin mümkün olduğunu göstermeyi hedefledik.

---

## Özellikler

- **7/24 otonom çalışma** — Telefon veya mikrofon üzerinden gelen aramaları karşılar.
- **Gerçek zamanlı takvim sorgulama** — Kullanıcı ne isterse önce takvim sorgulanır, sonuca göre konuşulur.
- **Randevu oluşturma** — Sesli komutlarla takvimde randevu açma ve yönetme.
- **Halüsinasyon önleyici mimari** — LLM tahmin yapmak yerine **tool-based** çalışır: önce takvimi sorgular, sonuca göre yanıt verir; böylece yanlış veya uydurma bilgi üretimi engellenir.
- **Voice-first deneyim** — Tüm etkileşim ses üzerinden; metin arayüzü destekleyici rol oynar.

---

## Teknoloji ve Altyapı

| Bileşen | Teknoloji |
|--------|------------|
| **LLM** | Gemini (Gemini 3) |
| **Ses tanıma (STT)** | Freya STT |
| **Takvim** | Google Calendar API — gerçek zamanlı veri senkronizasyonu |
| **Sesli arama / telefony** | Telefon ve mikrofon girişi desteklenir |
| **Mimari** | Tool-based; LLM karar vermeden önce takvim araçları çağrılır |

Backend **FastAPI**, frontend statik HTML/JS ile sunulmaktadır; ses ve takvim entegrasyonu backend üzerinden yönetilir.

---

## Proje Yapısı

```
├── backend/          # FastAPI, LLM, STT/TTS, takvim servisleri
│   ├── services/     # calendar, llm, phone, stt, tts servisleri
│   └── ...
├── frontend/         # Admin ve chat arayüzleri
├── .env.example      # Gerekli ortam değişkenleri şablonu
└── README.md
```

---

## Kurulum

1. **Depoyu klonlayın**
   ```bash
   git clone https://github.com/nisatas/voice-ai-project.git
   cd voice-ai-project
   ```

2. **Ortam değişkenleri**  
   `.env.example` dosyasını `.env` olarak kopyalayın ve kendi API anahtarlarınızı / credential dosyalarınızı ekleyin.  
   **Bu dosya repoda yoktur ve paylaşılmamalıdır.**

3. **Backend**
   ```bash
   cd backend
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Google Calendar**  
   Google Cloud Console'dan servis hesabı oluşturup `*service-account*.json` dosyasını proje köküne veya backend dizinine koyun. Bu dosya da repoda yer almaz.

5. **Frontend**  
   Gerekirse `npm install` ile bağımlılıkları kurun; statik sayfalar doğrudan tarayıcıdan veya bir sunucu üzerinden açılabilir.

---

## Güvenlik

- **API anahtarları** ve **`.env`** dosyası `.gitignore` ile repoya eklenmez.
- **Google servis hesap JSON** dosyası (`*service-account*.json`) da `.gitignore`'dadır; kendi credential dosyanızı yalnızca yerel ortamda kullanın.

---

## Ekip ve Etkinlik

Bu proje, **Türkiye'nin ilk Voice AI Hackathon**'unda (Freya AI × fal.ai) geliştirilmiştir.  

**Ekip:** Nisa, Reyhan, Uğur  

Voice-first bir geleceğin mümkün olduğunu göstermeyi hedefleyen bu deneyimde, tool-based ve halüsinasyon önleyici mimari ile güvenilir bir sesli asistan deneyimi sunmayı amaçladık.
