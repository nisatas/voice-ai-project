# backend/services/calendar_service.py
# Google Calendar entegrasyonu — müsaitlik (freebusy) + randevu kaydı (event insert)

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import GOOGLE_CALENDAR_CREDENTIALS_PATH, DEFAULT_GOOGLE_CALENDAR_ID  # noqa: F401

_SERVICE = None

# Python 3.9 uyumlu TR timezone (UTC+03:00)
TR_TZ = timezone(timedelta(hours=3))
TR_TZ_NAME = "Europe/Istanbul"


def resolve_credentials_path() -> Optional[str]:
    """
    Credentials path öncelik sırası:
    1) config.GOOGLE_CALENDAR_CREDENTIALS_PATH
    2) env GOOGLE_CALENDAR_CREDENTIALS_PATH
    3) env GOOGLE_APPLICATION_CREDENTIALS
    4) config'ten import edilen GOOGLE_CALENDAR_CREDENTIALS_PATH
    """
    cfg_mod = sys.modules.get("config")

    candidates = [
        getattr(cfg_mod, "GOOGLE_CALENDAR_CREDENTIALS_PATH", None),
        os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH"),
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        GOOGLE_CALENDAR_CREDENTIALS_PATH,
    ]

    for p in candidates:
        if not p:
            continue
        p = str(p).strip()
        if not p:
            continue
        # ~ genişlet
        p = os.path.expanduser(p)
        return p
    return None


def _get_calendar_service():
    """Google Calendar API servis nesnesi (Service Account)."""
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE

    creds_path = resolve_credentials_path()

    if not creds_path:
        print("[Calendar] Credentials path BOS. config/env ayarlanmamis.")
        return None

    if not os.path.isfile(creds_path):
        print(f"[Calendar] Credentials bulunamadi. Path: {creds_path}")
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        SCOPES = [
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.events",
        ]
        credentials = service_account.Credentials.from_service_account_file(
            creds_path, scopes=SCOPES
        )
        _SERVICE = build("calendar", "v3", credentials=credentials, cache_discovery=False)

        print(f"[Calendar] Service OK. creds_path={creds_path}")
        print(f"[Calendar] Service account: {getattr(credentials, 'service_account_email', '')}")
        return _SERVICE
    except Exception as e:
        print(f"[Calendar] Servis olusturulamadi: {e}")
        return None


def whoami() -> str:
    """Service account email (debug)."""
    creds_path = resolve_credentials_path()

    if not creds_path:
        print("[Calendar] whoami: creds_path BOS (config/env yok).")
        return ""

    if not os.path.isfile(creds_path):
        print(f"[Calendar] whoami: dosya yok. Path: {creds_path}")
        return ""

    try:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            creds_path, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return credentials.service_account_email or ""
    except Exception as e:
        print(f"[Calendar] whoami: okuyamadi: {e} path={creds_path}")
        return ""


def list_calendars() -> Tuple[List[dict], Optional[str]]:
    """Service Account'un erişebildiği takvimleri listele."""
    creds_path = resolve_credentials_path()
    service = _get_calendar_service()
    if not service:
        return [], f"Credentials yuklenemedi. Path: {creds_path}"

    try:
        result = service.calendarList().list().execute()
        items = result.get("items", []) or []
        return [
            {
                "id": c["id"],
                "summary": c.get("summary", c["id"]),
                "primary": c.get("primary", False),
                "accessRole": c.get("accessRole", ""),
                "timeZone": c.get("timeZone", ""),
            }
            for c in items
        ], None
    except Exception as e:
        print(f"[Calendar] Liste hatasi: {e}")
        return [], str(e)


def add_calendar_to_list(calendar_id: str) -> Tuple[bool, Optional[str]]:
    """Paylaşılan takvimi service account calendarList'e ekler."""
    service = _get_calendar_service()
    if not service:
        return False, "Calendar service yok (credentials?)"
    try:
        service.calendarList().insert(body={"id": calendar_id}).execute()
        print(f"[Calendar] calendarList'e eklendi: {calendar_id}")
        return True, None
    except Exception as e:
        return False, str(e)


def _parse_working_hours(hours_str: str) -> Tuple[int, int]:
    """Dakika cinsinden baslangic/bitis (09:00-18:00 -> 540, 1080)."""
    import re

    if not hours_str or not hours_str.strip():
        return 9 * 60, 18 * 60

    # 09:00-18:00 gibi
    m = re.search(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", hours_str)
    if m:
        start_h, start_m = int(m.group(1)), int(m.group(2))
        end_h, end_m = int(m.group(3)), int(m.group(4))
        return start_h * 60 + start_m, end_h * 60 + end_m

    # 9-18 gibi
    m2 = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})", hours_str)
    if m2:
        return int(m2.group(1)) * 60, int(m2.group(2)) * 60

    return 9 * 60, 18 * 60


def _to_rfc3339_tr(dt_naive: datetime) -> str:
    """Naive datetime -> TR (+03:00) timezone ile RFC3339."""
    return dt_naive.replace(tzinfo=TR_TZ).isoformat()


def _parse_api_time_to_tr_naive(s: str) -> Optional[datetime]:
    """Google API RFC3339 -> TR saatine çevrilmiş naive datetime"""
    s = (s or "").strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None

    if dt.tzinfo:
        dt = dt.astimezone(TR_TZ).replace(tzinfo=None)
    return dt


def get_available_slots_google(
    calendar_id: str,
    working_hours_str: str = "",
    from_date: Optional[datetime] = None,
    days: int = 7,
    slot_minutes: int = 30,
) -> List[dict]:
    """
    Google Calendar freebusy ile dolu saatleri çıkarıp müsait slotları döndürür.
    """
    service = _get_calendar_service()
    if not service:
        print("[Calendar] service yok -> slots boş")
        return []

    # Başlangıç günü (TR) 00:00
    from_date = from_date or datetime.now(TR_TZ).replace(tzinfo=None).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_min, end_min = _parse_working_hours(working_hours_str)
    time_max = from_date + timedelta(days=days)

    time_min_str = _to_rfc3339_tr(from_date)
    time_max_str = _to_rfc3339_tr(time_max)

    # freebusy
    try:
        body = {
            "timeMin": time_min_str,
            "timeMax": time_max_str,
            "timeZone": TR_TZ_NAME,  # ✅ kritik
            "items": [{"id": calendar_id}],
        }
        result = service.freebusy().query(body=body).execute()

        cal_data = result.get("calendars", {}).get(calendar_id, {})
        busy_list = cal_data.get("busy", []) or []

        print(f"[Calendar] freebusy query cal_id={calendar_id}")
        print(f"[Calendar] timeMin={time_min_str} timeMax={time_max_str}")
        print(f"[Calendar] freebusy: {len(busy_list)} meşgul dilim")

    except Exception as e:
        print(f"[Calendar] freebusy hatasi: {e}")
        return []

    # slot üret, busy ile çakışanları çıkar
    slots: List[dict] = []
    now_naive = datetime.now(TR_TZ).replace(tzinfo=None)

    for d in range(days):
        day = from_date + timedelta(days=d)
        if day.date() < now_naive.date():
            continue

        for m in range(start_min, end_min, slot_minutes):
            h, mn = divmod(m, 60)
            slot_start = day.replace(hour=h, minute=mn, second=0, microsecond=0)
            slot_end = slot_start + timedelta(minutes=slot_minutes)

            if slot_start <= now_naive:
                continue

            overlap = False
            for b in busy_list:
                b_start = _parse_api_time_to_tr_naive(b.get("start", ""))
                b_end = _parse_api_time_to_tr_naive(b.get("end", ""))
                if b_start is None or b_end is None:
                    continue
                if slot_start < b_end and slot_end > b_start:
                    overlap = True
                    break

            if not overlap:
                key = slot_start.strftime("%Y-%m-%d %H:%M")
                slots.append(
                    {"slot_at": key, "display": slot_start.strftime("%d.%m.%Y %H:%M")}
                )

    print(f"[Calendar] {len(slots)} müsait slot üretildi")
    return slots[:50]  # ✅ 50 yeterli


def create_google_event(
    calendar_id: str,
    start_datetime: str,
    summary: str,
    description: str = "",
    duration_minutes: int = 30,
) -> bool:
    """Takvimde etkinlik oluşturur. start_datetime: YYYY-MM-DD HH:MM"""
    service = _get_calendar_service()
    if not service:
        return False

    try:
        start_naive = datetime.strptime(start_datetime, "%Y-%m-%d %H:%M")
        end_naive = start_naive + timedelta(minutes=duration_minutes)

        # ✅ timezone’lu ISO üret (kritik)
        start_iso = start_naive.replace(tzinfo=TR_TZ).isoformat()
        end_iso = end_naive.replace(tzinfo=TR_TZ).isoformat()

        event = {
            "summary": summary,
            "description": description or "RandevuSes ile alındı",
            "start": {"dateTime": start_iso, "timeZone": TR_TZ_NAME},
            "end": {"dateTime": end_iso, "timeZone": TR_TZ_NAME},
        }
        service.events().insert(calendarId=calendar_id, body=event).execute()
        print(f"[Calendar] Etkinlik olusturuldu: {start_datetime} - {summary}")
        return True
    except Exception as e:
        print(f"[Calendar] Etkinlik olusturma hatasi: {e}")
        return False
