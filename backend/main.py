import sys, os, uuid, base64, re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from services.stt_service import transcribe_audio
from services.llm_service import chat, clear_history
from services.tts_service import synthesize_speech

import time

PHONE_STATE = {}

def _get_state(session_id: str) -> dict:
    now = time.time()
    st = PHONE_STATE.get(session_id) or {"ts": now}
    if now - st.get("ts", now) > 2 * 60 * 60:
        st = {"ts": now}
    st["ts"] = now
    PHONE_STATE[session_id] = st
    return st

def _clear_state(session_id: str):
    PHONE_STATE.pop(session_id, None)


# ─────────────────────────────────────────
# TELEFON SERVİSİ (Twilio)
# ─────────────────────────────────────────
try:
    from services.phone_service import (
    create_welcome_twiml,
    create_response_twiml,
    make_reminder_call,
    format_phone_for_twilio,
    should_end_call,
    normalize_ambiguous_time,
    get_twilio_client,
    TWILIO_AVAILABLE,
    TWILIO_PHONE_NUMBER,
)


except ImportError:
    TWILIO_AVAILABLE = False
    TWILIO_PHONE_NUMBER = ""

from database import (
    create_business,
    get_business_by_slug,
    list_businesses,
    delete_business,
    get_available_slots,
    book_appointment,
)

# ✅ Calendar service import (list_calendars)
try:
    from services.calendar_service import list_calendars
except Exception:
    try:
        from calendar_service import list_calendars  # type: ignore
    except Exception:
        list_calendars = None  # type: ignore

TR_TZ = timezone(timedelta(hours=3))

# ─────────────────────────────────────────
# PHONE TTS AUDIO CACHE (Twilio <Play> needs a public URL)
# ─────────────────────────────────────────
_AUDIO_CACHE: Dict[str, Tuple[bytes, str, datetime]] = {}  # audio_id -> (bytes, media_type, expires_at)


def _cache_audio(data: bytes, media_type: str, ttl_seconds: int = 300) -> str:
    audio_id = uuid.uuid4().hex
    _AUDIO_CACHE[audio_id] = (data, media_type, datetime.now(TR_TZ) + timedelta(seconds=ttl_seconds))

    # opportunistic cleanup
    now = datetime.now(TR_TZ)
    for k, (_, __, exp) in list(_AUDIO_CACHE.items()):
        if exp <= now:
            _AUDIO_CACHE.pop(k, None)

    return audio_id


async def _tts_url_for_text(base_url: str, text: str) -> str:
    """
    Freya TTS üret -> cache -> /api/phone/audio/{id}
    """
    try:
        audio_bytes, fmt = await synthesize_speech(text)
        if not audio_bytes or len(audio_bytes) < 50:
            return ""
        media_type = "audio/mpeg" if (fmt or "").lower() == "mp3" else "audio/wav"
        audio_id = _cache_audio(audio_bytes, media_type, ttl_seconds=300)
        return f"{base_url}/api/phone/audio/{audio_id}"
    except Exception:
        return ""


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def _norm(s: str) -> str:
    return (s or "").strip()


def _lower(s: str) -> str:
    return _norm(s).lower()


def _has_booking_intent(text: str) -> bool:
    """
    ✅ Kullanıcı 'randevu' demese bile tarih+saat veriyorsa booking flow'a gir.
    """
    t = _lower(text)

    if any(k in t for k in ["randevu", "randev", "appointment", "rezervasyon", "müsait", "musait"]):
        return True

    # saat var mı? (17:00 / 17.00 / 17 00 / 17)
    has_time = (
        re.search(r"\b(\d{1,2})[:.](\d{2})\b", t) is not None
        or re.search(r"\b(\d{1,2})\s+(\d{2})\b", t) is not None
        or re.search(r"\bsaat\s+(\d{1,2})\b", t) is not None
        or ("beşe" in t or "bese" in t or re.search(r"\bsaat\s+beş\b", t) is not None)
    )

    # tarih var mı?
    has_date = (
        re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t) is not None
        or re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b", t) is not None
        or any(k in t for k in ["bugün", "yarın", "pazartesi", "salı", "sali", "çarşamba", "carsamba", "perşembe", "persembe", "cuma", "cumartesi", "pazar"])
        or re.search(r"\b(\d{1,2})\s*(\'?s[ıiuü]nda|\'?s[ıiuü])\b", t) is not None  # 16'sı / 16sında
        or re.search(r"\b(\d{1,2})\s+([a-zçğıöşü]+)(?:\s+(\d{4}))?\b", t) is not None  # 16 şubat
    )

    return has_time and has_date


def _has_approval(text: str) -> bool:
    t = _lower(text)
    return any(k in t for k in ["evet", "tamam", "onay", "onaylıyorum", "onayliyorum", "olur", "kabul"])


def _extract_phone(text: str) -> str:
    raw = re.sub(r"\s+", "", text or "")
    m = re.search(r"(0?\d{10,11})", raw)
    return m.group(1) if m else ""


def _extract_name(text: str) -> str:
    """
    Telefonda kullanıcı genelde direkt isim der.
    'ad soyad' veya 'ismim ' gibi varyasyonları da yakala.
    """
    t = (text or "").strip()
    if not t:
        return ""

    # 1) Etiketli formatlar: "ad: X", "isim: X", "ad soyad: X"
    m = re.search(r"\b(isim|ad(?:\s+soyad)?)\b\s*[:\-]?\s*(.+)$", t, flags=re.IGNORECASE)
    if m:
        cand = m.group(2).strip()
        cand = re.sub(r"\b(onaylıyorum|onayliyorum|onay|evet|tamam|olur|kabul)\b", "", cand, flags=re.IGNORECASE).strip()
        cand = re.sub(r"\d", " ", cand)
        cand = re.sub(r"\s+", " ", cand).strip()
        if len(cand.split()) >= 2:
            return cand[:80]

    # 2) Etiketsiz format: "Ayten Öz" gibi
    # - Telefon, sayılar, onay kelimeleri, "telefon numaram" gibi ifadeleri temizle
    cleaned = t
    cleaned = re.sub(r"\b(onaylıyorum|onayliyorum|onay|evet|tamam|olur|kabul)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(telefon|numara|no|tel)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\d", " ", cleaned)  # tüm rakamları at
    cleaned = re.sub(r"[^\wçğıöşüÇĞİÖŞÜ\s'-]", " ", cleaned)  # harf/boşluk/'/- dışını temizle
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    parts = [p for p in cleaned.split(" ") if len(p) >= 2]

    # "iphone" gibi saçma tokenları ele (istersen listeyi büyütürüz)
    blacklist = {"iphone", "android", "numaram", "benim", "adim", "ismim"}
    parts = [p for p in parts if p.lower() not in blacklist]

    if len(parts) >= 2:
        # en fazla 4 kelime al (Ad + Soyad + varsa 2 ek)
        return " ".join(parts[:4])[:80]

    return ""



def _time_from_words(text: str) -> Optional[str]:
    """
    'saat beşe' / 'beşe' gibi TR konuşma biçimlerini HH:MM'e çevir.
    Heuristik:
    - 'sabah' varsa 05:00
    - 'akşam' varsa 17:00
    - yoksa TR kullanımında 'beşe' çoğunlukla 17:00 → 17:00 kabul
    """
    t = _lower(text)
    if "beşe" in t or "bese" in t or re.search(r"\bsaat\s+beş\b", t):
        if "sabah" in t:
            return "05:00"
        if "akşam" in t or "aksam" in t:
            return "17:00"
        return "17:00"
    return None


def _extract_time_hhmm(text: str) -> Optional[str]:
    t = _lower(text).replace(".", ":")

    # 17:00
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    # 17 00
    m2 = re.search(r"\b(\d{1,2})\s+(\d{2})\b", t)
    if m2:
        hh, mm = int(m2.group(1)), int(m2.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    # "saat 17"
    m3 = re.search(r"\bsaat\s+(\d{1,2})\b", t)
    if m3:
        hh = int(m3.group(1))
        if 0 <= hh <= 23:
            return f"{hh:02d}:00"

    # "beşe"
    return _time_from_words(t)


def _extract_weekday(text: str) -> Optional[int]:
    t = _lower(text)
    wd = {
        "pazartesi": 0,
        "salı": 1, "sali": 1,
        "çarşamba": 2, "carsamba": 2, "çarş": 2, "cars": 2,
        "perşembe": 3, "persembe": 3,
        "cuma": 4,
        "cumartesi": 5,
        "pazar": 6,
    }
    for k, v in wd.items():
        if k in t:
            return v
    return None


def _extract_tr_date(text: str) -> Optional[str]:
    t = _lower(text)

    months = {
        "ocak": 1,
        "şubat": 2, "subat": 2,
        "mart": 3,
        "nisan": 4,
        "mayıs": 5, "mayis": 5,
        "haziran": 6,
        "temmuz": 7,
        "ağustos": 8, "agustos": 8,
        "eylül": 9, "eylul": 9,
        "ekim": 10,
        "kasım": 11, "kasim": 11,
        "aralık": 12, "aralik": 12,
    }

    m = re.search(r"\b(\d{1,2})\s+([a-zçğıöşü]+)(?:\s+(\d{4}))?\b", t, flags=re.IGNORECASE)
    if not m:
        return None

    d = int(m.group(1))
    mon_name = (m.group(2) or "").lower()
    y = m.group(3)

    if mon_name not in months:
        return None

    year = int(y) if y else datetime.now(TR_TZ).year
    month = months[mon_name]

    try:
        dt = datetime(year, month, d, tzinfo=TR_TZ)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def _fix_year_if_past(year: int) -> int:
    """
    STT bazen '16.02.22' gibi saçmalıyor (2022).
    Eğer yıl geçmişte kalıyorsa, bu yıla çek.
    """
    now_year = datetime.now(TR_TZ).year
    if year < now_year:
        return now_year
    return year


def _extract_date_yyyy_mm_dd(text: str) -> Optional[str]:
    t = _lower(text)

    # YYYY-MM-DD
    m1 = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m1:
        y, mo, d = int(m1.group(1)), int(m1.group(2)), int(m1.group(3))
        try:
            dt = datetime(y, mo, d, tzinfo=TR_TZ)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None

    # DD.MM.YYYY or DD.MM.YY
    m2 = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b", t)
    if m2:
        d, mo, y_raw = int(m2.group(1)), int(m2.group(2)), m2.group(3)
        y = int(y_raw)
        if y < 100:
            y = 2000 + y
        y = _fix_year_if_past(y)
        try:
            dt = datetime(y, mo, d, tzinfo=TR_TZ)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None

    # "ayın 16'sı" / "16'sı"
    m3 = re.search(r"\b(\d{1,2})\s*(\'?s[ıiuü]nda|\'?s[ıiuü])\b", t)
    if m3:
        d = int(m3.group(1))
        now = datetime.now(TR_TZ)
        try:
            dt = datetime(now.year, now.month, d, tzinfo=TR_TZ)
            # Eğer bu ayın 16'sı geçmişse bir sonraki aya kaydır
            if dt.date() < now.date():
                # next month
                nm = (now.month % 12) + 1
                ny = now.year + (1 if nm == 1 else 0)
                dt = datetime(ny, nm, d, tzinfo=TR_TZ)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None

    # TR ay isimleri
    return _extract_tr_date(t)


def _target_date(text: str) -> str:
    """
    Tarih çıkar + gün adı varsa tutarlılık kontrolü.
    mismatch olursa '__WEEKDAY_MISMATCH__YYYY-MM-DD'
    """
    t = _lower(text)
    now = datetime.now(TR_TZ)

    explicit = _extract_date_yyyy_mm_dd(t)
    if not explicit:
        wd_only = _extract_weekday(t)
        if wd_only is not None:
            days_ahead = (wd_only - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            explicit = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        elif "yarın" in t:
            explicit = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        elif "bugün" in t:
            explicit = now.strftime("%Y-%m-%d")
        else:
            return ""

    wd = _extract_weekday(t)
    if wd is not None:
        dt = datetime.strptime(explicit, "%Y-%m-%d").replace(tzinfo=TR_TZ)
        if dt.weekday() != wd:
            return "__WEEKDAY_MISMATCH__" + explicit

    return explicit


def _slots_set(slug: str, days: int = 7, slot_minutes: int = 30) -> Tuple[List[Dict[str, Any]], set]:
    slots = get_available_slots(slug, days=days, slot_minutes=slot_minutes) or []
    sset = set([s.get("slot_at") for s in slots if s.get("slot_at")])
    return slots, sset


def _suggest_top3(slots: List[Dict[str, Any]]) -> str:
    top3 = [s.get("display") for s in (slots or [])[:3] if s.get("display")]
    return ", ".join(top3) if top3 else "müsait saat bulamadım"

# ─────────────────────────────────────────
# BUSINESS CONTEXT HELPERS (SERVIS / DOKTOR / FIYAT)
# ─────────────────────────────────────────
def _biz_services(biz: dict) -> List[Dict[str, Any]]:
    sv = biz.get("services") or []
    return [s for s in sv if isinstance(s, dict) and (s.get("name") or "").strip()]


def _biz_staff(biz: dict) -> List[Dict[str, Any]]:
    st = biz.get("staff") or []
    return [p for p in st if isinstance(p, dict) and (p.get("name") or "").strip()]


def _match_by_name(user_text: str, items: List[Dict[str, Any]], key: str = "name") -> Optional[Dict[str, Any]]:
    t = _lower(user_text)
    if not t:
        return None
    for it in items:
        name = (it.get(key) or "").strip()
        if not name:
            continue
        n = _lower(name).replace(".", "").replace("  ", " ")
        tt = t.replace(".", "").replace("  ", " ")
        if n and n in tt:
            return it
    return None


def _user_says_no_preference(user_text: str) -> bool:
    t = _lower(user_text)
    return any(k in t for k in [
        "farketmez", "fark etmez", "önemli değil", "onemli degil", "yok", "rastgele", "herhangi", "kim olursa",
        "sen seç", "sen sec", "siz seç", "siz sec"
    ])


def _format_services_for_prompt(services: List[Dict[str, Any]]) -> str:
    lines = []
    for s in services:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        dur = int(s.get("duration") or 30)
        price = int(s.get("price") or 0)
        if price > 0:
            lines.append(f"- {name} ({dur} dakika, {price} TL)")
        else:
            lines.append(f"- {name} ({dur} dakika)")
    return "\n".join(lines) if lines else ""


def _allowed_weekdays_from_working_hours(wh: str) -> Optional[set]:
    """
    'Pzt-Cuma 12:00-19:00' gibi metinden çalışma günlerini çıkar.
    """
    t = _lower(wh)
    if not t:
        return None

    map_tok = {
        "pzt": 0, "pazartesi": 0,
        "sal": 1, "sali": 1, "salı": 1,
        "car": 2, "çar": 2, "cars": 2, "çarş": 2, "carsamba": 2, "çarşamba": 2,
        "per": 3, "persembe": 3, "perşembe": 3,
        "cum": 4, "cuma": 4,
        "cmt": 5, "cumartesi": 5,
        "paz": 6, "pazar": 6,
    }

    m = re.search(r"\b([a-zçğıöşü]{2,9})\s*-\s*([a-zçğıöşü]{2,9})\b", t)
    if not m:
        return None

    a = m.group(1).strip().lower()
    b = m.group(2).strip().lower()
    if a not in map_tok or b not in map_tok:
        return None

    wa, wb = map_tok[a], map_tok[b]
    allowed = set()
    cur = wa
    allowed.add(cur)
    while cur != wb:
        cur = (cur + 1) % 7
        allowed.add(cur)
        if len(allowed) > 7:
            break
    return allowed


def _is_open_on_date(working_hours: str, date_ymd: str) -> Optional[bool]:
    allowed = _allowed_weekdays_from_working_hours(working_hours or "")
    if allowed is None:
        return None
    try:
        dt = datetime.strptime(date_ymd, "%Y-%m-%d").replace(tzinfo=TR_TZ)
        return dt.weekday() in allowed
    except Exception:
        return None


# ─────────────────────────────────────────
# APP
# ─────────────────────────────────────────
app = FastAPI(title="RandevuSes API", version="2.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────
# PAGES
# ─────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home():
    fp = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "frontend",
        "admin.html",
    )
    with open(fp, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/chat/{slug}", response_class=HTMLResponse)
async def chat_page(slug: str):
    biz = get_business_by_slug(slug)
    if not biz:
        return HTMLResponse("<h1>İşletme bulunamadı</h1>", status_code=404)

    fp = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "frontend",
        "chat.html",
    )
    with open(fp, "r", encoding="utf-8") as f:
        html = f.read()
        html = html.replace("{{SLUG}}", biz["slug"])
        html = html.replace("{{BIZ_NAME}}", biz["name"])
        html = html.replace("{{AGENT_NAME}}", biz.get("agent_name", "Asistan"))
        html = html.replace("{{SECTOR}}", biz.get("sector", ""))
        return HTMLResponse(content=html)

# ─────────────────────────────────────────
# BUSINESS API
# ─────────────────────────────────────────
@app.post("/api/businesses")
async def api_create_business(request: Request):
    data = await request.json()
    biz = create_business(data)

    phone = (data.get("phone") or "").strip()
    if phone and TWILIO_AVAILABLE:
        base_url = _get_base_url(request)
        _configure_twilio_webhook(phone, base_url)

    return JSONResponse(biz)


@app.get("/api/businesses")
async def api_list_businesses():
    return JSONResponse(list_businesses())


@app.get("/api/businesses/{slug}")
async def api_get_business(slug: str):
    biz = get_business_by_slug(slug)
    if not biz:
        return JSONResponse({"error": "Bulunamadi"}, status_code=404)
    return JSONResponse(biz)


@app.delete("/api/businesses/{slug}")
async def api_delete_business(slug: str):
    delete_business(slug)
    return JSONResponse({"status": "ok"})

# ─────────────────────────────────────────
# CALENDAR API
# ─────────────────────────────────────────
@app.get("/api/calendars")
async def api_calendars():
    if not list_calendars:
        return JSONResponse({"error": "calendar_service list_calendars import edilemedi"}, status_code=500)

    items, err = list_calendars()
    if err:
        return JSONResponse({"error": err, "items": items}, status_code=400)

    return JSONResponse(items)


@app.get("/api/calendar/whoami")
async def api_calendar_whoami():
    try:
        from services.calendar_service import whoami
        return JSONResponse({"service_account": whoami()})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/calendars/add")
async def api_calendars_add(calendar_id: str):
    try:
        from services.calendar_service import add_calendar_to_list
        ok, err = add_calendar_to_list(calendar_id)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        return JSONResponse({"ok": True, "added": calendar_id})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ─────────────────────────────────────────
# SLOTS API
# ─────────────────────────────────────────
@app.get("/api/chat/{slug}/slots")
async def api_slots(slug: str, days: int = 7, slot_minutes: int = 30):
    slots = get_available_slots(slug, days=days, slot_minutes=slot_minutes)
    return JSONResponse(slots)



# ─────────────────────────────────────────
# TELEFON İÇİN: LLM RANDEVU FORMAT YAKALAYICI
# ─────────────────────────────────────────
# LLM prompt'ta bu formatı yazması öğretildi:
#   RANDEVU: 2026-02-16 14:30 | Uğur Emirazi | 05538521360
# Bu fonksiyon o satırı yakalar ve book_appointment ile kaydeder.
# Google Calendar kontrolü book_appointment içinde zaten yapılıyor.

async def _try_auto_book_from_llm(slug: str, session_id: str, ai_text: str) -> str:
    """LLM cevabında 'RANDEVU: ...' varsa otomatik randevu oluştur."""
    m = re.search(
        r"RANDEVU:\s*(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})\s*\|\s*(.+?)\s*\|\s*(\S+)",
        ai_text
    )
    if not m:
        return ai_text  # Format yok → normal cevap

    slot_at = m.group(1).strip()
    name = m.group(2).strip()
    phone = m.group(3).strip()

    print(f"[AUTO-BOOK] slot={slot_at} name={name} phone={phone}")

    try:
        booked = book_appointment(
            slug=slug, slot_at=slot_at,
            customer_name=name, customer_phone=phone,
            session_id=session_id, duration_minutes=30,
        )
        print(f"[AUTO-BOOK] ✅ OK: {booked}")
        # RANDEVU satırını cevaptan çıkar, temiz konuşma kısmını döndür
        clean = re.sub(r"RANDEVU:.*", "", ai_text).strip()
        if not clean:
            clean = f"Randevunuz oluşturuldu! {slot_at} tarihinde bekleriz. İyi günler!"
        return clean
    except Exception as e:
        print(f"[AUTO-BOOK] ❌ Hata: {e}")
        # Booking başarısızsa, RANDEVU satırını kaldır ama konuşmayı bozmadan devam et
        clean = re.sub(r"RANDEVU:.*", "", ai_text).strip()
        if not clean:
            return f"Maalesef o saat dolu görünüyor: {e}. Başka bir saat önerebilir misiniz?"
        return clean


# ─────────────────────────────────────────
# BOOKING CORE (STATE DESTEKLİ - TEMİZ HAL)
# ─────────────────────────────────────────
async def _handle_message_and_maybe_book(slug: str, session_id: str, user_text: str) -> str:
    biz = get_business_by_slug(slug)
    if not biz:
        return "İşletme bulunamadı."

    st = _get_state(session_id)

    # -------------------------------------------------------------
    # 0) Eğer state'te seçilmiş slot varsa:
    #    Kullanıcı açıkça tarih/saat değiştirmedikçe slotu KORU,
    #    sadece isim/telefon/onay topla.
    #    (telefon numarasındaki sayılar "saat" sanılmayacak)
    # -------------------------------------------------------------
    if st.get("chosen"):
        t = _lower(user_text)

        # SADECE "gerçek" saat/tarih değişikliği sinyalleri
        explicit_change = (
            ("saat" in t)
            or (re.search(r"\b(\d{1,2})[:.](\d{2})\b", t) is not None)      # 12:30 / 12.30
            or (re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t) is not None)  # 2026-02-16
            or (re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2,4})\b", t) is not None)  # 16.02.2026
            or any(k in t for k in ["bugün", "yarın", "pazartesi", "salı", "sali", "çarşamba", "carsamba",
                                    "perşembe", "persembe", "cuma", "cumartesi", "pazar"])
        )

        # ✅ Değişiklik yoksa: sadece isim/telefon/onay topla ve burada bitir
        if not explicit_change:
            chosen = st["chosen"]

            name = _extract_name(user_text)
            phone = _extract_phone(user_text)
            approved = _has_approval(user_text)

            if name:
                st["name"] = name
            if phone:
                st["phone"] = phone
            if approved:
                st["approved"] = True

            final_name = _norm(st.get("name", ""))
            final_phone = _norm(st.get("phone", ""))
            final_approved = bool(st.get("approved", False))

            if final_name and final_phone and final_approved:
                try:
                    booked = book_appointment(
                        slug=slug,
                        slot_at=chosen,
                        customer_name=final_name,
                        customer_phone=final_phone,
                        session_id=session_id,
                        duration_minutes=int(st.get("duration_minutes") or 30),
                        service_name=_norm(st.get("service_name","")),
                        staff_name=_norm(st.get("staff_name","")),
                        price_tl=int(st.get("price_tl") or 0),

                    )
                    _clear_state(session_id)
                    # ✅ Kapanışı garanti etsin (should_end_call tetikler)
                    return f"Randevunuz oluşturuldu. Tarih-saat: {booked['slot_at']}. Iyi gunler."
                except Exception as e:
                    return f"Randevu oluşturulamadı: {str(e)}"

            missing = []
            if not final_name:
                missing.append("ad soyad")
            if not final_phone:
                missing.append("telefon")
            if not final_approved:
                missing.append("onay (Evet/Tamam)")

            return f"Randevuyu tamamlamak için lütfen {', '.join(missing)} bilgilerini paylaşır mısınız?"

        # explicit_change varsa aşağı akışa düşer ve slotu yeniden seçer

    # -------------------------------------------------------------
    # 1) Booking intent yoksa normal sohbet
    # -------------------------------------------------------------
    # -------------------------------------------------------------
    # 1) Booking intent yoksa normal sohbet
    #    AMA: booking akışı başlamışsa (hizmet/doktor/fiyat/slot konuşuyorsak)
    #    kesinlikle LLM chat'e düşme!
    # -------------------------------------------------------------
    booking_in_progress = bool(
        st.get("booking_active")
        or st.get("chosen")
        or st.get("pending_request_text")
        or st.get("awaiting_service")
        or st.get("service_name")
        or st.get("staff_done")
        or st.get("pricing_confirmed")
    )


    if (not _has_booking_intent(user_text)) and (not booking_in_progress):
        return await chat(user_text, session_id, biz)


    # -------------------------------------------------------------
    # 2) Slot havuzunu çek
    # -------------------------------------------------------------
    slots, slot_set = _slots_set(slug, days=7, slot_minutes=30)

    effective_text = user_text
    if st.get("pending_request_text"):
        # kullanıcı son mesajda tarih/saat söylemiyorsa, ilk isteği baz al
        if not _target_date(user_text) and not _extract_time_hhmm(user_text):
            effective_text = st["pending_request_text"]

    date_ymd = _target_date(effective_text)
    ...
    time_hhmm = _extract_time_hhmm(effective_text)


    # ✅ KRITIK FIX: TR konuşma saat düzeltme (işletme 12:00 sonrası açıksa)
    # "saat 3" -> 15:00, "2.30" -> 14:30
    if time_hhmm:
        try:
            hh, mm = map(int, time_hhmm.split(":"))
            hh2, mm2, changed = normalize_ambiguous_time(hh, mm, biz.get("working_hours", ""))
            if changed:
                time_hhmm = f"{hh2:02d}:{mm2:02d}"
        except Exception:
            pass

    if not date_ymd:
        date_ymd = (datetime.now(TR_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

    requested_exact = f"{date_ymd} {time_hhmm}" if time_hhmm else ""

    # Kullanıcı net saat söylediyse ve yoksa: asla "oluşturuyorum" deme
    if requested_exact and (requested_exact not in slot_set):
        day_slots = [s for s in (slots or []) if (s.get("slot_at", "") or "").startswith(date_ymd)]
        top_day = _suggest_top3(day_slots)
        top_any = _suggest_top3(slots)

        if not day_slots:
            return f"{date_ymd} için uygun saat yok. En yakın müsait saatler: {top_any}. Hangisi uygun?"
        return f"{date_ymd} {time_hhmm} dolu görünüyor. Aynı gün müsait saatler: {top_day}. Hangisini istersiniz?"

        # -------------------------------------------------------------
    # 1.5) HIZMET → DOKTOR → UCRET/ONAY (slot seçmeden önce)
    # -------------------------------------------------------------
    services = _biz_services(biz)
    staff_list = _biz_staff(biz)

    # ✅ Kullanıcı ilk mesajda tarih söylediğinde kaybetme
    if services and not st.get("pending_request_text"):
        if _target_date(user_text) or _extract_time_hhmm(user_text):
            st["pending_request_text"] = user_text

    # 1) Hizmet seçimi
    if services and not st.get("service_name"):
        picked = _match_by_name(user_text, services, key="name")
        if picked:
            st["service_name"] = (picked.get("name") or "").strip()
            st["duration_minutes"] = int(picked.get("duration") or 30)
            st["price_tl"] = int(picked.get("price") or 0)
            st["booking_active"] = True
        else:
            svc_txt = _format_services_for_prompt(services)

            # ✅ KRİTİK: booking state’i BAŞLAT (yoksa 2. mesajda LLM'e düşersin)
            st["booking_active"] = True
            st["awaiting_service"] = True
            st["pending_request_text"] = st.get("pending_request_text") or user_text

            if svc_txt:
                return "Hangi hizmeti almak istersiniz?\n" + svc_txt
            return "Hangi hizmeti almak istersiniz?"

    # 2) Doktor/personel tercihi
    if staff_list and not st.get("staff_done"):
        # service seçildiyse buraya gelir
        st["booking_active"] = True
        st["awaiting_service"] = False

        p = _match_by_name(user_text, staff_list, key="name")
        if p:
            st["staff_name"] = (p.get("name") or "").strip()
            st["staff_done"] = True
        elif _user_says_no_preference(user_text):
            st["staff_name"] = ""
            st["staff_done"] = True
        else:
            names = ", ".join([(p.get("name") or "").strip() for p in staff_list][:6])
            return f"Personel tercihiniz var mı? Yoksa fark etmez mi?"

    # 3) Ücret + onay
    if st.get("service_name") and not st.get("pricing_confirmed"):
        st["booking_active"] = True
        service_name = _norm(st.get("service_name", ""))
        dur = int(st.get("duration_minutes") or 30)
        price = int(st.get("price_tl") or 0)

        if _has_approval(user_text):
            st["pricing_confirmed"] = True
        else:
            if price > 0:
                return f"Seçtiğiniz hizmet: {service_name} ({dur} dakika) — Ücret: {price} TL. Devam edelim mi? evet diyerek onaylayabilirsiniz."
            return f"Seçtiğiniz hizmet: {service_name} ({dur} dakika). Devam edelim mi? evet diyerek onaylayabilirsiniz."

    # -------------------------------------------------------------
    # 3) Slot seçimi
    #    - Kullanıcı SAAT söylemişse: o saati dene
    #    - Saat söylememişse: otomatik seçme, 3 seçenek sun ve sor
    # -------------------------------------------------------------
    chosen = None

    day_slots = [s for s in (slots or []) if (s.get("slot_at", "") or "").startswith(date_ymd)]
    day_slots_sorted = sorted([s.get("slot_at") for s in day_slots if s.get("slot_at")])

    if time_hhmm:
        if requested_exact in slot_set:
            chosen = requested_exact
        else:
            top_day = _suggest_top3(day_slots)
            if not day_slots:
                return f"{date_ymd} için uygun saat yok. En yakın müsait saatler: {_suggest_top3(slots)}. Hangisi uygun?"
            return f"{date_ymd} {time_hhmm} dolu görünüyor. Aynı gün müsait saatler: {top_day}. Hangisini istersiniz?"
    else:
        # ✅ Saat yok → otomatik seçme
        if not day_slots_sorted:
            return f"{date_ymd} için uygun saat yok. En yakın müsait saatler: {_suggest_top3(slots)}. Hangi günü istersiniz?"
        top3 = ", ".join([s.get("display") for s in day_slots[:3] if s.get("display")]) or _suggest_top3(day_slots)
        st["awaiting_time"] = True
        return f"{date_ymd} için hangi saat uygun?"


    # -------------------------------------------------------------
    # 4) İlk turda da bilgileri yakalamaya çalış
    # -------------------------------------------------------------
    name = _extract_name(user_text)
    phone = _extract_phone(user_text)
    approved = _has_approval(user_text)

    if name:
        st["name"] = name
    if phone:
        st["phone"] = phone
    if approved:
        st["approved"] = True

    final_name = _norm(st.get("name", ""))
    final_phone = _norm(st.get("phone", ""))
    final_approved = bool(st.get("approved", False))

    if final_name and final_phone and final_approved:
        try:
            booked = book_appointment(
                slug=slug,
                slot_at=chosen,
                customer_name=final_name,
                customer_phone=final_phone,
                session_id=session_id,
                duration_minutes=int(st.get("duration_minutes") or 30),
                service_name=_norm(st.get("service_name","")),
                staff_name=_norm(st.get("staff_name","")),
                price_tl=int(st.get("price_tl") or 0),

            )
            _clear_state(session_id)
            # ✅ Kapanışı garanti etsin (should_end_call tetikler)
            return f"Randevunuz oluşturuldu. Tarih-saat: {booked['slot_at']}. Iyi gunler."
        except Exception as e:
            return f"Randevu oluşturulamadı: {str(e)}"

    return "Randevuyu tamamlamak için lütfen ad soyad, telefon ve onay (Evet/Onaylıyorum) bilgilerini paylaşır mısınız?"

# ─────────────────────────────────────────
# VOICE CHAT (web upload)
# ─────────────────────────────────────────
@app.post("/api/chat/{slug}")
async def voice_chat(
    slug: str,
    audio: UploadFile = File(...),
    session_id: str = Form(default=None),
):
    biz = get_business_by_slug(slug)
    if not biz:
        return JSONResponse({"error": "Isletme bulunamadi"}, status_code=404)

    if not session_id:
        session_id = str(uuid.uuid4())
        print(f"\n{'='*40}\nYENI: {biz['name']} ({session_id[:8]})\n{'='*40}")

    try:
        audio_bytes = await audio.read()
        user_text = await transcribe_audio(audio_bytes, audio.filename or "audio.wav", biz)

        if not user_text or not user_text.strip():
            return JSONResponse({
                "session_id": session_id,
                "user_text": "",
                "ai_text": "Sizi tam duyamadım, tekrar söyleyebilir misiniz?",
                "ai_audio": "",
                "audio_format": "wav",
            })

        ai_response = await _handle_message_and_maybe_book(slug, session_id, user_text)

        audio_response, audio_fmt = await synthesize_speech(ai_response)
        audio_b64 = ""
        if audio_response and len(audio_response) > 100:
            audio_b64 = base64.b64encode(audio_response).decode("utf-8")

        return JSONResponse({
            "session_id": session_id,
            "user_text": user_text,
            "ai_text": ai_response,
            "ai_audio": audio_b64,
            "audio_format": audio_fmt,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})


# ─────────────────────────────────────────
# TEXT CHAT (form)
# ─────────────────────────────────────────
@app.post("/api/chat-text/{slug}")
async def text_chat(
    slug: str,
    message: str = Form(...),
    session_id: str = Form(default="default"),
):
    biz = get_business_by_slug(slug)
    if not biz:
        return JSONResponse({"error": "Isletme bulunamadi"}, status_code=404)

    try:
        ai_response = await _handle_message_and_maybe_book(slug, session_id, message)

        audio_response, audio_fmt = await synthesize_speech(ai_response)
        audio_b64 = ""
        if audio_response and len(audio_response) > 100:
            audio_b64 = base64.b64encode(audio_response).decode("utf-8")

        return JSONResponse({
            "session_id": session_id,
            "user_text": message,
            "ai_text": ai_response,
            "ai_audio": audio_b64,
            "audio_format": audio_fmt,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/reset")
async def reset(session_id: str = Form(default="default")):
    clear_history(session_id)
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "twilio": TWILIO_AVAILABLE,
        "twilio_phone": TWILIO_PHONE_NUMBER or "(ayarlanmamış)",
    }


# ═══════════════════════════════════════════════════════════════════
#                   TELEFON ENDPOINT'LERİ (Twilio)
# ═══════════════════════════════════════════════════════════════════
def _find_business_by_twilio_number(called_number: str):
    businesses = list_businesses()
    clean = re.sub(r"[^\d]", "", called_number or "")

    for biz in businesses:
        biz_phone = re.sub(r"[^\d]", "", biz.get("phone", "") or "")
        if biz_phone and len(biz_phone) >= 10 and len(clean) >= 10:
            if clean[-10:] == biz_phone[-10:]:
                return biz

    return businesses[0] if businesses else None


def _configure_twilio_webhook(twilio_number: str, base_url: str):
    client = get_twilio_client()
    if not client:
        print("[TWILIO] Client yok, webhook ayarlanamadı")
        return

    clean = re.sub(r"[^\d+]", "", twilio_number)
    if not clean.startswith("+"):
        clean = "+" + clean

    try:
        numbers = client.incoming_phone_numbers.list(phone_number=clean)
        if not numbers:
            print(f"[TWILIO] Numara bulunamadı: {clean}")
            return

        webhook_url = f"{base_url}/api/phone/incoming"

        for num in numbers:
            num.update(
                voice_url=webhook_url,
                voice_method="POST",
            )
            print(f"[TWILIO] ✅ Webhook ayarlandı: {clean} → {webhook_url}")

    except Exception as e:
        print(f"[TWILIO] Webhook hatası: {e}")


def _get_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", "http")
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host", "localhost:8000")
    )
    return f"{proto}://{host}"


@app.get("/api/phone/test")
async def phone_test():
    businesses = list_businesses()
    return {
        "status": "ok",
        "twilio_available": TWILIO_AVAILABLE,
        "twilio_phone": TWILIO_PHONE_NUMBER or "(yok)",
        "businesses_count": len(businesses),
        "businesses": [{"name": b["name"], "slug": b["slug"], "phone": b.get("phone","")} for b in businesses[:5]],
        "message": "Bu sayfayı görüyorsan webhook çalışıyor!"
    }


@app.get("/api/phone/audio/{audio_id}")
async def phone_audio(audio_id: str):
    item = _AUDIO_CACHE.get(audio_id)
    if not item:
        return Response(status_code=404)

    data, media_type, exp = item
    if exp <= datetime.now(TR_TZ):
        _AUDIO_CACHE.pop(audio_id, None)
        return Response(status_code=404)

    return Response(content=data, media_type=media_type)


@app.post("/api/phone/incoming")
async def phone_incoming(request: Request):
    try:
        form = await request.form()
        call_sid = form.get("CallSid", "")
        from_number = form.get("From", "")
        to_number = form.get("To", "")

        print(f"\n{'='*50}")
        print(f"  GELEN ARAMA!")
        print(f"  Arayan: {from_number}")
        print(f"  Aranan: {to_number}")
        print(f"  CallSid: {call_sid}")
        print(f"{'='*50}")

        biz = _find_business_by_twilio_number(to_number)

        if not biz:
            twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" language="tr-TR" speechTimeout="auto" timeout="10" action="/api/phone/gather" method="POST">
        <Say language="tr-TR">Merhaba, hos geldiniz. Size nasil yardimci olabilirim?</Say>
    </Gather>
    <Hangup/>
</Response>"""
            return Response(content=twiml, media_type="application/xml")

        base_url = _get_base_url(request)

        agent = (biz.get("agent_name") or "Asistan")
        biz_name = (biz.get("name") or "")
        welcome_text = f"Merhaba, {biz_name} hoş geldiniz. Ben {agent}. Size nasıl yardımcı olabilirim?"
        welcome_audio_url = await _tts_url_for_text(base_url, welcome_text)

        twiml = create_welcome_twiml(biz, base_url, welcome_audio_url)
        return Response(content=twiml, media_type="application/xml")

    except Exception:
        fallback = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="tr-TR">Merhaba, bir teknik sorun var. Lutfen daha sonra tekrar arayiniz.</Say>
    <Hangup/>
</Response>"""
        return Response(content=fallback, media_type="application/xml")


@app.post("/api/phone/gather")
async def phone_gather(
    request: Request,
    slug: str = "",
    session_id: str = "",
):
    """
    ═══ TELEFON KONUŞMA AKIŞI ═══
    
    ÖNCEKİ SORUNLAR:
    1) _handle_message_and_maybe_book() HER mesajda Google Calendar çağırıyordu → latency
    2) "randevu" kelimesini duyunca hemen slot arıyordu → müşteri gün söylemeden "müsait yok"
    3) Tarih parse mantığı konuşma bağlamını bilmiyordu → "14:30 boş ama müsait yok"
    4) Robotik cevaplar veriyordu → telesekreter hissi
    
    YENİ YAKLAŞIM:
    - Sade chat() (LLM) kullan → doğal, sıcak konuşma
    - LLM prompt'ta randevu akışını biliyor (hizmet→tarih→saat→onay→ad+tel)
    - LLM RANDEVU: formatı yazınca _try_auto_book_from_llm() otomatik book eder
    - Google Calendar SADECE booking anında çağrılır (her turda değil)
    
    Freya TTS + <Play> KALIYOR.
    """
    try:
        form = await request.form()
        speech_result = form.get("SpeechResult", "")
        confidence = form.get("Confidence", "")
        call_sid = form.get("CallSid", "")

        if not session_id:
            session_id = f"phone-{call_sid}" if call_sid else str(uuid.uuid4())

        print(f'\n  🎤 Musteri: "{speech_result}" (guven: {confidence})')

        base_url = _get_base_url(request)

        # Müşteri konuşmadı
        if not speech_result or not (speech_result or "").strip():
            ai_text = "Sizi tam duyamadım, tekrar söyleyebilir misiniz?"
            audio_url = await _tts_url_for_text(base_url, ai_text)
            twiml = create_response_twiml(ai_text, slug, session_id, base_url, end_call=False, audio_url=audio_url)
            return Response(content=twiml, media_type="application/xml")

        # İşletmeyi bul
        biz = get_business_by_slug(slug) if slug else None
        if not biz:
            biz = _find_business_by_twilio_number(form.get("To", ""))
        if not biz:
            biz = _find_business_by_twilio_number("")
        if not biz:
            twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Say language="tr-TR">Bir sorun oluştu.</Say><Hangup/></Response>'
            return Response(content=twiml, media_type="application/xml")

        effective_slug = slug or biz["slug"]

        # ═══ SADE LLM CHAT ═══
        # LLM doğal konuşma yapar, prompt'ta randevu akışını biliyor.
        # "Merhaba nasılsınız" → sıcak cevap verir
        # "Randevu istiyorum" → hizmet sorar, sonra gün/saat sorar
        # Tüm bilgiler tamam olunca RANDEVU: formatı yazar
        ai_response = await chat(speech_result, session_id, biz)

        # LLM "RANDEVU: 2026-02-16 14:30 | Ad Soyad | 05551234567" yazdıysa → auto book
        ai_response = await _try_auto_book_from_llm(effective_slug, session_id, ai_response)

        print(f'  🤖 AI: "{ai_response}"')

        # ═══ FREYA TTS + <Play> ═══
        audio_url = await _tts_url_for_text(base_url, ai_response)
        end_call = should_end_call(ai_response)

        twiml = create_response_twiml(ai_response, effective_slug, session_id, base_url, end_call, audio_url=audio_url)
        return Response(content=twiml, media_type="application/xml")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print("PHONE_GATHER ERROR:", str(e))
        fallback = '<?xml version="1.0" encoding="UTF-8"?><Response><Say language="tr-TR">Bir teknik sorun oluştu. Lütfen tekrar arayınız.</Say><Hangup/></Response>'
        return Response(content=fallback, media_type="application/xml")


@app.post("/api/phone/reminder-response")
async def phone_reminder_response(request: Request):
    form = await request.form()
    speech_result = form.get("SpeechResult", "")

    lower = (speech_result or "").lower()

    if any(w in lower for w in ["evet", "geleceğim", "geliyorum", "tamam", "olur"]):
        text = "Harika, randevunuzu teyit ettim. Görüşmek üzere, iyi günler!"
    elif any(w in lower for w in ["hayır", "gelemiyorum", "iptal", "olmaz"]):
        text = "Anladım. İptal veya değişiklik için bizi arayabilirsiniz. İyi günler!"
    else:
        text = "Randevunuz geçerli olarak kalacaktır. Değişiklik için bizi arayabilirsiniz. İyi günler!"

    twiml = f'<Response><Say language="tr-TR">{text}</Say></Response>'
    return Response(content=twiml, media_type="application/xml")


@app.post("/api/phone/test-call")
async def phone_test_call(request: Request):
    data = await request.json()
    phone = data.get("phone", "")
    slug = data.get("slug", "")

    if not phone:
        return JSONResponse({"error": "Telefon numarası gerekli"}, status_code=400)

    if not TWILIO_AVAILABLE:
        return JSONResponse({"error": "Twilio yüklü değil. pip install twilio"}, status_code=500)

    client = get_twilio_client()
    if not client:
        return JSONResponse(
            {"error": "Twilio yapılandırılmamış. .env dosyasında TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER olmalı."},
            status_code=500
        )

    biz = get_business_by_slug(slug) if slug else None
    if not biz:
        businesses = list_businesses()
        biz = businesses[0] if businesses else None
    if not biz:
        return JSONResponse({"error": "İşletme bulunamadı"}, status_code=404)

    base_url = _get_base_url(request)
    formatted_phone = format_phone_for_twilio(phone)

    try:
        agent = biz.get("agent_name", "Asistan")
        biz_name = biz.get("name", "")
        welcome = f"Merhaba, ben {biz_name}'den {agent}. Size nasıl yardımcı olabilirim?"

        welcome_audio_url = await _tts_url_for_text(base_url, welcome)
        play_or_say = f"<Play>{welcome_audio_url}</Play>" if welcome_audio_url else f'<Say language="tr-TR">{welcome}</Say>'

        twiml_str = f"""<Response>
    <Gather input="speech" language="tr-TR" speechTimeout="auto" timeout="10"
            action="{base_url}/api/phone/gather?slug={biz['slug']}" method="POST">
        {play_or_say}
    </Gather>
    <Say language="tr-TR">Sizi duyamadım. Daha sonra tekrar arayacağız.</Say>
</Response>"""

        call = client.calls.create(
            to=formatted_phone,
            from_=TWILIO_PHONE_NUMBER,
            twiml=twiml_str,
        )

        return JSONResponse({
            "status": "ok",
            "call_sid": call.sid,
            "to": formatted_phone,
            "from": TWILIO_PHONE_NUMBER,
            "message": f"Arama başlatıldı → {formatted_phone}",
        })

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════════════
#               RANDEVU HATIRLATMA ZAMANLAYICISI
# ═══════════════════════════════════════════════════════════════════
import asyncio

async def reminder_scheduler():
    print("[REMINDER] Hatırlatma zamanlayıcısı başlatıldı")

    while True:
        await asyncio.sleep(1800)

        if not TWILIO_AVAILABLE:
            continue

        try:
            import sqlite3
            from config import DB_PATH

            now = datetime.now(TR_TZ)
            four_hours = now + timedelta(hours=4)

            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row

            try:
                conn.execute("ALTER TABLE appointments ADD COLUMN reminded INTEGER DEFAULT 0")
                conn.commit()
            except Exception:
                pass

            rows = conn.execute("""
                SELECT a.*, b.name as business_name
                FROM appointments a
                JOIN businesses b ON b.slug = a.business_slug
                WHERE a.slot_at >= ? AND a.slot_at <= ?
                AND a.customer_phone != ''
                AND (a.reminded IS NULL OR a.reminded = 0)
            """, (
                now.strftime("%Y-%m-%d %H:%M"),
                four_hours.strftime("%Y-%m-%d %H:%M"),
            )).fetchall()

            for row in rows:
                phone = format_phone_for_twilio(row["customer_phone"])
                slot_at = row["slot_at"]
                time_str = slot_at.split(" ")[1] if " " in slot_at else slot_at

                success = make_reminder_call(
                    to_phone=phone,
                    customer_name=row["customer_name"],
                    appointment_time=time_str,
                    service_name="",
                    business_name=row["business_name"],
                    base_url="",  # prod'da ngrok/base_url ver
                )

                if success:
                    conn.execute(
                        "UPDATE appointments SET reminded = 1 WHERE id = ?",
                        (row["id"],)
                    )
                    conn.commit()

            conn.close()

        except Exception as e:
            print(f"[REMINDER] Hata: {e}")


@app.on_event("startup")
async def startup_tasks():
    print("\n" + "=" * 50)
    print("  RandevuSes v2.1 + Telefon Entegrasyonu")
    print("=" * 50)
    print(f"  Admin Panel:  http://localhost:8000")
    print(f"  API Docs:     http://localhost:8000/docs")
    print(f"  Twilio:       {'✅ AKTIF' if TWILIO_AVAILABLE else '❌ PASIF (pip install twilio)'}")
    if TWILIO_PHONE_NUMBER:
        print(f"  Tel Numara:   {TWILIO_PHONE_NUMBER}")
    else:
        print("  Tel Numara:   (ayarlanmamış)")
    print("=" * 50 + "\n")

    asyncio.create_task(reminder_scheduler())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)