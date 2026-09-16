# -*- coding: utf-8 -*-
"""
מערכת טלפונית לשיחה עם AI - ימות המשיח + Gemini
גרסה מתוקנת וסופית: תמיכה מלאה בחיפוש גוגל, מנגינת המתנה מותאמת אישית (051), ניהול תורים ויציבות.
"""

from flask import Flask, request, Response
from yemot_flow.actions import (
    build_id_list_message,
    build_read,
    build_go_to_folder,
    build_combined_action,
)
from google import genai
from google.genai import types

import os
import re
import json
import threading
import datetime
import time
import traceback
import urllib.request
import urllib.parse

try:
    from zoneinfo import ZoneInfo
    IL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    IL_TZ = None

app = Flask(__name__)

# ============================================================================
#                             הגדרות סביבה
# ============================================================================

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "")
VOICE_EXTS = [e.strip().strip("/") for e in os.environ.get("VOICE_EXTS", "1").split(",") if e.strip()]
if not VOICE_EXTS:
    VOICE_EXTS = ["1"]
DATA_EXT = VOICE_EXTS[0]

OWNER_PHONES = ["0527661756", "0527609296"]
YEMOT_API = "https://www.call2all.co.il/ym/api/"

MAX_WAIT_ROUNDS = int(os.environ.get("MAX_WAIT_ROUNDS", "40"))
SEARCH_MODE = os.environ.get("SEARCH_MODE", "always").strip().lower()
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "8192"))
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "40"))

# ============================================================================
#                                 נתונים
# ============================================================================

names = {}          
LOG = []            
CALLS = []          
LOG_MAX = 1000
_lock = threading.Lock()

calls = {}          
_calls_lock = threading.Lock()

_daily_counts = {}  

GENERAL_RULES = (
    " אתה מדבר בטלפון, לכן ענה קצר וברור, בלי כוכביות, בלי רשימות, בלי אימוג'ים ובלי סימני עיצוב."
    " הגבל את עצמך לשלושה משפטים לכל היותר, אלא אם התבקשת במפורש להרחיב."
    " ענה בשפה שבה המשתמש דיבר אליך; ברירת המחדל היא עברית."
    " שמור על שפה מכובדת וצנועה, ואל תעסוק בנושאים לא צנועים."
    " כשאתה מספק מספרים או מחירים, כתוב אותם במילים בעברית ולא בספרות."
    " יש לך כלי חיפוש Google מחובר. כשנשאלת על מידע עדכני, מחירים, חדשות,"
    " מזג אוויר, שעות פתיחה או כל דבר שהשתנה לאחרונה - השתמש בו והשב לפי התוצאות."
    " אל תאמר למשתמש שאין לך גישה לאינטרנט."
)

PERSONAS = {
    "1": "אתה עוזר כללי ידידותי ומועיל." + GENERAL_RULES,
    "2": "אתה עוזר תורני. ענה בסגנון תורני מכובד, וציין מקורות כשאפשר." + GENERAL_RULES,
    "3": "אתה עוזר חוצפני וסרקסטי עם הומור. ענה בחוצפה משעשעת אבל בלי להעליב באמת." + GENERAL_RULES,
    "4": "אתה עוזר יצירתי. ספר סיפורים קצרים, כתוב שירים, בדיחות ורעיונות יצירתיים." + GENERAL_RULES,
    "5": "אתה עוזר טכני. הסבר דברים טכניים בפשטות: מחשבים, אינטרנט, טלפונים." + GENERAL_RULES,
    "6": "אתה ערס - ידיד קרוב וחמוד. תדבר בנימוס קלוקל מאוד. השתמש בביטויים כמו 'אחלה מה אחי', 'ספר לי', 'בואנו', 'כאן בדיוק'. תרגיש כמו ישיבה עם חבר טוב. פתוח, כיפי ותמיד עם חיוך." + GENERAL_RULES,
    "7": "אתה עוזר מוזיקלי. אתה מומחה למוזיקה, בדגש מיוחד על מוזיקה חסידית וישראלית. ענה על שאלות הקשורות למוזיקה, ספק אקורדים לשירים כשמבקשים, הסבר מושגים במוזיקה ושתף ידע על אמנים, שירים וסגנונות נגינה." + GENERAL_RULES,
}

PERSONA_NAMES = {
    "1": "העוזר הכללי",
    "2": "העוזר התורני",
    "3": "העוזר החוצפן",
    "4": "העוזר היצירתי",
    "5": "העוזר הטכני",
    "6": "הערס",
    "7": "העוזר המוזיקלי",
}

SETTINGS = {
    "daily_limit": int(os.environ.get("DAILY_LIMIT", "40")),
    "unlimited_phones": "0527661756,0527609296",
}

# ============================================================================
#                             מודלים של Gemini
# ============================================================================

MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]

_good_model = [None]
_client = None
_client_lock = threading.Lock()

def get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                api_key = os.environ.get("GEMINI_API_KEY", "").strip()
                if not api_key:
                    raise RuntimeError("GEMINI_API_KEY is missing")
                try:
                    _client = genai.Client(
                        api_key=api_key,
                        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT * 1000),
                    )
                except Exception:
                    _client = genai.Client(api_key=api_key)
    return _client

def _extract_text(response):
    try:
        txt = getattr(response, "text", None)
        if txt and txt.strip():
            return txt.strip()
    except Exception:
        pass
    chunks = []
    try:
        for cand in (getattr(response, "candidates", None) or []):
            content = getattr(cand, "content", None)
            for part in (getattr(content, "parts", None) or []):
                if getattr(part, "thought", False):
                    continue
                t = getattr(part, "text", None)
                if t:
                    chunks.append(t)
    except Exception:
        pass
    return "\n".join(chunks).strip()

def _ordered_models():
    good = _good_model[0]
    if good and good in MODELS:
        return [good] + [m for m in MODELS if m != good]
    return list(MODELS)

def gemini_call(system, contents, use_search=False, deadline=None):
    last_error = None
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("Gemini error: GEMINI_API_KEY is missing")
        return None

    for model in _ordered_models():
        if deadline and time.time() > deadline:
            print("Gemini: deadline exceeded, stopping fallback loop")
            break
        try:
            cfg_kwargs = {
                "system_instruction": system,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0.7,
            }
            if use_search:
                cfg_kwargs["tools"] = [{"google_search": {}}]

            cfg = types.GenerateContentConfig(**cfg_kwargs)

            t0 = time.time()
            response = get_client().models.generate_content(
                model=model, contents=contents, config=cfg,
            )
            text = _extract_text(response)
            took = round(time.time() - t0, 2)

            if text:
                _good_model[0] = model
                print("Gemini OK model=%s search=%s took=%ss" % (model, use_search, took))
                return text

            print("Gemini empty text model=%s search=%s took=%ss" % (model, use_search, took))
        except Exception as e:
            last_error = e
            print("Gemini model error", model, repr(e))
            continue

    print("Gemini final error:", repr(last_error))
    return None

# ============================================================================
#                             עזרי טקסט
# ============================================================================

def clean_for_tts(text, limit=700):
    text = str(text or "")
    text = re.sub(r"\[\d+(?:,\s*\d+)*\]", "", text)
    text = re.sub(r"[*_#`>\[\]{}]", "", text)
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = text.replace("\n", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(,\s*){2,}", ", ", text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    return cut[:dot + 1] if dot > limit * 0.6 else cut

def il_now():
    if IL_TZ is not None:
        return datetime.datetime.now(IL_TZ)
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)

def now_str():
    return il_now().strftime("%d/%m/%Y %H:%M")

def today_str():
    return il_now().strftime("%d/%m/%Y")

# ============================================================================
#                         תקשורת עם ימות המשיח
# ============================================================================

def _http_get(url, timeout):
    req = urllib.request.Request(url, headers={"Connection": "keep-alive"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def yemot_download(ext, file_name):
    url = YEMOT_API + "DownloadFile?" + urllib.parse.urlencode({
        "token": YEMOT_TOKEN,
        "path": "ivr2:/%s/%s.wav" % (ext, file_name),
    })
    data = _http_get(url, 15)
    if not data:
        raise RuntimeError("empty audio file")
    return data

def yemot_delete(ext, file_name):
    def _run():
        try:
            url = YEMOT_API + "FileAction?" + urllib.parse.urlencode({
                "token": YEMOT_TOKEN,
                "action": "delete",
                "what": "ivr2:/%s/%s.wav" % (ext, file_name),
            })
            _http_get(url, 10)
        except Exception as e:
            print("delete error:", repr(e))
    threading.Thread(target=_run, daemon=True).start()

_pending_saves = {}
_save_lock = threading.Lock()

def schedule_save(file_name, text):
    if not YEMOT_TOKEN:
        return
    with _save_lock:
        _pending_saves[file_name] = text

def save_names():
    with _lock:
        data = json.dumps(names, ensure_ascii=False)
    schedule_save("ai_names.txt", data)

def save_log():
    with _lock:
        data = json.dumps({"log": LOG[-LOG_MAX:], "calls": CALLS[-LOG_MAX:]}, ensure_ascii=False)
    schedule_save("ai_log.txt", data)

# ============================================================================
#                         מכסות ומונים
# ============================================================================

def bump_daily(phone):
    key = (today_str(), phone)
    with _lock:
        _daily_counts[key] = _daily_counts.get(key, 0) + 1

def messages_today(phone):
    with _lock:
        return _daily_counts.get((today_str(), phone), 0)

def over_limit(phone):
    limit = int(SETTINGS.get("daily_limit", 0) or 0)
    if limit <= 0:
        return False
    unlimited = [x.strip() for x in str(SETTINGS.get("unlimited_phones", "")).split(",") if x.strip()]
    if phone in unlimited or phone in OWNER_PHONES:
        return False
    return messages_today(phone) >= limit

# ============================================================================
#                      תמלול + תשובה מבוססת טקסט
# ============================================================================

VOICE_WORDS = ("החלף קול", "תחליף קול", "שנה קול", "להחליף קול")
MENU_WORDS = ("תפריט", "חזרה לתפריט", "חזור לתפריט", "תחזור לתפריט")
END_WORDS = ("סיים", "ביי", "להתראות", "תסיים", "סיום")

def build_chat_system(persona):
    now = il_now()
    base = PERSONAS.get(persona, PERSONAS["1"])
    return (
        base
        + "\n\n[הזמן בישראל כרגע: %s, תאריך: %s]" % (now.strftime("%H:%M"), now.strftime("%d/%m/%Y"))
    )

def detect_command(transcript, answer):
    t = (transcript or "").strip().strip(".!? ")
    if any(w in t for w in VOICE_WORDS):
        return "voice"
    if t in MENU_WORDS:
        return "menu"
    if t in END_WORDS:
        return "end"
    return None

def transcribe_only(audio, deadline=None):
    system = (
        "אתה מתמלל הקלטה טלפונית בעברית. "
        "החזר רק את הטקסט שנאמר בהקלטה, בלי הסברים ובלי סימני עיצוב. "
        "אם יש מילים לא ברורות, השלם לפי ההקשר."
    )
    part = types.Part.from_bytes(data=audio, mime_type="audio/wav")
    return clean_for_tts(gemini_call(
        system,
        [part],
        use_search=False,
        deadline=deadline,
    ) or "", limit=400)

def answer_from_text(persona, history, transcript, deadline=None):
    system = build_chat_system(persona)
    contents = list(history) + [{"role": "user", "parts": [{"text": transcript}]}]
    raw = gemini_call(system, contents, use_search=(SEARCH_MODE != "off"), deadline=deadline)
    return clean_for_tts(raw or "")

def ask_ai(persona, history, ext, file_name):
    deadline = time.time() + GEMINI_TIMEOUT * 2

    try:
        audio = yemot_download(ext, file_name)
    except Exception as e:
        print("download error:", repr(e))
        return "", "סליחה, לא הצלחתי לשמוע את ההקלטה. נסה שוב."

    yemot_delete(ext, file_name)

    # שלב 1: תמלול קול לטקסט בלבד
    transcript = transcribe_only(audio, deadline=deadline)
    if not transcript:
        return "", "סליחה, לא הצלחתי להבין את ההקלטה. נסה שוב."

    # שלב 2: קבלת תשובה על בסיס הטקסט (כאן החיפוש עובד בבטחה)
    answer = answer_from_text(persona, history, transcript, deadline=deadline)
    if not answer:
        return transcript, "סליחה, לא הצלחתי להשיג תשובה כרגע. נסה לשאול שוב."

    return transcript, answer

def transcribe_name(ext, file_name):
    try:
        audio = yemot_download(ext, file_name)
    except Exception as e:
        print("download name error:", repr(e))
        return ""
    yemot_delete(ext, file_name)
    text = transcribe_only(audio)
    text = re.sub(r"[^\u0590-\u05FF\- ]", "", text)
    return clean_for_tts(text, limit=30)[:30]

# ============================================================================
#                          בניית תגובות לימות
# ============================================================================

def menu(state, name, prefix=None):
    state["n"] += 1
    state["wait"] = "choice_%d" % state["n"]
    state["stage"] = "menu"
    read = build_read(
        [("text",
          "שלום %s. הקש 1 לעוזר כללי, 2 לעוזר תורני, 3 לעוזר החוצפן, 4 לעוזר היצירתי, "
          "5 לעוזר טכני, 6 לערס, 7 לעוזר המוזיקלי, או 9 לסיום." % name)],
        mode="tap",
        val_name=state["wait"],
        max_digits=1,
        min_digits=1,
        digits_allowed="12345679",
        sec_wait=10,
    )
    if prefix:
        return build_combined_action([build_id_list_message([("text", prefix)]), read])
    return read

def record(state, val_prefix, prompt, prefix=None):
    state["n"] += 1
    state["wait"] = "%s_%d" % (val_prefix, state["n"])
    file_name = "ai_%s_%d" % (re.sub(r"[^0-9a-zA-Z]", "", state["call_id"])[-12:], state["n"])
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
        max_length=25,
    )
    if prefix:
        return build_combined_action([build_id_list_message([("text", prefix)]), read])
    return read

def listen(state, prefix=None, first=False):
    state["stage"] = "chat"
    if first:
        return record(state, "speech",
                      (prefix + ". " if prefix else "") + "דבר אחרי הצפצוף, ובסיום הקש סולמית")
    return record(state, "speech", prefix or "אני מקשיב")

def goodbye(call_id, name):
    with _calls_lock:
        calls.pop(call_id, None)
    return build_combined_action([
        build_id_list_message([("text", "להתראות %s" % name)]),
        build_go_to_folder("hangup"),
    ])

def wait_response(state, ext):
    idx = state.get("polls", 0)
    if idx == 0:
        action = build_id_list_message([("text", "רגע אחד, אני בודק")])
    else:
        # השמעת מנגינת ההמתנה הייעודית.
        # ודא שהקובץ בשם 051 (או שם אחר שתשנה אליו) מועלה למערכת ימות המשיח שלך!
        action = build_id_list_message([("file", "051")])
        
    return build_combined_action([
        action,
        build_go_to_folder("/" + ext),
    ])

def start_job(state, persona, history, ext, file_name):
    job = {"done": False, "transcript": "", "answer": "", "error": None}
    state["job"] = job
    state["polls"] = 0
    state["stage"] = "thinking"
    state["wait"] = None

    def _run():
        try:
            t, a = ask_ai(persona, history, ext, file_name)
            job["transcript"], job["answer"] = t, a
        except Exception as e:
            print("job error:", repr(e))
            traceback.print_exc()
            job["error"] = repr(e)
            job["answer"] = "סליחה, קרתה תקלה זמנית. אפשר לנסות שוב."
        finally:
            job["done"] = True

    threading.Thread(target=_run, daemon=True).start()

# ============================================================================
#                               נקודת הכניסה
# ============================================================================

@app.route("/", methods=["GET", "POST"])
def yemot():
    try:
        return _handle()
    except Exception as e:
        print("FATAL handler error:", repr(e))
        traceback.print_exc()
        return Response(
            build_combined_action([
                build_id_list_message([("text", "סליחה, קרתה תקלה. מחזיר אותך לתפריט")]),
                build_go_to_folder("/"),
            ]),
            mimetype="text/plain; charset=utf-8",
        )

def _handle():
    params = request.values.to_dict()
    call_id = params.get("ApiCallId")
    
    if not call_id:
        return Response("ok", mimetype="text/plain")

    phone = params.get("ApiPhone", "")
    ext = params.get("ApiExtension", "")

    with _calls_lock:
        if call_id not in calls:
            calls[call_id] = {
                "call_id": call_id,
                "phone": phone,
                "stage": "init",
                "n": 0,
                "history": []
            }
            with _lock:
                CALLS.append({"call_id": call_id, "phone": phone, "time": now_str()})
        state = calls[call_id]

    stage = state.get("stage", "init")
    name = names.get(phone, "אורח")

    # 1. זיהוי משתמש חדש - בקשת שם
    if stage == "init":
        if phone and phone not in names:
            state["stage"] = "ask_name"
            state["n"] += 1
            state["wait"] = "name_%d" % state["n"]
            file_name = "name_%s" % re.sub(r"[^0-9a-zA-Z]", "", call_id)[-12:]
            state["file"] = file_name
            
            return Response(
                build_read(
                    [("text", "ברוך הבא. כדי שנוכל להכיר, אנא אמור את שמך לאחר הצפצוף, ובסיום הקש סולמית.")],
                    mode="record",
                    val_name=state["wait"],
                    path="",
                    file_name=file_name,
                    no_confirm_menu="no",
                    save_on_hangup="no",
                    min_length="",
                    max_length=6
                ),
                mimetype="text/plain"
            )
        else:
            return Response(menu(state, name), mimetype="text/plain")

    # 2. שמירת השם המוקלט
    elif stage == "ask_name":
        file_name = state.get("file")
        if file_name:
            text = transcribe_name(ext, file_name)
            if text:
                names[phone] = text
                name = text
                save_names()
                
        return Response(menu(state, name), mimetype="text/plain")

    # 3. תפריט בחירת אישיות
    elif stage == "menu":
        choice = params.get(state.get("wait", ""))
        if choice == "9":
            return Response(goodbye(call_id, name), mimetype="text/plain")
        if choice in PERSONAS:
            state["persona"] = choice
            return Response(listen(state, f"בחרת ב{PERSONA_NAMES[choice]}", first=True), mimetype="text/plain")
        return Response(menu(state, name, "בחירה לא חוקית"), mimetype="text/plain")

    # 4. הקלטת שאלת המשתמש
    elif stage == "chat":
        if over_limit(phone):
            return Response(goodbye(call_id, name), mimetype="text/plain")

        file_name = state.get("file")
        if not file_name:
            return Response(listen(state, "לא זוהתה הקלטה"), mimetype="text/plain")

        start_job(state, state.get("persona", "1"), state.get("history", []), ext, file_name)
        return Response(wait_response(state, ext), mimetype="text/plain")

    # 5. מצב המתנה (Polling) בזמן עבודת ה-AI
    elif stage == "thinking":
        job = state.get("job")
        if not job:
            return Response(menu(state, name, "שגיאה במערכת, מחזיר לתפריט"), mimetype="text/plain")

        if not job.get("done"):
            state["polls"] = state.get("polls", 0) + 1
            if state["polls"] >= MAX_WAIT_ROUNDS:
                state["stage"] = "chat"
                return Response(listen(state, "הפעולה ארכה זמן רב מדי. אנא נסה שוב."), mimetype="text/plain")
            return Response(wait_response(state, ext), mimetype="text/plain")

        ans = job.get("answer", "")
        transcript = job.get("transcript", "")
        cmd = detect_command(transcript, ans)

        if cmd == "menu":
            return Response(menu(state, name), mimetype="text/plain")
        elif cmd == "end":
            return Response(goodbye(call_id, name), mimetype="text/plain")

        # שמירת היסטוריית השיחה לזכרון של Gemini
        state.setdefault("history", []).extend([
            {"role": "user", "parts": [{"text": transcript}]},
            {"role": "model", "parts": [{"text": ans}]}
        ])
        state["history"] = state["history"][-10:]  # שומר רק 10 הודעות אחרונות

        with _lock:
            LOG.append({
                "time": now_str(),
                "phone": phone,
                "name": name,
                "persona": PERSONA_NAMES.get(state.get("persona", "1"), ""),
                "q": transcript,
                "a": ans
            })
            save_log()
            
        bump_daily(phone)
        state["stage"] = "chat"
        return Response(listen(state, ans), mimetype="text/plain")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
