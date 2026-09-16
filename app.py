import datetime as dt
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
from flask import Flask, Response, request
from google import genai
from google.genai import types

app = Flask(__name__)

# =============================================================================================================
# הגדרות מערכת וקבועים
# =============================================================================================================
YEMOT_API = "https://www.call2all.co.il/ym/api/"
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

VOICE_EXTS = [
    x.strip().strip("/")
    for x in os.environ.get("VOICE_EXTS", "1").split(",")
    if x.strip()
]
DATA_EXT = VOICE_EXTS[0] if VOICE_EXTS else "1"

AI_MODELS = [
    "gemini-2.5-flash",
    "gemini-1.5-flash",
]

MAX_AUDIO_BYTES = 7 * 1024 * 1024
MAX_HISTORY_PAIRS = 3
CALL_TTL_SECONDS = 30 * 60

try:
    DAILY_LIMIT = max(0, int(os.environ.get("DAILY_LIMIT", "40") or 0))
except ValueError:
    DAILY_LIMIT = 40

OWNER_PHONES = {
    "0527661756",
    "0527609296",
}

# =============================================================================================================
# ניהול מצב
# =============================================================================================================
calls = {}
usage = {}
state_lock = threading.RLock()
client_lock = threading.Lock()
_client = None

GENERAL_RULES = (
    "אתה עוזר קולי בטלפון. "
    "ענה תמיד בקצרה, בבהירות ובשפה טבעית. "
    "ללא כוכביות, מקפים, אימוג'ים, טבלאות, רשימות או תווי עיצוב. "
    "ענה בעברית פשוטה. "
    "כאשר אתה מציין מספרים או מחירים, כתוב אותם במילים."
)

DEFAULT_PERSONAS = {
    "1": (
        "העוזר הכללי",
        "אתה עוזר כללי רעיונות ומועיל."
    ),
    "2": (
        "העוזר הלימודי",
        "אתה עוזר לימודי עוזר יצירת. ענה בצורה ברורה, מסודרת ועניינית."
    ),
    "3": (
        "העוזר החוצפן",
        "אתה עוזר חוצפני וסרקסטי עם הומור. היה משעשע אבל קצר וקולע."
    ),
    "4": (
        "העוזר היצירתי",
        "אתה עוזר תוכן יצירתי קצרים ומעניינים."
    ),
    "5": (
        "העוזר הטכני",
        "אתה עוזר טכני. הסבר מחשבים, אוטומציות, אינטרנט וקוד בפשטות."
    ),
    "6": (
        "החבר",
        "אתה עוזר חם, זורם ונעים. דבר בשפה קלילה וטבעית."
    ),
    "7": (
        "העוזר המוזיקלי",
        "אתה מומחה למוזיקה. ספק מידע על אקורדים, מושגים, סגנונות ואמנים."
    ),
}

try:
    ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    ISRAEL_TZ = dt.timezone(dt.timedelta(hours=3))

def il_now():
    return dt.datetime.now(ISRAEL_TZ)

def text_response(text):
    return Response(
        str(text),
        content_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"}
    )

def clean_for_tts(text, limit=600):
    text = str(text or "")
    text = re.sub(r"[*_#`>\[\]{}]", "", text)
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = text.replace("\r", " ").replace("\n", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]

def normalize_phone(phone):
    return re.sub(r"\D", "", str(phone or "")) or "לא ידוע"

def normalize_call_id(value):
    value = str(value or "").strip()
    if not value:
        return ""
    return value[:200]

def cleanup_calls():
    now = time.monotonic()
    today = il_now().date().isoformat()
    with state_lock:
        expired_calls = [
            call_id for call_id, state in list(calls.items())
            if (now - state.get("last_seen", 0) > CALL_TTL_SECONDS)
        ]
        for call_id in expired_calls:
            calls.pop(call_id, None)

        expired_usage = [
            phone for phone, item in list(usage.items())
            if item.get("day") != today
        ]
        for phone in expired_usage:
            usage.pop(phone, None)

def over_limit(phone):
    if phone in OWNER_PHONES:
        return False
    if DAILY_LIMIT <= 0:
        return False
    today = il_now().date().isoformat()
    with state_lock:
        item = usage.get(phone)
        return bool(
            item and item.get("day") == today and item.get("count", 0) >= DAILY_LIMIT
        )

def consume_message(phone):
    if phone in OWNER_PHONES:
        return True
    if DAILY_LIMIT <= 0:
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

# =============================================================================================================
# תקשורת מול ימות המשיח
# =============================================================================================================
def yemot_download(ext, file_name):
    if not YEMOT_TOKEN:
        raise RuntimeError("YEMOT_TOKEN חסר")
    ext = str(ext or DATA_EXT).strip().strip("/")
    file_name = str(file_name or "").strip()
    if not file_name:
        raise RuntimeError("שם קובץ הקלטה ריק")
    if file_name.startswith("file_"):
        file_name = file_name[5:]
    file_name = os.path.basename(file_name)
    path = f"ivr2:/{ext}/{file_name}.wav"
    params = {
        "token": YEMOT_TOKEN,
        "path": path
    }
    url = f"{YEMOT_API}GetFile?{urllib.parse.urlencode(params)}"
    request_obj = urllib.request.Request(
        url,
        headers={
            "User-Agent": "YBY-AI-IVR/1.0",
            "Connection": "close"
        }
    )
    with urllib.request.urlopen(request_obj, timeout=15) as response:
        data = response.read(MAX_AUDIO_BYTES + 1)
        if not data:
            raise RuntimeError("קובץ שמע ריק")
        if len(data) > MAX_AUDIO_BYTES:
            raise RuntimeError("קובץ שמע גדול מדי")
        return data

def yemot_delete(ext, file_name):
    if not YEMOT_TOKEN:
        return
    ext = str(ext or DATA_EXT).strip().strip("/")
    file_name = os.path.basename(str(file_name or "").strip())
    if file_name.startswith("file_"):
        file_name = file_name[5:]
    if not file_name:
        return
    try:
        params = {
            "token": YEMOT_TOKEN,
            "action": "delete",
            "what": f"ivr2:/{ext}/{file_name}.wav"
        }
        url = f"{YEMOT_API}FileAction?{urllib.parse.urlencode(params)}"
        request_obj = urllib.request.Request(
            url,
            headers={"User-Agent": "YBY-AI-IVR/1.0"}
        )
        with urllib.request.urlopen(request_obj, timeout=5):
            pass
    except Exception as exc:
        print(f"[שגיאת מחיקת YEMOT] {exc}")

def yemot_delete_async(ext, file_name):
    threading.Thread(
        target=yemot_delete,
        args=(ext, file_name),
        daemon=True
    ).start()

# =============================================================================================================
# אינטגרציה עם Gemini
# =============================================================================================================
def get_client():
    global _client
    if _client is not None:
        return _client
    with client_lock:
        if _client is not None:
            return _client
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY משתנה סביבה חסר")
        _client = genai.Client(api_key=GEMINI_API_KEY)
        return _client

def ai_system(persona_key):
    persona_prompt = DEFAULT_PERSONAS.get(str(persona_key), DEFAULT_PERSONAS["1"])[1]
    now = il_now()
    return (
        f"{persona_prompt}"
        f"{GENERAL_RULES} "
        f"[זמן נוכחי: {now:%d/%m/%Y %H:%M}]\n"
        "הנחיות מענה:\n"
        "אם נדרש מידע עדכני מדויק, השתמש בחיפוש ב-Google.\n"
        "אם המשתמש מבקש לחזור לתפריט, החזר בדיוק COMMAND|MENU.\n"
        "אם המשתמש מבקש לנתק, החזר בדיוק COMMAND|HANGUP.\n"
        "בכל מענה רגיל החזר בדיוק שתי שורות:\n"
        "TRANSCRIPT|הטקסט שהבנת מההקלטה\n"
        "ANSWER|התשובה למתקשר"
    )

def extract_text(result):
    if not result:
        return ""
    try:
        if getattr(result, "text", None):
            return result.text.strip()
        candidates = getattr(result, "candidates", [])
        if candidates:
            content = getattr(candidates[0], "content", None)
            if content:
                parts = getattr(content, "parts", None) or []
                text_parts = [
                    getattr(part, "text", "") for part in parts if getattr(part, "text", None)
                ]
                if text_parts:
                    return "\n".join(text_parts).strip()
    except Exception:
        pass
    return ""

def build_history(history):
    result = []
    for item in history or []:
        role = item.get("role")
        parts = item.get("parts") or []
        if role not in {"user", "model"}:
            continue
        if not parts:
            continue
        text = str(parts[0] or "").strip()
        if not text:
            continue
        result.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=text)]
            )
        )
    return result

def ask_ai(persona_key, history, ext, file_name):
    try:
        audio = yemot_download(ext, file_name)
    except Exception as exc:
        print(f"[AUDIO DOWNLOAD ERROR] {exc}")
        return {
            "type": "error",
            "transcript": "",
            "answer": "סליחה, לא הצלחתי לקבל את ההקלטה."
        }
    
    try:
        client = get_client()
    except Exception as exc:
        print(f"[GEMINI CLIENT ERROR] {exc}")
        return {
            "type": "error",
            "transcript": "",
            "answer": "יש כרגע תקלה בחיבור לשירות. נסו שוב בעוד רגע."
        }
    
    contents = build_history(history)
    contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part.from_bytes(
                    data=audio,
                    mime_type="audio/wav"
                ),
                types.Part.from_text(
                    text="הקשב להקלטה, הבן את דברי המתקשר וענה לפיהם. השתמש בחיפוש בגוגל רק כאשר יש צורך במידע מעודכן."
                )
            ]
        )
    )

    raw = ""
    google_search_tool = types.Tool(google_search={})
    
    for model in AI_MODELS:
        try:
            config = types.GenerateContentConfig(
                system_instruction=ai_system(persona_key),
                max_output_tokens=250,
                tools=[google_search_tool]
            )
            result = client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
            raw = extract_text(result)
            if raw:
                print(f"[GEMINI OK] model={model}")
                break
            print(f"[GEMINI EMPTY] model={model}")
        except Exception as exc:
            print(f"[GEMINI MODEL ERROR] model={model} error={exc}")

    yemot_delete_async(ext, file_name)

    if not raw:
        return {
            "type": "error",
            "transcript": "",
            "answer": "השגיאה זמנית בעבודה. נסו שוב."
        }

    upper = raw.upper()
    if "COMMAND|MENU" in upper or "COMMAND|CHANGE_VOICE" in upper:
        return {
            "type": "menu",
            "transcript": "",
            "answer": ""
        }
    if "COMMAND|HANGUP" in upper:
        return {
            "type": "hangup",
            "transcript": "",
            "answer": ""
        }

    transcript_match = re.search(
        r"TRANSCRIPT\|(.*?)(?:\r?\n|$)", raw, re.IGNORECASE | re.DOTALL
    )
    answer_match = re.search(
        r"ANSWER\|(.*)", raw, re.IGNORECASE | re.DOTALL
    )

    transcript = clean_for_tts(transcript_match.group(1), 400) if transcript_match else ""
    if answer_match:
        answer = clean_for_tts(answer_match.group(1), 600)
    else:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        answer = clean_for_tts(lines[-1] if lines else "", 600)

    return {
        "type": "answer" if answer else "error",
        "transcript": transcript,
        "answer": answer or "לא הצלחתי להבין, נסו שוב."
    }

# =============================================================================================================
# תגובות מערכת IVR
# =============================================================================================================
def build_menu_response():
    menu_text = (
        "ברוכים הבאים למערכת. "
        "הקישו 1 לעוזר הכללי, "
        "2 לעוזר הלימודי, "
        "3 לחוצפן, "
        "4 ליצירתי, "
        "5 לטכני, "
        "6 לחבר, "
        "ו-7 לעוזר המוזיקלי."
    )
    return text_response(f"id_list_message={menu_text}&read=t-הקישו את מספר השחזור או בחרו תוכנית,ספרות,1,1,פעם אחת,כן")

def build_record_response(persona_name):
    return text_response(
        f"id_list_message=t-בחרתם ב{persona_name}."
        "&read=t-הקליטו את שאלתכם לאחר הצליל, ובסיום הקישו סולמית=קול_record,,record"
    )

def build_error_response():
    return text_response(
        "id_list_message=אירעה תקלה זמנית. נסו שוב."
        "&read=t-הקליטו את שאלתכם לאחר הצליל, ובסיום הקישו סולמית=קול_תקליט,,תקליט"
    )

# =============================================================================================================
# נתיבי Flask
# =============================================================================================================
@app.route("/", methods=["GET", "POST"])
@app.route("/ivr", methods=["GET", "POST"])
def ivr_entry():
    try:
        cleanup_calls()
        phone = normalize_phone(request.values.get("ApiPhone"))
        api_call_id = normalize_call_id(request.values.get("ApiCallId"))
        call_id = api_call_id or phone

        val = str(request.values.get("val") or request.values.get("menu_choice") or "").strip()
        voice_record = str(request.values.get("voice_record") or request.values.get("הקלטה") or "").strip()
        ext = str(request.values.get("ext") or DATA_EXT).strip().strip("/")

        if not voice_record:
            voice_record = str(request.values.get("קובץ") or request.values.get("שם_קובץ") or "").strip()

        with state_lock:
            call_data = calls.setdefault(
                call_id,
                {
                    "phone": phone,
                    "persona": "1",
                    "history": [],
                    "last_seen": 0
                }
            )
            call_data["phone"] = phone
            call_data["last_seen"] = time.monotonic()

        print("[IVR]", {
            "method": request.method,
            "call_id": call_id,
            "phone": phone,
            "ext": ext,
            "val": val,
            "menu_choice": request.values.get("menu_choice"),
            "voice_record": voice_record,
            "recording": request.values.get("recording")
        })

        if not val and not voice_record:
            return build_menu_response()

        menu_choice = str(request.values.get("menu_choice") or val).strip()
        if menu_choice in DEFAULT_PERSONAS and not voice_record:
            with state_lock:
                calls[call_id]["persona"] = menu_choice
                calls[call_id]["history"] = []
            persona_name = DEFAULT_PERSONAS[menu_choice][0]
            return build_record_response(persona_name)

        file_name = voice_record
        if file_name.startswith("file_"):
            file_name = file_name[5:]
        if not file_name:
            return build_error_response()

        if over_limit(phone):
            return text_response(
                "id_list_message=t-הגעתם למכסת ההודעות היומית. תודה ולהתראות."
                "&go_to_folder=hangup"
            )

        with state_lock:
            saved_call = calls.get(call_id, {})
            persona = saved_call.get("persona", "1")
            history = list(saved_call.get("history", []))

        ai_res = ask_ai(persona, history, ext, file_name)

        if ai_res["type"] == "answer":
            if not consume_message(phone):
                return text_response(
                    "id_list_message=t-הגעת למכסת ההודעות היומית. תודה ולהתראות."
                    "&go_to_folder=hangup"
                )
            with state_lock:
                calls[call_id]["history"].extend([
                    {
                        "role": "user",
                        "parts": [ai_res["transcript"] or "הקלטה קולית"]
                    },
                    {
                        "role": "model",
                        "parts": [ai_res["answer"]]
                    }
                ])
                calls[call_id]["history"] = calls[call_id]["history"][-MAX_HISTORY_PAIRS * 2:]

            return text_response(
                f"id_list_message=t-{ai_res['answer']}"
                "&read=t-השמיעו שאלה נוספת לאחר הצליל, ובסיום הקישו סולמית. לחזרה לתפריט הקישו 0"
                "=הקלטת_קול,,הקלטה"
            )

        if ai_res["type"] == "menu":
            return build_menu_response()

        if ai_res["type"] == "hangup":
            return text_response(
                "id_list_message=t-תודה רבה ולהתראות."
                "&go_to_folder=hangup"
            )

        return text_response(
            f"id_list_message=t-{ai_res['answer']}"
            "&read=t-נסו שוב לאחר הצליל ובסיום הקישו סולמית=הקלטת_קול,,הקלטה"
        )

    except Exception as exc:
        print(f"[שגיאה חמורה ב-IVR] {type(exc).__name__}: {exc}")
        return build_error_response()

@app.route("/health", methods=["GET"])
def health():
    return text_response("OK")

# =============================================================================================================
# הרצת השרת
# =============================================================================================================
if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", "5000") or 5000)
    except ValueError:
        port = 5000

    try:
        from waitress import serve
        print(f"הפעלת YBY AI IVR שרת בפורט {port}...")
        serve(app, host="0.0.0.0", port=port, threads=12)
    except ImportError:
        print("המלצרית אינה מותקנת. חוזר ל-Flask.")
        app.run(host="0.0.0.0", port=port, threaded=True)
