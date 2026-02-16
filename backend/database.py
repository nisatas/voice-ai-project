# backend/database.py
import sqlite3
import json
import re
from typing import Optional, List, Set
from datetime import datetime, timedelta
from config import DB_PATH, DEFAULT_GOOGLE_CALENDAR_ID

# Calendar entegrasyonu (Google)
try:
    from services.calendar_service import get_available_slots_google, create_google_event
except Exception:
    try:
        from calendar_service import get_available_slots_google, create_google_event  # type: ignore
    except Exception:
        get_available_slots_google = None  # type: ignore
        create_google_event = None  # type: ignore


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            agent_name TEXT DEFAULT 'Asistan',
            sector TEXT DEFAULT '',
            address TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            working_hours TEXT DEFAULT '',
            services TEXT DEFAULT '[]',
            staff TEXT DEFAULT '[]',
            campaigns TEXT DEFAULT '[]',
            custom_rules TEXT DEFAULT '[]',
            google_calendar_id TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_slug TEXT NOT NULL,
            session_id TEXT DEFAULT '',
            slot_at TEXT NOT NULL,
            customer_name TEXT DEFAULT '',
            customer_phone TEXT DEFAULT '',
            google_calendar_id TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ✅ Aynı işletmede aynı slot 2 kez book edilemesin
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_unique ON appointments(business_slug, slot_at)"
        )
    except Exception:
        pass

    # businesses kolon kontrolü (geriye dönük uyum)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()]
        if "google_calendar_id" not in cols:
            conn.execute("ALTER TABLE businesses ADD COLUMN google_calendar_id TEXT DEFAULT ''")
    except Exception:
        pass

    conn.commit()
    conn.close()
    print("[DB] Veritabani hazir")


def slugify(text: str) -> str:
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    text = (text or "").translate(tr_map)
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "isletme"


def create_business(data: dict) -> dict:
    conn = get_db()
    slug = slugify(data.get("name", "isletme"))

    existing = conn.execute("SELECT slug FROM businesses WHERE slug = ?", (slug,)).fetchone()
    if existing:
        slug = f"{slug}-{int(datetime.now().timestamp()) % 10000}"

    conn.execute("""
        INSERT INTO businesses (
            slug, name, agent_name, sector, address, phone, working_hours,
            services, staff, campaigns, custom_rules, google_calendar_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        slug,
        data.get("name", ""),
        data.get("agent_name", "Asistan"),
        data.get("sector", ""),
        data.get("address", ""),
        data.get("phone", ""),
        data.get("working_hours", ""),
        json.dumps(data.get("services", []), ensure_ascii=False),
        json.dumps(data.get("staff", []), ensure_ascii=False),
        json.dumps(data.get("campaigns", []), ensure_ascii=False),
        json.dumps(data.get("custom_rules", []), ensure_ascii=False),
        (data.get("google_calendar_id") or "").strip(),
    ))
    conn.commit()

    biz = get_business_by_slug(slug)
    conn.close()
    print(f"[DB] Isletme olusturuldu: {slug}")
    return biz


def get_business_by_slug(slug: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM businesses WHERE slug = ? AND is_active = 1",
        (slug,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_dict(row)


def list_businesses() -> List[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM businesses WHERE is_active = 1 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def delete_business(slug: str):
    conn = get_db()
    conn.execute("UPDATE businesses SET is_active = 0 WHERE slug = ?", (slug,))
    conn.commit()
    conn.close()


def _allowed_weekdays_from_working_hours(wh: str) -> Optional[set]:
    """
    working_hours alanından gün filtresi çıkarır.
    'Pzt-Cuma 12:00-19:00' => {0,1,2,3,4}
    """
    t = (wh or "").lower()
    if not t:
        return None

    if (("pzt" in t) or ("pazartesi" in t)) and ("cuma" in t or "cum" in t):
        return {0, 1, 2, 3, 4}

    if "cumartesi" in t:
        return {0, 1, 2, 3, 4, 5}

    if "pazar" in t:
        return {0, 1, 2, 3, 4, 5, 6}

    return None


def _get_booked_slot_set(slug: str, from_dt: datetime, to_dt: datetime) -> Set[str]:
    """
    ✅ Doluluk için TEK KAYNAK: SQLite appointments
    Bu aralıkta (slot_at) dolu olanları set olarak döndürür.
    slot_at formatı: 'YYYY-MM-DD HH:MM'
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT slot_at FROM appointments
        WHERE business_slug = ?
          AND slot_at >= ?
          AND slot_at <  ?
        """,
        (
            slug,
            from_dt.strftime("%Y-%m-%d %H:%M"),
            to_dt.strftime("%Y-%m-%d %H:%M"),
        ),
    ).fetchall()
    conn.close()
    return set([(r["slot_at"] or "").strip() for r in rows if r and r["slot_at"]])


def get_available_slots(slug: str, days: int = 7, slot_minutes: int = 30) -> List[dict]:
    biz = get_business_by_slug(slug)
    if not biz:
        return []

    cal_id = (biz.get("google_calendar_id") or "").strip() or (DEFAULT_GOOGLE_CALENDAR_ID or "").strip()
    working_hours = biz.get("working_hours", "") or ""
    allowed = _allowed_weekdays_from_working_hours(working_hours)

    now = datetime.now()
    from_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    to_dt = from_dt + timedelta(days=days)

    # ✅ DB dolu slotlar (Google’dan gelse bile filtreleyeceğiz)
    booked_set = _get_booked_slot_set(slug, from_dt, to_dt)

    # ✅ GOOGLE freebusy
    if cal_id and get_available_slots_google:
        print("[SLOTS] using GOOGLE freebusy. cal_id=", cal_id, "working_hours=", working_hours)

        slots = get_available_slots_google(
            calendar_id=cal_id,
            working_hours_str=working_hours,
            from_date=None,
            days=days,
            slot_minutes=slot_minutes,
        ) or []

        # ✅ weekday filtresi + ✅ DB doluluk filtresi
        filtered = []
        for s in slots:
            sa = (s.get("slot_at", "") or "").strip()
            if not sa:
                continue

            # DB doluysa çıkar
            if sa in booked_set:
                continue

            # weekday filtresi
            if allowed is not None:
                try:
                    dt = datetime.strptime(sa, "%Y-%m-%d %H:%M")
                    if dt.weekday() not in allowed:
                        continue
                except Exception:
                    continue

            filtered.append(s)

        return filtered

    # ✅ FALLBACK local slots
    print("[SLOTS] FALLBACK local slots (NO google freebusy). cal_id=", cal_id)

    import re as _re

    def _parse_working_hours_local(hours_str: str):
        if not hours_str or not str(hours_str).strip():
            return 9 * 60, 18 * 60
        m = _re.search(r"(\d{1,2}):?(\d{2})?\s*-\s*(\d{1,2}):?(\d{2})?", str(hours_str))
        if not m:
            return 9 * 60, 18 * 60
        sh, sm = int(m.group(1)), int(m.group(2) or 0)
        eh, em = int(m.group(3)), int(m.group(4) or 0)
        return sh * 60 + sm, eh * 60 + em

    start_min, end_min = _parse_working_hours_local(working_hours)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    slots: List[dict] = []
    for d in range(days):
        day = today + timedelta(days=d)
        for m in range(start_min, end_min, slot_minutes):
            h, mn = divmod(m, 60)
            slot_start = day.replace(hour=h, minute=mn)

            # ✅ gün filtresi
            if allowed is not None and slot_start.weekday() not in allowed:
                continue

            if slot_start <= datetime.now():
                continue

            key = slot_start.strftime("%Y-%m-%d %H:%M")

            # ✅ DB doluysa çıkar
            if key in booked_set:
                continue

            slots.append({
                "slot_at": key,
                "display": slot_start.strftime("%d.%m.%Y %H:%M"),
            })

    return slots[:200]


def _slot_is_currently_available(slug: str, slot_at: str, duration_minutes: int = 30) -> bool:
    """
    ✅ TEK GARANTİ:
    - Slot, get_available_slots() çıktısında yoksa: dolu/kapalı/mesai dışı kabul et.
    - Böylece LLM/Twilio nereden gelirse gelsin, dolu slota asla booking yapılmaz.
    """
    slot_at = (slot_at or "").strip()
    if not slot_at:
        return False

    # DB'de zaten varsa zaten dolu
    conn = get_db()
    exists = conn.execute(
        "SELECT id FROM appointments WHERE business_slug = ? AND slot_at = ?",
        (slug, slot_at),
    ).fetchone()
    conn.close()
    if exists:
        return False

    # Google/freebusy slot listesi üzerinden doğrula
    # (7 gün yetmiyorsa: randevu tarihi çok ileri ise gün sayısını büyüt)
    try:
        target_dt = datetime.strptime(slot_at, "%Y-%m-%d %H:%M")
    except Exception:
        return False

    now = datetime.now()
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    delta_days = (target_dt.date() - base.date()).days
    if delta_days < 0:
        return False

    days = max(7, delta_days + 2)  # hedef tarih kapsansın
    slots = get_available_slots(slug, days=days, slot_minutes=30) or []
    sset = set([(s.get("slot_at") or "").strip() for s in slots if s.get("slot_at")])
    return slot_at in sset


def book_appointment(
    slug: str,
    slot_at: str,
    customer_name: str,
    customer_phone: str,
    session_id: str = "",
    duration_minutes: int = 30,
    service_name: str = "",
    staff_name: str = "",
    price_tl: int = 0,
) -> dict:
    biz = get_business_by_slug(slug)
    if not biz:
        raise ValueError("Isletme bulunamadi")

    slot_at = (slot_at or "").strip()
    if not slot_at:
        raise ValueError("slot_at gerekli")

    # ✅ SON KAPI: Slot gerçekten müsait mi?
    if not _slot_is_currently_available(slug, slot_at, duration_minutes=duration_minutes):
        raise ValueError("Bu saat dolu veya mesai dışı (slot listesinde yok)")

    conn = get_db()

    exists = conn.execute(
        "SELECT id FROM appointments WHERE business_slug = ? AND slot_at = ?",
        (slug, slot_at),
    ).fetchone()
    if exists:
        conn.close()
        raise ValueError("Bu saat zaten dolu (DB)")

    cal_id = (biz.get("google_calendar_id") or "").strip() or (DEFAULT_GOOGLE_CALENDAR_ID or "").strip()

    print(f"\n{'='*60}")
    print(f"[BOOKING] Isletme: {biz.get('name')}")
    print(f"[BOOKING] Calendar ID: '{cal_id}' {'MEVCUT' if cal_id else 'YOK - Google kayit yapilmayacak!'}")
    print(f"[BOOKING] Slot: {slot_at}")
    print(f"[BOOKING] Musteri: {customer_name} ({customer_phone})")
    if service_name:
        print(f"[BOOKING] Hizmet: {service_name}")
    if staff_name:
        print(f"[BOOKING] Personel: {staff_name}")
    if create_google_event:
        print(f"[BOOKING] create_google_event: MEVCUT")
    else:
        print(f"[BOOKING] create_google_event: YOK")
    print(f"{'='*60}")

    # Önce Google
    if cal_id and create_google_event:
        summary = f"Randevu - {customer_name}".strip()

        extra = []
        if service_name:
            extra.append(f"Hizmet: {service_name}")
        if staff_name:
            extra.append(f"Doktor/Personel: {staff_name}")
        if price_tl:
            extra.append(f"Ucret: {price_tl} TL")

        desc_lines = [
            f"Telefon: {customer_phone}",
            f"Isletme: {biz.get('name','')}",
        ]
        if extra:
            desc_lines.append(" | ".join(extra))
        if session_id:
            desc_lines.append(f"Session: {session_id}")

        desc = "\n".join([l for l in desc_lines if l]).strip()

        ok = create_google_event(
            calendar_id=cal_id,
            start_datetime=slot_at,
            summary=summary,
            description=desc,
            duration_minutes=duration_minutes,
        )
        if not ok:
            conn.close()
            print(f"[BOOKING] Google Calendar'a etkinlik olusturulamadi!")
            raise RuntimeError("Google Calendar'a etkinlik olusturulamadi")
        print(f"[BOOKING] Google Calendar'a basariyla kaydedildi!")
    elif not cal_id:
        print(f"[BOOKING] Google Calendar ID bos - SADECE DB'ye kaydediliyor")
    elif not create_google_event:
        print(f"[BOOKING] create_google_event fonksiyonu yuklenemedi")

    # DB insert
    try:
        conn.execute(
            """
            INSERT INTO appointments (business_slug, session_id, slot_at, customer_name, customer_phone, google_calendar_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (slug, session_id or "", slot_at, customer_name or "", customer_phone or "", cal_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise ValueError("Bu saat zaten dolu (DB unique)")

    appt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    return {
        "id": appt_id,
        "business_slug": slug,
        "slot_at": slot_at,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "google_calendar_id": cal_id,
    }



def _row_to_dict(row) -> dict:
    d = dict(row)
    for key in ["services", "staff", "campaigns", "custom_rules"]:
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                d[key] = []
    return d


init_db()