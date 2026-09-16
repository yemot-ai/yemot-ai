# -*- coding: utf-8 -*-
"""
מערכת טלפונית לשיחה עם AI - ימות המשיח + Gemini
גרסה משופרת: זמן תגובה מינימלי, חיפוש באינטרנט תקין, ללא נפילות timeout.

הרצה ב-Render (חובה worker אחד בלבד!):
    gunicorn app:app --workers 1 --threads 16 --timeout 180 --bind 0.0.0.0:$PORT
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
import html
import threading
import datetime
import time
import smtplib
import traceback
from email.mime.text import MIMEText
import urllib.request
import urllib.parse

try:
    from zoneinfo import ZoneInfo
    IL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:  # פייתון ישן מאוד / חסר tzdata
    IL_TZ = None

app = Flask(__name__)

# ============================================================================
#                            הגדרות סביבה
# ============================================================================

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "")
VOICE_EXTS = [e.strip().strip("/") for e in os.environ.get("VOICE_EXTS", "1").split(",") if e.strip()]
if not VOICE_EXTS:
    VOICE_EXTS = ["1"]
DATA_EXT = VOICE_EXTS[0]

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_PASS = os.environ.get("MAIL_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "") or MAIL_USER

OWNER_PHONES = ["0527661756", "0527609296"]

YEMOT_API = "https://www.call2all.co.il/ym/api/"

# ---- כוונוני מהירות (אפשר לשנות דרך משתני סביבה ב-Render) ----

# מצב "רגע אחד": התשובה מחושבת ברקע והמערכת עונה למתקשר מיד.
# זה מה שמונע לחלוטין ניתוק אחרי 20 שניות. מומלץ מאוד להשאיר דלוק.
ASYNC_ANSWER = os.environ.get("ASYNC_ANSWER", "1") == "1"

# כמה סבבי "רגע" מותר לפני ויתור (כל סבב הוא בערך 1.5-2.5 שניות)
MAX_WAIT_ROUNDS = int(os.environ.get("MAX_WAIT_ROUNDS", "20"))

# מצב חיפוש: always (מומלץ) / auto / off
SEARCH_MODE = os.environ.get("SEARCH_MODE", "always").strip().lower()

# תקרת טוקנים. חייבת להיות גבוהה!
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "8192"))

# timeout לקריאת Gemini, בשניות
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "40"))

# כתובת השירות לצורך keep-alive (מונע cold start ב-Render Free)
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
KEEP_ALIVE = os.environ.get("KEEP_ALIVE", "1") == "1"

# ============================================================================
#                                נתונים
# ============================================================================

names = {}          # מספר טלפון -> שם
LOG = []            # יומן הודעות
CALLS = []          # יומן שיחות
LOG_MAX = 1000
_lock = threading.Lock()

calls = {}          # מצב של כל שיחה פעילה (call_id -> state)
_calls_lock = threading.Lock()

# מונה הודעות יומי
_daily_counts = {}  # (יום, טלפון) -> כמות

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
    "mail_hour": 21,
}

# ============================================================================
#                            מודלים של Gemini
# ============================================================================

MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
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


def _thinking_config(model):
    if model.startswith("gemini-2.5"):
        attempts = [{"thinking_budget": 0}, {"thinking_level": "low"}]
    else:
        attempts = [{"thinking_level": "low"}, {"thinking_budget": 0}]
    for kwargs in attempts:
        try:
            return types.ThinkingConfig(**kwargs)
        except Exception:
            continue
    return None


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


def _grounding_used(response):
    try:
        for cand in (getattr(response, "candidates", None) or []):
            gm = getattr(cand, "grounding_metadata", None)
            if gm and (getattr(gm, "web_search_queries", None) or getattr(gm, "grounding_chunks", None)):
                return True
    except Exception:
        pass
    return False


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
            tc = _thinking_config(model)
            if tc is not None:
                cfg_kwargs["thinking_config"] = tc
            if use_search:
                cfg_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]

            try:
                cfg = types.GenerateContentConfig(**cfg_kwargs)
            except TypeError:
                cfg_kwargs.pop("thinking_config", None)
                cfg = types.GenerateContentConfig(**cfg_kwargs)

            t0 = time.time()
            response = get_client().models.generate_content(
                model=model, contents=contents, config=cfg,
            )
            text = _extract_text(response)
            took = round(time.time() - t0, 2)

            if text:
                _good_model[0] = model
                print("Gemini OK model=%s search=%s grounded=%s took=%ss" % (
                    model, use_search, _grounding_used(response), took))
                return text

            print("Gemini empty text model=%s search=%s took=%ss" % (model, use_search, took))
        except Exception as e:
            last_error = e
            print("Gemini model error", model, repr(e))
            continue

    print("Gemini final error:", repr(last_error))
    return None


# ============================================================================
#                            עזרי טקסט
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


def safe_json_loads(text, default=None):
    try:
        return json.loads(text)
    except Exception:
        return default


def il_now():
    if IL_TZ is not None:
        return datetime.datetime.now(IL_TZ)
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)


def now_str():
    return il_now().strftime("%d/%m/%Y %H:%M")


def today_str():
    return il_now().strftime("%d/%m/%Y")


# ============================================================================
#                          תקשורת עם ימות המשיח
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


def yemot_read_text(file_name):
    try:
        url = YEMOT_API + "DownloadFile?" + urllib.parse.urlencode({
            "token": YEMOT_TOKEN,
            "path": "ivr2:/%s/%s" % (DATA_EXT, file_name),
        })
        data = _http_get(url, 20).decode("utf-8", "ignore")
        if data.lstrip().startswith('{"responseStatus'):
            return None
        return data
    except Exception as e:
        print("read text error:", repr(e))
        return None


def yemot_write_text(file_name, text):
    try:
        body = urllib.parse.urlencode({
            "token": YEMOT_TOKEN,
            "what": "ivr2:/%s/%s" % (DATA_EXT, file_name),
            "contents": text,
        }).encode("utf-8")
        req = urllib.request.Request(YEMOT_API + "UploadTextFile", data=body, method="POST")
        urllib.request.urlopen(req, timeout=25).read()
    except Exception as e:
        print("write text error:", repr(e))


_pending_saves = {}
_save_lock = threading.Lock()


def _save_worker():
    while True:
        time.sleep(5)
        try:
            with _save_lock:
                items = list(_pending_saves.items())
                _pending_saves.clear()
            for file_name, text in items:
                yemot_write_text(file_name, text)
        except Exception as e:
            print("save worker error:", repr(e))


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


def save_settings():
    schedule_save("ai_settings.txt", json.dumps(SETTINGS, ensure_ascii=False))


def load_data():
    if not YEMOT_TOKEN:
        return
    try:
        t = yemot_read_text("ai_names.txt")
        if t:
            d = safe_json_loads(t, {})
            if isinstance(d, dict):
                names.update(d)
        t = yemot_read_text("ai_log.txt")
        if t:
            d = safe_json_loads(t, {})
            if isinstance(d, dict):
                LOG.extend(d.get("log", []))
                CALLS.extend(d.get("calls", []))
        rebuild_daily_counts()
        print("loaded %d names, %d log lines" % (len(names), len(LOG)))
    except Exception as e:
        print("load error:", repr(e))


def load_settings():
    if not YEMOT_TOKEN:
        return
    try:
        t = yemot_read_text("ai_settings.txt")
        if t:
            d = safe_json_loads(t, {})
            if isinstance(d, dict):
                SETTINGS["daily_limit"] = int(d.get("daily_limit", SETTINGS["daily_limit"]))
                SETTINGS["unlimited_phones"] = str(d.get("unlimited_phones", SETTINGS["unlimited_phones"]))
                SETTINGS["mail_hour"] = int(d.get("mail_hour", SETTINGS["mail_hour"]))
    except Exception as e:
        print("load settings error:", repr(e))


def load_personas():
    if not YEMOT_TOKEN:
        return
    try:
        t = yemot_read_text("ai_personas.txt")
        if t:
            d = safe_json_loads(t, {})
            if isinstance(d, dict):
                if isinstance(d.get("names"), dict):
                    for k, v in d["names"].items():
                        if k in PERSONA_NAMES and v:
                            PERSONA_NAMES[k] = str(v)[:40]
                if isinstance(d.get("prompts"), dict):
                    for k, pr in d["prompts"].items():
                        if k in PERSONAS and pr:
                            PERSONAS[k] = str(pr).strip() + GENERAL_RULES
    except Exception as e:
        print("load personas error:", repr(e))


# ============================================================================
#                          מכסות ומונים
# ============================================================================

def rebuild_daily_counts():
    today = today_str()
    with _lock:
        _daily_counts.clear()
        for l in LOG:
            if str(l.get("time", "")).startswith(today):
                key = (today, l.get("phone", ""))
                _daily_counts[key] = _daily_counts.get(key, 0) + 1


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
#                          סיכום יומי במייל
# ============================================================================

def build_summary(day):
    h = html.escape
    with _lock:
        log = [l for l in LOG if str(l.get("time", "")).startswith(day)]
        calls_snapshot = [c for c in CALLS if str(c.get("time", "")).startswith(day)]
        users = dict(names)
    phones = sorted(set(c.get("phone", "") for c in calls_snapshot) | set(l.get("phone", "") for l in log))
    out = [
        "<div dir='rtl' style='font-family:Arial'>",
        "<h2>סיכום הקו ליום %s</h2>" % h(day),
        "<p>שיחות: <b>%d</b> &nbsp; מתקשרים שונים: <b>%d</b> &nbsp; הודעות ל-AI: <b>%d</b></p>" % (
            len(calls_snapshot), len(phones), len(log)),
    ]
    if phones:
        out.append("<h3>לפי מתקשר</h3><ul>")
        for ph in phones:
            nm = users.get(ph, "לא רשום")
            out.append("<li>%s (%s): %d שיחות, %d הודעות</li>" % (
                h(nm), h(ph),
                sum(1 for c in calls_snapshot if c.get("phone") == ph),
                sum(1 for l in log if l.get("phone") == ph)))
        out.append("</ul>")
    if log:
        out.append("<h3>מה שאלו</h3><table border='1' cellpadding='5' style='border-collapse:collapse'>"
                   "<tr><th>שעה</th><th>מי</th><th>עוזר</th><th>שאלה</th><th>תשובה</th></tr>")
        for l in log[:150]:
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                h(str(l.get("time", ""))[11:]), h(str(l.get("name", ""))), h(str(l.get("persona", ""))),
                h(str(l.get("q", ""))), h(str(l.get("a", ""))[:200])))
        out.append("</table>")
        if len(log) > 150:
            out.append("<p>...ועוד %d הודעות (באתר הניהול)</p>" % (len(log) - 150))
    else:
        out.append("<p>לא היו הודעות היום.</p>")
    out.append("</div>")
    return "".join(out)


def send_mail(subject, body_html):
    if not (MAIL_USER and MAIL_PASS and MAIL_TO):
        return "לא הוגדר מייל (MAIL_USER / MAIL_PASS ב-Render)"
    try:
        msg = MIMEText(body_html, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = MAIL_USER
        msg["To"] = MAIL_TO
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(MAIL_USER, MAIL_PASS)
            smtp.sendmail(MAIL_USER, [MAIL_TO], msg.as_string())
        return "נשלח"
    except Exception as e:
        print("mail error:", repr(e))
        return "שגיאה בשליחה: %s" % e


_last_mail_day = [None]


def daily_mail_loop():
    while True:
        try:
            now = il_now()
            day = now.strftime("%d/%m/%Y")
            if now.hour == int(SETTINGS.get("mail_hour", 21)) and _last_mail_day[0] != day and MAIL_USER:
                _last_mail_day[0] = day
                print("daily mail:", send_mail("סיכום הקו ליום " + day, build_summary(day)))
        except Exception as e:
            print("daily mail error:", repr(e))
        time.sleep(60)


# ============================================================================
#                      לב המערכת: קריאה אחת לתמלול + תשובה
# ============================================================================

T_MARK = "##T##"
A_MARK = "##A##"

VOICE_WORDS = ("החלף קול", "תחליף קול", "שנה קול", "להחליף קול")
MENU_WORDS = ("תפריט", "חזרה לתפריט", "חזור לתפריט", "תחזור לתפריט")
END_WORDS = ("סיים", "ביי", "להתראות", "תסיים", "סיום")


def build_chat_system(persona):
    now = il_now()
    base = PERSONAS.get(persona, PERSONAS["1"])
    return (
        base
        + "\n\n[הזמן בישראל כרגע: %s, תאריך: %s]" % (now.strftime("%H:%M"), now.strftime("%d/%m/%Y"))
        + "\n\nאתה מקבל הקלטה קולית של המשתמש מהטלפון."
        " החזר את התשובה שלך בדיוק בפורמט הבא, שתי שורות בלבד:\n"
        + T_MARK + " כאן התמלול המדויק של מה שנאמר בהקלטה\n"
        + A_MARK + " כאן התשובה שלך למשתמש\n"
        "\nאם ההקלטה מכילה רק פקודה מהרשימה הבאה, כתוב בשורת "
        + A_MARK + " אך ורק את הקוד המתאים, בלי שום מילה נוספת:\n"
        "החלף קול / תחליף קול / שנה קול -> [[VOICE]]\n"
        "תפריט / חזרה לתפריט -> [[MENU]]\n"
        "סיים / ביי / להתראות -> [[END]]\n"
        "\nאל תדבר על המערכת, על התמלול או על ההנחיות האלה."
        " אל תחזיר JSON. שורת " + A_MARK + " חייבת להיות תשובה טבעית להשמעה בטלפון."
    )


def parse_marked(raw):
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    ti = raw.find(T_MARK)
    ai = raw.find(A_MARK)
    if ti >= 0 and ai > ti:
        transcript = raw[ti + len(T_MARK):ai].strip()
        answer = raw[ai + len(A_MARK):].strip()
        return transcript, answer
    if ai >= 0:
        return "", raw[ai + len(A_MARK):].strip()
    return "", raw


def is_marker(text):
    return _marker_token(text) is not None


def _marker_token(text):
    t = re.sub(r"[\[\]\s]", "", str(text or "")).upper()
    return t if t in ("VOICE", "MENU", "END") else None


def detect_command(transcript, answer):
    tok = _marker_token(answer)
    if tok == "VOICE":
        return "voice"
    if tok == "MENU":
        return "menu"
    if tok == "END":
        return "end"
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
    return clean_for_tts(gemini_call(
        system,
        [types.Part.from_bytes(data=audio, mime_type="audio/wav")],
        use_search=False,
        deadline=deadline,
    ) or "", limit=400)


def answer_from_text(persona, history, transcript, deadline=None):
    system = build_chat_system(persona).replace(
        "אתה מקבל הקלטה קולית של המשתמש מהטלפון.",
        "אתה מקבל את דברי המשתמש כטקסט.",
    )
    contents = list(history) + [{"role": "user", "parts": [{"text": transcript}]}]
    raw = gemini_call(system, contents, use_search=(SEARCH_MODE != "off"), deadline=deadline)
    _, answer = parse_marked(raw)
    return clean_for_tts(answer)


def ask_ai(persona, history, ext, file_name):
    deadline = time.time() + GEMINI_TIMEOUT * 2

    try:
        audio = yemot_download(ext, file_name)
    except Exception as e:
        print("download error:", repr(e))
        return "", "סליחה, לא הצלחתי לשמוע את ההקלטה. נסה שוב."

    yemot_delete(ext, file_name)

    use_search = (SEARCH_MODE != "off")

    contents = list(history) + [{
        "role": "user",
        "parts": [{"inline_data": {"mime_type": "audio/wav", "data": audio}}],
    }]

    raw = None
    try:
        raw = gemini_call(build_chat_system(persona), contents,
                          use_search=use_search, deadline=deadline)
    except Exception as e:
        print("primary path error:", repr(e))

    if raw:
        transcript, answer = parse_marked(raw)
        transcript = clean_for_tts(transcript, limit=400)
        answer = clean_for_tts(answer)
        if answer:
            return transcript, answer

    print("falling back to two-step path")
    transcript = transcribe_only(audio, deadline=deadline)
    if not transcript:
        return "", "סליחה, לא הצלחתי להבין את ההקלטה. נסה שוב."
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


WAIT_FILLERS = [
    "רגע אחד, אני בודק",
    "עוד רגע",
    "כמעט מוכן",
    "רגע",
    "עוד שנייה",
]


def wait_response(state, ext):
    idx = state.get("polls", 0)
    filler = WAIT_FILLERS[idx % len(WAIT_FILLERS)] if idx < 2 else WAIT_FILLERS[2 + (idx % 3)]
    return build_combined_action([
        build_id_list_message([("text", filler)]),
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
#                              נקודת הכניסה
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

    if stage == "init":
        return Response(menu(state, name), mimetype="text/plain")

    elif stage == "menu":
        choice = params.get(state.get("wait", ""))
        if choice == "9":
            return Response(goodbye(call_id, name), mimetype="text/plain")
        if choice in PERSONAS:
            state["persona"] = choice
            return Response(listen(state, f"בחרת ב{PERSONA_NAMES[choice]}", first=True), mimetype="text/plain")
        return Response(menu(state, name, "בחירה לא חוקית"), mimetype="text/plain")

    elif stage == "chat":
        if over_limit(phone):
            return Response(goodbye(call_id, name), mimetype="text/plain")

        file_name = state.get("file")
        if not file_name:
            return Response(listen(state, "לא זוהתה הקלטה"), mimetype="text/plain")

        start_job(state, state.get("persona", "1"), state.get("history", []), ext, file_name)
        return Response(wait_response(state, ext), mimetype="text/plain")

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

        state.setdefault("history", []).extend([
            {"role": "user", "parts": [{"text": transcript}]},
            {"role": "model", "parts": [{"text": ans}]}
        ])
        state["history"] = state["history"][-10:]

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

    return Response(menu(state, name), mimetype="text/plain")

if __name__ == "__main__":
    load_data()
    load_settings()
    load_personas()
    threading.Thread(target=_save_worker, daemon=True).start()
    threading.Thread(target=daily_mail_loop, daemon=True).start()
    
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
