# backend/services/phone_service.py
# ═══════════════════════════════════════════════════════════════
# TWILIO TELEFON SERVİSİ v2 (FIXED)
#
# FIX:
# - create_response_twiml içinde 2. Gather + ek Say'leri kaldırdım.
#   Tek tur = tek Gather. Yoksa Twilio aynı turda tekrar konuşturuyor.
# - Play/Say zaten tek seçiliyor (audio_url varsa Play, yoksa Say).
# - action_url her zaman session_id taşır (state bozulmasın).
# ═══════════════════════════════════════════════════════════════

import os, sys, html as html_lib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from twilio.rest import Client as TwilioClient
    TWILIO_SDK_AVAILABLE = True
except ImportError:
    TWILIO_SDK_AVAILABLE = False
    print("[PHONE] twilio SDK yok (giden arama çalışmaz). pip install twilio")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

TWILIO_AVAILABLE = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER)

_twilio_client = None

def get_twilio_client():
    global _twilio_client
    if _twilio_client is None and TWILIO_SDK_AVAILABLE and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        _twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client


def _esc(text: str) -> str:
    return html_lib.escape(text or "", quote=True)


def create_welcome_twiml(business_config: dict, base_url: str, welcome_audio_url: str = "", session_id: str = "") -> str:
    agent = _esc(business_config.get("agent_name", "Asistan"))
    biz_name = _esc(business_config.get("name", ""))
    slug = business_config.get("slug", "")

    welcome = f"Merhaba, {biz_name} hoş geldiniz. Ben {agent}. Size nasıl yardımcı olabilirim?"

    # session_id varsa action'a koy (state aynı kalsın)
    if session_id:
        action_url = f"{base_url}/api/phone/gather?slug={slug}&session_id={_esc(session_id)}"
    else:
        action_url = f"{base_url}/api/phone/gather?slug={slug}"

    play_or_say = (
        f"<Play>{_esc(welcome_audio_url)}</Play>"
        if (welcome_audio_url or "").startswith("http")
        else f"<Say language=\"tr-TR\">{_esc(welcome)}</Say>"
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" language="tr-TR" speechTimeout="auto" timeout="10" action="{_esc(action_url)}" method="POST">
        {play_or_say}
    </Gather>
    <Say language="tr-TR">Sizi duyamadım. Tekrar arayabilirsiniz. Hoşça kalın.</Say>
    <Hangup/>
</Response>"""


def create_response_twiml(ai_text: str, slug: str, session_id: str, base_url: str, end_call: bool = False, audio_url: str = "") -> str:
    """
    ✅ TEK TUR = TEK Gather
    """
    safe_text = _esc(ai_text)

    # session_id her zaman aksın
    action_url = _esc(f"{base_url}/api/phone/gather?slug={slug}&session_id={session_id}")

    play_or_say = (
        f"<Play>{_esc(audio_url)}</Play>"
        if (audio_url or "").startswith("http")
        else f"<Say language=\"tr-TR\">{safe_text}</Say>"
    )

    if end_call:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {play_or_say}
    <Hangup/>
</Response>"""

    # Normal: cevabı söyle, sonra tekrar dinle
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" language="tr-TR" speechTimeout="auto" timeout="10" action="{action_url}" method="POST">
        {play_or_say}
    </Gather>
    <Say language="tr-TR">Sizi duyamadım. Lütfen tekrar arayın.</Say>
    <Hangup/>
</Response>"""


def make_reminder_call(to_phone, customer_name, appointment_time, service_name, business_name, base_url):
    client = get_twilio_client()
    if not client:
        print("[PHONE] Twilio client yok")
        return False
    try:
        text = _esc(f"Merhaba {customer_name}. {business_name} arıyor. Bugün saat {appointment_time} randevunuz var. Gelecek misiniz?")
        twiml = f"""<Response>
    <Gather input="speech" language="tr-TR" timeout="8" action="{_esc(base_url)}/api/phone/reminder-response" method="POST">
        <Say language="tr-TR">{text}</Say>
    </Gather>
    <Say language="tr-TR">Cevabinizi alamadim. Randevunuz gecerli kalacaktir. Iyi gunler!</Say>
</Response>"""
        call = client.calls.create(to=to_phone, from_=TWILIO_PHONE_NUMBER, twiml=twiml)
        print(f"[PHONE] Hatırlatma: {to_phone} (SID: {call.sid})")
        return True
    except Exception as e:
        print(f"[PHONE] Arama hatası: {e}")
        return False


def format_phone_for_twilio(phone: str) -> str:
    phone = (phone or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("+"):
        return phone
    if phone.startswith("0"):
        return "+9" + phone
    if len(phone) == 10:
        return "+90" + phone
    return "+90" + phone


import re
from typing import Optional, Tuple

def _parse_working_hours_range(working_hours: str) -> Tuple[Optional[int], Optional[int]]:
    """
    "Pzt-Cuma 12:00-19:00" gibi stringten start_hour, end_hour alır.
    """
    if not working_hours:
        return None, None
    m = re.search(r"(\d{1,2})[:.]\d{2}\s*-\s*(\d{1,2})[:.]\d{2}", working_hours)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def normalize_ambiguous_time(hour: int, minute: int, working_hours: str) -> Tuple[int, int, bool]:
    """
    Türkçe konuşmada '2 buçuk' çoğu zaman 14:30 demektir.
    Kural: İşletme başlangıç saati >= 11 ise ve kullanıcı saati 1..7 verdiyse +12 uygula.
    (02:30 -> 14:30)
    Returns: (new_hour, new_minute, changed?)
    """
    start_h, end_h = _parse_working_hours_range(working_hours)

    # çalışma saatini okuyamadıysak dokunma
    if start_h is None:
        return hour, minute, False

    # işletme öğlen/öğleden sonra açılıyorsa ve saat çok erken gelmişse
    if start_h >= 11 and 1 <= hour <= 7:
        return hour + 12, minute, True

    return hour, minute, False


import re

def should_end_call(ai_text: str) -> bool:
    lower = (ai_text or "").lower()

    # 1) Backend final mesajı (senin logdaki gibi) -> kesin kapat


    # 2) RANDEVU satiri varsa -> kesin kapat
    if re.search(r"\brand(e)?vu:\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\b", lower):
        return True

    endings = ["iyi günler","iyi gunler","hoşça kal","hosca kal","görüşmek üzere","gorusmek uzere","görüşürüz","gorusuruz","güle güle","gule gule"]
    return any(e in lower for e in endings)

