import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from flask import Flask, Response, request
from google import genai
from google.genai import types

app = Flask(__name__)

# ============================================================
# הגדרות סביבה
# ============================================================

YEMOT_API = "https://www.call2all.co.il/ym/api/"
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()

VOICE_EXTS = [
    x.strip().strip("/")
    for x in os.environ.get("VOICE_EXTS", "1").split(",")
    if x.strip()
]
if not VOICE_EXTS:
    VOICE_EXTS = ["1"]

DATA_EXT = VOICE_EXTS[0]

AI_MODELS = [
    "gemini-2.0-flash-lite",  # מהיר ביותר ל-IVR
    "gemini-2.0-flash",       # גיבוי ראשון
    "gemini-1.5-flash",       # גיבוי שני
]

MAX_AUDIO_BYTES = 7 * 1024 * 1024
MAX_HISTORY_PAIRS = 3
CALL_TTL_SECONDS = 30 * 60

try:
    DAILY_LIMIT = max(0, int(os.environ.get("DAILY_LIMIT", "40") or 0))
except Exception:
    DAILY_LIMIT = 40

OWNER_PHONES = {"0527661756", "0527609296"}

# ============================================================
# ניהול מצב בזיכרון (Thread-Safe)
# ============================================================

names = {}
persona_names = {}
persona_prompts = {}
calls = {}
usage = {}

state_lock = threading.RLock()
client_lock = threading.Lock()
_client = None

GENERAL_RULES = (
    "אתה עוזר קולי בטלפון. ענה תמיד בקצרה, בבהירות ובשפה טבעית. "
    "חוסר מוחלט של: כוכביות, מקפים, אימוג'ים, תווי עיצוב או רשימות. "
    "ענה בעברית פשוטה. "
    "כאשר אתה מציין מספרים או מחירים, כתוב אותם במילים (לדוגמה: חמישים ולא 50)."
)

DEFAULT_PERSONAS = {
    "1": ("העוזר הכללי", "אתה עוזר כללי ידידותי ומועיל."),
    "2": ("העוזר הלימודי", "אתה עוזר לימודי ומכובד. ענה בצורה ברורה ומסודרת."),
    "3": ("העוזר החוצפן", "אתה עוזר חוצפני וסרקסטי עם הומור. היה משעשע אבל אל תעליב."),
    "4": ("העוזר היצירתי", "אתה עוזר יצירתי. הצע רעיונות, סיפורים קצרים ותוכן יצירתי."),
    "5": ("העוזר הטכני", "אתה עוזר טכני. הסבר מחשבים, אינטרנט וטלפונים בפשטות."),
    "6": ("החבר", "אתה חבר קרוב, חמוד וזורם. דבר בחום ובשפה קלילה."),
    "7": ("העוזר המוזיקלי", "אתה עוזר מוזיקלי. אתה מומחה למוזיקה, אקורדים, מושגים, סגנונות ואמנים."),
}

for key, (name, prompt) in DEFAULT_PERSONAS.items():
    persona_names[key] = name
    persona_prompts[key] = prompt

try:
    ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    ISRAEL_TZ = dt.timezone(dt.timedelta(hours=3))

def il_now():
    return dt.datetime.now(ISRAEL_TZ)

def clean_for_tts(text, limit=600):
    text = str(text or "")
    text = re.sub(r"[*_#`>\[\]{}]", "", text)
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = text.replace("\r", " ").replace("\n", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]

def normalize_phone(phone):
    phone = re.sub(r"\D", "", str(phone or ""))
    return phone or "unknown"

def cleanup_calls():
    now = time.monotonic()
    today = il_now().date().isoformat()
    with state_lock:
        expired_calls = [c for c, s in calls.items() if now - s.get("last_seen", 0) > CALL_TTL_SECONDS]
        for c in expired_calls:
            calls.pop(c, None)
        expired_usage = [p for p, u in usage.items() if u.get("day") != today]
        for p in expired_usage:
            usage.pop(p, None)

def over_limit(phone):
    if phone in OWNER_PHONES or DAILY_LIMIT <= 0:
        return False
    today = il_now().date().isoformat()
    with state_lock:
        item = usage.get(phone)
        if not item or item.get("day") != today:
            return False
        return int(item.get("count", 0)) >= DAILY_LIMIT

def consume_message(phone):
    if phone in OWNER_PHONES or DAILY_LIMIT <= 0:
        return True
    today = il_now().date().isoformat()
    with state_lock:
        item = usage.get(phone)
        if not item or item.get("day") != today:
            item = {"day": today, "count": 0}
            usage[phone] = item
        if item["count"] >= DAILY_LIMIT:
            return False
        item["count"] += 1
        return True

# ============================================================
# תקשורת מול ימות המשיח
# ============================================================

def yemot_download(ext, file_name):
    if not YEMOT_TOKEN:
        raise RuntimeError("YEMOT_TOKEN is missing")
    url = f"{YEMOT_API}DownloadFile?{urllib.parse.urlencode({'token': YEMOT_TOKEN, 'path': f'ivr2:/{ext}/{file_name}.wav'})}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as response:
        data = response.read(MAX_AUDIO_BYTES + 1)
    if not data:
        raise RuntimeError("empty audio file")
    if len(data) > MAX_AUDIO_BYTES:
        raise RuntimeError("audio file exceeds size limit")
    return data

def yemot_delete(ext, file_name):
    if not YEMOT_TOKEN:
        return
    try:
        url = f"{YEMOT_API}FileAction?{urllib.parse.urlencode({'token': YEMOT_TOKEN, 'action': 'delete', 'what': f'ivr2:/{ext}/{file_name}.wav'})}"
        urllib.request.urlopen(url, timeout=5).read()
    except Exception as exc:
        print("delete error:", repr(exc))

def delete_audio_async(ext, file_name):
    threading.Thread(target=yemot_delete, args=(ext, file_name), daemon=True).start()

# ============================================================
# Gemini Client & Logic
# ============================================================

def get_client():
    global _client
    if _client is not None:
        return _client
    with client_lock:
        if _client is not None:
            return _client
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY environment variable missing")
        _client = genai.Client(api_key=GEMINI_API_KEY)
        return _client

def ai_system(persona_key):
    with state_lock:
        persona = persona_prompts.get(persona_key, persona_prompts["1"])
    now = il_now()
    return (
        f"{persona} {GENERAL_RULES} [זמן נוכחי: {now:%d/%m/%Y %H:%M}]\n"
        "הנחיות מענה:\n"
        "1. אם המשתמש מבקש מידע עדכני, חדשות, או נתונים משתנים - השתמש ב-Google Search.\n"
        "2. פקודות מיוחדות התקפות במידת הצורך:\n"
        "   - החלף קול / שנה קול -> COMMAND|CHANGE_VOICE\n"
        "   - תפריט / חזרה -> COMMAND|MENU\n"
        "   - יציאה / סיום -> COMMAND|HANGUP\n"
        "3. לכל מענה רגיל החזר בפורמט מדוייק:\n"
        "TRANSCRIPT|הטקסט שהבנת מההקלטה\n"
        "ANSWER|התשובה הקצרה שלך"
    )

def extract_text_from_result(result):
    if not result:
        return ""
    if hasattr(result, "text") and result.text:
        return result.text.strip()
    
    try:
        if result.candidates and result.candidates[0].content and result.candidates[0].content.parts:
            parts = result.candidates[0].content.parts
            texts = [p.text for p in parts if hasattr(p, "text") and p.text]
            if texts:
                return "\n".join(texts).strip()
    except Exception as e:
        print("Error extracting text parts:", repr(e))
    return ""

def ai_audio_turn(persona_key, history, audio):
    formatted_contents = []
    
    for turn in history:
        formatted_contents.append(
            types.Content(
                role=turn["role"],
                parts=[types.Part.from_text(text=turn["parts"][0])]
            )
        )

    formatted_contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part.from_bytes(data=audio, mime_type="audio/wav"),
                types.Part.from_text(text="הקשב להקלטה הקולית וענה לפיה."),
            ]
        )
    )

    client = get_client()

    for model in AI_MODELS:
        try:
            config = types.GenerateContentConfig(
                system_instruction=ai_system(persona_key),
                max_output_tokens=160,
                tools=[{"google_search": {}}],
            )

            result = client.models.generate_content(
                model=model,
                contents=formatted_contents,
                config=config,
            )

            text = extract_text_from_result(result)
            if text:
                print(f"Gemini Success ({model}):", text[:60])
                return text

        except Exception as exc:
            print(f"Gemini error on model {model}:", repr(exc))

    return ""

def parse_ai_result(raw):
    raw = str(raw or "").strip()
    if not raw:
        return {"type": "error", "transcript": "", "answer": ""}

    upper = raw.upper()
    if "COMMAND|CHANGE_VOICE" in upper:
        return {"type": "change_voice", "transcript": "", "answer": ""}
    if "COMMAND|MENU" in upper:
        return {"type": "menu", "transcript": "", "answer": ""}
    if "COMMAND|HANGUP" in upper:
        return {"type": "hangup", "transcript": "", "answer": ""}

    transcript_match = re.search(r"TRANSCRIPT\|(.*?)(?:\n|$)", raw, re.IGNORECASE)
    answer_match = re.search(r"ANSWER\|(.*)", raw, re.IGNORECASE | re.DOTALL)

    transcript = clean_for_tts(transcript_match.group(1).strip(), 400) if transcript_match else ""
    answer = clean_for_tts(answer_match.group(1).strip(), 600) if answer_match else ""

    if not answer:
        lines = [x.strip() for x in raw.splitlines() if x.strip()]
        if lines:
            cleaned = re.sub(r"^(ANSWER|TRANSCRIPT)\s*\|\s*", "", lines[-1], flags=re.IGNORECASE)
            answer = clean_for_tts(cleaned, 600)

    return {
        "type": "answer" if answer else "error",
        "transcript": transcript,
        "answer": answer,
    }

def ask_ai(persona_key, history, ext, file_name):
    try:
        audio = yemot_download(ext, file_name)
    except Exception as exc:
        print("Audio download error:", repr(exc))
        return {
            "type": "error",
            "transcript": "",
            "answer": "לא הצלחתי לקלוט את ההקלטה. אנא נסה לדבר שוב.",
        }

    raw_res = ai_audio_turn(persona_key, history, audio)
    return parse_ai_result(raw_res)

# ============================================================
# נתיבי API עבור ימות המשיח (Endpoints)
# ============================================================

@app.route("/", methods=["GET", "POST"])
@app.route("/ivr", methods=["GET", "POST"])
def ivr_entry():
    cleanup_calls()
    phone = normalize_phone(request.values.get("ApiPhone"))
    call_id = request.values.get("ApiCallId") or phone

    with state_lock:
        if call_id not in calls:
            calls[call_id] = {
                "phone": phone,
                "persona": "1",
                "history": [],
                "last_seen": time.monotonic(),
            }
        else:
            calls[call_id]["last_seen"] = time.monotonic()

    # בדיקת חריגה משרת בימות המשיח
    val = request.values.get("val")
    ext = request.values.get("ext", DATA_EXT)

    if val and val.startswith("file_"):
        file_name = val.replace("file_", "")
        delete_audio_async(ext, file_name)

        if over_limit(phone):
            res_text = "id_list_message=t-הגעת למכסת ההודעות היומית. תודה ושלום.&hangup"
            return Response(res_text, content_type="text/plain; charset=utf-8")

        with state_lock:
            call_data = calls[call_id]
            persona = call_data["persona"]
            history = list(call_data["history"])

        ai_res = ask_ai(persona, history, ext, file_name)

        if ai_res["type"] == "answer":
            consume_message(phone)
            with state_lock:
                calls[call_id]["history"].append({"role": "user", "parts": [ai_res["transcript"] or "הקלטה קולית"]})
                calls[call_id]["history"].append({"role": "model", "parts": [ai_res["answer"]]})
                if len(calls[call_id]["history"]) > MAX_HISTORY_PAIRS * 2:
                    calls[call_id]["history"] = calls[call_id]["history"][-MAX_HISTORY_PAIRS * 2:]

            res_text = (
                f"id_list_message=t-{ai_res['answer']}&"
                f"read=t-השמע את שאלתך לאחר הצליל ובסיום הקש סולמית=val,no,25,2,N,file,select_val=no"
            )
            return Response(res_text, content_type="text/plain; charset=utf-8")

        elif ai_res["type"] == "change_voice":
            res_text = "go_to_folder=/persona_select"
            return Response(res_text, content_type="text/plain; charset=utf-8")

        elif ai_res["type"] == "hangup":
            res_text = "id_list_message=t-תודה רבה ויום טוב.&hangup"
            return Response(res_text, content_type="text/plain; charset=utf-8")

        else:
            res_text = (
                f"id_list_message=t-{ai_res['answer'] or 'לא הצלחתי להבין, אנא נסה שוב.'}&"
                f"read=t-השמע את שאלתך לאחר הצליל ובסיום הקש סולמית=val,no,25,2,N,file,select_val=no"
            )
            return Response(res_text, content_type="text/plain; charset=utf-8")

    # ברירת מחדל: בקשת הקלטה מהמשתמש
    res_text = "read=t-שלום, השמע את שאלתך לאחר הצליל ובסיום הקש סולמית=val,no,25,2,N,file,select_val=no"
    return Response(res_text, content_type="text/plain; charset=utf-8")

# ============================================================
# הרצת השרת
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # threaded=True קריטי לתמיכה במספר משתמשים במקביל
    app.run(host="0.0.0.0", port=port, threaded=True)
