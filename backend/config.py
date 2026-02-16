# backend/config.py
import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

FAL_API_KEY = os.getenv("FAL_API_KEY")

# API endpoint'leri (dogrulandi)
FAL_STT_URL = "https://fal.run/freya-mypsdi253hbk/freya-stt/audio/transcriptions"
FAL_TTS_URL = "https://fal.run/freya-mypsdi253hbk/freya-tts/audio/speech"
FAL_LLM_URL = "https://fal.run/openrouter/router"

# HIZ AYARLARI
LLM_MODEL = "google/gemini-3-flash-preview"
LLM_MAX_TOKENS = 200  # RANDEVU satırı + kapanış için yeterli
LLM_TEMPERATURE = 0.4

# SQLite veritabani yolu
DB_PATH = Path(__file__).parent / "randevuses.db"

# ─────────────────────────────────────────
# Google Calendar (Service Account) Ayarlari
#
# Varsayilan: bu klasorde "calendar-service-account.json".
# Dilersen .env icine GOOGLE_CALENDAR_CREDENTIALS_PATH vererek
# baska bir yol da gosterebilirsin.

GOOGLE_CALENDAR_CREDENTIALS_PATH = (
    os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH")
    or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    or str(Path(__file__).parent / "calendar-service-account.json")
)

# Isletme kaydinda google_calendar_id bos gelirse fallback.
# Not: "primary" sadece user OAuth icindir; service account'ta genelde calismaz.
DEFAULT_GOOGLE_CALENDAR_ID = os.getenv("DEFAULT_GOOGLE_CALENDAR_ID") or ""