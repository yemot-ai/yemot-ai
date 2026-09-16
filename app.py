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
# הגדרות סביבה ותצורה
# ============================================================

YEMOT_API = "https://www.call2all.co.il/ym/api/"
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

VOICE_EXTS = [
    x.strip().strip("/")
    for x in os.environ.get("VOICE_EXTS", "1").split(",")
    if x.strip()
]
DATA_EXT = VOICE_EXTS[0] if VOICE_EXTS else "1"

# שימוש במודלים המהירים ביותר לזמן תגובה מינימלי
AI_MODELS = [
    "gemini-2.0-flash-lite",  # מודל קל ומהיר במיוחד לשיחות IVR
    "gemini-2.0-flash",       # מודל גיבוי עוצמתי
]

MAX_AUDIO_BYTES = 7 * 1024 * 1024
MAX_HISTORY_PAIRS = 3
CALL_TTL_SECONDS = 30 * 60
DAILY_LIMIT = max(0, int(os.environ.get("DAILY_LIMIT", "40") or 0))
OWNER_PHONES = {"0527661756", "0527609296"}

# ============================================================
# ניהול מצב בזיכרון (Thread-Safe ומהיר)
# ============================================================

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

# הגדרות 7 השלוחות לביצועים מדויקים
DEFAULT_PERSONAS = {
    "1": ("העוזר הכללי", "אתה עוזר כללי ידידותי ומועיל."),
    "2": ("העוזר הלימודי", "אתה עוזר לימודי מקצועי. ענה בצורה ברורה, מסודרת ועניינית."),
    "3": ("העוזר החוצפן", "אתה עוזר חוצפני וסרקסטי עם הומור. היה משעשע אבל קצר וקולע."),
    "4": ("העוזר היצירתי", "אתה עוזר יצירתי. הצע רעיונות, סיפורים קצרים ותוכן יצירתי."),
    "5": ("העוזר הטכני", "אתה עוזר טכני. הסבר תהליכי מחשוב, אוטומציות, אינטרנט וקוד בפשטות."),
    "6": ("החבר", "אתה חבר קרוב, חמוד וזורם. דבר בחום ובשפה קלילה ופתוחה."),
    "7": ("העוזר המוזיקלי", "אתה מומחה למוזיקה. ספק מידע על אקורדים, מושגים, סגנונות, ואמנים ישראלים."),
}

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
    return re.sub(r"\D", "", str(phone or "")) or "unknown"

def cleanup_calls():
    now = time.monotonic()
    today = il_now().date().isoformat()
    with state_lock:
        expired_calls = [c for c, s in list(calls.items()) if now - s.get("last_seen", 0) > CALL_TTL_SECONDS]
        for c in expired_calls:
            calls.pop(c, None)
        expired_usage = [p for p, u in list(usage.items()) if u.get("day") != today]
        for p in expired_usage:
            usage.pop(p, None)

def over_limit(phone):
    if phone in OWNER_PHONES or DAILY_LIMIT <= 0:
        return False
    today = il_now().date().isoformat()
    with state_lock:
        item = usage.get(phone)
        return bool(item and item.get("day") == today and item.get("count", 0) >= DAILY_LIMIT)

def consume_message(phone):
    if phone in OWNER_PHONES or DAILY_LIMIT <= 0:
        return True
    today = il_now().date().isoformat()
    with state_lock:
        item = usage.setdefault(phone, {"day": today, "count": 0})
        if item["day"] != today:
            item.update({"day": today, "count": 0})
        if item["count"] >= DAILY_LIMIT:
            return False
        item["count"] += 1
        return True

# ============================================================
# תקשורת יעילה ומהירה מול ימות המשיח
# ============================================================

def yemot_download(ext, file_name):
    if not YEMOT_TOKEN:
        raise RuntimeError("YEMOT_TOKEN is missing")
    url = f"{YEMOT_API}DownloadFile?{urllib.parse.urlencode({'token': YEMOT_TOKEN, 'path': f'ivr2:/{ext}/{file_name}.wav'})}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Connection": "keep-alive"})
    with urllib.request.urlopen(req, timeout=10) as response:
        data = response.read(MAX_AUDIO_BYTES + 1)
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise RuntimeError("Invalid or too large audio file")
    return data

def yemot_delete_async(ext, file_name):
    def delete_task():
        if not YEMOT_TOKEN: return
        try:
            url = f"{YEMOT_API}FileAction?{urllib.parse.urlencode({'token': YEMOT_TOKEN, 'action': 'delete', 'what': f'ivr2:/{ext}/{file_name}.wav'})}"
            urllib.request.urlopen(url, timeout=4).read()
        except Exception:
            pass
    threading.Thread(target=delete_task, daemon=True).start()

# ============================================================
# Gemini AI - חיפוש ברשת ועיבוד קולי
# ============================================================

def get_client():
    global _client
    if _client: return _client
    with client_lock:
        if not _client:
            if not GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY environment variable missing")
            _client = genai.Client(api_key=GEMINI_API_KEY)
        return _client

def ai_system(persona_key):
    persona_prompt = DEFAULT_PERSONAS.get(persona_key, DEFAULT_PERSONAS["1"])[1]
    now = il_now()
    return (
        f"{persona_prompt} {GENERAL_RULES} [זמן נוכחי: {now:%d/%m/%Y %H:%M}]\n"
        "הנחיות מענה:\n"
        "1. אם הבקשה דורשת ידע עולמי עדכני, עובדות, או נתונים משתנים - השתמש באופן פעיל בכלי החיפוש (Google Search) כדי לספק מענה מדויק.\n"
        "2. פקודות מיוחדות (רק במידת הצורך):\n"
        "   - החלף קול / שנה קול -> COMMAND|MENU\n"
        "   - יציאה / סיום -> COMMAND|HANGUP\n"
        "3. לכל מענה רגיל החזר בפורמט המדויק הבא בלבד:\n"
        "TRANSCRIPT|הטקסט שהבנת מההקלטה\n"
        "ANSWER|התשובה המלאה והברורה שלך"
    )

def extract_text(result):
    if not result: return ""
    if hasattr(result, "text") and result.text: return result.text.strip()
    try:
        if result.candidates and result.candidates[0].content.parts:
            return "\n".join(p.text for p in result.candidates[0].content.parts if hasattr(p, "text") and p.text).strip()
    except Exception:
        pass
    return ""

def ask_ai(persona_key, history, ext, file_name):
    try:
        audio = yemot_download(ext, file_name)
    except Exception:
        return {"type": "error", "transcript": "", "answer": "סליחה, הקו נקטע או שההקלטה לא נקלטה. אנא נסו שוב."}

    contents = [types.Content(role=t["role"], parts=[types.Part.from_text(text=t["parts"][0])]) for t in history]
    contents.append(types.Content(
        role="user",
        parts=[
            types.Part.from_bytes(data=audio, mime_type="audio/wav"),
            types.Part.from_text(text="הקשב להקלטה הקולית, חפש ברשת במידת הצורך, וענה לפיה."),
        ]
    ))

    client = get_client()
    for model in AI_MODELS:
        try:
            config = types.GenerateContentConfig(
                system_instruction=ai_system(persona_key),
                max_output_tokens=250,
                tools=[{"google_search": {}}],  # כלי החיפוש באינטרנט מופעל כאן
            )
            result = client.models.generate_content(model=model, contents=contents, config=config)
            raw = extract_text(result)
            if raw: break
        except Exception as exc:
            raw = ""
            print(f"Model error {model}: {exc}")

    if not raw:
        return {"type": "error", "transcript": "", "answer": "הייתה שגיאה בעיבוד. נסו שוב."}

    upper = raw.upper()
    if "COMMAND|MENU" in upper or "COMMAND|CHANGE_VOICE" in upper: return {"type": "menu", "transcript": "", "answer": ""}
    if "COMMAND|HANGUP" in upper: return {"type": "hangup", "transcript": "", "answer": ""}

    t_match = re.search(r"TRANSCRIPT\|(.*?)(?:\n|$)", raw, re.IGNORECASE)
    a_match = re.search(r"ANSWER\|(.*)", raw, re.IGNORECASE | re.DOTALL)
    
    answer = clean_for_tts(a_match.group(1), 600) if a_match else clean_for_tts(re.sub(r"^(ANSWER|TRANSCRIPT)\s*\|\s*", "", raw.splitlines()[-1], flags=re.IGNORECASE), 600)
    
    return {
        "type": "answer" if answer else "error",
        "transcript": clean_for_tts(t_match.group(1), 400) if t_match else "",
        "answer": answer or "לא הצלחתי להבין, נסו שוב.",
    }

# ============================================================
# נתיבי ה-API (Routing) 
# ============================================================

def build_menu_response():
    menu_text = (
        "ברוכים הבאים למערכת. הקישו 1 לעוזר הכללי, 2 לעוזר הלימודי, 3 לחוצפן, "
        "4 ליצירתי, 5 לטכני, 6 לחבר, ו-7 לעוזר המוזיקלי."
    )
    return Response(f"read=t-{menu_text}=val,1,1,1,1,Number,menu_choice=no", content_type="text/plain; charset=utf-8")

@app.route("/", methods=["GET", "POST"])
@app.route("/ivr", methods=["GET", "POST"])
def ivr_entry():
    cleanup_calls()
    phone = normalize_phone(request.values.get("ApiPhone"))
    call_id = request.values.get("ApiCallId") or phone

    with state_lock:
        call_data = calls.setdefault(call_id, {"phone": phone, "persona": "1", "history": [], "last_seen": 0})
        call_data["last_seen"] = time.monotonic()

    val = request.values.get("val", "").strip()
    ext = request.values.get("ext", DATA_EXT)

    # 1. תפריט ראשי
    if not val or val in ["0", "*"]:
        return build_menu_response()

    # 2. בחירת שלוחה
    if val in DEFAULT_PERSONAS:
        with state_lock:
            calls[call_id].update({"persona": val, "history": []})
        persona_name = DEFAULT_PERSONAS[val][0]
        return Response(
            f"id_list_message=t-בחרתם ב{persona_name}.&read=t-השמיעו את שאלתכם לאחר הצליל ובסיום הקישו סולמית=val,no,25,2,N,file,select_val=no",
            content_type="text/plain; charset=utf-8"
        )

    # 3. עיבוד הקלטה
    if val.startswith("file_"):
        file_name = val.replace("file_", "")
        yemot_delete_async(ext, file_name)

        if over_limit(phone):
            return Response("id_list_message=t-הגעתם למכסת ההודעות היומית. תודה ושלום.&hangup", content_type="text/plain; charset=utf-8")

        with state_lock:
            persona, history = call_data["persona"], list(call_data["history"])

        ai_res = ask_ai(persona, history, ext, file_name)

        if ai_res["type"] == "answer":
            consume_message(phone)
            with state_lock:
                calls[call_id]["history"].extend([
                    {"role": "user", "parts": [ai_res["transcript"] or "הקלטה קולית"]},
                    {"role": "model", "parts": [ai_res["answer"]]}
                ])
                calls[call_id]["history"] = calls[call_id]["history"][-MAX_HISTORY_PAIRS * 2:]
            
            return Response(f"id_list_message=t-{ai_res['answer']}&read=t-השמיעו שאלה נוספת לאחר הצליל או הקישו 0 לתפריט=val,no,25,2,N,file,select_val=no", content_type="text/plain; charset=utf-8")

        if ai_res["type"] == "menu": return build_menu_response()
        if ai_res["type"] == "hangup": return Response("id_list_message=t-תודה רבה ולהתראות.&hangup", content_type="text/plain; charset=utf-8")
        
        return Response(f"id_list_message=t-{ai_res['answer']}&read=t-נסו שוב לאחר הצליל=val,no,25,2,N,file,select_val=no", content_type="text/plain; charset=utf-8")

    return build_menu_response()

# ============================================================
# הרצת השרת
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    try:
        from waitress import serve
        print(f"Starting highly optimized Waitress server on port {port}...")
        serve(app, host="0.0.0.0", port=port, threads=12) # תמיכה ב-12 קריאות מקביליות מהירות
    except ImportError:
        print("Waitress not installed. Falling back to Flask dev server. Run 'pip install waitress' for maximum performance.")
        app.run(host="0.0.0.0", port=port, threaded=True)
