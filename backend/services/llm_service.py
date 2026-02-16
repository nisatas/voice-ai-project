# backend/services/llm_service.py
# ─────────────────────────────────────────────────
# Prompt Optimizasyonu:
# - Kullanicinin detayli prompt'u ~800 token
# - Optimize edilmis versiyon ~350 token
# - Tum kritik kurallar korunuyor
# - Daha az token = daha hizli cevap
# ─────────────────────────────────────────────────

import httpx, sys, os, re
from datetime import datetime, timedelta, timezone
from typing import Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import FAL_API_KEY, FAL_LLM_URL, LLM_MODEL, LLM_MAX_TOKENS, LLM_TEMPERATURE

# Türkiye saati sabit: UTC+03 (Python 3.9 uyumlu)
TR_TZ = timezone(timedelta(hours=3))


def build_system_prompt(biz: dict) -> str:
    """
    Optimize edilmis system prompt.
    - Sesli konusma optimizasyonu (1-2 cumle)
    - Randevu akisi + guvenlik
    - En kritik EK: BUGUNUN TARIHI / SAATI (LLM'in "yarin" sapitmasini keser)
    - En kritik EK: RANDEVU formatini ZORUNLU kil
    - ✅ FIX: "Bugün istiyorum" ama işletme kapalıysa "müsait yok" demeden açıklayıp en yakın güne yönlendir
    """
    now = datetime.now(TR_TZ)
    today_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    weekday_tr = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"][now.weekday()]

    services = "\n".join([
        f"- {s['name']} ({s.get('duration',30)}dk, {s.get('price',0)}TL)"
        for s in biz.get("services", [])
    ]) or "- (tanımlı hizmet yok)"

    staff = "\n".join([
        f"- {s['name']} ({s.get('working_days','')}, {s.get('hours','')})"
        for s in biz.get("staff", [])
    ]) or "- (tanımlı personel yok)"

    campaigns = "\n".join([f"- {c}" for c in biz.get("campaigns", [])]) or "- (kampanya yok)"
    rules = "\n".join([f"- {r}" for r in biz.get("custom_rules", [])]) or "- (özel kural yok)"

    agent = biz.get("agent_name", "Asistan")
    name = biz.get("name", "")

    working_hours = biz.get("working_hours", "")

    return f"""Sen {name} sesli AI resepsiyonistsin. Adin {agent}. Telefonda gercek bir insan gibi sicak, samimi, profesyonel konus.

KONUSMA TARZI (COK ONEMLI):
- Gercek bir resepsiyonist gibi konus. Robotik olma.
- "Merhaba nasilsiniz?" derse: "Merhabalar, iyiyim tesekkur ederim! Siz nasilsiniz? Size nasil yardimci olabilirim?" gibi dogal cevap ver.
- Kisa ama sicak cevaplar ver. 1-2 cumle yeterli.
- Musteri sohbet ederse sohbet et, hemen randevuya zorla yonlendirme.
- Emoji ve markdown KULLANMA (sesli konusma).

ZAMAN (KESIN):
- Su an: {today_str} {time_str} ({weekday_tr})
- "bugun" = {today_str}, "yarin" = bugune +1 gun
- Tarih/saat UYDURMA. Emin degilsen netlestir.

CALISMA SAATLERI: {working_hours}
- Kapali gune randevu isterse: "O gun maalesef kapaliyiz, X gunu uygun olur mu?" de.
- Mesai disi saate: "O saat mesai disinda, Y saati nasil olur?" de.

RANDEVU AKISI (dogal konusma icinde):
1) Sicak karsilama, sohbet
2) Randevu isterse → hizmet sor
3) Doktor/personel tercihi sor
4) Gun/saat sor
5) Fiyat ve kampanya bilgisi ver
6) Onay al ("evet/tamam")
7) Ad soyad al
8) Telefon al
9) TELEFON ALINCA HEMEN RANDEVU SATIRINI YAZ (asagida)
10) Ardindan "Randevunuz olusturuldu" de.

!!! KRITIK - RANDEVU FORMATI !!!
Ad + Soyad + Telefon aldiktan sonra MUTLAKA bu satiri yaz:

RANDEVU: YYYY-MM-DD HH:MM | AD SOYAD | TELEFON

ORNEK (TAM BU FORMATTA):
RANDEVU: 2026-02-16 16:00 | Nuray Karakus | 05538521360

KURALLAR:
- Bu satiri YAZMADAN "Randevunuz olusturuldu" DEME
- Telefon numarasini tekrar etme, onaylama, DIREKT RANDEVU satirini yaz
- Format AYNEN bu sekilde olmali: RANDEVU: tarih saat | ad soyad | telefon

ISLETME: {name}
Sektor: {biz.get('sector','')}
Adres: {biz.get('address','')}
Tel: {biz.get('phone','')}

HIZMETLER:
{services}

PERSONEL:
{staff}

KAMPANYALAR:
{campaigns}

OZEL KURALLAR:
{rules}

GUVENLIK:
- Onay almadan randevu olusturma
- Mesai disi saate randevu verme
- Tibbi/uzman tavsiyesi verme
- Telefon numarasini tekrar ederek dogrula
"""


# Oturumlar
sessions: Dict[str, Dict[str, Any]] = {}
MAX_HISTORY = 16


def _dedupe_repeats(text: str) -> str:
    """
    LLM bazen aynı cümleyi/paragraph'ı iki kez döndürür.
    - ilk cümle tekrarını kırp
    - birebir tekrar eden satırları temizle
    """
    t = (text or "").strip()
    if not t:
        return t

    # Assistant: prefix temizliği burada da güvenli
    if t.startswith("Assistant:"):
        t = t[len("Assistant:"):].strip()

    # 1) İlk cümle tekrarını kırp (çok pratik, sesli tekrarı azaltır)
    parts = re.split(r"([.!?…])", t, maxsplit=1)
    if len(parts) >= 2:
        first_sentence = (parts[0] + parts[1]).strip()
        rest = t[len(first_sentence):].lstrip()
        if rest.startswith(first_sentence):
            rest = rest[len(first_sentence):].lstrip()
            t = (first_sentence + " " + rest).strip()

    # 2) Satır bazlı ardışık tekrarları temizle
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    cleaned = []
    for ln in lines:
        if not cleaned or cleaned[-1] != ln:
            cleaned.append(ln)
    t2 = "\n".join(cleaned).strip()

    # 3) Çok kısa cevaplarda "X X" gibi tekrar varsa
    t2 = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", t2, flags=re.IGNORECASE)

    return t2.strip()


async def chat(user_message: str, session_id: str, business_config: dict) -> str:
    if session_id not in sessions:
        sessions[session_id] = {"history": [], "business": business_config}

    sess = sessions[session_id]

    # business güncellenmiş olabilir → günceli yaz
    sess["business"] = business_config

    # user ekle
    sess["history"].append({"role": "user", "content": user_message})

    # trim
    if len(sess["history"]) > MAX_HISTORY:
        sess["history"] = sess["history"][-MAX_HISTORY:]

    system = build_system_prompt(sess["business"])

    # Prompt string (router model)
    parts = [f"System: {system}"]
    for msg in sess["history"]:
        r = "User" if msg["role"] == "user" else "Assistant"
        parts.append(f"{r}: {msg['content']}")
    parts.append("Assistant:")
    prompt_str = "\n".join(parts)

    print(f"[LLM] {LLM_MODEL}, {len(prompt_str)} char")

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                FAL_LLM_URL,
                headers={"Authorization": f"Key {FAL_API_KEY}", "Content-Type": "application/json"},
                json={
                    "prompt": prompt_str,
                    "model": LLM_MODEL,
                    "max_tokens": LLM_MAX_TOKENS,
                    "temperature": LLM_TEMPERATURE,
                    "stop": ["\nUser:", "\nUser", "User:", "\nSystem:", "System:"],
                },
            )

            if resp.status_code != 200:
                print(f"[LLM] Hata {resp.status_code}: {resp.text[:300]}")
                return "Bir sorun olustu, tekrar dener misiniz?"

            data = resp.json()
            msg = _extract(data)
            if not msg:
                return "Sizi tam anlayamadim, tekrar soyleyebilir misiniz?"

            msg = _dedupe_repeats(msg)

            # assistant ekle
            sess["history"].append({"role": "assistant", "content": msg})

            # trim tekrar (assistant ekledik)
            if len(sess["history"]) > MAX_HISTORY:
                sess["history"] = sess["history"][-MAX_HISTORY:]

            print(f"[LLM] -> \"{msg[:120]}\"")
            return msg

    except Exception as e:
        print(f"[LLM] Hata: {e}")
        return "Bir sorun olustu, tekrar dener misiniz?"


def _extract(data) -> str:
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return ""
    ch = data.get("choices")
    if ch and len(ch) > 0:
        c = ch[0]
        if "text" in c and isinstance(c["text"], str):
            return c["text"].strip()
        m = c.get("message", {})
        if isinstance(m, dict) and "content" in m:
            return m["content"].strip()
    for k in ["output", "text", "content", "response", "result", "generated_text"]:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def clear_history(session_id: str):
    if session_id in sessions:
        del sessions[session_id]