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
#                              הגדרות סביבה
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
# תוקן: כשמופעל חיפוש (grounding), המודל צורך לעיתים כ-1,900 עד 4,900 טוקני
# "חשיבה"/עיבוד רק כדי לבצע את החיפוש עצמו - לפני שהוא כותב אפילו מילה אחת
# מהתשובה. תקרה של 2048 (כפי שהיה קודם) נחתכת בדיוק באמצע התהליך הזה,
# מחזירה טקסט ריק, ומפילה את הקוד למסלול הגיבוי - שם לעיתים גם הוא נחתך.
# זו הייתה הסיבה שהמודל "התנצל" שאין לו גישה לאינטרנט, למרות שכלי החיפוש
# היה דלוק. 8192 נותן מרווח בטוח גם לשיחות עם חיפוש כבד.
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "8192"))

# timeout לקריאת Gemini, בשניות
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "40"))

# כתובת השירות לצורך keep-alive (מונע cold start ב-Render Free)
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
KEEP_ALIVE = os.environ.get("KEEP_ALIVE", "1") == "1"

# ============================================================================
#                                 נתונים
# ============================================================================

names = {}          # מספר טלפון -> שם
LOG = []            # יומן הודעות
CALLS = []          # יומן שיחות
LOG_MAX = 1000
_lock = threading.Lock()

calls = {}          # מצב של כל שיחה פעילה (call_id -> state)
_calls_lock = threading.Lock()

# מונה הודעות יומי - במקום לסרוק את כל הלוג בכל הודעה
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
#
# הסדר הוא לפי מהירות: flash-lite קודם.
# אומת מול https://ai.google.dev/gemini-api/docs/models בספטמבר 2026.
# gemini-2.5-flash הוסר מהרשימה - הוא בדרך להפסקת תמיכה.
#
MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
]

# המודל האחרון שעבד בהצלחה. חוסך round-trips מיותרים בכל שיחה.
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
                    # גרסת SDK ישנה שלא מכירה http_options
                    _client = genai.Client(api_key=api_key)
    return _client


def _thinking_config(model):
    """
    כיבוי / מזעור חשיבה. זה החיסכון הגדול ביותר בזמן תגובה.
    Gemini 3.x משתמש ב-thinking_level, Gemini 2.5 ב-thinking_budget.
    עטוף ב-try כדי לא להישבר בגרסאות SDK שונות.
    """
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
    """
    חילוץ טקסט בטוח. response.text לבדו מחזיר None כשיש grounding,
    קריאות כלים, או כשהחשיבה קטעה את התשובה. כאן עוברים על כל החלקים.
    """
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
                    continue  # לא לקרוא את מחשבות המודל בקול
                t = getattr(part, "text", None)
                if t:
                    chunks.append(t)
    except Exception:
        pass
    return "\n".join(chunks).strip()


def _grounding_used(response):
    """
    האם השימוש בחיפוש אכן הניב תוצאות (יש groundingMetadata עם שאילתות).
    משמש רק ללוגים/אבחון - לא משנה את ההתנהגות.
    """
    try:
        for cand in (getattr(response, "candidates", None) or []):
            gm = getattr(cand, "grounding_metadata", None)
            if gm and (getattr(gm, "web_search_queries", None) or getattr(gm, "grounding_chunks", None)):
                return True
    except Exception:
        pass
    return False


def _ordered_models():
    """המודל שעבד לאחרונה קודם, אחריו השאר."""
    good = _good_model[0]
    if good and good in MODELS:
        return [good] + [m for m in MODELS if m != good]
    return list(MODELS)


def gemini_call(system, contents, use_search=False, deadline=None):
    """
    קריאה אחת ל-Gemini עם fallback בין מודלים.

    שתי נקודות קריטיות כאן:
    1. max_output_tokens חייב להיות גבוה. לפי התיעוד הרשמי, הפרמטר הזה סופר
       גם טוקני חשיבה/עיבוד חיפוש, ואם המודל מגיע לתקרה תוך כדי כך הוא מחזיר
       פלט ריק. זו הייתה הסיבה ל"תקלה בחיבור לאינטרנט" - עם חיפוש, תקרה
       נמוכה מדי נגמרת באמצע התהליך והתשובה חוזרת ריקה.
    2. thinking מוגדר ל-low במקום ברירת המחדל. זה מה שמקצר את הזמן.
    """
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
                if use_search:
                    print("Gemini OK model=%s search=%s grounded=%s took=%ss" % (
                        model, use_search, _grounding_used(response), took))
                else:
                    print("Gemini OK model=%s search=%s took=%ss" % (model, use_search, took))
                return text

            print("Gemini empty text model=%s search=%s took=%ss (finish=%s)" % (
                model, use_search, took,
                getattr((getattr(response, "candidates", None) or [None])[0],
                        "finish_reason", "?")))
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
    """ניקוי טקסט להקראה. מסיר עיצוב, קישורים, וסימוני מקורות של grounding."""
    text = str(text or "")
    text = re.sub(r"\[\d+(?:,\s*\d+)*\]", "", text)      # [1] [2,3] של grounding
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
    # גיבוי: קיזוז קבוע (לא מדויק בחורף)
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
    """מחיקה ברקע בלבד - אסור לחסום את השיחה בשביל זה."""
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


# ---- שמירה מושהית: מונעת העלאת קובץ שלם בכל הודעה בודדת ----

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
#                         מכסות ומונים
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
#
# במקום שתי קריאות סדרתיות (תמלול, ואז תשובה) - קריאה אחת שמחזירה את שתיהן.
# זה חוסך בערך חצי מזמן התגובה.
# הפורמט הוא טקסטואלי פשוט ולא JSON schema, כי JSON schema אכן מתנגש
# עם Google Search - וזו הייתה הסיבה שפיצלת מלכתחילה.

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
    """פירוק התשובה לתמלול ותשובה. סלחני - עובד גם אם המודל חרג מהפורמט."""
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
    # המודל התעלם מהפורמט - נניח שכל הטקסט הוא התשובה
    return "", raw


def is_marker(text):
    """האם הטקסט הוא קוד פקודה (לפני או אחרי ניקוי סוגריים)."""
    return _marker_token(text) is not None


def _marker_token(text):
    t = re.sub(r"[\[\]\s]", "", str(text or "")).upper()
    return t if t in ("VOICE", "MENU", "END") else None


def detect_command(transcript, answer):
    """
    זיהוי פקודה גם מהקוד של המודל וגם מהטקסט עצמו, ליתר ביטחון.
    שים לב: clean_for_tts מסיר סוגריים מרובעים, ולכן [[MENU]] עלול
    להגיע לכאן כ-MENU. הבדיקה כאן מנוטרלת מסוגריים בכוונה.
    """
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
    """מסלול גיבוי: תמלול נקי בלי כלים."""
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
    """מסלול גיבוי: תשובה מטקסט."""
    system = build_chat_system(persona).replace(
        "אתה מקבל הקלטה קולית של המשתמש מהטלפון.",
        "אתה מקבל את דברי המשתמש כטקסט.",
    )
    contents = list(history) + [{"role": "user", "parts": [{"text": transcript}]}]
    raw = gemini_call(system, contents, use_search=(SEARCH_MODE != "off"), deadline=deadline)
    _, answer = parse_marked(raw)
    return clean_for_tts(answer)


def ask_ai(persona, history, ext, file_name):
    """
    מחזיר (transcript, answer).
    מסלול ראשי: קריאה אחת - אודיו + חיפוש + תשובה.
    מסלול גיבוי: תמלול, ואז תשובה. רץ רק אם הראשי נכשל.
    """
    deadline = time.time() + GEMINI_TIMEOUT * 2

    try:
        audio = yemot_download(ext, file_name)
    except Exception as e:
        print("download error:", repr(e))
        return "", "סליחה, לא הצלחתי לשמוע את ההקלטה. נסה שוב."

    yemot_delete(ext, file_name)  # ברקע, לא חוסם

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

    # ---- מסלול גיבוי ----
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


# ---- מנגנון "רגע אחד" ----

WAIT_FILLERS = [
    "רגע אחד, אני בודק",
    "עוד רגע",
    "כמעט מוכן",
    "רגע",
    "עוד שנייה",
]


def wait_response(state, ext):
    """משמיע מילת המתנה קצרה וחוזר לשלוחה. כך ימות אף פעם לא נתקעת בהמתנה."""
    idx = state.get("polls", 0)
    filler = WAIT_FILLERS[idx % len(WAIT_FILLERS)] if idx < 2 else WAIT_FILLERS[2 + (idx % 3)]
    return build_combined_action([
        build_id_list_message([("text", filler)]),
        build_go_to_folder("/" + ext),
    ])


def start_job(state, persona, history, ext, file_name):
    """מפעיל את חישוב התשובה ברקע ומחזיר מיד."""
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
        return Response("ok", mimetype="text/plain; charset=utf-8")

    if params.get("hangup") == "yes":
        with _calls_lock:
            calls.pop(call_id, None)
        return Response("noop", mimetype="text/plain; charset=utf-8")

    phone = params.get("ApiPhone", "unknown")
    ext = (params.get("ApiExtension", "") or VOICE_EXTS[0]).strip("/") or VOICE_EXTS[0]

    with _calls_lock:
        state = calls.get(call_id)
        if state is None:
            state = {
                "stage": "start", "n": 0, "wait": None, "persona": None,
                "history": [], "call_id": call_id, "file": None,
                "resume": None, "job": None, "polls": 0, "ext": ext,
            }
            calls[call_id] = state
            new_call = True
        else:
            new_call = False
    state["ext"] = ext

    if new_call:
        with _lock:
            CALLS.append({"time": now_str(), "phone": phone, "name": names.get(phone, "")})
            del CALLS[:-LOG_MAX]
        save_log()

    has_value = bool(state["wait"]) and state["wait"] in params
    value = (params.get(state["wait"], "") or "").strip() if has_value else ""
    if value == "None":
        value = ""

    name = names.get(phone)

    # ----- שלב ההמתנה לתשובה שמתחשבת ברקע -----
    if state["stage"] == "thinking":
        job = state.get("job") or {"done": True, "transcript": "", "answer": ""}
        if not job["done"]:
            state["polls"] = state.get("polls", 0) + 1
            if state["polls"] > MAX_WAIT_ROUNDS:
                state["job"] = None
                resp = listen(state, prefix="סליחה, זה לוקח יותר מדי זמן. אפשר לשאול שוב")
                return Response(resp, mimetype="text/plain; charset=utf-8")
            return Response(wait_response(state, ext), mimetype="text/plain; charset=utf-8")
        state["job"] = None
        return finish_turn(state, job["transcript"], job["answer"],
                           phone, name or "אורח", ext, call_id)

    if state["resume"]:
        mode = state["resume"]
        state["resume"] = None
        resp = listen(state, prefix="הקול הוחלף") if mode == "chat" \
            else menu(state, name or "אורח", prefix="הקול הוחלף")
        return Response(resp, mimetype="text/plain; charset=utf-8")

    if state["stage"] == "start":
        if name:
            resp = menu(state, name)
        else:
            state["stage"] = "ask_name"
            resp = record(state, "name",
                          "שלום, זו הפעם הראשונה שלך בקו. אמור את שמך הפרטי, ובסיום הקש סולמית")
        return Response(resp, mimetype="text/plain; charset=utf-8")

    if state["stage"] == "ask_name":
        if not has_value:
            resp = record(state, "name", "אמור את שמך הפרטי, ובסיום הקש סולמית")
            return Response(resp, mimetype="text/plain; charset=utf-8")
        name = transcribe_name(ext, state["file"]) or "אורח"
        names[phone] = name
        save_names()
        resp = menu(state, name, prefix="נעים להכיר %s, השם נשמר" % name)
        return Response(resp, mimetype="text/plain; charset=utf-8")

    name = name or "אורח"

    if state["stage"] == "menu":
        if value == "9":
            return Response(goodbye(call_id, name), mimetype="text/plain; charset=utf-8")
        if value in PERSONAS:
            state["persona"] = value
            state["history"] = []
            resp = listen(
                state,
                prefix="אתה עכשיו עם %s. אמור החלף קול כדי להחליף את הקול, "
                       "תפריט כדי לחזור, או סיים כדי לסיים" % PERSONA_NAMES[value],
                first=True,
            )
        else:
            resp = menu(state, name)
        return Response(resp, mimetype="text/plain; charset=utf-8")

    if state["stage"] == "chat":
        if not has_value:
            resp = listen(state, prefix="לא שמעתי אותך")
            return Response(resp, mimetype="text/plain; charset=utf-8")

        if over_limit(phone):
            yemot_delete(ext, state["file"])
            resp = menu(state, name, prefix="הגעת למכסת ההודעות היומית שלך. אפשר לנסות שוב מחר")
            return Response(resp, mimetype="text/plain; charset=utf-8")

        if ASYNC_ANSWER:
            start_job(state, state["persona"], list(state["history"]), ext, state["file"])
            return Response(
                build_combined_action([
                    build_id_list_message([("text", "רגע אחד, אני בודק")]),
                    build_go_to_folder("/" + ext),
                ]),
                mimetype="text/plain; charset=utf-8",
            )

        transcript, answer = ask_ai(state["persona"], state["history"], ext, state["file"])
        return finish_turn(state, transcript, answer, phone, name, ext, call_id)

    resp = menu(state, name)
    return Response(resp, mimetype="text/plain; charset=utf-8")


def finish_turn(state, transcript, answer, phone, name, ext, call_id):
    """מטפל בתוצאה: פקודות, לוג, והשמעת התשובה."""
    cmd = detect_command(transcript, answer)

    if cmd == "voice":
        if len(VOICE_EXTS) < 2 or ext not in VOICE_EXTS:
            resp = listen(state, prefix="אין קולות נוספים להחלפה")
            return Response(resp, mimetype="text/plain; charset=utf-8")
        next_ext = VOICE_EXTS[(VOICE_EXTS.index(ext) + 1) % len(VOICE_EXTS)]
        state["resume"] = "chat"
        state["wait"] = None
        return Response(build_go_to_folder("/" + next_ext), mimetype="text/plain; charset=utf-8")

    if cmd == "menu":
        return Response(menu(state, name), mimetype="text/plain; charset=utf-8")

    if cmd == "end":
        return Response(goodbye(call_id, name), mimetype="text/plain; charset=utf-8")

    if transcript or answer:
        state["history"].append({"role": "user", "parts": [{"text": transcript or "(הקלטה)"}]})
        state["history"].append({"role": "model", "parts": [{"text": answer}]})
        state["history"] = state["history"][-10:]
        with _lock:
            LOG.append({
                "time": now_str(), "phone": phone, "name": name,
                "persona": PERSONA_NAMES.get(state["persona"], ""),
                "q": transcript, "a": answer,
            })
            del LOG[:-LOG_MAX]
        bump_daily(phone)
        save_log()

    resp = listen(state, prefix=answer or "לא הצלחתי להבין, נסה שוב")
    return Response(resp, mimetype="text/plain; charset=utf-8")


# ============================================================================
#                              אתר ניהול
# ============================================================================

ADMIN_CSS = """
<style>
body{font-family:Arial,sans-serif;direction:rtl;background:#f4f6f9;margin:0;color:#222}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
h1{margin:8px 0 16px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.card{background:#fff;border-radius:10px;padding:14px 18px;box-shadow:0 1px 3px #0002;min-width:150px}
.card b{font-size:26px;display:block}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px #0002;margin-bottom:20px}
th,td{padding:8px 10px;border-bottom:1px solid #eee;text-align:right;vertical-align:top;font-size:14px}
th{background:#2d3e50;color:#fff}
input[type=text]{padding:5px;border:1px solid #ccc;border-radius:6px;width:130px}
button{padding:5px 10px;border:0;border-radius:6px;background:#2d3e50;color:#fff;cursor:pointer}
button.red{background:#c0392b}
form.inline{display:inline}
.q{color:#1a5fb4}.a{color:#333}
textarea{width:100%;height:70px;padding:6px;border:1px solid #ccc;border-radius:6px;font-family:inherit}
.top{display:flex;justify-content:space-between;align-items:center}
a{color:#1a5fb4}
</style>
"""


def is_admin():
    if not ADMIN_KEY:
        return False
    key = request.values.get("key") or request.cookies.get("admin_key")
    return key == ADMIN_KEY


def admin_login_page(msg=""):
    page = ADMIN_CSS + """<div class="wrap" style="max-width:380px;margin-top:80px">
    <div class="card"><h2>כניסה לניהול הקו</h2>%s
    <form method="post" action="/admin/login">
    <input type="password" name="key" placeholder="סיסמה" style="width:100%%;padding:8px;box-sizing:border-box;margin-bottom:8px">
    <button style="width:100%%;padding:8px">כניסה</button></form></div></div>""" % (
        "<p style='color:#c0392b'>%s</p>" % html.escape(msg) if msg else "")
    return Response(page, mimetype="text/html; charset=utf-8")


@app.route("/admin/login", methods=["POST"])
def admin_login():
    key = request.form.get("key", "")
    if not ADMIN_KEY:
        return admin_login_page("לא הוגדרה סיסמה (ADMIN_KEY) בשרת")
    if key != ADMIN_KEY:
        return admin_login_page("סיסמה שגויה")
    resp = Response("", status=302, headers={"Location": "/admin"})
    resp.set_cookie("admin_key", key, max_age=60 * 60 * 24 * 90, httponly=True, samesite="Lax")
    return resp


def calls_state_snapshot():
    with _calls_lock:
        return dict(calls)


@app.route("/admin", methods=["GET"])
def admin():
    if not is_admin():
        return admin_login_page()
    h = html.escape
    filt = request.args.get("phone", "").strip()
    today = today_str()

    with _lock:
        users = dict(names)
        log = list(LOG)
        calls_snapshot = list(CALLS)

    calls_today = sum(1 for c in calls_snapshot if str(c.get("time", "")).startswith(today))
    active = len(calls_state_snapshot())

    out = [ADMIN_CSS, '<div class="wrap"><div class="top"><h1>ניהול הקו</h1>'
           '<a href="/admin">רענון</a></div>']
    out.append('<div class="cards">'
               '<div class="card">משתמשים רשומים<b>%d</b></div>'
               '<div class="card">שיחות היום<b>%d</b></div>'
               '<div class="card">סה"כ שיחות<b>%d</b></div>'
               '<div class="card">שיחות פעילות עכשיו<b>%d</b></div>'
               '<div class="card">הודעות ביומן<b>%d</b></div>'
               '<div class="card">מודל פעיל<b style="font-size:15px">%s</b></div></div>' % (
                   len(users), calls_today, len(calls_snapshot), active, len(log),
                   h(_good_model[0] or MODELS[0])))

    out.append('<h2>משתמשים רשומים</h2><table><tr><th>שם</th><th>טלפון</th><th>שיחות</th>'
               '<th>הודעות</th><th>פעולות</th></tr>')
    for phone, nm in sorted(users.items(), key=lambda x: x[1]):
        n_calls = sum(1 for c in calls_snapshot if c.get("phone") == phone)
        n_msgs = sum(1 for l in log if l.get("phone") == phone)
        out.append('<tr><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>'
                   '<form class="inline" method="post" action="/admin/rename">'
                   '<input type="hidden" name="phone" value="%s">'
                   '<input type="text" name="name" value="%s"> <button>שנה שם</button></form> '
                   '<form class="inline" method="post" action="/admin/delete" '
                   'onsubmit="return confirm(\'למחוק את המשתמש?\')">'
                   '<input type="hidden" name="phone" value="%s"><button class="red">מחק</button></form> '
                   '<a href="/admin?phone=%s">הצג שיחות</a></td></tr>' % (
                       h(nm), h(phone), n_calls, n_msgs, h(phone), h(nm), h(phone), h(phone)))
    if not users:
        out.append('<tr><td colspan="5">עדיין אין משתמשים רשומים</td></tr>')
    out.append('</table>')

    shown = [l for l in log if not filt or l.get("phone") == filt][::-1][:300]
    out.append('<h2>מה דיברו עם הקו%s</h2>' % (
        " – " + h(filt) + ' (<a href="/admin">הצג הכל</a>)' if filt else ""))
    out.append('<form class="inline" method="post" action="/admin/clear" '
               'onsubmit="return confirm(\'למחוק את כל היומן?\')">'
               '<button class="red">נקה יומן</button></form>')
    out.append('<table><tr><th>זמן</th><th>מי</th><th>עוזר</th><th>מה נאמר</th></tr>')
    for l in shown:
        out.append('<tr><td>%s</td><td>%s<br><small>%s</small></td><td>%s</td>'
                   '<td><div class="q">שאל: %s</div><div class="a">ענה: %s</div></td></tr>' % (
                       h(str(l.get("time", ""))), h(str(l.get("name", ""))), h(str(l.get("phone", ""))),
                       h(str(l.get("persona", ""))), h(str(l.get("q", ""))), h(str(l.get("a", "")))))
    if not shown:
        out.append('<tr><td colspan="4">אין הודעות עדיין</td></tr>')
    out.append('</table>')

    out.append('<h2>העוזרים (אפשר לערוך את האופי של כל עוזר)</h2>')
    out.append('<form method="post" action="/admin/personas"><table>'
               '<tr><th style="width:40px">מס</th><th style="width:180px">שם העוזר</th>'
               '<th>ההנחיה ל-AI</th></tr>')
    for k in sorted(PERSONAS):
        base = PERSONAS[k].replace(GENERAL_RULES, "")
        out.append('<tr><td>%s</td><td><input type="text" name="name_%s" value="%s"></td>'
                   '<td><textarea name="prompt_%s">%s</textarea></td></tr>' % (
                       k, k, h(PERSONA_NAMES[k]), k, h(base)))
    out.append('</table><button>שמור עוזרים</button></form>')
    out.append('<p><small>הכללים הקבועים מתווספים אוטומטית לכל עוזר.</small></p>')

    out.append('<h2>הגדרות</h2><form method="post" action="/admin/settings"><table>'
               '<tr><th style="width:260px">הגדרה</th><th>ערך</th></tr>'
               '<tr><td>הודעות ליום לכל משתמש (0 = בלי הגבלה)</td>'
               '<td><input type="text" name="daily_limit" value="%d"></td></tr>'
               '<tr><td>מספרים ללא הגבלה (מופרדים בפסיק)</td>'
               '<td><input type="text" name="unlimited_phones" value="%s" style="width:320px"></td></tr>'
               '<tr><td>שעת שליחת הסיכום היומי למייל (0-23)</td>'
               '<td><input type="text" name="mail_hour" value="%d"></td></tr>'
               '</table><button>שמור הגדרות</button></form>' % (
                   SETTINGS["daily_limit"], h(SETTINGS["unlimited_phones"]), SETTINGS["mail_hour"]))

    mail_state = ("מוגדר, נשלח אל " + h(MAIL_TO)) if (MAIL_USER and MAIL_PASS) \
        else "לא מוגדר (צריך MAIL_USER ו-MAIL_PASS ב-Render)"
    out.append('<p>סיכום יומי למייל: %s &nbsp; '
               '<form class="inline" method="post" action="/admin/sendmail">'
               '<button>שלח סיכום של היום עכשיו</button></form> %s</p>' % (
                   mail_state,
                   "<b style='color:#1a5fb4'>%s</b>" % h(request.args.get("mail", ""))
                   if request.args.get("mail") else ""))
    out.append('<p><a href="/admin/logout">יציאה</a></p></div>')
    return Response("".join(out), mimetype="text/html; charset=utf-8")


@app.route("/admin/rename", methods=["POST"])
def admin_rename():
    if not is_admin():
        return admin_login_page()
    phone = request.form.get("phone", "")
    nm = clean_for_tts(request.form.get("name", ""), limit=30)[:30]
    if phone in names and nm:
        names[phone] = nm
        save_names()
    return Response("", status=302, headers={"Location": "/admin"})


@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    if not is_admin():
        return admin_login_page()
    names.pop(request.form.get("phone", ""), None)
    save_names()
    return Response("", status=302, headers={"Location": "/admin"})


@app.route("/admin/clear", methods=["POST"])
def admin_clear():
    if not is_admin():
        return admin_login_page()
    with _lock:
        LOG.clear()
        _daily_counts.clear()
    save_log()
    return Response("", status=302, headers={"Location": "/admin"})


@app.route("/admin/personas", methods=["POST"])
def admin_personas():
    if not is_admin():
        return admin_login_page()
    for k in list(PERSONAS):
        nm = request.form.get("name_" + k, "").strip()
        pr = request.form.get("prompt_" + k, "").strip()
        if nm:
            PERSONA_NAMES[k] = nm[:40]
        if pr:
            PERSONAS[k] = pr + GENERAL_RULES
    data = json.dumps({
        "names": PERSONA_NAMES,
        "prompts": {k: PERSONAS[k].replace(GENERAL_RULES, "") for k in PERSONAS},
    }, ensure_ascii=False)
    schedule_save("ai_personas.txt", data)
    return Response("", status=302, headers={"Location": "/admin"})


@app.route("/admin/settings", methods=["POST"])
def admin_settings():
    if not is_admin():
        return admin_login_page()
    try:
        SETTINGS["daily_limit"] = max(0, int(request.form.get("daily_limit", "0") or 0))
    except ValueError:
        pass
    try:
        SETTINGS["mail_hour"] = min(23, max(0, int(request.form.get("mail_hour", "21") or 21)))
    except ValueError:
        pass
    SETTINGS["unlimited_phones"] = re.sub(r"[^0-9,]", "", request.form.get("unlimited_phones", ""))
    save_settings()
    return Response("", status=302, headers={"Location": "/admin"})


@app.route("/admin/sendmail", methods=["POST"])
def admin_sendmail():
    if not is_admin():
        return admin_login_page()
    day = today_str()
    result = send_mail("סיכום הקו ליום " + day, build_summary(day))
    return Response("", status=302, headers={"Location": "/admin?mail=" + urllib.parse.quote(result)})


@app.route("/admin/logout")
def admin_logout():
    resp = Response("", status=302, headers={"Location": "/admin"})
    resp.set_cookie("admin_key", "", max_age=0, httponly=True, samesite="Lax")
    return resp


@app.route("/health", methods=["GET"])
def health():
    return Response("ok", mimetype="text/plain; charset=utf-8")


@app.route("/warm", methods=["GET"])
def warm():
    """בדיקת תקינות מלאה: מוודא שהמודל והחיפוש עובדים."""
    t0 = time.time()
    txt = gemini_call("ענה במילה אחת בלבד.", "מה השעה עכשיו בישראל?", use_search=True)
    return Response(json.dumps({
        "ok": bool(txt),
        "model": _good_model[0],
        "seconds": round(time.time() - t0, 2),
        "answer": (txt or "")[:200],
    }, ensure_ascii=False), mimetype="application/json; charset=utf-8")


# ============================================================================
#                          חימום ושמירה על ערנות
# ============================================================================

def warmup():
    """
    קריאה קטנה בהפעלה: פותחת את חיבור ה-TLS, מאתחלת את הלקוח,
    ומגלה מראש איזה מודל עובד. חוסך 2-4 שניות בשיחה הראשונה.
    """
    try:
        time.sleep(2)
        txt = gemini_call("ענה במילה אחת.", "שלום", use_search=False)
        print("warmup done, model =", _good_model[0], "reply =", (txt or "")[:30])
    except Exception as e:
        print("warmup error:", repr(e))


def keep_alive_loop():
    """
    מונע cold start ב-Render Free (השירות נכבה אחרי 15 דקות חוסר פעילות).
    זו הסיבה העיקרית לשיחות שלוקחות 30-60 שניות אחרי הפסקה.
    """
    if not (KEEP_ALIVE and SELF_URL):
        print("keep-alive off (RENDER_EXTERNAL_URL not set)")
        return
    while True:
        time.sleep(600)  # כל 10 דקות
        try:
            _http_get(SELF_URL + "/health", 20)
        except Exception as e:
            print("keep-alive error:", repr(e))


load_data()
load_settings()
load_personas()

threading.Thread(target=_save_worker, daemon=True).start()
threading.Thread(target=daily_mail_loop, daemon=True).start()
threading.Thread(target=warmup, daemon=True).start()
threading.Thread(target=keep_alive_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
