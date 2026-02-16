# backend/services/stt_service.py
# ─────────────────────────────────────────────────
# Freya STT — Dogrulandi (FAL-STT-DOCS.docx'ten):
#
# Endpoint: /audio/transcriptions (OpenAI uyumlu)
# Method: POST multipart/form-data
# Parametreler:
#   - file: ses dosyasi (zorunlu)
#   - model: "freya-stt-v1" (default)
#   - language: "tr"
#   - prompt: baglam ipucu (Whisper prompt)
#   - response_format: "json" | "text" | "verbose_json"
#   - temperature: 0.0 (deterministik) - 1.0
#
# KRITIK: "model" parametresini gondermemiz LAZIM!
# Onceden gondermiyorduk, bu yuzden yanlis algiliyordu.
# Ayrica temperature=0.0 cok daha tutarli sonuc verir.
# ─────────────────────────────────────────────────

import httpx, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import FAL_API_KEY, FAL_STT_URL


def build_stt_prompt(biz: dict = None) -> str:
    """
    Whisper prompt: modele "bu kelimeleri duyabilirsin" ipucu verir.
    Ozellikle Turkce'de benzer sesler icin kritik:
    - yarin vs iyi gunler
    - dis vs cin
    - dolgu vs dolu
    """
    words = [
        # Randevu kavramlari
        "randevu", "randevu almak istiyorum", "iptal", "değişiklik",
        # Zaman
        "yarın", "bugün", "öbür gün", "gelecek hafta", "bu hafta",
        "pazartesi", "salı", "çarşamba", "perşembe", "cuma", "cumartesi", "pazar",
        "saat", "sabah", "öğleden sonra", "akşam", "buçuk",
        "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz", "on",
        "on bir", "on iki","on üç", "on dört", "on beş", "on altı", "on yedi", "on sekiz", "on dokuz", "yirmi", 
        "yirmi bir", "yirmi iki", "yirmi üç", "yirmi dört",
        # Kisisel bilgi
        "adım", "soyadım", "telefon numaram", "numarası",
        "sıfır", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz",
        "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan",
        "yüz",
        # Onaylama
        "evet", "hayır", "tamam", "olur", "teşekkürler",
        # Selamlama
        "merhaba", "selam", "günaydın", "iyi günler", "hoşça kalın",
        "evet", "hayır", "tamam", "olur", "teşekkürler",
    ]
    
    # Isletmeye ozel kelimeler
    if biz:
        for s in biz.get("services", []):
            n = s.get("name", "")
            if n: words.append(n)
        for p in biz.get("staff", []):
            n = p.get("name", "")
            if n: words.append(n)
        sector = biz.get("sector", "")
        if sector: words.append(sector)
        bname = biz.get("name", "")
        if bname: words.append(bname)
    
    return ", ".join(words)


async def transcribe_audio(audio_bytes: bytes, filename: str = "audio.wav", business_config: dict = None) -> str:
    """Ses -> Yazi. Freya STT API dokumantasyonuna gore."""
    if not audio_bytes or len(audio_bytes) < 100:
        return ""
    
    # MIME type
    mime = "audio/wav"
    if filename.endswith(".webm"): mime = "audio/webm"
    elif filename.endswith(".mp3"): mime = "audio/mpeg"
    elif filename.endswith(".m4a"): mime = "audio/mp4"
    
    prompt = build_stt_prompt(business_config)
    
    print(f"[STT] {len(audio_bytes)} byte, mime={mime}")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                FAL_STT_URL,
                headers={"Authorization": f"Key {FAL_API_KEY}"},
                files={"file": (filename, audio_bytes, mime)},
                data={
                    "model": "freya-stt-v1",     # DOCS'ta var, onceden gondermiyorduk!
                    "language": "tr",
                    "prompt": prompt,             # Baglam ipucu
                    "response_format": "verbose_json",  # Daha detayli cevap
                    "temperature": "0.1",         # Deterministik = daha tutarli
                }
            )
            
            print(f"[STT] Status: {resp.status_code}")
            
            if resp.status_code != 200:
                print(f"[STT] Hata: {resp.text[:300]}")
                return ""
            
            data = resp.json()
            print(f"[STT] Raw: {json.dumps(data, ensure_ascii=False)[:400]}")
            
            text = _extract(data)
            print(f"[STT] Sonuc: \"{text}\"")
            return text
            
    except Exception as e:
        print(f"[STT] Hata: {e}")
        return ""


def _extract(data) -> str:
    """API cevabindan metin cikar"""
    if isinstance(data, str): return data.strip()
    if not isinstance(data, dict): return ""
    
    # Standart alanlar
    for k in ["text", "transcription", "transcript"]:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    
    # verbose_json formatinda segments olabilir
    segs = data.get("segments") or data.get("chunks") or []
    if segs:
        return " ".join(s.get("text", "").strip() for s in segs if isinstance(s, dict))
    
    return ""