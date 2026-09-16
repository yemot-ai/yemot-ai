import datetime as dt
import hashlib
import hmac
import html
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
from yemot_flow.actions import (
    build_combined_action,
    build_go_to_folder,
    build_id_list_message,
    build_read,
)

app = Flask(__name__)

# ============================================================
# הגדרות
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

# ============================================================
# מודלי Gemini תקינים
# ============================================================

AI_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
]

# ============================================================
# מגבלות
# ============================================================

MAX_AUDIO_SECONDS = 25
MAX_AUDIO_BYTES = 7 * 1024 * 1024
MAX_HISTORY_PAIRS = 4
CALL_TTL_SECONDS = 45 * 60

try:
    DAILY_LIMIT = max(0, int(os.environ.get("DAILY_LIMIT", "40") or 0))
except Exception:
    DAILY_LIMIT = 40

OWNER_PHONES = {
    "0527661756",
    "0527609296",
}

# ============================================================
# זיכרון זמני
# ============================================================

names = {}
persona_names = {}
persona_prompts = {}
calls = {}
usage = {}

state_lock = threading.RLock()
client_lock = threading.Lock()
_client = None

# ============================================================
# כללים כלליים ל-AI
# ============================================================

GENERAL_RULES = (
    " אתה מדבר בטלפון, לכן ענה קצר וברור, "
    "בלי כוכביות, בלי רשימות, בלי אימוג'ים "
    "ובלי סימני עיצוב."
    " ענה בשפה שבה המשתמש דיבר אליך; "
    "ברירת המחדל היא עברית."
    " שמור על שפה מכובדת."
    " כשאתה מספק מספרים או מחירים, "
    "כתוב אותם במילים בעברית ולא בספרות."
)

DEFAULT_PERSONAS = {
    "1": ("העוזר הכללי", "אתה עוזר כללי ידידותי ומועיל."),
    "2": ("העוזר הלימודי", "אתה עוזר לימודי ומכובד. ענה בצורה ברורה ומסודרת."),
    "3": ("העוזר החוצפן", "אתה עוזר חוצפני וסרקסטי עם הומור. היה משעשע אבל אל תעליב באמת."),
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

def clean_for_tts(text, limit=650):
    text = str(text or "")
    text = re.sub(r"[*_#`>\[\]{}]", "", text)
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = text.replace("\r", " ")
    text = text.replace("\n", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]

def normalize_phone(phone):
    phone = re.sub(r"\D", "", str(phone or ""))
    return phone or "unknown"

def cleanup_calls():
    now = time.monotonic()
    today = il_now().date().isoformat()
    with state_lock:
        expired_calls = [
            call_id for call_id, state in calls.items()
            if now - state.get("last_seen", 0) > CALL_TTL_SECONDS
        ]
        for call_id in expired_calls:
            calls.pop(call_id, None)

        expired_usage = [
            phone for phone, item in usage.items()
            if item.get("day") != today
        ]
        for phone in expired_usage:
            usage.pop(phone, None)

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

def yemot_download(ext, file_name):
    if not YEMOT_TOKEN:
        raise RuntimeError("YEMOT_TOKEN is missing")
    url = (
        YEMOT_API + "DownloadFile?" +
        urllib.parse.urlencode({
            "token": YEMOT_TOKEN,
            "path": f"ivr2:/{ext}/{file_name}.wav",
        })
    )
    with urllib.request.urlopen(url, timeout=20) as response:
        data = response.read(MAX_AUDIO_BYTES + 1)
    if not data:
        raise RuntimeError("empty audio")
    if len(data) > MAX_AUDIO_BYTES:
        raise RuntimeError("audio file too large")
    return data

def yemot_delete(ext, file_name):
    if not YEMOT_TOKEN:
        return
    try:
        url = (
            YEMOT_API + "FileAction?" +
            urllib.parse.urlencode({
                "token": YEMOT_TOKEN,
                "action": "delete",
                "what": f"ivr2:/{ext}/{file_name}.wav",
            })
        )
        urllib.request.urlopen(url, timeout=8).read()
    except Exception as exc:
        print("delete error:", repr(exc))

def delete_audio_async(ext, file_name):
    threading.Thread(
        target=yemot_delete,
        args=(ext, file_name),
        daemon=True
    ).start()

def yemot_read_text(file_name):
    if not YEMOT_TOKEN:
        return None
    try:
        url = (
            YEMOT_API + "DownloadFile?" +
            urllib.parse.urlencode({
                "token": YEMOT_TOKEN,
                "path": f"ivr2:/{DATA_EXT}/{file_name}",
            })
        )
        with urllib.request.urlopen(url, timeout=12) as response:
            data = response.read(512 * 1024).decode("utf-8", "ignore")
        if data.lstrip().startswith('{"responseStatus'):
            return None
        return data
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        print("read text HTTP error:", exc.code, repr(exc))
        return None
    except Exception as exc:
        print("read text error:", repr(exc))
        return None

def yemot_write_text(file_name, text):
    if not YEMOT_TOKEN:
        return False
    try:
        body = urllib.parse.urlencode({
            "token": YEMOT_TOKEN,
            "what": f"ivr2:/{DATA_EXT}/{file_name}",
            "contents": text,
        }).encode("utf-8")
        req = urllib.request.Request(
            YEMOT_API + "UploadTextFile",
            data=body,
            method="POST"
        )
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as exc:
        print("write text error:", repr(exc))
        return False

def save_names():
    with state_lock:
        data = json.dumps(names, ensure_ascii=False)
    threading.Thread(
        target=yemot_write_text,
        args=("ai_names.txt", data),
        daemon=True
    ).start()

def save_personas():
    with state_lock:
        data = json.dumps(
            {"names": persona_names, "prompts": persona_prompts},
            ensure_ascii=False
        )
    threading.Thread(
        target=yemot_write_text,
        args=("ai_personas.txt", data),
        daemon=True
    ).start()

def load_names():
    text = yemot_read_text("ai_names.txt")
    if not text:
        return
    try:
        data = json.loads(text)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    with state_lock:
        for phone, name in data.items():
            name = clean_for_tts(name, 30)
            if name:
                names[normalize_phone(phone)] = name

def load_personas():
    text = yemot_read_text("ai_personas.txt")
    if not text:
        return
    try:
        data = json.loads(text)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    with state_lock:
        loaded_names = data.get("names")
        loaded_prompts = data.get("prompts")
        if isinstance(loaded_names, dict):
            for key, value in loaded_names.items():
                if key in persona_names and value:
                    persona_names[key] = clean_for_tts(value, 40)
        if isinstance(loaded_prompts, dict):
            for key, value in loaded_prompts.items():
                if key in persona_prompts and value:
                    persona_prompts[key] = clean_for_tts(value, 1200)

if YEMOT_TOKEN:
    load_names()
    load_personas()

# ============================================================
# Gemini Client
# ============================================================

def get_client():
    global _client
    if _client is not None:
        return _client
    with client_lock:
        if _client is not None:
            return _client
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is missing")
        _client = genai.Client(api_key=GEMINI_API_KEY)
        return _client

def ai_system(persona_key):
    with state_lock:
        persona = persona_prompts.get(persona_key, persona_prompts["1"])
    now = il_now()
    return (
        persona + GENERAL_RULES +
        f" [תאריך בישראל: {now:%d/%m/%Y}; שעה: {now:%H:%M}] " +
        """
אתה מקבל הקלטה קולית של משתמש בטלפון.
הבן מה המשתמש אמר וענה לו בתשובה טבעית וקצרה.

אם המשתמש מבקש מידע עדכני או מידע שיכול להשתנות, השתמש ב-Google Search.
אם אין צורך במידע עדכני, אל תבצע חיפוש מיותר.

פקודות מיוחדות:
החלף קול / תחליף קול / שנה קול -> COMMAND|CHANGE_VOICE
תפריט / חזרה -> COMMAND|MENU
סיים / ביי / להתראות -> COMMAND|HANGUP

בכל שאלה רגילה החזר בדיוק:
TRANSCRIPT|הטקסט שהמשתמש אמר
ANSWER|התשובה שלך

אל תוסיף שום דבר אחר.
התשובה חייבת להיות קצרה ומתאימה להשמעה בטלפון.
אין כוכביות. אין רשימות. אין אימוג'ים.
"""
    )

def ai_audio_turn(persona_key, history, audio):
    contents = list(history)
    contents.append({
        "role": "user",
        "parts": [
            types.Part.from_bytes(data=audio, mime_type="audio/wav"),
            types.Part.from_text(text="האזן להקלטה ופעל לפי ההוראות."),
        ],
    })

    for model in AI_MODELS:
        try:
            config = types.GenerateContentConfig(
                system_instruction=ai_system(persona_key),
                max_output_tokens=180,
                tools=[{"google_search": {}}],
            )

            result = get_client().models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

            text = getattr(result, "text", None)
            if text:
                print(f"Gemini success ({model}):", text[:50])
                return text.strip()

            print(f"Gemini empty response from {model}")

        except Exception as exc:
            print(f"Gemini error on {model}:", repr(exc))

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

    transcript = clean_for_tts(transcript_match.group(1).strip(), 500) if transcript_match else ""
    answer = clean_for_tts(answer_match.group(1).strip(), 700) if answer_match else ""

    if not answer:
        lines = [x.strip() for x in raw.splitlines() if x.strip()]
        if lines:
            answer = re.sub(r"^(ANSWER|TRANSCRIPT)\s*\|\s*", "", lines[-1], flags=re.IGNORECASE)
            answer = clean_for_tts(answer, 700)

    return {
        "type": "answer" if answer else "error",
        "transcript": transcript,
        "answer": answer,
    }

def ask_ai(persona_key, history, ext, file_name):
    try:
        audio = yemot_download(ext, file_name)
    except Exception as exc:
        print("download error:", repr(exc))
        return {
            "type": "error",
            "transcript": "",
            "answer": "סליחה, לא הצלחתי לקבל את ההקלטה. נסה שוב.",
        }

    delete_audio_async(ext, file_name)
    raw = ai_audio_turn(persona_key, history, audio)
    result = parse_ai_result(raw)

    if result["type"] == "error":
        result["answer"] = "סליחה, יש בעיה זמנית בחיבור ל-AI. נסה שוב."

    return result

def menu(state, name, prefix=None):
    state["n"] += 1
    state["wait"] = f"choice_{state['n']}"
    state["stage"] = "menu"

    text = (
        f"שלום {name}. "
        "הקש 1 לעוזר כללי, 2 לעוזר לימודי, 3 לעוזר החוצפן, "
        "4 לעוזר היצירתי, 5 לעוזר טכני, 6 לחבר, 7 לעוזר המוזיקלי, או 9 לסיום."
    )

    read = build_read(
        [("text", text)],
        mode="tap",
        val_name=state["wait"],
        max_digits=1,
        min_digits=1,
        digits_allowed="12345679",
        sec_wait=10,
    )

    if prefix:
        return build_combined_action([
            build_id_list_message([("text", prefix)]),
            read,
        ])
    return read

def record(state, prompt, prefix=None):
    state["n"] += 1
    state["wait"] = f"speech_{state['n']}"

    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", state["call_id"]) or "call"
    file_name = f"ai_{safe_id[-18:]}_{state['n']}"
    state["file"] = file_name

    read = build_read(
        [("text", prompt)],
        mode="record",
        val_name=state["wait"],
        path="",
        file_name=file_name,
        no_confirm_menu="no",
        save_on_hangup="no",
        min_length="",
        max_length=MAX_AUDIO_SECONDS,
    )

    if prefix:
        return build_combined_action([
            build_id_list_message([("text", prefix)]),
            read,
        ])
    return read

def listen(state, prefix=None, first=False):
    state["stage"] = "chat"
    prompt = "דבר אחרי הצפצוף, ובסיום הקש סולמית" if first else "אני מקשיב"
    return record(state, prompt, prefix=prefix)

def goodbye(call_id, name):
    with state_lock:
        calls.pop(call_id, None)
    return build_combined_action([
        build_id_list_message([("text", f"להתראות {name}")]),
        build_go_to_folder("hangup"),
    ])

@app.route("/", methods=["GET", "POST"])
def yemot():
    cleanup_calls()
    params = request.values.to_dict()
    call_id = params.get("ApiCallId", "").strip()

    if not call_id:
        return Response("ok", mimetype="text/plain; charset=utf-8")

    if params.get("hangup") == "yes":
        with state_lock:
            calls.pop(call_id, None)
        return Response("noop", mimetype="text/plain; charset=utf-8")

    phone = normalize_phone(params.get("ApiPhone", "unknown"))
    ext = (params.get("ApiExtension", "") or VOICE_EXTS[0]).strip("/")
    if not ext or ext not in VOICE_EXTS:
        ext = VOICE_EXTS[0]

    with state_lock:
        state = calls.get(call_id)
        if state is None:
            state = {
                "stage": "menu",
                "n": 0,
                "wait": None,
                "persona": None,
                "history": [],
                "call_id": call_id,
                "file": None,
                "resume": None,
                "voice_ext": ext,
                "last_seen": time.monotonic(),
                "message_count": 0,
                "lock": threading.Lock(),
            }
            calls[call_id] = state
        else:
            state["last_seen"] = time.monotonic()
            state["voice_ext"] = ext

    with state["lock"]:
        return handle_call(state, params, phone, ext)

def handle_call(state, params, phone, ext):
    has_value = bool(state["wait"]) and state["wait"] in params
    value = (params.get(state["wait"], "") or "").strip() if has_value else ""
    if value == "None":
        value = ""

    with state_lock:
        name = names.get(phone) or "אורח"

    if state.get("resume"):
        mode = state["resume"]
        state["resume"] = None
        prefix = "הקול הוחלף"
        res = listen(state, prefix=prefix) if mode == "chat" else menu(state, name, prefix=prefix)
        return Response(res, mimetype="text/plain; charset=utf-8")

    if state["stage"] == "menu":
        if value == "9":
            return Response(goodbye(state["call_id"], name), mimetype="text/plain; charset=utf-8")
        if value in persona_names:
            state["persona"] = value
            state["history"] = []
            state["message_count"] = 0
            persona = persona_names[value]
            return Response(
                listen(state, prefix=f"אתה עכשיו עם {persona}", first=True),
                mimetype="text/plain; charset=utf-8",
            )
        return Response(menu(state, name), mimetype="text/plain; charset=utf-8")

    if state["stage"] == "chat":
        if not has_value:
            return Response(listen(state, prefix="לא שמעתי אותך"), mimetype="text/plain; charset=utf-8")

        if over_limit(phone) or not consume_message(phone):
            if state.get("file"):
                delete_audio_async(ext, state["file"])
            state["stage"] = "menu"
            msg = "הגעת למכסת ההודעות היומית שלך. אפשר לנסות שוב מחר"
            return Response(menu(state, name, prefix=msg), mimetype="text/plain; charset=utf-8")

        result = ask_ai(state["persona"], state["history"], ext, state["file"])
        result_type = result.get("type")
        answer = result.get("answer", "")

        if result_type == "change_voice":
            if len(VOICE_EXTS) < 2:
                return Response(listen(state, prefix="אין קולות נוספים להחלפה"), mimetype="text/plain; charset=utf-8")
            current_idx = VOICE_EXTS.index(ext) if ext in VOICE_EXTS else 0
            next_ext = VOICE_EXTS[(current_idx + 1) % len(VOICE_EXTS)]
            state["resume"] = "chat"
            return Response(build_go_to_folder(f"/{next_ext}"), mimetype="text/plain; charset=utf-8")

        if result_type == "menu":
            state["stage"] = "menu"
            return Response(menu(state, name), mimetype="text/plain; charset=utf-8")

        if result_type == "hangup":
            return Response(goodbye(state["call_id"], name), mimetype="text/plain; charset=utf-8")

        if result_type == "answer" and answer:
            if result.get("transcript"):
                state["history"].append({"role": "user", "parts": [result["transcript"]]})
                state["history"].append({"role": "model", "parts": [answer]})
                if len(state["history"]) > MAX_HISTORY_PAIRS * 2:
                    state["history"] = state["history"][-MAX_HISTORY_PAIRS * 2:]

            return Response(listen(state, prefix=answer), mimetype="text/plain; charset=utf-8")

        return Response(listen(state, prefix="סליחה, יש בעיה זמנית בחיבור ל-AI. נסה שוב."), mimetype="text/plain; charset=utf-8")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
