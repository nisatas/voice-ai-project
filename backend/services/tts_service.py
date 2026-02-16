# backend/services/tts_service.py
import httpx, json, sys, os
from typing import Tuple
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import FAL_API_KEY, FAL_TTS_URL

async def synthesize_speech(text: str) -> Tuple[bytes, str]:
    if not text or not text.strip():
        return b"", "wav"
    print(f"[TTS] \"{text[:50]}\"")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(FAL_TTS_URL,
                headers={"Authorization": f"Key {FAL_API_KEY}", "Content-Type": "application/json"},
                json={"input": text, "language": "tr"})
            if resp.status_code != 200:
                print(f"[TTS] Hata {resp.status_code}: {resp.text[:200]}")
                return b"", "wav"
            ct = resp.headers.get("content-type", "")
            if "audio" in ct or len(resp.content) > 1000:
                fmt = "wav"
                if "mpeg" in ct or "mp3" in ct: fmt = "mp3"
                print(f"[TTS] -> {len(resp.content)} bytes ({fmt})")
                return resp.content, fmt
            if "json" in ct:
                data = resp.json()
                for k in ["url","audio_url","output_url"]:
                    u = data.get(k) or data.get("output",{}).get(k,"")
                    if isinstance(u, str) and u.startswith("http"):
                        ar = await client.get(u)
                        if ar.status_code == 200:
                            return ar.content, "wav"
            return b"", "wav"
    except Exception as e:
        print(f"[TTS] Hata: {e}")
        return b"", "wav"