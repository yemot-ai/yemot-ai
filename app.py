# -*- coding: utf-8 -*-
"""
קו AI טלפוני בימות המשיח
- Render (חינם) + Gemini (חינם) + Edge TTS (חינם, קול טבעי)
- רישום שם לפי מספר טלפון, 7 עוזרים, חיפוש באינטרנט, החלפת קול, אתר ניהול חי
- חיפוש באינטרנט: חיפוש גוגל של Gemini + כמה מנועי חיפוש חינמיים במקביל + קריאת תוכן האתרים עצמם
- תחבורה ציבורית: לוחות הזמנים הרשמיים של משרד התחבורה (דרך המאגר הפתוח של הסדנא לידע ציבורי), חינם
- אזהרות: מי ששואל על נושאים לא צנועים שומע "זוהי אזהרה X מתוך 3" (בלי חסימה)
"""
from flask import Flask, request, Response
from yemot_flow.actions import build_id_list_message, build_read, build_go_to_folder, build_combined_action
from google import genai
from google.genai import types
import os
import re
import sys
import functools
print = functools.partial(print, flush=True)
import io
import json
import html
import time
import uuid
import base64
import asyncio
import smtplib
import threading
import datetime
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo
import importlib.resources  # noqa: F401  (טעינה מוקדמת - מונע תקלת ייבוא מקבילית ב-threads)
try:
    from pyluach import dates as hebdates
    HAVE_HEB = True
except Exception as _e:
    print("pyluach missing:", _e)
    HAVE_HEB = False
try:
    from ddgs import DDGS
    HAVE_DDGS = True
except Exception as _e:
    print("ddgs missing:", _e)
    HAVE_DDGS = False
try:
    import edge_tts  # noqa: F401
    import imageio_ffmpeg  # noqa: F401
    HAVE_TTS = True
except Exception as _e:
    print("tts libs missing:", _e)
    HAVE_TTS = False

app = Flask(__name__)

# ============================================================ הגדרות
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "")            # מספר המערכת:סיסמה
YEMOT_API = "https://www.call2all.co.il/ym/api/"
EXT = (os.environ.get("VOICE_EXTS", "1").split(",")[0]).strip().strip("/") or "1"   # השלוחה של הקו
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_PASS = os.environ.get("MAIL_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "") or MAIL_USER
OWNER_PHONES = ["0527661756", "0527609296"]                 # תמיד בלי הגבלה

# רשימת מודלים לניחוש ראשוני. בעליית השרת הרשימה מתעדכנת אוטומטית לפי המודלים שבאמת זמינים במפתח שלך
MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
          "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview"]
MODEL_STATUS = {}     # שם מודל -> {"dead": True} (לא קיים) או {"until": זמן} (מכסה נגמרה, לנסות שוב אחר כך)
MODEL_THINK = {}      # שם מודל -> הגדרת החשיבה שהוא מקבל ("level" / "budget" / None), נלמד מהניסיון


def model_ok(model):
    st = MODEL_STATUS.get(model)
    if not st:
        return True
    if st.get("dead"):
        return False
    return time.time() > st.get("until", 0)


def discover_models():
    """שואל את גוגל אילו מודלים זמינים במפתח, ומסדר: הדור החדש קודם, ובתוך הדור - המהיר (lite) קודם"""
    try:
        found = []
        for m in get_client().models.list():
            name = (m.name or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            if not name.startswith("gemini-") or "flash" not in name:
                continue
            if any(x in name for x in ("tts", "image", "embedding", "live", "audio", "computer", "robot", "thinking", "exp", "8b")):
                continue
            ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
            v = float(ver.group(1)) if ver else 0
            found.append((v, 0 if "lite" in name else 1, 1 if "preview" in name else 0, name))
        if found:
            found.sort(key=lambda x: (-x[0], x[1], x[2]))
            seen, ordered = set(), []
            for _, _, _, name in found:
                if name not in seen:
                    seen.add(name)
                    ordered.append(name)
            MODELS[:] = ordered[:6]
            print("models available:", ", ".join(MODELS))
        else:
            print("models: list came back empty, keeping defaults")
    except Exception as e:
        print("models: could not list (%s), keeping defaults, will retry" % str(e)[:120])
        raise


def discover_loop():
    for _ in range(10):
        try:
            discover_models()
            return
        except Exception:
            time.sleep(30)


# קולות גבר טבעיים (Edge TTS, חינם). הראשון הוא ברירת המחדל.
DEFAULT_VOICES = "he-IL-AvriNeural,en-US-AndrewMultilingualNeural,en-US-BrianMultilingualNeural,de-DE-FlorianMultilingualNeural"

SETTINGS = {
    "daily_limit": int(os.environ.get("DAILY_LIMIT", "40")),
    "unlimited_phones": ",".join(OWNER_PHONES),
    "blocked_phones": "",
    "announcement": "",             # הודעה שמושמעת בתחילת כל שיחה
    "mail_hour": 21,
    "tts": "on",                    # קול טבעי: on / off
    "voices": DEFAULT_VOICES,
    "record_max": 25,               # שניות הקלטה מקסימום
    "model": "",                    # מודל מועדף (ריק = אוטומטי: הראשון ברשימה)
    "vocab": "",                    # שמות ומונחים כלליים שעוזרים ל-AI להבין את ההקלטות (למשל שמות של חברים, מקומות)
    "wait_music": "trance",         # מנגינה בזמן ההמתנה לתשובה: trance / bells / custom / yemot / off
    "shabbat_mode": "on",           # סגירה אוטומטית בשבת ובחג: on / off
    "shabbat_city": "בני ברק",      # לפי איזו עיר מחשבים שקיעה, ובאיזו עיר עונים על זמני היום כשלא אמרו עיר
    "shabbat_before": 30,           # כמה דקות לפני השקיעה בערב שבת/חג הקו נסגר
    "shabbat_after": 50,            # כמה דקות אחרי השקיעה במוצאי שבת/חג הקו נפתח
    "keep_rec_days": 3,             # כמה ימים לשמור את ההקלטות של המתקשרים כדי לשמוע אותן באתר (0 = לא לשמור)
}

# כל הנוסחים שהקו אומר - ניתנים לעריכה באתר הניהול. {name} = שם המתקשר, {assistant} = שם העוזר
TEXTS = {
    "first_time": "שלום, זו הפעם הראשונה שלך בקו. אמור את שמך הפרטי, ובסיום הקש סולמית",
    "ask_name_again": "אמור את שמך הפרטי, ובסיום הקש סולמית",
    "name_saved": "נעים להכיר {name}, השם נשמר",
    "menu_hello": "שלום {name}.",
    "menu_item": "הקש {digit} ל{assistant}.",
    "menu_end": "הקש 9 לסיום.",
    "enter": "אתה עם {assistant}. דבר אחרי הצפצוף, ובסיום הקש סולמית",
    "listening": "אני מקשיב",
    "not_heard": "לא שמעתי אותך",
    "wait": "רק רגע, עוד רגע, רק שניה, כבר עונה, עוד שניה, רגע אחד",
    "too_long": "סליחה, זה לוקח יותר מדי זמן. אפשר לנסות שוב",
    "limit": "הגעת למכסת ההודעות היומית שלך. אפשר לנסות שוב מחר",
    "blocked": "המספר שלך אינו מורשה להשתמש בקו",
    "voice_changed": "הקול הוחלף",
    "goodbye": "להתראות {name}",
    "error": "סליחה, יש בעיה זמנית. נסה שוב",
    "not_understood": "לא הבנתי, אפשר לחזור על זה?",
    "name_not_understood": "לא הצלחתי להבין את השם.",
    "closed": "הקו סגור ב{holiday}, ויחזור לפעול בשעה {time}",
    "warning": "זוהי אזהרה {n} מתוך 3. הקו מיועד לשאלות מכובדות בלבד",   # {n} = מספר האזהרה של המתקשר (עד 3)
}
WARN_MAX = 3      # המספר שמוקרא באזהרה לא עולה מעל זה. אין חסימה - רק אזהרה

GENERAL_RULES = (
    " אתה מדבר בטלפון, לכן ענה קצר וברור: משפט עד שלושה משפטים, אלא אם ביקשו במפורש משהו ארוך (סיפור, שיר, הסבר מפורט)."
    " בלי כוכביות, בלי רשימות, בלי אימוג'ים ובלי סימני עיצוב."
    " ענה בשפה שבה המשתמש דיבר אליך; ברירת המחדל היא עברית."
    " שמור על שפה מכובדת וצנועה, ואל תעסוק בנושאים לא צנועים."
    " ענה בדיוק על מה שנשאל, ולא על נושא קרוב או כללי. המשפט הראשון בתשובה צריך כבר לענות על השאלה עצמה, בלי הקדמות."
    " אם השאלה ממשיכה את השיחה (למשל: ומה עם מחר, וכמה זה עולה), הבן אותה לפי מה שנאמר קודם בשיחה."
    " אל תמציא עובדות, מספרים, שעות או שמות. אם אינך יודע בוודאות, אמור זאת בקצרה."
    " אם ההקלטה לא ברורה, חתוכה או שאפשר להבין אותה בכמה דרכים שונות, אל תנחש: בקש במשפט קצר לחזור על השאלה או שאל למה התכוון."
    " יש לך כלי חיפוש באינטרנט. השתמש בו כשהמשתמש מבקש לחפש, או כשהתשובה דורשת מידע עדכני:"
    " מחירים, חנויות, חדשות, מזג אוויר, שעות פתיחה, תחבורה ציבורית, תוצאות, מה קורה עכשיו. אחרי חיפוש תן תשובה מדויקת עם המספרים והשמות שמצאת."
)

# העוזרים: רשימה מסודרת (הסדר = מספר ההקשה בתפריט). ניתן להוסיף, למחוק ולסדר באתר הניהול.
ASSISTANTS = [
    {"id": "general", "name": "העוזר הכללי", "on": True, "keywords": "כללי",
     "prompt": "אתה עוזר כללי ידידותי ומועיל."},
    {"id": "torah", "name": "העוזר התורני", "on": True, "keywords": "תורני, רב",
     "prompt": "אתה עוזר תורני. ענה בסגנון תורני מכובד, וציין מקורות כשאפשר."},
    {"id": "sassy", "name": "העוזר החוצפן", "on": True, "keywords": "חוצפן, חוצפני",
     "prompt": "אתה עוזר חוצפני וסרקסטי עם הומור. ענה בחוצפה משעשעת אבל בלי להעליב באמת."},
    {"id": "creative", "name": "העוזר היצירתי", "on": True, "keywords": "יצירתי",
     "prompt": "אתה עוזר יצירתי. ספר סיפורים קצרים, כתוב שירים, בדיחות ורעיונות יצירתיים."},
    {"id": "tech", "name": "העוזר הטכני", "on": True, "keywords": "טכני",
     "prompt": "אתה עוזר טכני. הסבר דברים טכניים בפשטות: מחשבים, אינטרנט, טלפונים."},
    {"id": "ars", "name": "הערס", "on": True, "keywords": "ערס",
     "prompt": "אתה מדבר כמו ערס ישראלי מגניב: סלנג רחוב (אחי, וואלה, סבבה, יא מלך, בקטנה), ביטחון עצמי, חוצפה וקטע של מגניבות. "
               "עונה לעניין אבל בסטייל. בלי קללות ובלי להעליב באמת."},
    {"id": "music", "name": "המומחה למוזיקה", "on": True, "keywords": "מוזיקה, מוזיקלי, מוסיקה",
     "vocab": "ישי ריבו, מוטי שטיינמץ, אברהם פריד, מרדכי בן דוד, יעקב שוואקי, שמואלי אונגר, דודי לינקר, שרולי גרין, בני פרידמן, "
              "מרדכי שפירו, ליפא שמעלצר, יואלי קליין, בערי וובר, שלמה כץ, אהרן רזאל, ישי לפידות, עמירן דביר, איציק דדיה, "
              "מוטי וייס, אלי מרכוס, יונתן רזאל, חיים ישראל, ליאור נרקיס, עומר אדם, אייל גולן, שלמה ארצי, אריק איינשטיין, "
              "נפתלי קמפה, שולם למר, זאנוויל וינברגר, ניגון, טיש, ג'ינגל, אקורדים, סולם, מעבר",
     "prompt": "אתה מומחה למוזיקה חסידית וישראלית: זמרים, מלחינים, אלבומים, ניגונים, היסטוריה, וגם תיאוריה מוזיקלית - סולמות, אקורדים, "
               "מבנה שירים, מעברים. כששואלים על אקורדים או סולם של שיר, תן את הסולם ואת סדר האקורדים לפי חלקי השיר. "
               "אל תצטט מילים של שירים - אפשר לתאר על מה השיר ומי כתב והלחין."},
]

names = {}        # טלפון -> שם
voices = {}       # טלפון -> מספר קול מועדף
notes = {}        # טלפון -> הודעה אישית לשיחה הבאה
warnings = {}     # טלפון -> כמה אזהרות קיבל על שאלות לא צנועות
LOG = []          # מה נאמר
CALLS = []        # שיחות
calls = {}        # מצב של שיחות פעילות (לפי ApiCallId)
LOG_MAX = 2000
_lock = threading.Lock()

_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""),
                               http_options=types.HttpOptions(timeout=15000,
                                                              retry_options=types.HttpRetryOptions(attempts=1)))
    return _client


GEMINI_DEADLINE = 14   # שניות מקסימום לפנייה אחת. אחרי זה עוברים למודל הבא, גם אם גוגל עדיין "חושבים"
STRONG_GRACE = 2.5     # כשהמודל המהיר (lite) ענה ראשון - כמה שניות לחכות לתשובה של המודל החזק יותר שכבר רץ במקביל


class Deadline(Exception):
    pass


def call_with_deadline(fn, seconds):
    """מריץ פנייה ברקע ומחכה לה עד X שניות - שמירה קשיחה גם אם ספריית גוגל לא מכבדת את ה-timeout שלה"""
    box = {}

    def run():
        try:
            box["r"] = fn()
        except Exception as e:
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise Deadline("timed out after %ds" % seconds)
    if "e" in box:
        raise box["e"]
    return box["r"]


def il_now():
    """שעון ישראל, כולל מעבר אוטומטי בין שעון קיץ לחורף"""
    return datetime.datetime.now(ZoneInfo("Asia/Jerusalem")).replace(tzinfo=None)


def now_str():
    return il_now().strftime("%d/%m/%Y %H:%M")


def today_str():
    return il_now().strftime("%d/%m/%Y")


def clean_for_tts(text):
    text = re.sub(r"[*_#`>\[\]]", "", text or "")
    text = text.replace("\n", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:900]


def csv_list(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def T(key, **kw):
    """נוסח מהרשימה הניתנת לעריכה"""
    t = TEXTS.get(key, "")
    try:
        return t.format(**kw)
    except Exception:
        return t


def active_assistants():
    """העוזרים הפעילים עם מספר ההקשה שלהם (עד 8)"""
    out = []
    for a in ASSISTANTS:
        if a.get("on", True) and len(out) < 8:
            out.append((str(len(out) + 1), a))
    return out


def assistant_by_id(aid):
    for a in ASSISTANTS:
        if a["id"] == aid:
            return a
    return ASSISTANTS[0] if ASSISTANTS else {"id": "x", "name": "העוזר", "prompt": "אתה עוזר ידידותי.", "on": True, "keywords": ""}


# ============================================================ ימות המשיח
def yemot_path(file_name):
    return "ivr2:/%s/%s" % (EXT, file_name)


def yemot_download(file_name):
    url = YEMOT_API + "DownloadFile?" + urllib.parse.urlencode({"token": YEMOT_TOKEN, "path": yemot_path(file_name)})
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()


def yemot_delete(file_name):
    try:
        url = YEMOT_API + "FileAction?" + urllib.parse.urlencode(
            {"token": YEMOT_TOKEN, "action": "delete", "what": yemot_path(file_name)})
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as e:
        print("delete error:", e)


def yemot_read_text(file_name):
    try:
        url = YEMOT_API + "DownloadFile?" + urllib.parse.urlencode({"token": YEMOT_TOKEN, "path": yemot_path(file_name)})
        with urllib.request.urlopen(url, timeout=20) as r:
            data = r.read().decode("utf-8", "ignore")
        if data.lstrip().startswith('{"responseStatus'):
            return None
        return data
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print("read text error:", e)
        return None
    except Exception as e:
        print("read text error:", e)
        return None


def yemot_write_text(file_name, text):
    try:
        body = urllib.parse.urlencode({"token": YEMOT_TOKEN, "what": yemot_path(file_name), "contents": text}).encode()
        req = urllib.request.Request(YEMOT_API + "UploadTextFile", data=body, method="POST")
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print("write text error:", e)


def yemot_upload_file(file_name, data, mime="audio/wav", full_path=None):
    """העלאת קובץ קול לשלוחה (multipart). full_path - נתיב מלא אחר (למשל תיקיית מוזיקה בהמתנה)"""
    boundary = "----yemot" + uuid.uuid4().hex
    fields = {"token": YEMOT_TOKEN, "path": full_path or yemot_path(file_name), "convertAudio": "1"}
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode())
    body.write(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                % (boundary, file_name, mime)).encode())
    body.write(data)
    body.write(("\r\n--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(YEMOT_API + "UploadFile", data=body.getvalue(), method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = r.read().decode("utf-8", "ignore")
    if '"OK"' not in resp and "OK" not in resp[:200]:
        raise RuntimeError("upload failed: " + resp[:200])


# ============================================================ שמירה וטעינה
def _bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()


def save_names():
    with _lock:
        data = json.dumps({"names": names, "voices": voices, "notes": notes, "warnings": warnings}, ensure_ascii=False)
    _bg(yemot_write_text, "ai_names.txt", data)


LOG_SAVE_EVERY = 20       # היומן נשמר בימות לכל היותר פעם ב-20 שניות (במקום בכל שיחה והודעה), כדי לא להעמיס על החיבור לימות
_log_dirty = threading.Event()


def save_log():
    """מסמן שהיומן השתנה. השמירה עצמה נעשית ברקע, בכותב אחד בלבד - כך שמירה ישנה אף פעם לא דורסת חדשה"""
    _log_dirty.set()


def _log_writer_loop():
    while True:
        _log_dirty.wait()
        time.sleep(LOG_SAVE_EVERY)          # אוספים את כל השינויים של הזמן הזה לשמירה אחת
        _log_dirty.clear()
        try:
            with _lock:
                lines = [json.dumps({"t": "log", **l}, ensure_ascii=False) for l in LOG[-LOG_MAX:]]
                lines += [json.dumps({"t": "call", **c}, ensure_ascii=False) for c in CALLS[-LOG_MAX:]]
            yemot_write_text("ai_log.txt", "\n".join(lines))
        except Exception as e:
            print("log save error:", e)
            _log_dirty.set()                # ננסה שוב בסבב הבא


threading.Thread(target=_log_writer_loop, daemon=True).start()


def save_settings():
    _bg(yemot_write_text, "ai_settings.txt", json.dumps(SETTINGS, ensure_ascii=False))


def save_assistants():
    _bg(yemot_write_text, "ai_assistants.txt", json.dumps(ASSISTANTS, ensure_ascii=False))


def save_texts():
    _bg(yemot_write_text, "ai_texts.txt", json.dumps(TEXTS, ensure_ascii=False))


def load_all():
    if not YEMOT_TOKEN:
        return
    try:
        t = yemot_read_text("ai_names.txt")
        if t:
            d = json.loads(t)
            if "names" in d and isinstance(d["names"], dict):
                names.update(d["names"])
                voices.update({k: int(v) for k, v in d.get("voices", {}).items()})
                notes.update(d.get("notes", {}))
                warnings.update({k: int(v) for k, v in (d.get("warnings") or {}).items()})
            else:
                names.update(d)
    except Exception as e:
        print("load names error:", e)
    try:
        t = yemot_read_text("ai_log.txt")
        if t:
            bad = 0
            for line in t.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    kind = d.pop("t", "log")
                    (CALLS if kind == "call" else LOG).append(d)
                except Exception:
                    bad += 1
            if bad:
                print("log: skipped %d broken lines" % bad)
    except Exception as e:
        print("load log error:", e)
    try:
        t = yemot_read_text("ai_settings.txt")
        if t:
            d = json.loads(t)
            for k in SETTINGS:
                if k in d:
                    SETTINGS[k] = type(SETTINGS[k])(d[k])
    except Exception as e:
        print("load settings error:", e)
    try:
        t = yemot_read_text("ai_assistants.txt")
        if t:
            d = json.loads(t)
            if isinstance(d, list) and d:
                ASSISTANTS[:] = [a for a in d if a.get("id") and a.get("name")]
    except Exception as e:
        print("load assistants error:", e)
    try:
        t = yemot_read_text("ai_texts.txt")
        if t:
            d = json.loads(t)
            for k in TEXTS:
                if d.get(k):
                    TEXTS[k] = str(d[k])
    except Exception as e:
        print("load texts error:", e)
    print("loaded: %d names, %d log, %d calls, %d assistants" % (len(names), len(LOG), len(CALLS), len(ASSISTANTS)))


load_all()
try:
    get_client()  # יצירת הלקוח בתהליך הראשי, לפני שה-threads מתחילים
except Exception as _e:
    print('gemini client init error:', _e)
threading.Thread(target=discover_loop, daemon=True).start()


# ============================================================ מנגינת המתנה
# בזמן שהבינה חושבת, אחרי "רק רגע" מתנגנת מנגינה קצרה בלי מילים. השרת מייצר בעצמו מנגינות מקוריות ומעלה אותן לשלוחה:
#   trance - קטע טראנס אינסטרומנטלי (138 BPM): בס דרום, בס בהיסט, היי-האט וארפג'יו סינתי עם "שאיבה"
#   bells  - פעמונים רכים
#   custom - קובץ משלכם: מעלים לשלוחה קובץ בשם ai_wait_music.wav (השרת אף פעם לא דורס אותו)
#   yemot  - "מוזיקה בהמתנה" של ימות עצמם: ימות מנגנים בזמן שהשרת חושב, והמנגינה נעצרת ברגע שהתשובה מוכנה.
#            דורש בשלוחה (ext.ini):  api_wait_answer_music_on_hold=yes
#            השרת מעלה את הטראנס גם לתיקייה /ai_moh, כדי שאפשר יהיה להפוך אותה למוזיקה בהמתנה באתר של ימות
MOH_FOLDER = "ai_moh"
HOLD_WAIT = 12        # במצב yemot: כמה שניות השרת "מחזיק" את הבקשה (ימות מנגנים בינתיים). פחות מזמן הניתוק של ימות
WAIT_FILES = {"trance": "ai_wait_trance", "bells": "ai_wait_bells", "custom": "ai_wait_music"}
MUSIC_READY = {}      # שם קובץ -> קיים בשלוחה


def _wav_bytes(buf, rate, level=0.45):
    import struct
    import wave
    peak = max(abs(x) for x in buf) or 1
    n = len(buf)
    fin, fout = int(rate * 0.02), int(rate * 0.15)
    frames = bytearray()
    for i, x in enumerate(buf):
        g = min(1.0, i / fin if fin else 1.0, (n - i) / fout if fout else 1.0)
        frames += struct.pack("<h", int(x / peak * level * 32767 * g))
    out = io.BytesIO()
    w = wave.open(out, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(bytes(frames))
    w.close()
    return out.getvalue()


def make_bells():
    """מנגינת פעמונים רכה ומקורית, כ-4 שניות"""
    import math
    rate = 8000
    notes = [(440.0, 0.0), (523.25, 0.3), (659.25, 0.6), (880.0, 0.9), (783.99, 1.35), (659.25, 1.65),
             (587.33, 1.95), (659.25, 2.4), (523.25, 2.7), (440.0, 3.0)]
    total = int(rate * 4.2)
    buf = [0.0] * total
    for freq, start in notes:
        s0 = int(start * rate)
        for i in range(int(rate * 1.1)):
            j = s0 + i
            if j >= total:
                break
            t = i / rate
            env = math.exp(-t * 4.2) * min(1.0, t * 200)
            buf[j] += env * (math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(2 * math.pi * freq * 2 * t))
    return _wav_bytes(buf, rate, 0.32)


def make_trance():
    """קטע טראנס מקורי בלי מילים: 2 תיבות ב-138 BPM (כ-3.5 שניות), לה מינור -> פה מז'ור.
    כל הצלילים בנויים כך שיישמעו גם בקו טלפון (שמעביר רק 300-3400 הרץ): לבס ולתוף יש הרמוניות גבוהות."""
    import math
    import random
    rate = 8000
    bpm = 138.0
    s16 = 60.0 / bpm / 4
    total = int(rate * s16 * 32)
    buf = [0.0] * total
    rnd = random.Random(7)
    two_pi = 2 * math.pi

    def saw(f, t, nyq=3600.0, nmax=9):
        out = 0.0
        k = 1
        while k <= nmax and f * k < nyq:
            out += math.sin(two_pi * f * k * t) / k
            k += 1
        return out

    def add(start, length, fn):
        s0 = int(start * rate)
        for i in range(int(length * rate)):
            j = s0 + i
            if j >= total:
                break
            buf[j] += fn(i / rate)

    A2, F2 = 110.0, 87.31
    chords = [[440.0, 523.25, 659.25, 880.0], [349.23, 440.0, 523.25, 698.46]]   # Am, F
    arp = [0, 1, 2, 3, 2, 1, 0, 2, 1, 3, 2, 0, 3, 2, 1, 2]
    for n in range(32):
        st = n * s16
        bar = n // 16
        if n % 4 == 0:      # בס דרום (קיק) עם "קליק" שנשמע בטלפון
            def kick(t):
                f = 50 + 160 * math.exp(-t * 28)
                ph = two_pi * (50 * t + 160 * (1 - math.exp(-t * 28)) / 28)
                return 1.1 * math.exp(-t * 9) * (math.sin(ph) + 0.35 * math.sin(2 * ph) + 0.2 * math.sin(3 * ph)) + (0.25 * math.exp(-t * 400) if f else 0)
            add(st, 0.32, kick)
        if n % 4 == 2:      # בס בהיסט + היי-האט
            root = A2 if bar == 0 else F2
            add(st, s16 * 1.8, lambda t, r=root: 0.55 * min(1.0, t * 300) * math.exp(-t * 6) * saw(r, t, nmax=14))
            noise = [rnd.uniform(-1, 1) for _ in range(int(rate * 0.06))]
            add(st, 0.06, lambda t, nz=noise: 0.22 * math.exp(-t * 70) * (nz[int(t * rate)] - nz[int(t * rate) - 1] if int(t * rate) > 0 else 0))
        f = chords[bar][arp[n % 16]]
        since_kick = (n % 4) * s16

        def lead(t, f=f, sk=since_kick):
            pump = 0.3 + 0.7 * min(1.0, (sk + t) / 0.18)                # "שאיבה" מול הקיק
            env = min(1.0, t * 400) * math.exp(-t * 11)
            return 0.42 * pump * env * (saw(f, t) + saw(f * 1.006, t) + saw(f * 0.994, t)) / 3
        add(st, s16 * 1.6, lead)
    for n in range(0, 32, 2):     # שכבת "פאד" רכה של האקורד
        st = n * s16
        bar = n // 16
        for f in chords[bar][:3]:
            add(st, s16 * 2, lambda t, f=f: 0.06 * math.sin(two_pi * f * t) * min(1.0, t * 40))
    # מסנן נמוכים: בטלפון ממילא לא שומעים מתחת ל-300 הרץ, אז העוצמה הולכת לצלילים שכן נשמעים
    a = 1 / (1 + 2 * math.pi * 260 / rate)
    for _ in range(2):
        prev_x = prev_y = 0.0
        for i, v in enumerate(buf):
            prev_y = a * (prev_y + v - prev_x)
            prev_x = v
            buf[i] = prev_y
    return _wav_bytes(buf, rate, 0.5)


def music_exists(name):
    try:
        data = yemot_download(name + ".wav")
        return len(data) > 1000 and not data[:40].lstrip().startswith(b"{")
    except Exception:
        return False


def setup_wait_music():
    if not YEMOT_TOKEN:
        return
    makers = {"ai_wait_trance": make_trance, "ai_wait_bells": make_bells}
    for _ in range(5):
        try:
            MUSIC_READY["ai_wait_music"] = music_exists("ai_wait_music")
            for name, maker in makers.items():
                if MUSIC_READY.get(name):
                    continue
                if not music_exists(name):
                    yemot_upload_file(name + ".wav", maker())
                    time.sleep(2)
                MUSIC_READY[name] = music_exists(name)
            if MUSIC_READY.get("ai_wait_trance") and not MUSIC_READY.get("moh"):
                try:
                    moh_path = "ivr2:/%s/ai_wait_trance.wav" % MOH_FOLDER
                    url = YEMOT_API + "DownloadFile?" + urllib.parse.urlencode({"token": YEMOT_TOKEN, "path": moh_path})
                    with urllib.request.urlopen(url, timeout=20) as r:
                        have = r.read(2000)
                    if len(have) < 1000 or have[:40].lstrip().startswith(b"{"):
                        yemot_upload_file("ai_wait_trance.wav", make_trance(), full_path=moh_path)
                    MUSIC_READY["moh"] = True
                except Exception as e:
                    print("moh folder upload error:", str(e)[:120])
            print("wait music:", MUSIC_READY)
            if all(MUSIC_READY.get(n) for n in makers):
                return
        except Exception as e:
            print("wait music error:", str(e)[:150])
        time.sleep(30)


def music_style():
    st = SETTINGS.get("wait_music", "trance")
    return "trance" if st == "on" else st


def music_file():
    """הקובץ שינוגן בהמתנה, או None. מנגינה משלכם שעוד לא הועלתה -> מנגנים טראנס במקומה"""
    st = music_style()
    if st not in WAIT_FILES:
        return None
    name = WAIT_FILES[st]
    if MUSIC_READY.get(name):
        return name
    if st == "custom" and MUSIC_READY.get(WAIT_FILES["trance"]):
        return WAIT_FILES["trance"]
    return None


def music_on():
    return music_file() is not None


def hold_mode():
    """מצב "מוזיקה בהמתנה של ימות": השרת מחזיק את הבקשה עד שהתשובה מוכנה, וימות מנגנים בינתיים"""
    return music_style() == "yemot"


threading.Thread(target=setup_wait_music, daemon=True).start()


# ============================================================ זמני היום, שבת וחג
# חישוב אסטרונומי מקומי (בלי אינטרנט ובלי ספריות נוספות). דיוק של כדקה מול לוחות השנה.
# שבת וחג: הקו נסגר לבד מכמה דקות לפני השקיעה בערב שבת/חג ועד כמה דקות אחרי השקיעה במוצאי שבת/חג.
ZMANIM_WORDS = ("זמני היום", "שקיעה", "השקיעה", "זריחה", "הזריחה", "נץ", "קריאת שמע", "ק\"ש", "סוף זמן", "הדלקת נרות",
                "כניסת שבת", "כניסת השבת", "נכנסת שבת", "צאת שבת", "צאת השבת", "יציאת שבת", "יוצאת שבת", "מוצאי שבת",
                "צאת הכוכבים", "חצות", "מנחה גדולה", "מנחה קטנה", "פלג המנחה", "עלות השחר", "זמן תפילה", "זמן תפילין",
                "כניסת החג", "צאת החג", "יציאת החג")


def sun_time(day, lat, lon, depression, rising):
    """מתי השמש נמצאת X מעלות מתחת לאופק, בבוקר או בערב (זריחה/שקיעה = 0.833). מחזיר שעון ישראל או None"""
    import math
    zen = 90.0 + depression
    n = day.timetuple().tm_yday
    lng_hour = lon / 15.0
    t = n + ((6 if rising else 18) - lng_hour) / 24.0
    UT = None
    for _ in range(2):
        M = (0.9856 * t) - 3.289
        L = (M + 1.916 * math.sin(math.radians(M)) + 0.020 * math.sin(math.radians(2 * M)) + 282.634) % 360
        RA = math.degrees(math.atan(0.91764 * math.tan(math.radians(L)))) % 360
        RA = (RA + (math.floor(L / 90) * 90 - math.floor(RA / 90) * 90)) / 15.0
        sin_dec = 0.39782 * math.sin(math.radians(L))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = (math.cos(math.radians(zen)) - sin_dec * math.sin(math.radians(lat))) / (cos_dec * math.cos(math.radians(lat)))
        if cos_h > 1 or cos_h < -1:
            return None
        H = (360 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
        T = H / 15.0 + RA - (0.06571 * t) - 6.622
        UT = (T - lng_hour) % 24
        t = n + UT / 24.0
    base = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)
    return (base + datetime.timedelta(hours=UT)).astimezone(ZoneInfo("Asia/Jerusalem"))


def place_of(city):
    """עיר -> (שם, קו רוחב, קו אורך). ברירת מחדל: העיר שבהגדרות"""
    c = (city or "").strip().strip(".") or SETTINGS.get("shabbat_city", "בני ברק") or "בני ברק"
    try:
        g = geocode(c)
        if g:
            return g
    except Exception:
        pass
    return CITIES["בני ברק"]


def day_zmanim(day, lat, lon):
    """רשימת (שם הזמן, שעה) ליום אחד"""
    rise, sset = sun_time(day, lat, lon, 0.833, True), sun_time(day, lat, lon, 0.833, False)
    alot, tzeit16 = sun_time(day, lat, lon, 16.1, True), sun_time(day, lat, lon, 16.1, False)
    mish, tzeit = sun_time(day, lat, lon, 11.5, True), sun_time(day, lat, lon, 8.5, False)
    if not (rise and sset):
        return []
    gra = (sset - rise) / 12
    out = [("עלות השחר", alot), ("זמן ציצית ותפילין (משיכיר)", mish), ("הנץ החמה (זריחה)", rise)]
    if alot and tzeit16:
        mga = (tzeit16 - alot) / 12
        out.append(("סוף זמן קריאת שמע לפי המגן אברהם", alot + mga * 3))
    out.append(("סוף זמן קריאת שמע לפי הגר\"א", rise + gra * 3))
    if alot and tzeit16:
        out.append(("סוף זמן תפילה לפי המגן אברהם", alot + mga * 4))
    out += [("סוף זמן תפילה לפי הגר\"א", rise + gra * 4), ("חצות היום", rise + gra * 6),
            ("מנחה גדולה", rise + gra * 6.5), ("מנחה קטנה", rise + gra * 9.5), ("פלג המנחה", rise + gra * 10.75),
            ("שקיעה", sset), ("צאת הכוכבים", tzeit), ("צאת הכוכבים לרבנו תם (72 דקות)", sset + datetime.timedelta(minutes=72))]
    return [(n, t) for n, t in out if t]


def holy_name(day):
    """אם היום (לפי התאריך האזרחי) הוא שבת או יום טוב - השם שלו, אחרת ריק"""
    names_ = []
    if HAVE_HEB:
        try:
            f = hebdates.GregorianDate(day.year, day.month, day.day).to_heb().festival(israel=True, hebrew=True, include_working_days=False)
            if f:
                names_.append(f)
        except Exception as e:
            print("festival error:", str(e)[:80])
    if day.weekday() == 5:
        names_.insert(0, "שבת")
    return " ו".join(names_)


def holy_windows(start_day, days=16):
    """חלונות הסגירה הקרובים: [(התחלה, סוף, שם)], ימים צמודים (שבת+חג) מאוחדים לחלון אחד"""
    name_, lat, lon = place_of(SETTINGS.get("shabbat_city", ""))
    before = datetime.timedelta(minutes=int(SETTINGS.get("shabbat_before", 30) or 0))
    after = datetime.timedelta(minutes=int(SETTINGS.get("shabbat_after", 50) or 0))
    wins = []
    for i in range(-1, days):
        d = start_day + datetime.timedelta(days=i)
        nm = holy_name(d)
        if not nm:
            continue
        st = sun_time(d - datetime.timedelta(days=1), lat, lon, 0.833, False) - before
        en = sun_time(d, lat, lon, 0.833, False) + after
        if wins and st <= wins[-1][1] + datetime.timedelta(hours=1):
            wins[-1] = (wins[-1][0], en, wins[-1][2] if nm in wins[-1][2] else wins[-1][2] + " ו" + nm)
        else:
            wins.append((st, en, nm))
    return wins


_shabbat_cache = {"t": 0, "v": None}


def shabbat_status():
    """{closed, name, until, next_name, next_start, next_end} - נשמר בזיכרון לדקה"""
    if time.time() - _shabbat_cache["t"] < 60 and _shabbat_cache["v"]:
        return _shabbat_cache["v"]
    now = datetime.datetime.now(ZoneInfo("Asia/Jerusalem"))
    out = {"enabled": SETTINGS.get("shabbat_mode", "on") == "on", "closed": False, "festivals": HAVE_HEB}
    try:
        for st, en, nm in holy_windows(now.date()):
            if st <= now < en:
                out.update(closed=True, name=nm, until=en.strftime("%H:%M"), until_day=en.strftime("%d/%m"))
            elif st > now and "next_start" not in out:
                out.update(next_name=nm, next_start=st.strftime("%d/%m %H:%M"), next_end=en.strftime("%d/%m %H:%M"))
    except Exception as e:
        out["error"] = str(e)[:150]
        print("shabbat status error:", out["error"])
    _shabbat_cache.update(t=time.time(), v=out)
    return out


def line_closed():
    """(סגור עכשיו?, טקסט ההודעה)"""
    st = shabbat_status()
    if st.get("enabled") and st.get("closed"):
        return True, T("closed", holiday=st.get("name", "שבת"), time=st.get("until", ""))
    return False, ""


def zmanim_text(spec="", transcript=""):
    """זמני היום לעיר ותאריך, + זמני השבת/החג הקרובים. spec: 'עיר | יום'"""
    parts = [p.strip() for p in (spec or "").split("|")]
    city = parts[0] if parts and parts[0] not in ("", "-") else ""
    when = parts[1] if len(parts) > 1 else ""
    text = (transcript or "") + " " + when
    if not city:
        for c in sorted({k for k in CITIES if re.search(r"[א-ת]", k)}, key=len, reverse=True):
            if c in transcript:
                city = c
                break
    name_, lat, lon = place_of(city)
    today = il_now().date()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", when)
    if m:
        day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    elif "מחרתיים" in text:
        day = today + datetime.timedelta(days=2)
    elif "מחר" in text:
        day = today + datetime.timedelta(days=1)
    else:
        day = today
    lines = ["מיקום: %s%s" % (name_, "" if city else " (ברירת המחדל של הקו, המתקשר לא אמר עיר)")]
    heb = ""
    if HAVE_HEB:
        try:
            heb = " (" + hebdates.GregorianDate(day.year, day.month, day.day).to_heb().hebrew_date_string() + ")"
        except Exception:
            pass
    days_he = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
    lines.append("זמני היום ליום %s %s%s:" % (days_he[day.weekday()], day.strftime("%d/%m/%Y"), heb))
    for n, t in day_zmanim(day, lat, lon):
        lines.append("- %s: %s" % (n, t.strftime("%H:%M")))
    # השבת או החג הקרובים
    for i in range(0, 12):
        d = today + datetime.timedelta(days=i)
        nm = holy_name(d)
        if not nm:
            continue
        eve = d - datetime.timedelta(days=1)
        if holy_name(eve):
            continue
        sset = sun_time(eve, lat, lon, 0.833, False)
        last = d
        while holy_name(last + datetime.timedelta(days=1)):
            last += datetime.timedelta(days=1)
        end_set = sun_time(last, lat, lon, 0.833, False)
        end_tz = sun_time(last, lat, lon, 8.5, False)
        if "ירושלים" in name_:
            candles = "הדלקת נרות (מנהג ירושלים, 40 דקות לפני השקיעה): %s" % (sset - datetime.timedelta(minutes=40)).strftime("%H:%M")
        else:
            candles = ("הדלקת נרות: המנהג משתנה ממקום למקום - 20 דקות לפני השקיעה: %s, 30 דקות לפני: %s, 40 דקות לפני: %s"
                       % tuple((sset - datetime.timedelta(minutes=x)).strftime("%H:%M") for x in (20, 30, 40)))
        lines.append("%s הקרוב/ה (%s): שקיעה בערב %s בשעה %s. %s. יציאה (צאת הכוכבים): %s, לרבנו תם: %s." % (
            nm, d.strftime("%d/%m"), days_he[eve.weekday()], sset.strftime("%H:%M"), candles,
            end_tz.strftime("%H:%M") if end_tz else "", (end_set + datetime.timedelta(minutes=72)).strftime("%H:%M")))
        break
    return "\n".join(lines)


# ============================================================ הקלטות לבדיקה
def keep_days():
    try:
        return max(0, int(SETTINGS.get("keep_rec_days", 3) or 0))
    except (ValueError, TypeError):
        return 0


def recordings_cleanup():
    """מוחק מימות הקלטות ישנות מכמה ימים שהוגדרו (עד 40 בכל סבב)"""
    days = keep_days()
    cutoff = il_now() - datetime.timedelta(days=days)
    todo = []
    with _lock:
        for l in LOG:
            if l.get("rec"):
                try:
                    t = datetime.datetime.strptime(l["time"], "%d/%m/%Y %H:%M")
                except Exception:
                    continue
                if days == 0 or t < cutoff:
                    todo.append(l)
                    if len(todo) >= 40:
                        break
        for l in todo:
            rec = l.pop("rec", "")
            if rec:
                _bg(yemot_delete, rec + ".wav")
    if todo:
        save_log()
        print("recordings: deleted %d old" % len(todo))


# ============================================================ קול טבעי
def voice_list():
    return csv_list(SETTINGS.get("voices", "")) or csv_list(DEFAULT_VOICES)


def make_tts(text, voice_name):
    """טקסט -> קובץ wav 8kHz מונו (Edge TTS + ffmpeg מובנה). מחזיר bytes או None"""
    if not HAVE_TTS:
        return None

    async def gen():
        com = edge_tts.Communicate(text, voice_name, rate="+5%")
        buf = io.BytesIO()
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    t0 = time.time()
    mp3 = asyncio.run(gen())
    if not mp3:
        return None
    t1 = time.time()
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    p = subprocess.run([ff, "-loglevel", "error", "-i", "pipe:0", "-ar", "8000", "-ac", "1",
                        "-acodec", "pcm_s16le", "-f", "wav", "pipe:1"],
                       input=mp3, capture_output=True, timeout=40)
    if p.returncode != 0 or len(p.stdout) < 100:
        raise RuntimeError("ffmpeg failed: " + p.stderr.decode("utf-8", "ignore")[:200])
    print("timing: tts detail - edge %.1fs, convert %.1fs, %d chars, voice %s" % (t1 - t0, time.time() - t1, len(text), voice_name))
    return p.stdout


VOICE_SLOW = {}   # קול -> זמן שעד אליו לא משתמשים בו (כי היה איטי מדי)
TTS_DEADLINE = 7  # שניות מקסימום ליצירת קול; מעבר לזה עוברים לקול העברי המהיר


def voice_ok(v):
    return time.time() > VOICE_SLOW.get(v, 0)


def speak_file(text, voice_idx, call_id):
    """מייצר קול טבעי ומעלה לימות. מחזיר שם קובץ (בלי סיומת) או None אם לא הצליח"""
    if SETTINGS.get("tts", "on") != "on" or not text:
        return None
    try:
        vl = voice_list()
        chosen = vl[voice_idx % len(vl)]
        candidates = [chosen] + [v for v in vl if v != chosen and "he-IL" in v] + [v for v in vl if v != chosen and "he-IL" not in v]
        t0 = time.time()
        wav = None
        for v in candidates[:2]:
            if not voice_ok(v):
                continue
            try:
                wav = call_with_deadline(lambda: make_tts(text, v), TTS_DEADLINE)
                if wav:
                    break
            except Exception as e:
                print("tts voice %s failed: %s" % (v, str(e)[:80]))
                VOICE_SLOW[v] = time.time() + 1800   # חצי שעה בלי הקול הזה
        if not wav:
            return None
        t1 = time.time()
        fname = "ai_tts_%s_%s" % (re.sub(r"[^0-9a-zA-Z]", "", call_id)[-10:], uuid.uuid4().hex[:6])
        yemot_upload_file(fname + ".wav", wav)
        print("timing: tts %.1fs, upload %.1fs" % (t1 - t0, time.time() - t1))
        return fname
    except Exception as e:
        print("tts error:", e)
        return None


# ============================================================ Gemini
def _one_call(model, system, contents, use_search, think):
    kw = dict(system_instruction=system, max_output_tokens=600)
    if use_search:
        kw["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    if think == "level":
        kw["thinking_config"] = types.ThinkingConfig(thinking_level="minimal")
    elif think == "budget":
        kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    r = get_client().models.generate_content(model=model, contents=contents, config=types.GenerateContentConfig(**kw))
    return r.text


HEDGE_DELAY = 3.0      # כמה שניות המודל החזק עובד לבד, לפני שמצרפים אליו מודל גיבוי מהיר. חוסך מכסה בלי לפגוע במהירות
MAX_TRIES = 4          # כמה מודלים לכל היותר מנסים לפנייה אחת


def _quota_pause(msg):
    """כמה שניות לתת למודל לנוח אחרי שנגמרה לו מכסה.
    מכסה יומית - עד שהיא מתחדשת (חצות בשעון קליפורניה, בערך 10 בבוקר בישראל), כדי לא לבזבז זמן של מתקשרים על ניסיונות שנכשלים.
    מכסה לדקה - לפי הזמן שגוגל מבקשים, או דקה."""
    low = msg.lower()
    if "perday" in low or "per day" in low or "daily" in low:
        try:
            pt = datetime.datetime.now(ZoneInfo("America/Los_Angeles"))
            nxt = (pt + datetime.timedelta(days=1)).replace(hour=0, minute=2, second=0, microsecond=0)
            return max(300, (nxt - pt).total_seconds())
        except Exception:
            return 3600
    m = re.search(r"retry(?:delay)?['\"]?\s*(?:in|:)\s*['\"]?(\d+(?:\.\d+)?)\s*s", low)
    if m:
        return min(600, float(m.group(1)) + 2)
    return 65


def _note_failure(model, use_search, msg):
    if "NOT_FOUND" in msg or "no longer available" in msg or "not found" in msg:
        MODEL_STATUS[model] = {"dead": True}
    elif "RESOURCE_EXHAUSTED" in msg or msg.startswith("429"):
        if not use_search:
            MODEL_STATUS[model] = {"until": time.time() + _quota_pause(msg)}


def gemini(system, contents, search=False, prefer_strong=False):
    """שולח את הפנייה קודם למודל החזק ביותר שזמין. אם הוא לא ענה תוך כמה שניות - מצרפים במקביל מודל מהיר (גיבוי),
    ולוקחים את התשובה הראשונה. מודל שנכשל (מכסה, תקלה) - עוברים מיד לבא בתור.
    ככה המתקשר מקבל את התשובה המדויקת של המודל החזק, לא מחכה למודל תקוע, והמכסה החינמית לא נשרפת פי שלושה
    (קודם כל פנייה נשלחה לשלושה מודלים, והמודלים החזקים נגמרו מוקדם ביום).
    prefer_strong: אם המודל המהיר ענה ראשון והחזק עדיין רץ - מחכים לו עוד כמה שניות, כי התשובה שלו מדויקת יותר."""
    order = [m for m in MODELS if model_ok(m)]
    strong = [m for m in order if "lite" not in m]
    lite = [m for m in order if "lite" in m]
    queue = strong[:1] + lite[:1] + strong[1:] + lite[1:]
    pref = SETTINGS.get("model", "")
    if pref and model_ok(pref):
        queue = [pref] + [m for m in queue if m != pref]
    queue = queue[:MAX_TRIES]
    if not queue:
        print("Gemini error: no available models")
        return None
    t0 = time.time()
    answers = {}          # מודל -> תשובה
    arrival = []          # סדר ההגעה של התשובות
    launched = []         # מודלים שכבר נשלחה אליהם הפנייה
    finished = set()      # מודלים שסיימו (הצליחו או נכשלו)
    lock = threading.Lock()
    wake = threading.Event()

    def worker(model):
        try:
            options = ("level", "budget", None)
            known = MODEL_THINK.get(model)
            if known in options:
                options = (known,) + tuple(o for o in options if o != known)
            for think in options:
                try:
                    text = _one_call(model, system, contents, search, think)
                    MODEL_THINK[model] = think
                    if text:
                        with lock:
                            answers[model] = text
                            arrival.append(model)
                    return
                except Exception as e:
                    msg = str(e)
                    print("gemini variant failed (%s search=%s think=%s) after %.1fs: %s" % (model, search, think, time.time() - t0, msg[:120]))
                    _note_failure(model, search, msg)
                    if "NOT_FOUND" in msg or "no longer available" in msg or "RESOURCE_EXHAUSTED" in msg or "429" in msg[:8]:
                        return
                    if "thinking" not in msg.lower() and "level" not in msg.lower() and "budget" not in msg.lower():
                        return
        finally:
            with lock:
                finished.add(model)
            wake.set()

    def launch_next(want_fast=False):
        """שולח למודל הבא בתור. גיבוי למודל איטי -> מודל מהיר. מחליף למודל שנכשל -> קודם מודל חזק אחר"""
        with lock:
            left = [m for m in queue if m not in launched]
            if not left:
                return False
            if want_fast:
                pick = [m for m in left if "lite" in m]
            else:
                pick = [m for m in left if "lite" not in m] if not launched or "lite" not in launched[-1] else []
            m = (pick or left)[0]
            launched.append(m)
        print("gemini: asking %s (search=%s) at %.1fs" % (m, search, time.time() - t0))
        threading.Thread(target=worker, args=(m,), daemon=True).start()
        return True

    def running():
        with lock:
            return [m for m in launched if m not in finished]

    launch_next()
    hedge_at = t0 + HEDGE_DELAY
    deadline = t0 + GEMINI_DEADLINE
    while time.time() < deadline:
        with lock:
            have = bool(answers)
        if have:
            break
        now_running = running()
        if not now_running:
            if not launch_next():
                break                       # כל המודלים בתור נכשלו
            continue
        if time.time() >= hedge_at and len(now_running) < 2:
            launch_next(want_fast=True)     # החזק מתעכב - מצרפים גיבוי מהיר
        wake.wait(0.15)
        wake.clear()

    def strong_still_running():
        return [m for m in running() if "lite" not in m]

    if answers and prefer_strong and all("lite" in m for m in list(answers)) and strong_still_running():
        end = min(deadline, time.time() + STRONG_GRACE)
        while time.time() < end and strong_still_running() and all("lite" in m for m in list(answers)):
            time.sleep(0.1)
    with lock:
        got = dict(answers)
        arr = list(arrival)
    if got:
        strong_ans = [m for m in arr if "lite" not in m]
        model = strong_ans[0] if (prefer_strong and strong_ans) else arr[0]
        print("gemini: %s answered in %.1fs" % (model, time.time() - t0))
        return got[model]
    print("Gemini error: no model answered within %.0fs (tried %s)" % (time.time() - t0, ", ".join(launched)))
    return None


def is_silent(wav):
    """האם ההקלטה ריקה: קצרה מאוד או שקטה לגמרי (המתקשר הקיש סולמית בלי לדבר).
    בודק לפני סינון הרעשים. בכל ספק מחזיר False - וההקלטה נשלחת לבינה כרגיל"""
    try:
        import wave
        import array
        w = wave.open(io.BytesIO(wav))
        if w.getsampwidth() != 2 or w.getframerate() <= 0:
            return False
        dur = w.getnframes() / float(w.getframerate())
        frames = w.readframes(w.getnframes())
        if dur < 0.3:
            return True
        a = array.array("h")
        a.frombytes(frames[:len(frames) // 2 * 2])
        if sys.byteorder == "big":
            a.byteswap()
        if not a:
            return True
        peak = max(abs(x) for x in a)
        sample = a[::4] or a
        rms = (sum(x * x for x in sample) / len(sample)) ** 0.5
        return peak < 400 and rms < 40
    except Exception:
        return False


def clean_audio(wav):
    """שיפור הקלטת טלפון לפני שליחה ל-AI: סינון רעש, הגברה אחידה, דגימה ל-16 קילוהרץ. אם נכשל - מחזיר את המקור"""
    if not HAVE_TTS:
        return wav
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        p = subprocess.run([ff, "-loglevel", "error", "-i", "pipe:0", "-af", "highpass=f=120,lowpass=f=3800,loudnorm=I=-16:TP=-1.5",
                            "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le", "-f", "wav", "pipe:1"],
                           input=wav, capture_output=True, timeout=15)
        if p.returncode == 0 and len(p.stdout) > 1000:
            return p.stdout
    except Exception as e:
        print("clean audio error:", str(e)[:100])
    return wav


def hebrew_today():
    """התאריך העברי של עכשיו. אחרי השקיעה (לפי השקיעה האמיתית בעיר של הקו, לא שעה קבועה) - כבר היום העברי הבא"""
    if not HAVE_HEB:
        return ""
    try:
        d = il_now()
        h = hebdates.GregorianDate(d.year, d.month, d.day).to_heb()
        try:
            _, lat, lon = place_of(SETTINGS.get("shabbat_city", ""))
            sset = sun_time(d.date(), lat, lon, 0.833, False)
            after_sunset = bool(sset) and d >= sset.replace(tzinfo=None)
        except Exception:
            after_sunset = d.hour >= 19      # גיבוי בלבד, אם החישוב נכשל
        s = h.hebrew_date_string()
        if after_sunset:
            s += " (אחרי השקיעה - כבר " + (h + 1).hebrew_date_string() + ")"
        return s
    except Exception:
        return ""


def context_line(assistant):
    """שורת הקשר שמצורפת לכל פנייה: תאריך לועזי ועברי, ומילון שמות שכדאי לצפות להם"""
    d = il_now()
    days = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
    line = " היום יום %s, %s, השעה %s." % (days[d.weekday()], d.strftime("%d/%m/%Y"), d.strftime("%H:%M"))
    heb = hebrew_today()
    if heb:
        line += " התאריך העברי היום: %s." % heb
    vocab = (SETTINGS.get("vocab", "") + ", " + assistant.get("vocab", "")).strip(", ")
    if vocab:
        line += " שמות ומונחים שסביר שיוזכרו בהקלטה (העדף אותם כשההגייה דומה): " + vocab + "."
    return line


def transcribe_name(file_name):
    try:
        audio = yemot_download(file_name + ".wav")
    except Exception as e:
        print("download error:", e)
        return ""
    _bg(yemot_delete, file_name + ".wav")
    if is_silent(audio):
        print("name recording is empty/silent")
        return ""
    audio = clean_audio(audio)
    text = gemini("בהקלטה טלפונית באיכות נמוכה אדם אומר את שמו הפרטי בעברית (שם ישראלי או יהודי נפוץ). "
                  "החזר רק את השם הפרטי, מילה אחת או שתיים, בלי שום תוספת. אם לא נאמר שם או שלא ברור מה נאמר, החזר רק: -",
                  [types.Part.from_bytes(data=audio, mime_type="audio/wav")])
    name = clean_for_tts(text or "")[:30].strip(" .,-'\"")
    if not name or len(name.split()) > 3 or any(w in name for w in ("לא ", "אין ", "ברור", "שם פרטי", "הקלטה")):
        return ""             # לא שם - נבקש מהמתקשר לומר שוב
    return name


def http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "yemot-ai-line/1.0 (contact: admin)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


WEATHER_CODES = {0: "בהיר", 1: "בהיר בעיקר", 2: "מעונן חלקית", 3: "מעונן", 45: "ערפל", 48: "ערפל", 51: "טפטוף קל", 53: "טפטוף",
                 55: "טפטוף", 61: "גשם קל", 63: "גשם", 65: "גשם חזק", 71: "שלג קל", 73: "שלג", 75: "שלג כבד", 80: "ממטרים קלים",
                 81: "ממטרים", 82: "ממטרים חזקים", 95: "סופת רעמים", 96: "סופת רעמים עם ברד", 99: "סופת רעמים עם ברד"}


CITIES = {
    "jerusalem": ("ירושלים", 31.78, 35.22), "ירושלים": ("ירושלים", 31.78, 35.22),
    "tel aviv": ("תל אביב", 32.08, 34.78), "תל אביב": ("תל אביב", 32.08, 34.78),
    "bnei brak": ("בני ברק", 32.08, 34.83), "bene beraq": ("בני ברק", 32.08, 34.83), "בני ברק": ("בני ברק", 32.08, 34.83),
    "givat shmuel": ("גבעת שמואל", 32.08, 34.85), "גבעת שמואל": ("גבעת שמואל", 32.08, 34.85),
    "petah tikva": ("פתח תקווה", 32.09, 34.89), "petach tikva": ("פתח תקווה", 32.09, 34.89), "פתח תקווה": ("פתח תקווה", 32.09, 34.89),
    "ramat gan": ("רמת גן", 32.07, 34.82), "רמת גן": ("רמת גן", 32.07, 34.82), "givatayim": ("גבעתיים", 32.07, 34.81),
    "holon": ("חולון", 32.02, 34.78), "חולון": ("חולון", 32.02, 34.78), "bat yam": ("בת ים", 32.02, 34.75), "בת ים": ("בת ים", 32.02, 34.75),
    "rishon lezion": ("ראשון לציון", 31.97, 34.79), "ראשון לציון": ("ראשון לציון", 31.97, 34.79),
    "rehovot": ("רחובות", 31.89, 34.81), "רחובות": ("רחובות", 31.89, 34.81), "yavne": ("יבנה", 31.88, 34.74),
    "beit shemesh": ("בית שמש", 31.75, 34.99), "bet shemesh": ("בית שמש", 31.75, 34.99), "בית שמש": ("בית שמש", 31.75, 34.99),
    "modiin illit": ("מודיעין עילית", 31.93, 35.04), "modi'in illit": ("מודיעין עילית", 31.93, 35.04), "kiryat sefer": ("מודיעין עילית", 31.93, 35.04),
    "מודיעין עילית": ("מודיעין עילית", 31.93, 35.04), "קרית ספר": ("מודיעין עילית", 31.93, 35.04),
    "modiin": ("מודיעין", 31.90, 35.01), "מודיעין": ("מודיעין", 31.90, 35.01),
    "beitar illit": ("ביתר עילית", 31.70, 35.12), "ביתר עילית": ("ביתר עילית", 31.70, 35.12),
    "elad": ("אלעד", 32.05, 34.95), "אלעד": ("אלעד", 32.05, 34.95), "rosh haayin": ("ראש העין", 32.10, 34.95),
    "haifa": ("חיפה", 32.79, 34.99), "חיפה": ("חיפה", 32.79, 34.99), "rekhasim": ("רכסים", 32.75, 35.10), "רכסים": ("רכסים", 32.75, 35.10),
    "kiryat ata": ("קרית אתא", 32.81, 35.11), "kiryat motzkin": ("קרית מוצקין", 32.84, 35.08), "kiryat bialik": ("קרית ביאליק", 32.83, 35.09),
    "ashdod": ("אשדוד", 31.80, 34.65), "אשדוד": ("אשדוד", 31.80, 34.65), "ashkelon": ("אשקלון", 31.67, 34.57), "אשקלון": ("אשקלון", 31.67, 34.57),
    "netanya": ("נתניה", 32.33, 34.86), "נתניה": ("נתניה", 32.33, 34.86), "hadera": ("חדרה", 32.44, 34.92),
    "beer sheva": ("באר שבע", 31.25, 34.79), "beersheba": ("באר שבע", 31.25, 34.79), "באר שבע": ("באר שבע", 31.25, 34.79),
    "tiberias": ("טבריה", 32.80, 35.53), "טבריה": ("טבריה", 32.80, 35.53), "safed": ("צפת", 32.96, 35.50), "tzfat": ("צפת", 32.96, 35.50), "צפת": ("צפת", 32.96, 35.50),
    "meron": ("מירון", 32.98, 35.44), "מירון": ("מירון", 32.98, 35.44), "eilat": ("אילת", 29.56, 34.95), "אילת": ("אילת", 29.56, 34.95),
    "nahariya": ("נהריה", 33.01, 35.10), "afula": ("עפולה", 32.61, 35.29), "karmiel": ("כרמיאל", 32.92, 35.30), "migdal haemek": ("מגדל העמק", 32.67, 35.24),
    "lod": ("לוד", 31.95, 34.89), "ramla": ("רמלה", 31.93, 34.87), "raanana": ("רעננה", 32.18, 34.87), "kfar saba": ("כפר סבא", 32.18, 34.91),
    "herzliya": ("הרצליה", 32.16, 34.84), "hod hasharon": ("הוד השרון", 32.15, 34.89), "kiryat ono": ("קרית אונו", 32.06, 34.86),
    "kiryat gat": ("קרית גת", 31.61, 34.77), "kiryat malachi": ("קרית מלאכי", 31.73, 34.75), "ofakim": ("אופקים", 31.31, 34.62),
    "netivot": ("נתיבות", 31.42, 34.59), "sderot": ("שדרות", 31.53, 34.60), "dimona": ("דימונה", 31.07, 35.03), "arad": ("ערד", 31.26, 35.21),
    "emanuel": ("עמנואל", 32.16, 35.13), "givat zeev": ("גבעת זאב", 31.86, 35.17), "kochav yaakov": ("כוכב יעקב", 31.88, 35.25),
    "tel zion": ("תל ציון", 31.88, 35.25), "telz stone": ("קרית יערים", 31.80, 35.10), "kiryat yearim": ("קרית יערים", 31.80, 35.10),
    "or yehuda": ("אור יהודה", 32.03, 34.86), "yehud": ("יהוד", 32.03, 34.89), "israel": ("ישראל", 32.08, 34.78),
}


def geocode(place):
    key = (place or "").strip().lower()
    if key in CITIES:
        return CITIES[key]
    for k, v in CITIES.items():
        if k in key or key in k:
            return v
    try:
        g = http_json("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": place, "count": 1, "language": "he"}))
        res = (g.get("results") or [None])[0]
        if res:
            return (res.get("name", place), res["latitude"], res["longitude"])
    except Exception as e:
        print("geocode error:", str(e)[:100])
    return None


WTTR_CODES = {"113": "בהיר", "116": "מעונן חלקית", "119": "מעונן", "122": "מעונן", "143": "ערפל", "176": "גשם קל", "200": "סופת רעמים",
              "248": "ערפל", "260": "ערפל", "263": "טפטוף", "266": "טפטוף", "293": "גשם קל", "296": "גשם קל", "299": "גשם", "302": "גשם",
              "305": "גשם חזק", "308": "גשם חזק", "353": "ממטרים", "356": "ממטרים", "359": "ממטרים חזקים", "386": "סופת רעמים", "389": "סופת רעמים"}


def weather_wttr(name, place):
    """גיבוי: wttr.in (חינמי, בלי מפתח)"""
    d = http_json("https://wttr.in/%s?format=j1&lang=he" % urllib.parse.quote(place), timeout=10)
    cur = d["current_condition"][0]
    lines = ["מיקום: %s" % name, "עכשיו: %s מעלות, %s" % (cur.get("temp_C"), WTTR_CODES.get(cur.get("weatherCode"), ""))]
    labels = ["היום", "מחר", "מחרתיים"]
    for i, day in enumerate(d.get("weather", [])[:3]):
        lines.append("%s (%s): %s עד %s מעלות" % (labels[i], day.get("date", "")[5:].replace("-", "/"), day.get("mintempC"), day.get("maxtempC")))
        if i == 0:
            for h in day.get("hourly", []):
                if h.get("time") in ("2100", "0"):
                    lines.append("הלילה בשעה %s: %s מעלות, %s" % (h["time"].zfill(4)[:2] + ":00", h.get("tempC"), WTTR_CODES.get(h.get("weatherCode"), "")))
    return "\n".join(lines)


def weather_lookup(place):
    """מזג אוויר חינמי: טבלת ערים מובנית + Open-Meteo, ואם הוא חסום - wttr.in"""
    geo = geocode(place)
    if not geo:
        return None
    name, lat, lon = geo
    try:
        res = {"latitude": lat, "longitude": lon}
        f = http_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": res["latitude"], "longitude": res["longitude"], "timezone": "Asia/Jerusalem", "forecast_days": 3,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "hourly": "temperature_2m,weather_code"}))
        cur = f.get("current", {})
        lines = ["מיקום: %s" % name,
                 "עכשיו: %s מעלות, %s, רוח %s קמ\"ש" % (round(cur.get("temperature_2m", 0)), WEATHER_CODES.get(cur.get("weather_code"), ""), round(cur.get("wind_speed_10m", 0)))]
        d = f.get("daily", {})
        labels = ["היום", "מחר", "מחרתיים"]
        for i, day in enumerate(d.get("time", [])[:3]):
            lines.append("%s (%s): %s עד %s מעלות, %s, סיכוי גשם %s%%" % (
                labels[i], day[5:].replace("-", "/"), round(d["temperature_2m_min"][i]), round(d["temperature_2m_max"][i]),
                WEATHER_CODES.get(d["weather_code"][i], ""), d["precipitation_probability_max"][i]))
        h = f.get("hourly", {})
        night = [(t, temp, code) for t, temp, code in zip(h.get("time", []), h.get("temperature_2m", []), h.get("weather_code", []))
                 if t[:10] == d.get("time", [""])[0] and int(t[11:13]) in (21, 0)]
        for t, temp, code in night:
            lines.append("הלילה בשעה %s: %s מעלות, %s" % (t[11:16], round(temp), WEATHER_CODES.get(code, "")))
        return "\n".join(lines)
    except Exception as e:
        print("weather (open-meteo) error:", str(e)[:100])
    try:
        return weather_wttr(name, place)
    except Exception as e:
        print("weather (wttr) error:", str(e)[:100])
        return None


def wiki_search(query):
    """ויקיפדיה בעברית - חינמי ואמין, טוב לשאלות 'מי זה' ו'מה זה'"""
    try:
        r = http_json("https://he.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
            {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 3}))
        hits = r.get("query", {}).get("search", [])
        if not hits:
            return None
        titles = "|".join(h["title"] for h in hits[:2])
        r2 = http_json("https://he.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
            {"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "titles": titles, "format": "json", "exchars": 1200}))
        out = []
        for p in r2.get("query", {}).get("pages", {}).values():
            if p.get("extract"):
                out.append("- %s: %s" % (p.get("title", ""), p["extract"][:1200]))
        return "\n".join(out) if out else None
    except Exception as e:
        print("wiki error:", str(e)[:150])
        return None


# ============================================================ תחבורה ציבורית
# לוחות הזמנים הרשמיים של משרד התחבורה (GTFS), דרך המאגר הפתוח והחינמי של הסדנא לידע ציבורי (Open Bus Stride).
# בלי מפתח ובלי הרשמה. יודע לענות על:
#   - קו מסוים (למשל 402 מבני ברק לירושלים): מתי היציאות הבאות, ומתי הוא עובר בעיר המוצא
#   - תחנה לפי המספר שעל השלט (5 ספרות): אילו קווים מגיעים בשעה וחצי הקרובה ומתי
#   - מעיר לעיר בלי מספר קו: אילו קווים נוסעים ישירות, ומתי
# המאגר מעדכן לפעמים את הלוח של היום באיחור. במקרה כזה לוקחים את הלוח של אותו יום בשבוע שעבר (בדרך כלל זהה),
# ומציינים זאת בתשובה.
STRIDE_API = "https://open-bus-stride-api.hasadna.org.il"
TRANSIT_WORDS = ("אוטובוס", "תחנה", "רכבת", "לוח זמנים", "לוחות זמנים", "מתי יוצא", "מתי מגיע", "מתי עובר", "קו ")
TRANSIT_CACHE = {}
TRANSIT_CACHE_TTL = 120
TRANSIT_SHIFTS = (0, 7, 14)     # כמה ימים אחורה לחפש לוח, אם הלוח של היום עוד לא נטען (תמיד אותו יום בשבוע)


def _shift_note(days):
    """הערה לתשובה כשהשעות נלקחו מהלוח של אותו יום בשבוע שעבר"""
    if not days:
        return ""
    return ("הערה: לוח הזמנים של היום עוד לא עודכן במאגר, לכן השעות לפי הלוח של אותו יום בשבוע לפני %d ימים"
            " (בדרך כלל הוא זהה)." % days)


def stride_get(path, params, timeout=9):
    url = STRIDE_API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "yemot-ai-line/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return data if isinstance(data, list) else []


def _il_aware():
    return datetime.datetime.now(ZoneInfo("Asia/Jerusalem")).replace(microsecond=0)


def _hhmm(value):
    """זמן מהמאגר (בדרך כלל לפי גריניץ') -> שעה בישראל"""
    try:
        d = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d.astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%H:%M")
    except Exception:
        return ""


def _tnorm(s):
    """השוואת שמות ערים בלי תלות בכתיב מלא/חסר, גרשיים ורווחים (תקווה/תקוה, קרית/קריית)"""
    s = re.sub(r"[\"'\u05f3\u05f4\s\-_.,()]", "", s or "")
    return s.replace("ו", "").replace("י", "").lower()


def _route_ends(long_name):
    """'ת. מרכזית בני ברק-בני ברק<->ת. מרכזית ירושלים-ירושלים-1#' -> (מוצא, יעד) בניסוח שאפשר להקריא"""
    parts = (long_name or "").split("<->")
    a = parts[0] if parts else ""
    b = parts[1] if len(parts) > 1 else ""
    b = re.sub(r"-\d+[#\w]*$", "", b.strip()).rstrip("#")
    return a.strip().replace("-", ", "), b.strip().replace("-", ", ")


def parse_transit(spec, transcript=""):
    """'402 | בני ברק | ירושלים | -' -> מילון. משלים מספר קו / מספר תחנה גם מתוך התמלול עצמו"""
    out = {"line": "", "from": "", "to": "", "stop": ""}
    parts = [p.strip().strip(".") for p in (spec or "").split("|")]
    for k, p in zip(("line", "from", "to", "stop"), parts):
        if p and p not in ("-", "לא", "אין", "לא ידוע", "לא ידועה"):
            out[k] = p
    out["line"] = re.sub(r"[^0-9א-ת]", "", out["line"])[:5]
    if out["line"] and not re.search(r"\d", out["line"]):
        out["line"] = ""
    out["stop"] = re.sub(r"\D", "", out["stop"])[:6]
    text = transcript or ""
    if not out["line"]:
        m = re.search(r"קו\s*(?:מספר\s*)?(\d{1,3})", text)
        if m:
            out["line"] = m.group(1)
    if not out["stop"]:
        m = re.search(r"תחנה\s*(?:מספר\s*)?(\d{4,6})", text) or re.search(r"\b(\d{5})\b", text)
        if m:
            out["stop"] = m.group(1)
    if text and (not out["to"] or not out["from"]):
        heb = sorted({k for k in CITIES if re.search(r"[א-ת]", k)}, key=len, reverse=True)
        for c in heb:
            if not out["to"] and re.search(r"(?:^|\s)(?:ל|אל\s|עד\s)" + re.escape(c), text):
                out["to"] = c
            if not out["from"] and re.search(r"(?:^|\s)(?:מ|מתחנת\s.*?ב)" + re.escape(c), text):
                out["from"] = c
    return out


def _rides_text(route, now):
    """היציאות הבאות של מסלול אחד מתחנת המוצא שלו"""
    rides = stride_get("/gtfs_rides/list", {
        "gtfs_route_id": route["id"],
        "start_time_from": (now - datetime.timedelta(minutes=3)).isoformat(),
        "start_time_to": (now + datetime.timedelta(hours=10)).isoformat(),
        "order_by": "start_time asc", "limit": 8})
    rides.sort(key=lambda r: str(r.get("start_time", "")))
    times = [t for t in (_hhmm(r.get("start_time")) for r in rides) if t][:6]
    a, b = _route_ends(route.get("route_long_name"))
    head = "קו %s של %s, מ%s ל%s" % (route.get("route_short_name", ""), route.get("agency_name", ""), a, b)
    if times:
        return head + ": היציאות הבאות מתחנת המוצא בשעות " + ", ".join(times)
    return head + ": לפי הלוח אין יותר יציאות היום"


def _routes_for(params, timeout=9):
    """מסלולים לפי הלוח של היום; אם הלוח של היום עוד לא נטען - לפי אותו יום בשבוע שעבר. מחזיר (מסלולים, כמה ימים אחורה)"""
    now = _il_aware()
    for days in TRANSIT_SHIFTS:
        d = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        p = dict(params)
        p.update(date_from=d, date_to=d)
        routes = stride_get("/gtfs_routes/list", p, timeout=timeout)
        if routes:
            if days:
                print("transit: today's schedule missing, using %d days ago" % days)
            return routes, days
    return [], 0


def transit_by_stop(code, line="", to=""):
    """הקווים שעוברים בתחנה. מחפש קודם בשלוש השעות הקרובות; אם אין כמעט כלום (לילה, שבת) - עד 14 שעות קדימה,
    כך שגם "מתי האוטובוס הראשון" מקבל תשובה"""
    now = _il_aware()
    rows, shift = [], 0
    for days in TRANSIT_SHIFTS:
        base = now - datetime.timedelta(days=days)
        for hours, limit in ((3, 120), (14, 300)):
            params = {"gtfs_stop__code": code,
                      "arrival_time_from": (base - datetime.timedelta(minutes=2)).isoformat(),
                      "arrival_time_to": (base + datetime.timedelta(hours=hours)).isoformat(),
                      "order_by": "arrival_time asc", "limit": limit}
            if line:
                params["gtfs_route__route_short_name"] = line
            rows = stride_get("/gtfs_ride_stops/list", params, timeout=12)
            if len(rows) >= 4:
                break
        if rows:
            shift = days
            break
    if not rows:
        return None
    rows.sort(key=lambda r: str(r.get("arrival_time", "")))
    first = rows[0]
    nt = _tnorm(to)
    picked, note = rows, ""
    if nt:
        match = [r for r in rows if nt in _tnorm(_route_ends(r.get("gtfs_route__route_long_name"))[1])]
        if match:
            picked, note = match, " (רק קווים שנוסעים ל%s)" % to
        else:
            note = " (לא נמצא קו מהתחנה הזו שהיעד הסופי שלו הוא %s, הנה כל הקווים)" % to
    first_time = _hhmm(picked[0].get("arrival_time"))
    out = ["תחנה מספר %s: %s, %s" % (code, first.get("gtfs_stop__name", ""), first.get("gtfs_stop__city", "")),
           "השעה עכשיו: %s. האוטובוס הבא/הראשון מהתחנה: %s." % (now.strftime("%H:%M"), first_time),
           "האוטובוסים הקרובים בתחנה לפי לוח הזמנים הרשמי%s:" % note]
    seen = set()
    for r in picked:
        rid = r.get("gtfs_ride_id")
        if rid in seen:
            continue
        seen.add(rid)
        _, dest = _route_ends(r.get("gtfs_route__route_long_name"))
        out.append("- קו %s (%s) לכיוון %s: בשעה %s" % (r.get("gtfs_route__route_short_name", ""), r.get("gtfs_route__agency_name", ""),
                                                      dest, _hhmm(r.get("arrival_time"))))
        if len(out) >= 18:
            break
    if shift:
        out.append(_shift_note(shift))
    return "\n".join(out)


def transit_by_line(line, frm="", to=""):
    routes, shift = _routes_for({"route_short_name": line, "limit": 80})
    if not routes:
        return None
    base = _il_aware() - datetime.timedelta(days=shift)     # "עכשיו" ביום של הלוח שנמצא
    nf, nt = _tnorm(frm), _tnorm(to)

    def score(r):
        a, b = _route_ends(r.get("route_long_name"))
        a, b = _tnorm(a), _tnorm(b)
        s = 0
        if nf and nf in a:
            s += 2
        if nt and nt in b:
            s += 2
        if nf and nf in b:
            s -= 1      # כנראה הכיוון ההפוך
        if nt and nt in a:
            s -= 1
        return s
    routes.sort(key=lambda r: -score(r))
    if nf or nt:
        best = score(routes[0])
        chosen = [r for r in routes if score(r) == best][:3]
    else:
        chosen = routes[:4]
    out = ["תוצאות לקו %s (לפי לוח הזמנים הרשמי של היום):" % line]
    texts = run_parallel([(lambda r=r: _rides_text(r, base)) for r in chosen], 10)
    out += ["- " + t for t in texts if t]
    # אם המתקשר עולה באמצע המסלול (לא בתחנת המוצא) - מתי הקו עובר בעיר שלו
    if frm and chosen:
        try:
            ids = set(r["id"] for r in chosen)
            rows = stride_get("/gtfs_ride_stops/list", {
                "gtfs_route__route_short_name": line, "gtfs_stop__city": frm,
                "arrival_time_from": (base - datetime.timedelta(minutes=2)).isoformat(),
                "arrival_time_to": (base + datetime.timedelta(hours=3)).isoformat(),
                "order_by": "arrival_time asc", "limit": 200})
            first_by_ride = {}
            for r in sorted(rows, key=lambda x: str(x.get("arrival_time", ""))):
                if r.get("gtfs_ride__gtfs_route_id") in ids and r.get("gtfs_ride_id") not in first_by_ride:
                    first_by_ride[r.get("gtfs_ride_id")] = r
            if first_by_ride:
                items = list(first_by_ride.values())[:6]
                out.append("- הקו עובר ב%s (בתחנה %s) בשעות: %s" % (
                    frm, items[0].get("gtfs_stop__name", ""), ", ".join(_hhmm(r.get("arrival_time")) for r in items)))
        except Exception as e:
            print("transit mid-route error:", str(e)[:120])
    if len(out) <= 1:
        return None
    if shift:
        out.append(_shift_note(shift))
    return "\n".join(out)


def transit_by_places(frm, to):
    routes, shift = _routes_for({"route_long_name_contains": to, "limit": 600}, timeout=12)
    base = _il_aware() - datetime.timedelta(days=shift)
    nf, nt = _tnorm(frm), _tnorm(to)
    found, seen = [], set()
    for r in routes:
        a, b = _route_ends(r.get("route_long_name"))
        if nf in _tnorm(a) and nt in _tnorm(b):
            k = (r.get("route_short_name"), r.get("agency_name"))
            if k in seen:
                continue
            seen.add(k)
            found.append(r)
    if not found:
        return None
    out = ["קווים ישירים מ%s ל%s לפי לוח הזמנים הרשמי: %s" % (
        frm, to, ", ".join("קו %s של %s" % (r.get("route_short_name", ""), r.get("agency_name", "")) for r in found[:10]))]
    texts = run_parallel([(lambda r=r: _rides_text(r, base)) for r in found[:3]], 10)
    out += ["- " + t for t in texts if t]
    if shift:
        out.append(_shift_note(shift))
    return "\n".join(out)


def transit_lookup(spec, transcript=""):
    """מחזיר טקסט עם נתוני תחבורה ציבורית, או None אם אין מספיק פרטים / לא נמצא"""
    p = parse_transit(spec, transcript)
    key = json.dumps(p, sort_keys=True, ensure_ascii=False)
    cached = TRANSIT_CACHE.get(key)
    if cached and time.time() - cached[0] < TRANSIT_CACHE_TTL:
        return cached[1]
    t0 = time.time()
    res = None
    try:
        if p["stop"]:
            res = transit_by_stop(p["stop"], p["line"], p["to"])
        if not res and p["line"]:
            res = transit_by_line(p["line"], p["from"], p["to"])
        if not res and p["from"] and p["to"]:
            res = transit_by_places(p["from"], p["to"])
        _search_note("transit", bool(res), "" if res else "לא נמצא במאגר: %s" % key)
    except Exception as e:
        _search_note("transit", False, e)
        print("transit error:", str(e)[:150])
        res = None
    print("timing: transit %.1fs - %s - %s" % (time.time() - t0, key, "found" if res else "nothing"))
    if res:
        TRANSIT_CACHE[key] = (time.time(), res)
        if len(TRANSIT_CACHE) > 200:
            for k in sorted(TRANSIT_CACHE, key=lambda x: TRANSIT_CACHE[x][0])[:80]:
                TRANSIT_CACHE.pop(k, None)
    return res


# ============================================================ חיפוש באינטרנט
# שתי דרכים רצות במקביל, והראשונה שמצליחה עונה:
#   1. חיפוש גוגל המובנה של Gemini (כמו בג'מיני באתר של גוגל)
#   2. מנועי חיפוש חינמיים (DDGS: בינג/ברייב/גוגל/יאהו... + בינג ישיר + גוגל חדשות),
#      ואז כניסה לאתרים עצמם וקריאת התוכן שלהם - כך אפשר לענות מכל אתר: תחבורה, חדשות, מוזיקה, חנויות
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")
SEARCH_STATE = {"ground_until": 0, "last": {}}   # מצב כל מקור חיפוש (מוצג בבדיקת המערכת)
SEARCH_CACHE = {}                                # שאילתה -> (זמן, תוצאות) - חוסך חיפוש כפול על אותה שאלה
SEARCH_CACHE_TTL = 600
SEARCH_BUDGET = 30          # שניות מקסימום לכל שלב החיפוש (כדי שהשיחה לא תיפול)
GROUND_GRACE = 8            # כמה שניות לחכות לחיפוש גוגל לפני שמסתפקים בתשובה מהמנועים החינמיים
ENGINES_WAIT = 9            # מקסימום שניות למנועי החיפוש החינמיים (בדרך כלל הם חוזרים תוך 2-4 שניות, כי כולם רצים במקביל)
PAGE_WAIT = 4               # מקסימום שניות לקריאת דף אחד מתוך התוצאות (הדפים נקראים במקביל)
SKIP_DOMAINS = ("youtube.com", "youtu.be", "facebook.com", "instagram.com", "tiktok.com", "twitter.com", "x.com",
                "news.google.com", "linkedin.com", "pinterest.")
NEWS_WORDS = ("חדשות", "מה קרה", "מה חדש", "מה נשמע ב", "עדכון", "עדכונים", "מבזק", "היום", "אתמול", "הלילה", "עכשיו",
              "השבוע", "בחירות", "פיגוע", "תאונה", "תוצאה", "תוצאות", "משחק", "שביתה")
DONT_KNOW_WORDS = ("לא יודע", "אינני יודע", "איני יודע", "לא יודעת", "אין לי מידע", "אין לי פרטים", "אין לי נתונים",
                   "אין בידי", "אין ברשותי", "לא מכיר", "אינני מכיר", "איני מכיר", "לא ידוע לי", "לא מצאתי", "לא הצלחתי למצוא",
                   "אינני בטוח", "לא בטוח מה", "אין לי גישה", "לא נמצא בידי", "אין לי ידע")
FORCE_SEARCH_WORDS = ("תחפש", "חפש ", "תבדוק באינטרנט", "בדוק באינטרנט", "באינטרנט", "בגוגל", "תגגל")


def _search_note(src, ok, err=""):
    SEARCH_STATE["last"][src] = {"ok": bool(ok), "time": now_str(), "error": str(err)[:160]}


def run_parallel(tasks, timeout):
    """מריץ כמה פונקציות במקביל ומחזיר את התוצאות שהספיקו לחזור בזמן (None למי שנכשל או איחר)"""
    box = [None] * len(tasks)

    def run(i, fn):
        try:
            box[i] = fn()
        except Exception as e:
            print("parallel task error:", str(e)[:120])
    threads = [threading.Thread(target=run, args=(i, fn), daemon=True) for i, fn in enumerate(tasks)]
    for t in threads:
        t.start()
    end = time.time() + timeout
    for t in threads:
        t.join(max(0, end - time.time()))
    return list(box)


def http_get(url, timeout=6, max_bytes=700000):
    """הורדת דף כמו דפדפן רגיל. מחזיר (סוג תוכן, טקסט)"""
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.7,en;q=0.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "") or ""
        data = r.read(max_bytes)
    charset = ""
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    else:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", data[:4000], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    try:
        text = data.decode(charset or "utf-8", "ignore")
    except LookupError:
        text = data.decode("utf-8", "ignore")
    return ctype, text


def _strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", s or ""))).strip()


def _main_part(page):
    """מוצא את גוף התוכן של הדף (המאמר עצמו), בלי תפריטים צדדיים ורשימות שפות. אם לא נמצא - כל הדף"""
    i = page.find('id="mw-content-text"')          # ויקיפדיה ואתרי ויקי
    if i > 0:
        j = page.find(">", i)
        if j > 0:
            return page[j + 1:]
    for pat in (r"(?is)<article\b[^>]*>(.*?)</article\s*>", r"(?is)<main\b[^>]*>(.*?)</main\s*>"):
        m = re.search(pat, page)
        if m and len(_strip_tags(m.group(1))) > 400:
            return m.group(1)
    return page


def html_to_text(page):
    """הופך דף אינטרנט לטקסט נקי: בלי תפריטים, סקריפטים וכפתורים"""
    page = _main_part(page)
    page = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer|form|iframe|aside|button|select)\b[^>]*>.*?</\1\s*>", " ", page)
    page = re.sub(r"(?is)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)\s*>", "\n", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    page = html.unescape(page)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in page.split("\n")]
    lines = [ln for ln in lines if len(ln) > 25]
    return "\n".join(lines)


def _domain(url):
    try:
        return urllib.parse.urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


DDGS_ENGINES = ("google", "brave", "duckduckgo", "yahoo", "mojeek")   # מנועים שמחזירים תוצאות טובות בעברית
DDGS_GRACE = 1.5    # אחרי שהמנוע הראשון החזיר תוצאות - כמה שניות לחכות לשאר, כדי לאסוף תוצאות ממקורות נוספים


def _ddgs_one(query, n, backend):
    try:
        res = DDGS(timeout=6).text(query, region="il-he", max_results=n, backend=backend)
    except TypeError:
        res = DDGS().text(query, region="il-he", max_results=n, backend=backend)
    return [{"title": r.get("title", ""), "body": r.get("body", ""), "href": r.get("href", "")} for r in (res or [])]


def search_ddgs(query, n=6, on_hits=None):
    """DDGS: כמה מנועי חיפוש (גוגל, ברייב, דאקדאקגו, יאהו, מוג'יק) - כולם במקביל.
    קודם המצב "auto" הריץ רק שניים בכל פעם ובסבבים, והתחיל בוויקיפדיה, ולכן לקח כ-11 שניות.
    עכשיו: ברגע שמנוע אחד החזיר תוצאות מחכים עוד רגע קצר לשאר, ומאחדים את כל התוצאות. אם כולם נכשלו - מנסים את "auto" כגיבוי."""
    if not HAVE_DDGS:
        return []
    t0 = time.time()
    results = {}
    lock = threading.Lock()
    wake = threading.Event()

    def run(engine):
        try:
            got = _ddgs_one(query, n, engine)
        except Exception as e:
            print("ddgs %s error: %s" % (engine, str(e)[:100]))
            got = []
        if on_hits and got:
            try:
                on_hits(got)            # מתחילים לקרוא את האתרים מהתוצאות האלה כבר עכשיו, בזמן שהמנועים האחרים עוד מחפשים
            except Exception:
                pass
        with lock:
            results[engine] = got
        wake.set()
    for eng in DDGS_ENGINES:
        threading.Thread(target=run, args=(eng,), daemon=True).start()
    first_hit = None
    end = t0 + ENGINES_WAIT
    while time.time() < end:
        with lock:
            done = len(results)
            hits = sum(len(v) for v in results.values())
        if done >= len(DDGS_ENGINES):
            break
        if hits and first_hit is None:
            first_hit = time.time()
        if first_hit and time.time() - first_hit >= DDGS_GRACE and hits >= n:
            break
        if first_hit and time.time() - first_hit >= DDGS_GRACE * 2:
            break
        wake.wait(0.1)
        wake.clear()
    out, seen = [], set()
    with lock:
        ordered = [results.get(e) or [] for e in DDGS_ENGINES]
    for i in range(max([len(x) for x in ordered] or [0])):
        for lst in ordered:
            if i < len(lst):
                h = lst[i]          # לסירוגין בין המנועים - כך התוצאות המובילות של כל מנוע נכנסות ראשונות
                k = (h.get("href") or "").split("?")[0].rstrip("/")
                if k and k not in seen:
                    seen.add(k)
                    out.append(h)
    if not out:
        try:
            try:
                res = DDGS().text(query, region="il-he", max_results=n, backend="auto")
            except TypeError:
                res = DDGS().text(query, region="il-he", max_results=n)
            out = [{"title": r.get("title", ""), "body": r.get("body", ""), "href": r.get("href", "")} for r in (res or [])]
        except Exception as e:
            print("ddgs auto error:", str(e)[:150])
    with lock:
        which = [e for e in DDGS_ENGINES if results.get(e)]
    print("timing: ddgs %.1fs - %d results (%s)" % (time.time() - t0, len(out), ", ".join(which) or "auto"))
    _search_note("ddg", bool(out), "" if out else "לא חזרו תוצאות")
    return out[:max(n, 10)]


def _bing_url(u):
    u = html.unescape(u or "")
    if "bing.com/ck/a" in u:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("u", [""])[0]
            if q.startswith("a1"):
                s = q[2:]
                s += "=" * (-len(s) % 4)
                return base64.urlsafe_b64decode(s).decode("utf-8", "ignore")
        except Exception:
            pass
    return u


def search_bing(query, n=6, on_hits=None):
    """חיפוש ישיר בבינג (גיבוי כשמנועים אחרים חסומים)"""
    try:
        _, page = http_get("https://www.bing.com/search?" + urllib.parse.urlencode(
            {"q": query, "setlang": "he", "cc": "IL", "mkt": "he-IL", "count": 10}), timeout=7)
        out = []
        for chunk in page.split('<li class="b_algo"')[1:]:
            m = re.search(r'(?s)<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk)
            if not m:
                continue
            sn = re.search(r'(?s)<p[^>]*>(.*?)</p>', chunk) or re.search(r'(?s)class="b_lineclamp\d*"[^>]*>(.*?)</', chunk)
            out.append({"title": _strip_tags(m.group(2)), "href": _bing_url(m.group(1)), "body": _strip_tags(sn.group(1)) if sn else ""})
            if len(out) >= n:
                break
        _search_note("bing", bool(out), "" if out else "לא חזרו תוצאות")
        if on_hits and out:
            try:
                on_hits(out)
            except Exception:
                pass
        return out
    except Exception as e:
        _search_note("bing", False, e)
        print("bing error:", str(e)[:150])
        return []


def search_news(query, n=6):
    """כותרות חדשות עדכניות: גוגל חדשות (RSS חינמי), ואם לא - חדשות של DDGS"""
    out = []
    try:
        _, x = http_get("https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": query, "hl": "he", "gl": "IL", "ceid": "IL:he"}), timeout=7)
        root = ET.fromstring(x.encode("utf-8"))
        for it in root.iter("item"):
            src = it.find("source")
            out.append({"title": (it.findtext("title") or "").strip(),
                        "body": "פורסם: %s%s" % ((it.findtext("pubDate") or "").strip(), (", מקור: " + src.text) if src is not None and src.text else ""),
                        "href": ""})
            if len(out) >= n:
                break
    except Exception as e:
        print("google news error:", str(e)[:150])
    if not out and HAVE_DDGS:
        try:
            for r in DDGS().news(query, region="il-he", max_results=n) or []:
                out.append({"title": r.get("title", ""), "body": "פורסם: %s, מקור: %s. %s" % (r.get("date", ""), r.get("source", ""), r.get("body", "")),
                            "href": r.get("url", "")})
        except Exception as e:
            print("ddgs news error:", str(e)[:150])
    _search_note("news", bool(out), "" if out else "לא חזרו כותרות")
    return out


def fetch_page_text(url):
    """נכנס לאתר וקורא את התוכן שלו (כמו שג'מיני קורא דפים)"""
    try:
        ctype, page = http_get(url, timeout=PAGE_WAIT)
        if "html" not in ctype.lower() and not page.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
            return None
        t = html_to_text(page)
        return t[:4000] if len(t) > 150 else None
    except Exception as e:
        print("fetch page error (%s): %s" % (_domain(url), str(e)[:100]))
        return None


PREFETCH_MAX = 8     # כמה אתרים לכל היותר מתחילים לקרוא מראש, לפני שכל המנועים סיימו


def _page_ok(u):
    """אתר שכדאי להיכנס אליו ולקרוא את התוכן שלו"""
    return bool(u) and u.startswith("http") and not any(d in u for d in SKIP_DOMAINS) and not u.lower().endswith(".pdf")


def gather_web(query, newsy=False):
    """אוסף מידע מהאינטרנט: כמה מנועי חיפוש במקביל + תוכן האתרים המובילים. מחזיר טקסט או None"""
    key = (query or "").strip().lower()
    if not key:
        return None
    cached = SEARCH_CACHE.get(key)
    if cached and time.time() - cached[0] < SEARCH_CACHE_TTL:
        return cached[1]
    t0 = time.time()
    # קריאת אתרים מוקדמת: ברגע שמנוע כלשהו מחזיר תוצאות, מתחילים לקרוא את האתרים שלו ברקע, בזמן שהמנועים האחרים עוד מחפשים.
    # בסוף בוחרים בדיוק את אותם אתרים כמו קודם (לפי כל התוצאות מכל המנועים), כך שהתשובה מבוססת על אותו מידע - רק מהר יותר.
    pre = {}
    pre_lock = threading.Lock()

    def prefetch(hits, force=False):
        for h in hits or []:
            u = h.get("href", "")
            if not _page_ok(u):
                continue
            with pre_lock:
                if u in pre or (not force and len(pre) >= PREFETCH_MAX):
                    continue
                slot = {"ev": threading.Event(), "text": None}
                pre[u] = slot

            def run(u=u, slot=slot):
                try:
                    slot["text"] = fetch_page_text(u)
                finally:
                    slot["ev"].set()
            threading.Thread(target=run, daemon=True).start()

    tasks = [lambda: search_ddgs(query, on_hits=prefetch), lambda: search_bing(query, on_hits=prefetch)]
    if newsy:
        tasks.append(lambda: search_news(query))
    res = run_parallel(tasks, ENGINES_WAIT)
    hits, seen = [], set()
    for h in (res[0] or []) + (res[1] or []):
        href = h.get("href", "")
        k = href.split("?")[0].rstrip("/")
        if not href or k in seen:
            continue
        seen.add(k)
        hits.append(h)
    news = (res[2] if newsy else None) or []
    if not hits and not news:
        news = search_news(query)
    extra = None
    if not hits and not news:
        extra = wiki_search(query)
    urls = [h["href"] for h in hits if _page_ok(h["href"])][:4]
    prefetch([{"href": u} for u in urls], force=True)       # אתר מהרשימה הסופית שעוד לא התחילו לקרוא - מתחילים עכשיו
    early = sum(1 for u in urls if pre.get(u) and pre[u]["ev"].is_set())
    end = time.time() + PAGE_WAIT + 1                         # אותו זמן המתנה כמו קודם; אתרים שכבר נקראו מוכנים מיד
    pages = []
    for u in urls:
        slot = pre.get(u)
        if slot:
            slot["ev"].wait(max(0, end - time.time()))
            pages.append(slot["text"])
        else:
            pages.append(None)
    parts = []
    if hits:
        parts.append("תוצאות חיפוש:\n" + "\n".join("- %s: %s (%s)" % (h["title"], h["body"], _domain(h["href"])) for h in hits[:8]))
    if news:
        parts.append("כותרות חדשות אחרונות:\n" + "\n".join("- %s (%s)" % (h["title"], h["body"]) for h in news[:6]))
    for u, txt in zip(urls, pages):
        if txt:
            parts.append("תוכן מתוך האתר %s:\n%s" % (_domain(u), txt[:2500]))
    if extra:
        parts.append("ערכים מוויקיפדיה:\n" + extra)
    text = "\n\n".join(parts) or None
    print("timing: web gather %.1fs - %d results, %d news, %d pages read (%d were ready early)" % (
        time.time() - t0, len(hits), len(news), sum(1 for p in pages if p), early if urls else 0))
    if text:
        SEARCH_CACHE[key] = (time.time(), text)
        if len(SEARCH_CACHE) > 300:
            for k in sorted(SEARCH_CACHE, key=lambda x: SEARCH_CACHE[x][0])[:100]:
                SEARCH_CACHE.pop(k, None)
    return text


def web_search(query, max_results=6):
    """תאימות לאחור: חיפוש חינמי - מחזיר טקסט של תוצאות או None"""
    hits = search_ddgs(query, max_results) or search_bing(query, max_results)
    if not hits:
        return None
    return "\n".join("- %s: %s (%s)" % (r.get("title", ""), r.get("body", ""), r.get("href", "")) for r in hits)


SEARCH_MODEL_UNTIL = {}     # מודל -> זמן שעד אליו לא מנסים בו חיפוש גוגל (המכסה שלו נגמרה)
GROUND_TOTAL = 26           # שניות מקסימום לכל סבב חיפוש גוגל, על כל המודלים יחד


def grounded_answer(system, contents):
    """חיפוש גוגל המובנה של Gemini. מודל אחד בכל פעם (לא כמה במקביל) כדי לא לשרוף את המכסה היומית החינמית.
    אם למודל אחד נגמרה מכסת החיפוש - עוברים למודל הבא ברשימה, עד שאחד מצליח או שכולם נוסו.
    מודל שהמכסה שלו נגמרה לא מנוסה שוב בחיפוש במשך רבע שעה (רק הוא, לא כל השאר)."""
    if time.time() < SEARCH_STATE["ground_until"]:
        return None
    now = time.time()
    order = [m for m in MODELS if model_ok(m) and now > SEARCH_MODEL_UNTIL.get(m, 0)]
    order.sort(key=lambda m: 1 if "lite" in m else 0)          # לחיפוש - מודל מלא קודם, הוא מחפש טוב יותר
    pref = SETTINGS.get("model", "")
    if pref and pref in order:
        order = [pref] + [m for m in order if m != pref]
    if not order:
        _search_note("google", False, "המכסה החינמית של חיפוש גוגל נגמרה בכל המודלים זמנית")
        return None
    t_start = time.time()
    for m in order:
        left = GROUND_TOTAL - (time.time() - t_start)
        if left < 3:
            break
        known = MODEL_THINK.get(m, "level")
        thinks = [known] + [t for t in ("level", "budget", None) if t != known]
        for think in thinks:
            try:
                text = call_with_deadline(lambda m=m, think=think: _one_call(m, system, contents, True, think), min(18, left))
                if text:
                    _search_note("google", True)
                    print("google search: answered by %s" % m)
                    return text
                break
            except Exception as e:
                msg = str(e)
                print("google search failed (%s think=%s): %s" % (m, think, msg[:140]))
                if "RESOURCE_EXHAUSTED" in msg or "429" in msg[:8] or "quota" in msg.lower():
                    SEARCH_MODEL_UNTIL[m] = time.time() + max(900, _quota_pause(msg))   # רק המודל הזה בהפסקה (מכסה יומית - עד שמתחדשת)
                    _search_note("google", False, "המכסה החינמית של חיפוש גוגל נגמרה זמנית ב-%s" % m)
                    break
                if "NOT_FOUND" in msg or "no longer available" in msg:
                    _note_failure(m, True, msg)
                    break
                if isinstance(e, Deadline):
                    _search_note("google", False, "חיפוש גוגל לא ענה בזמן (%s)" % m)
                    break
                if "thinking" not in msg.lower() and "level" not in msg.lower() and "budget" not in msg.lower():
                    _search_note("google", False, msg)
                    break
    left_models = [m for m in MODELS if model_ok(m) and time.time() > SEARCH_MODEL_UNTIL.get(m, 0)]
    if not left_models:
        print("google search: quota exhausted on all models, free engines continue")
    return None


ONLY_ANSWER = (" כתוב רק את התשובה עצמה, בדיוק כפי שאומרים אותה למתקשר: בלי לחזור על השאלה, בלי לתאר מה חיפשת או מה עשית,"
               " בלי כותרות כמו 'שאלה:', 'חיפוש:' או 'תשובה:' ובלי פירוט של שלבי העבודה.")


def answer_with_search(assistant, history, transcript, search_line="", query=""):
    """שלב חיפוש: מזג אוויר -> Open-Meteo. תחבורה ציבורית -> מאגר משרד התחבורה.
    אחרת חיפוש גוגל של Gemini ומנועים חינמיים + קריאת אתרים - במקביל"""
    base = assistant["prompt"] + GENERAL_RULES + context_line(assistant) + ONLY_ANSWER
    q = (query or transcript or "").strip()
    t0 = time.time()
    if "מזג" in search_line:
        place = search_line.split(":", 1)[1].strip(" -.") if ":" in search_line else ""
        no_city = not place
        w = weather_lookup(place or SETTINGS.get("shabbat_city") or "בני ברק")   # בלי עיר - העיר של הקו
        if w:
            sys_w = base + " קיבלת תחזית מזג אוויר. ענה על השאלה לפי המידע הזה, קצר ומתאים להקראה בטלפון." + (
                " המתקשר לא אמר עיר: ציין לאיזו עיר התחזית, ושאפשר לשאול על עיר אחרת." if no_city else "")
            ans = gemini(sys_w, list(history) + [{"role": "user", "parts": [{"text": "השאלה: %s\n\nתחזית מזג אוויר:\n%s" % (transcript, w)}]}])
            if ans:
                return ans
    if "זמנים" in search_line:
        spec = search_line.split(":", 1)[1].strip() if ":" in search_line else ""
        try:
            zt = zmanim_text(spec, transcript)
        except Exception as e:
            print("zmanim error:", str(e)[:120])
            zt = None
        if zt:
            sys_z = base + (" קיבלת זמני היום שחושבו במדויק לפי מיקום השמש. ענה רק לפי המידע הזה, ורק על מה שנשאל:"
                            " אם שאלו על זמן אחד - תן רק אותו, עם שם העיר. אם יש כמה שיטות (גר\"א ומגן אברהם) אמור את שתיהן בקצרה."
                            " בהדלקת נרות, אם המקום אינו ירושלים, אמור שהמנהג משתנה ממקום למקום ותן את הזמנים לפי 20, 30 ו-40 דקות."
                            " אם המתקשר לא אמר עיר, ציין לאיזו עיר הזמנים ושאפשר לשאול על עיר אחרת. קצר ומתאים להקראה בטלפון.")
            ans = gemini(sys_z, list(history) + [{"role": "user", "parts": [{"text": "השאלה: %s\n\n%s" % (transcript, zt)}]}], prefer_strong=True)
            if ans:
                print("search: answered by zmanim in %.1fs" % (time.time() - t0))
                return ans
    if "תחבור" in search_line:
        spec = search_line.split(":", 1)[1].strip() if ":" in search_line else ""
        try:
            tr = call_with_deadline(lambda: transit_lookup(spec, transcript), 14)
        except Exception as e:
            print("transit step error:", str(e)[:120])
            tr = None
        if tr:
            sys_t = base + (" קיבלת נתוני תחבורה ציבורית ממאגר משרד התחבורה: לוח הזמנים הרשמי של היום."
                            " ענה לפי המידע הזה בלבד: מספר הקו, החברה, הכיוון והשעות. תן את שלוש או ארבע השעות הקרובות, לא יותר."
                            " אם יש כמה כיוונים או כמה קווים ולא ברור לאיזה המתקשר התכוון, אמור את העיקר ושאל בקצרה לאיזה כיוון."
                            " ציין במילים ספורות שאלו זמנים לפי לוח הזמנים, ושייתכנו עיכובים. קצר ומתאים להקראה בטלפון.")
            ans = gemini(sys_t, list(history) + [{"role": "user", "parts": [{"text": "השאלה: %s\nהשעה עכשיו: %s\n\nנתוני תחבורה:\n%s" % (
                transcript, il_now().strftime("%H:%M"), tr)}]}], prefer_strong=True)
            if ans:
                print("search: answered by transit data in %.1fs" % (time.time() - t0))
                return ans
        # לא נמצא במאגר - ממשיכים לחיפוש הרגיל באינטרנט (עם זמן מלא משלו)
        t0 = time.time()
    newsy = any(w in (transcript + " " + q) for w in NEWS_WORDS)
    contents = list(history) + [{"role": "user", "parts": [{"text": transcript}]}]
    sys_g = base + (" חפש באינטרנט, בכל אתר שצריך: חדשות, תחבורה ציבורית ולוחות זמנים, מוזיקה, חנויות, מחירים, שעות פתיחה."
                    " ענה תשובה מדויקת עם המספרים, השעות והשמות שמצאת. תשובה קצרה, מתאימה להקראה בטלפון. אל תקרא כתובות אינטרנט.")
    box = {}

    def run_google():
        try:
            box["g"] = grounded_answer(sys_g, contents)
        except Exception as e:
            print("google search step error:", e)
            box["g"] = None

    def run_free():
        try:
            data = gather_web(q, newsy)
            if not data and transcript and transcript.strip() != q:
                data = gather_web(transcript, newsy)      # לא נמצא כלום - מנסים שוב בניסוח של המתקשר עצמו
            if not data:
                box["f"] = None
                return
            sys2 = base + (" חיפשת באינטרנט וקיבלת את המידע שלמטה: תוצאות חיפוש ותוכן שנקרא מתוך האתרים עצמם."
                           " ענה על השאלה לפי המידע הזה, עם המספרים, השעות והשמות שמופיעים בו, קצר ומתאים להקראה בטלפון."
                           " אל תקרא כתובות אינטרנט. אפשר לציין מאיזה אתר המידע."
                           " אל תענה רק 'לא מצאתי': אם המידע חלקי, תן את כל מה שכן נמצא ושעונה על השאלה,"
                           " והשלם מהידע שלך רק דברים שאתה בטוח בהם. אל תמציא מספרים, שעות או מחירים.")
            box["f"] = gemini(sys2, list(history) + [{"role": "user", "parts": [{"text": "השאלה: %s\n\nמה שנמצא באינטרנט:\n%s" % (transcript, data[:12000])}]}])
        except Exception as e:
            print("free search step error:", e)
            box["f"] = None
    threading.Thread(target=run_google, daemon=True).start()
    threading.Thread(target=run_free, daemon=True).start()
    end = t0 + SEARCH_BUDGET
    while time.time() < end:
        if box.get("g"):
            print("search: answered by google search in %.1fs" % (time.time() - t0))
            return box["g"]
        if box.get("f") and (time.time() - t0 > GROUND_GRACE or "g" in box):
            print("search: answered by web engines in %.1fs" % (time.time() - t0))
            return box["f"]
        if "g" in box and "f" in box:
            break
        time.sleep(0.3)
    if box.get("g"):
        return box["g"]
    if box.get("f"):
        return box["f"]
    print("search: all sources failed after %.1fs" % (time.time() - t0))
    sys4 = base + (" החיפוש באינטרנט לא הצליח הפעם. ענה כמיטב ידיעתך. רק אם התשובה באמת תלויה במידע עדכני"
                   " (שעות, מחירים, חדשות, לוחות זמנים), אמור במשפט קצר שכרגע לא הצלחת לבדוק ושאפשר לנסות שוב בעוד רגע.")
    return gemini(sys4, contents)


ACTION_RE = re.compile(r"תמלול\s*:\s*(.*?)\s*\n\s*פעולה\s*:\s*(.*?)\s*\n\s*חיפוש\s*:\s*(.*?)\s*\n\s*תשובה\s*:\s*(.*)", re.S)

# שדות הפורמט, גם כשהבינה מוסיפה כוכביות, מקפים או נקודתיים מסוג אחר
_FIELD_RE = re.compile(r"^[ \t*_#>\-•]*(תמלול|פעולה|חיפוש|תשובה)[ \t*_]*[:：]", re.M)
# שורות "מאחורי הקלעים" שאסור להקריא למתקשר
META_LABELS = ("תמלול", "פעולה", "חיפוש", "תשובה", "השאלה שלך", "השאלה", "שאלה", "שאלת", "מה שנמצא באינטרנט", "תוצאות חיפוש",
               "תוצאות החיפוש", "נתוני תחבורה", "תחזית מזג אוויר", "מילות חיפוש", "מקורות", "מקור",
               "transcript", "action", "search", "answer", "question", "query")
_META_RE = re.compile(r"^(%s)\s*[:：]" % "|".join(re.escape(x) for x in META_LABELS), re.I)
VALID_ACTIONS = ("none", "menu", "end", "voice", "warn")


def parse_fields(raw):
    """מפרק את תשובת הבינה לשדות (תמלול / פעולה / חיפוש / תשובה) גם כשהפורמט לא מדויק:
    כוכביות, שורה חסרה, סדר אחר. מחזיר מילון, או None אם אין בכלל שדות"""
    text = (raw or "").replace("\r", "")
    matches = list(_FIELD_RE.finditer(text))
    if not matches:
        return None
    out = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        val = text[m.end():end].strip().strip("*_").strip()
        if m.group(1) not in out:
            out[m.group(1)] = val
    return out


def strip_meta(text):
    """מנקה מהתשובה כל מה שהוא "מאחורי הקלעים" (שאלת..., חיפוש..., תמלול...) - רק התשובה עצמה מוקראת"""
    t = re.sub(r"[*_`#]", "", (text or "").replace("\r", ""))
    last = None
    for m in re.finditer(r"(?:^|\n)[ \t\-•>]*תשובה[ \t]*[:：]", t):
        last = m
    if last:
        t = t[last.end():]          # אם יש "תשובה:" - מקריאים רק את מה שאחריה
    keep = []
    for ln in t.split("\n"):
        s = ln.strip().lstrip("-•> ").strip()
        if s and not _META_RE.match(s):
            keep.append(s)
    return " ".join(keep).strip()


CHAT_INTRO_AUDIO = (
        " תקבל הקלטה של מה שהמשתמש אמר עכשיו. ההקלטה היא משיחת טלפון באיכות נמוכה, בעברית מדוברת,"
        " לפעמים עם רעשי רקע. הקשב בתשומת לב מלאה, והשתמש בהקשר של השיחה ובתחום של העוזר כדי להשלים מילים לא ברורות"
        " (שמות של זמרים, מלחינים, מקומות, מונחים). אם משהו באמת לא ברור, שאל בקצרה במקום לנחש."
        " סדר העבודה: קודם תמלל בדיוק מה נאמר, אחר כך בדוק מה בדיוק המשתמש שואל או מבקש, ורק אז ענה - על זה ולא על משהו אחר."
        " אם בתמלול חסרות מילים חשובות או שהוא לא הגיוני, אל תענה על ניחוש - בקש בתשובה לחזור על השאלה."
)
CHAT_INTRO_TEXT = (
        " תקבל את מה שהמשתמש אמר עכשיו בכתב (זו בדיקה של מנהל הקו מתוך אתר הניהול)."
        " התייחס לזה בדיוק כמו לשאלה שנשאלה בטלפון, ועבוד באותו סדר: קודם הבן מה בדיוק נשאל, ורק אז ענה - על זה ולא על משהו אחר."
        " בשורת התמלול העתק את הטקסט בדיוק כפי שנכתב."
)


def chat_system(assistant, from_text=False):
    """ההנחיות לבינה בשיחה - אותן הנחיות בדיוק לטלפון ולבדיקה מאתר הניהול. רק הפתיחה שונה (הקלטה / טקסט)"""
    others = "; ".join("%s = %s (מילים: %s)" % (a["id"], a["name"], a.get("keywords", "")) for _, a in active_assistants() if a["id"] != assistant["id"])
    return assistant["prompt"] + GENERAL_RULES + context_line(assistant) + (CHAT_INTRO_TEXT if from_text else CHAT_INTRO_AUDIO) + (
        " ענה בדיוק בפורמט הבא, ארבע שורות:\n"
        "תמלול: <תמלול מדויק של ההקלטה>\n"
        "פעולה: <אחת מהאפשרויות: none | menu | end | voice | warn | switch:מזהה>\n"
        "חיפוש: <לא | כן: מילות חיפוש קצרות וברורות כמו שכותבים בגוגל | מזג אוויר: שם המקום באנגלית"
        " | תחבורה: מספר קו | עיר או תחנת מוצא | עיר יעד | מספר תחנה | זמנים: עיר בעברית | היום / מחר / תאריך>."
        " זמנים = כל שאלה על זמני היום: שקיעה, זריחה, נץ, סוף זמן קריאת שמע ותפילה, חצות, מנחה, פלג המנחה, צאת הכוכבים,"
        " הדלקת נרות, כניסת ויציאת שבת או חג. למשל: זמנים: ירושלים | מחר. אם לא נאמרה עיר כתוב: זמנים: - | היום."
        " תחבורה = כל שאלה על אוטובוסים: מתי יוצא קו, מתי מגיע, אילו קווים נוסעים ממקום למקום, מה מגיע לתחנה."
        " כתוב ארבעה חלקים מופרדים בקו |, שמות ערים בעברית כמו שכותבים אותם, ובמקום פרט שלא נאמר כתוב -."
        " מספר תחנה הוא המספר של 5 ספרות שכתוב על שלט התחנה. דוגמאות: תחבורה: 402 | בני ברק | ירושלים | -"
        " או: תחבורה: - | - | - | 21345 או: תחבורה: - | אלעד | בני ברק | -."
        " כן = כשהתשובה דורשת חיפוש באינטרנט: המשתמש ביקש לחפש או לבדוק, או שצריך מידע עדכני - מחירים, חדשות, רכבות,"
        " שעות פתיחה, תוצאות, שירים ואלבומים חדשים, מה קורה עכשיו."
        " כתוב כן גם כששואלים על משהו מסוים שאינך מכיר היטב ובוודאות: בית כנסת, ישיבה, מוסד, רב או אדם, חנות או עסק,"
        " רחוב, שכונה, אירוע, ספר או שיר מסוים. במקרים כאלה לעולם אל תענה 'אינני יודע' בלי לחפש קודם."
        " אם השאלה על מזג האוויר, כתוב: מזג אוויר: ואז שם העיר באנגלית (למשל: מזג אוויר: Bnei Brak)."
        " אם המתקשר לא אמר עיר, אל תנחש עיר - כתוב: מזג אוויר: -\n"
        "תשובה: <התשובה שלך למשתמש. אם צריך חיפוש (כן / מזג אוויר / תחבורה / זמנים), כתוב כאן רק: מחפש>\n"
        "כללי הפעולה: menu אם ביקש לחזור לתפריט. end אם ביקש לסיים או להתנתק או אמר להתראות. "
        "voice אם ביקש להחליף קול. switch:מזהה אם ביקש לעבור לעוזר אחר מהרשימה: " + others + ". "
        "warn אם המשתמש שאל, ביקש או אמר משהו לא צנוע או גס: תוכן מיני או אינטימי, פריצות, ניבול פה. "
        "במקרה של warn אל תענה על התוכן עצמו, ובשורת החיפוש כתוב: לא. "
        "אחרת none. כשהפעולה אינה none, כתוב בתשובה משפט קצר מתאים (למשל: בטח, מעביר אותך)."
    )


def finish_answer(assistant, history, raw, info=None, known=""):
    """מפרק את תשובת הבינה, מחפש אם צריך, ומחזיר (תמלול, פעולה, תשובה). משותף לטלפון ולבדיקה באתר"""
    f = parse_fields(raw)
    need_search = False
    query = ""
    if f:
        transcript, search_line, answer = f.get("תמלול", ""), f.get("חיפוש", ""), f.get("תשובה", "")
        action = ((f.get("פעולה", "") or "none").strip().lower().split() or ["none"])[0].strip(".,")
        if action not in VALID_ACTIONS and not action.startswith("switch"):
            action = "none"
        need_search = ("כן" in search_line) or ("מזג" in search_line) or ("תחבור" in search_line) or ("זמנים" in search_line)
        if "כן" in search_line and ":" in search_line and "תחבור" not in search_line and "זמנים" not in search_line:
            query = search_line.split(":", 1)[1].strip().strip(".")
    else:
        transcript, action, search_line = "", "none", ""
        answer = raw.strip()
    if known and not transcript:
        transcript = known            # בבדיקה מהאתר השאלה ידועה מראש, גם אם הבינה לא החזירה שורת תמלול
    if action == "warn":
        return transcript, "warn", ""      # אזהרה: הנוסח נקבע לפי מספר האזהרות של המתקשר (ב-ai_worker)
    if transcript and action == "none" and "תחבור" not in search_line and any(w in transcript + " " for w in TRANSIT_WORDS):
        tp = parse_transit("", transcript)
        if tp["line"] or tp["stop"]:
            need_search = True    # שאלה על קו או תחנה - קודם מאגר התחבורה, גם אם הבינה סימנה חיפוש רגיל
            search_line = "תחבורה: "
    if transcript and action == "none" and "תחבור" not in search_line and "זמנים" not in search_line and "מזג" not in search_line \
            and any(w in transcript for w in ZMANIM_WORDS):
        need_search = True        # שאלה על זמני היום - עונים מחישוב מדויק, לא מהזיכרון של הבינה
        search_line = "זמנים: "
    if not need_search and transcript and action == "none" and any(w in transcript + " " for w in FORCE_SEARCH_WORDS):
        need_search = True        # המשתמש ביקש במפורש לחפש - מחפשים גם אם הבינה לא סימנה
    if not need_search and transcript and action == "none" and any(w in answer for w in DONT_KNOW_WORDS):
        need_search = True        # הבינה ענתה "לא יודע" בלי לחפש - מחפשים באינטרנט (לפי השאלה עצמה) לפני שמוותרים
    if not need_search and transcript and action == "none" and answer.strip(" .") == "מחפש":
        need_search = True        # הבינה כתבה "מחפש" אבל שכחה את שורת החיפוש - מחפשים לפי השאלה עצמה
    if info is not None:
        info["search"] = search_kind(search_line) if (need_search and transcript and action == "none") else ""
    if need_search and transcript and action == "none":
        t0 = time.time()
        found = answer_with_search(assistant, history, transcript, search_line, query)
        print("timing: search step %.1fs" % (time.time() - t0))
        if found:
            answer = strip_meta(found)
    if answer.strip() in ("מחפש", "מחפש.", "מחפש..."):
        answer = T("error")
    low = transcript.lower()
    if action == "none":
        if "החלף קול" in low or "תחליף קול" in low or "שנה קול" in low:
            action = "voice"
        elif low.strip() in ("תפריט", "תפריט.", "חזרה לתפריט"):
            action = "menu"
        elif low.strip() in ("סיים", "ביי", "להתראות", "סיים.", "ביי.", "להתראות."):
            action = "end"
        else:
            for _, a in active_assistants():
                if a["id"] != assistant["id"] and any(k and ("ל" + k in low or "את ה" + k in low) for k in csv_list(a.get("keywords", ""))):
                    if any(w in low for w in ("תעביר", "עבור", "תחליף", "רוצה", "תן לי")):
                        action = "switch:" + a["id"]
                        break
    if action.startswith("switch"):
        aid = action.split(":", 1)[1].strip() if ":" in action else ""
        action = "switch:" + aid if any(a["id"] == aid for _, a in active_assistants()) else "none"
    answer = strip_meta(answer)      # הגנה אחרונה: לא מקריאים "שאלת... חיפוש..." וכדומה
    if not answer:
        answer = T("not_understood")
    return transcript, action, clean_for_tts(answer)


def search_kind(search_line):
    """תיאור קצר של סוג החיפוש, לתצוגה באתר הניהול"""
    if "תחבור" in search_line:
        return "תחבורה ציבורית"
    if "זמנים" in search_line:
        return "זמני היום"
    if "מזג" in search_line:
        return "מזג אוויר"
    return "חיפוש באינטרנט"


def ask_ai_text(assistant, history, text, info=None):
    """בדיקה מאתר הניהול: אותה בינה, אותן הנחיות ואותו חיפוש כמו בטלפון - רק שהשאלה מגיעה כטקסט ולא כהקלטה"""
    contents = list(history) + [{"role": "user", "parts": [{"text": text}]}]
    raw = gemini(chat_system(assistant, from_text=True), contents, prefer_strong=True)
    if not raw:
        return text, "none", T("error")
    return finish_answer(assistant, history, raw, info, known=text)


def ask_ai(assistant, history, file_name):
    """מחזיר (תמלול, פעולה, תשובה). פעולה: none / menu / end / voice / warn / switch:id"""
    t0 = time.time()
    try:
        audio = yemot_download(file_name + ".wav")
    except Exception as e:
        print("download error:", e)
        return "", "none", "סליחה, לא הצלחתי לשמוע את ההקלטה. נסה שוב."
    print("timing: download %.1fs" % (time.time() - t0))
    if keep_days() == 0:
        _bg(yemot_delete, file_name + ".wav")      # אחרת ההקלטה נשמרת כמה ימים, כדי שאפשר יהיה לשמוע אותה באתר
    if is_silent(audio):
        print("recording is empty/silent - answering at once without AI")
        return "", "none", T("not_heard")
    t0 = time.time()
    audio = clean_audio(audio)     # סינון רעשים והגברה - ה-AI שומע הרבה יותר טוב ומבין נכון את השאלה
    print("timing: clean audio %.1fs" % (time.time() - t0))

    system = chat_system(assistant)
    contents = list(history) + [{
        "role": "user",
        "parts": [{"text": "ההקלטה של המשתמש:"}, types.Part.from_bytes(data=audio, mime_type="audio/wav")],
    }]
    t0 = time.time()
    raw = gemini(system, contents, prefer_strong=True)
    print("timing: gemini(audio) %.1fs" % (time.time() - t0))
    if not raw:
        return "", "none", T("error")
    return finish_answer(assistant, history, raw)


# ============================================================ מכסה / חסימה
def messages_today(phone):
    today = today_str()
    with _lock:
        return sum(1 for l in LOG if l["phone"] == phone and l["time"].startswith(today))


def over_limit(phone):
    limit = SETTINGS.get("daily_limit", 0)
    if limit <= 0 or phone in OWNER_PHONES or phone in csv_list(SETTINGS.get("unlimited_phones", "")):
        return False
    return messages_today(phone) >= limit


def is_blocked(phone):
    return phone in csv_list(SETTINGS.get("blocked_phones", "")) and phone not in OWNER_PHONES


def add_warning(phone):
    """סופר אזהרה למתקשר ומחזיר את המספר שיוקרא (1, 2, 3, ומשם תמיד 3). אין חסימה"""
    with _lock:
        warnings[phone] = warnings.get(phone, 0) + 1
        n = min(warnings[phone], WARN_MAX)
    save_names()
    return n


# ============================================================ בניית תגובות לימות
def R(text):
    return Response(text, mimetype="text/plain; charset=utf-8")


def msg_part(state, text):
    f = state.pop("tts_file", None)
    if f:
        return ("file", f)
    return ("text", text)


def menu(state, name, prefix=None):
    state["n"] += 1
    state["wait"] = "choice_%d" % state["n"]
    state["stage"] = "menu"
    items = [T("menu_hello", name=name)]
    allowed = ""
    for digit, a in active_assistants():
        nm = a["name"]
        nm = nm[1:] if nm.startswith("ה") else nm
        items.append(T("menu_item", digit=digit, assistant=nm))
        allowed += digit
    items.append(T("menu_end"))
    read = build_read([("text", " ".join(items))], mode="tap", val_name=state["wait"], max_digits=1, min_digits=1,
                      digits_allowed=allowed + "9", sec_wait=10)
    if prefix:
        return build_combined_action([build_id_list_message([msg_part(state, prefix)]), read])
    return read


def record(state, val_prefix, message, prefix=None):
    state["n"] += 1
    state["wait"] = "%s_%d" % (val_prefix, state["n"])
    file_name = "ai_%s_%d" % (re.sub(r"[^0-9a-zA-Z]", "", state["call_id"])[-12:], state["n"])
    state["file"] = file_name
    read = build_read([message], mode="record", val_name=state["wait"], path="", file_name=file_name,
                      no_confirm_menu="no", save_on_hangup="no", min_length="",
                      max_length=int(SETTINGS.get("record_max", 25) or 25))
    if prefix:
        return build_combined_action([build_id_list_message([prefix]), read])
    return read


def listen(state, text=None, first=False):
    state["stage"] = "chat"
    if first:
        return record(state, "speech", ("text", text))
    return record(state, "speech", msg_part(state, text or T("listening")))


def wait_message(state):
    phrases = csv_list(TEXTS.get("wait", "")) or ["רק רגע"]
    i = state.get("wait_i", 0)
    state["wait_i"] = i + 1
    state["n"] += 1
    mf = music_file()
    if hold_mode():
        parts = [("text", phrases[i % len(phrases)])]     # ימות ממשיכים לנגן כשהשרת שוב מחזיק את הבקשה
        gap = 1
    elif mf:
        # המנגינה מתחילה מיד; "רק רגע" נאמר לפניה רק בכל פעם שלישית (בפעם השנייה, החמישית...), כדי שלא יחזור שוב ושוב
        parts = ([("text", phrases[(i // 3) % len(phrases)])] if i % 3 == 1 else []) + [("file", mf)]
        gap = 1
    else:
        parts = [("text", phrases[i % len(phrases)])]
        gap = 2
    return build_read(parts, mode="tap", val_name="w_%d" % state["n"],
                      max_digits=1, min_digits=1, sec_wait=gap, amount_attempts=1, allow_empty="Ok", empty_val="None")


def goodbye(call_id, name, state=None):
    part = msg_part(state, T("goodbye", name=name)) if state else ("text", T("goodbye", name=name))
    with _lock:
        calls.pop(call_id, None)
    return build_combined_action([build_id_list_message([part]), build_go_to_folder("hangup")])


def ai_worker(pending, state, assistant, history, file_name, call_id, voice_idx):
    try:
        transcript, action, answer = ask_ai(assistant, history, file_name)
        if action == "warn":
            n = add_warning(state.get("phone", ""))
            answer = clean_for_tts(T("warning", n=n))
            pending["warn"] = n
            action = "none"                 # השיחה ממשיכה כרגיל אחרי האזהרה
            print("warning %d for %s" % (n, state.get("phone", "")))
        state["pending_q"], state["pending_a"] = transcript, answer   # מוצג באתר עוד לפני שהקול מוכן
        if keep_days() and transcript:
            pending["rec"] = file_name          # ההקלטה נשמרת כמה ימים - אפשר לשמוע אותה באתר
        elif keep_days():
            _bg(yemot_delete, file_name + ".wav")
        tts = None
        if action in ("none", "voice") or action.startswith("switch"):
            v = voice_idx
            if action == "voice":
                vl = voice_list()
                v = (voice_idx + 1) % max(1, len(vl))
                for _ in range(len(vl)):
                    if voice_ok(vl[v % len(vl)]):
                        break
                    v = (v + 1) % len(vl)
            tts = speak_file(answer, v, call_id)
        pending["result"] = (transcript, action, answer, tts)
    except Exception as e:
        print("worker error:", e)
        pending["result"] = ("", "none", T("error"), None)
    finally:
        pending["done"] = True
        pending["event"].set()


def cleanup_loop():
    while True:
        try:
            cutoff = time.time() - 2 * 3600
            with _lock:
                for cid in [c for c, st in calls.items() if st.get("last", 0) < cutoff]:
                    calls.pop(cid, None)
        except Exception as e:
            print("cleanup error:", e)
        try:
            recordings_cleanup()
        except Exception as e:
            print("recordings cleanup error:", e)
        time.sleep(600)


threading.Thread(target=cleanup_loop, daemon=True).start()


# ============================================================ הקו
@app.route("/", methods=["GET", "POST"])
def yemot():
    params = request.values.to_dict()
    call_id = params.get("ApiCallId")
    if not call_id:
        return R("ok")
    if params.get("hangup") == "yes":
        with _lock:
            st = calls.pop(call_id, None)
        if st and st.get("tts_file"):
            _bg(yemot_delete, st["tts_file"] + ".wav")
        return R("noop")

    phone = params.get("ApiPhone", "unknown")

    with _lock:
        state = calls.get(call_id)
        new_call = state is None
        if new_call:
            state = {"stage": "start", "n": 0, "wait": None, "assistant": None, "history": [], "call_id": call_id,
                     "file": None, "pending": None, "wait_i": 0, "phone": phone, "started": time.time(),
                     "voice": voices.get(phone, 0), "last_q": "", "tts_file": None, "played": []}
            calls[call_id] = state
            CALLS.append({"time": now_str(), "phone": phone, "name": names.get(phone, ""), "call": call_id})
            del CALLS[:-LOG_MAX]
        state["last"] = time.time()
    if new_call:
        save_log()
        if is_blocked(phone):
            with _lock:
                calls.pop(call_id, None)
            return R(build_combined_action([build_id_list_message([("text", T("blocked"))]), build_go_to_folder("hangup")]))
        try:
            closed, closed_msg = line_closed()
        except Exception as e:
            print("shabbat check error:", e)
            closed = False
        if closed:      # שבת או חג - הודעה וניתוק (שיחה שכבר התחילה לפני הכניסה ממשיכה כרגיל)
            with _lock:
                calls.pop(call_id, None)
            return R(build_combined_action([build_id_list_message([("text", closed_msg)]), build_go_to_folder("hangup")]))

    if state["played"]:
        for f in state["played"]:
            _bg(yemot_delete, f + ".wav")
        state["played"] = []

    has_value = bool(state["wait"]) and state["wait"] in params
    value = (params.get(state["wait"], "") or "").strip() if has_value else ""
    if value == "None":
        value = ""
    name = names.get(phone)

    # ---- התחלה
    if state["stage"] == "start":
        note = None
        n = notes.get(phone)
        if isinstance(n, str):
            n = {"text": n, "created": "", "heard": ""}
            notes[phone] = n
        if n and n.get("text") and not n.get("heard"):
            note = n["text"]
            n["heard"] = now_str()
            save_names()
        parts = [p for p in [SETTINGS.get("announcement", "").strip(), note] if p]
        ann = ". ".join(parts) if parts else None
        if name:
            return R(menu(state, name, prefix=ann))
        state["stage"] = "ask_name"
        return R(record(state, "name", ("text", T("first_time")), prefix=("text", ann) if ann else None))

    # ---- קבלת שם
    if state["stage"] == "ask_name":
        if not has_value:
            return R(record(state, "name", ("text", T("ask_name_again"))))
        name = transcribe_name(state["file"])
        if not name:
            state["name_tries"] = state.get("name_tries", 0) + 1
            if state["name_tries"] < 3:     # לא הובן - מבקשים שוב (עד פעמיים)
                return R(record(state, "name", ("text", T("name_not_understood") + " " + T("ask_name_again"))))
            return R(menu(state, "אורח"))   # עדיין לא הובן - ממשיכים בלי לשמור, ובשיחה הבאה נשאל שוב
        names[phone] = name
        save_names()
        return R(menu(state, name, prefix=T("name_saved", name=name)))

    name = name or "אורח"

    # ---- תפריט
    if state["stage"] == "menu":
        if value == "9":
            return R(goodbye(call_id, name, state))
        for digit, a in active_assistants():
            if value == digit:
                state["assistant"] = a["id"]
                state["history"] = []
                return R(listen(state, T("enter", assistant=a["name"], name=name), first=True))
        return R(menu(state, name))

    # ---- שיחה
    if state["stage"] == "chat":
        assistant = assistant_by_id(state["assistant"])
        pending = state.get("pending")
        if pending is None:
            if not has_value:
                return R(listen(state, T("not_heard")))
            if over_limit(phone):
                _bg(yemot_delete, state["file"] + ".wav")
                return R(menu(state, name, prefix=T("limit")))
            pending = {"done": False, "result": None, "started": time.time(), "event": threading.Event()}
            state["pending"] = pending
            state["wait_i"] = 0
            _bg(ai_worker, pending, state, assistant, list(state["history"]), state["file"], call_id, state["voice"])
            if hold_mode():
                pending["event"].wait(HOLD_WAIT)   # ימות מנגנים מוזיקה בהמתנה, ונעצרים ברגע שהתשובה חוזרת
            elif not music_on():
                pending["event"].wait(11)      # עם מנגינה - לא מחכים בכלל: המנגינה מתחילה מיד אחרי הסולמית

        if not pending["done"]:
            if time.time() - pending["started"] > 75:
                state["pending"] = None
                return R(listen(state, T("too_long")))
            if state["wait_i"] > 0:
                pending["event"].wait(HOLD_WAIT if hold_mode() else (1.5 if music_on() else 6))   # עם מנגינה - המנגינה ממלאת את זמן ההמתנה
            if not pending["done"]:
                return R(wait_message(state))

        state["pending"] = None
        state["pending_q"] = state["pending_a"] = ""
        transcript, action, answer, tts = pending["result"]
        if tts:
            state["tts_file"] = tts
            state["played"].append(tts)

        if transcript:
            state["last_q"] = transcript
            state["history"].append({"role": "user", "parts": [{"text": transcript}]})
            state["history"].append({"role": "model", "parts": [{"text": answer}]})
            state["history"] = state["history"][-12:]
            entry = {"time": now_str(), "phone": phone, "name": name, "call": call_id,
                     "persona": assistant["name"], "q": transcript, "a": answer, "rec": pending.get("rec", "")}
            if pending.get("warn"):
                entry["warn"] = pending["warn"]
            with _lock:
                LOG.append(entry)
                del LOG[:-LOG_MAX]
            save_log()

        if action == "menu":
            state["tts_file"] = None
            return R(menu(state, name))
        if action == "end":
            state["tts_file"] = None
            return R(goodbye(call_id, name, state))
        if action == "voice":
            vl = voice_list()
            nxt = (state["voice"] + 1) % max(1, len(vl))
            for _ in range(len(vl)):
                if voice_ok(vl[nxt % len(vl)]):
                    break
                nxt = (nxt + 1) % len(vl)
            state["voice"] = nxt
            voices[phone] = state["voice"]
            save_names()
            if tts:
                return R(listen(state, answer))
            return R(listen(state, T("voice_changed") + ". " + answer))
        if action.startswith("switch:"):
            state["assistant"] = action.split(":", 1)[1]
            return R(listen(state, answer))
        return R(listen(state, answer))

    return R(menu(state, name))


# ============================================================ מייל יומי
def snapshot():
    with _lock:
        return dict(names), list(LOG), list(CALLS), {k: dict(v) for k, v in calls.items()}


def build_summary(day):
    h = html.escape
    users, log, cl, _ = snapshot()
    log = [l for l in log if l["time"].startswith(day)]
    cl = [c for c in cl if c["time"].startswith(day)]
    phones = sorted(set(c["phone"] for c in cl) | set(l["phone"] for l in log))
    out = ["<div dir='rtl' style='font-family:Arial'><h2>סיכום הקו ליום %s</h2>" % h(day),
           "<p>שיחות: <b>%d</b> &nbsp; מתקשרים שונים: <b>%d</b> &nbsp; הודעות ל-AI: <b>%d</b></p>" % (len(cl), len(phones), len(log))]
    if phones:
        out.append("<h3>לפי מתקשר</h3><ul>")
        for ph in phones:
            out.append("<li>%s (%s): %d שיחות, %d הודעות</li>" % (h(users.get(ph, "לא רשום")), h(ph),
                                                                 sum(1 for c in cl if c["phone"] == ph), sum(1 for l in log if l["phone"] == ph)))
        out.append("</ul>")
    if log:
        out.append("<h3>מה שאלו</h3><table border='1' cellpadding='5' style='border-collapse:collapse'><tr><th>שעה</th><th>מי</th><th>עוזר</th><th>שאלה</th><th>תשובה</th></tr>")
        for l in log[:150]:
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                h(l["time"][11:]), h(l["name"]), h(l.get("persona", "")), h(l["q"]), h(l["a"][:200])))
        out.append("</table>")
    else:
        out.append("<p>לא היו הודעות היום.</p>")
    out.append("</div>")
    return "".join(out)


def send_mail(subject, body_html):
    if not (MAIL_USER and MAIL_PASS and MAIL_TO):
        return "לא הוגדר מייל (MAIL_USER / MAIL_PASS ב-Render)"
    try:
        msg = MIMEText(body_html, "html", "utf-8")
        msg["Subject"], msg["From"], msg["To"] = subject, MAIL_USER, MAIL_TO
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(MAIL_USER, MAIL_PASS)
            smtp.sendmail(MAIL_USER, [MAIL_TO], msg.as_string())
        return "נשלח"
    except Exception as e:
        print("mail error:", e)
        return "שגיאה בשליחה: %s" % e


_last_mail_day = [None]


def daily_mail_loop():
    while True:
        try:
            now = il_now()
            day = now.strftime("%d/%m/%Y")
            if now.hour == SETTINGS.get("mail_hour", 21) and _last_mail_day[0] != day and MAIL_USER:
                _last_mail_day[0] = day
                print("daily mail:", send_mail("סיכום הקו ליום " + day, build_summary(day)))
        except Exception as e:
            print("daily mail error:", e)
        time.sleep(60)


threading.Thread(target=daily_mail_loop, daemon=True).start()


# ============================================================ אתר ניהול (API)
def is_admin():
    if not ADMIN_KEY:
        return False
    return (request.values.get("key") or request.cookies.get("admin_key")) == ADMIN_KEY


def J(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False), status=status, mimetype="application/json; charset=utf-8")


def api_guard():
    if not is_admin():
        return J({"error": "unauthorized"}, 403)
    return None


@app.route("/admin/login", methods=["POST"])
def admin_login():
    key = (request.get_json(silent=True) or {}).get("key") or request.form.get("key", "")
    if not ADMIN_KEY:
        return J({"ok": False, "error": "לא הוגדרה סיסמה (ADMIN_KEY) בשרת"})
    if key != ADMIN_KEY:
        return J({"ok": False, "error": "סיסמה שגויה"})
    resp = J({"ok": True})
    resp.set_cookie("admin_key", ADMIN_KEY, max_age=60 * 60 * 24 * 180, httponly=True)
    return resp


@app.route("/admin/logout")
def admin_logout():
    resp = Response("", status=302, headers={"Location": "/admin"})
    resp.set_cookie("admin_key", "", max_age=0)
    return resp


def live_data():
    users, log, cl, active = snapshot()
    today = today_str()
    act = []
    for cid, st in sorted(active.items(), key=lambda x: -x[1].get("started", 0)):
        a = assistant_by_id(st.get("assistant")) if st.get("assistant") else None
        act.append({"phone": st.get("phone", ""), "name": users.get(st.get("phone", ""), "לא רשום"),
                    "assistant": a["name"] if a else "בתפריט",
                    "since": datetime.datetime.fromtimestamp(st.get("started", 0), ZoneInfo("Asia/Jerusalem")).strftime("%H:%M"),
                    "last_q": st.get("last_q", ""),
                    "state": "מחכה לתשובה" if st.get("pending") else ("מדבר" if st.get("stage") == "chat" else "בתפריט")})
    return {"active": act, "calls_today": sum(1 for c in cl if c["time"].startswith(today)),
            "msgs_today": sum(1 for l in log if l["time"].startswith(today)), "users": len(users),
            "calls_total": len(cl), "msgs_total": len(log), "time": now_str()}


@app.route("/api/live")
def api_live():
    g = api_guard()
    return g or J(live_data())


@app.route("/api/state")
def api_state():
    g = api_guard()
    if g:
        return g
    users, log, cl, _ = snapshot()
    blocked, unlimited = csv_list(SETTINGS["blocked_phones"]), csv_list(SETTINGS["unlimited_phones"])
    per_user = {}
    for l in log:
        per_user[l["phone"]] = per_user.get(l["phone"], 0) + 1
    ulist = []
    for ph, nm in users.items():
        ulist.append({"phone": ph, "name": nm, "calls": sum(1 for c in cl if c["phone"] == ph), "msgs": per_user.get(ph, 0),
                      "today": sum(1 for l in log if l["phone"] == ph and l["time"].startswith(today_str())),
                      "blocked": ph in blocked, "unlimited": ph in unlimited or ph in OWNER_PHONES, "owner": ph in OWNER_PHONES,
                      "note": (notes.get(ph) if isinstance(notes.get(ph), dict) else ({"text": notes.get(ph), "created": "", "heard": ""} if notes.get(ph) else None)),
                      "voice": voices.get(ph, 0), "warnings": warnings.get(ph, 0),
                      "last": max([c["time"] for c in cl if c["phone"] == ph] or [""])})
    days = [(il_now() - datetime.timedelta(days=i)).strftime("%d/%m/%Y") for i in range(13, -1, -1)]
    per_day = [[d[:5], sum(1 for c in cl if c["time"].startswith(d))] for d in days]
    per_a = {}
    for l in log:
        per_a[l.get("persona", "")] = per_a.get(l.get("persona", ""), 0) + 1
    return J({
        "settings": SETTINGS, "texts": TEXTS, "assistants": ASSISTANTS, "users": ulist,
        "log": log[-600:], "log_total": len(log), "calls": cl[-300:][::-1],
        "charts": {"per_day": per_day, "per_assistant": sorted(per_a.items(), key=lambda x: -x[1])[:8],
                   "top_users": [[users.get(p, p), n] for p, n in sorted(per_user.items(), key=lambda x: -x[1])[:10]]},
        "voices": voice_list(), "mail": bool(MAIL_USER and MAIL_PASS), "mail_to": MAIL_TO, "owners": OWNER_PHONES,
        "shabbat": shabbat_status(), "cities": sorted({k for k in CITIES if re.search(r"[א-ת]", k)}),
        "models": [{"name": m, "ok": model_ok(m), "dead": bool(MODEL_STATUS.get(m, {}).get("dead"))}
                   for m in ([SETTINGS["model"]] if SETTINGS.get("model") else []) + [x for x in MODELS if x != SETTINGS.get("model")]],
        "search": {"sources": SEARCH_STATE["last"],
                   "google_paused_min": max(0, round((SEARCH_STATE["ground_until"] - time.time()) / 60)),
                   "google_paused_models": {m: max(1, round((u - time.time()) / 60)) for m, u in SEARCH_MODEL_UNTIL.items() if u > time.time()}},
        "live": live_data(),
    })


@app.route("/api/assistants", methods=["POST"])
def api_assistants():
    g = api_guard()
    if g:
        return g
    d = request.get_json(silent=True) or {}
    lst = d.get("assistants")
    if not isinstance(lst, list):
        return J({"ok": False, "error": "bad data"})
    out = []
    for a in lst:
        aid = re.sub(r"[^a-z0-9_]", "", str(a.get("id", "")).lower()) or ("a" + uuid.uuid4().hex[:6])
        nm = str(a.get("name", "")).strip()[:40]
        pr = str(a.get("prompt", "")).strip()[:2000]
        if not nm or not pr:
            continue
        out.append({"id": aid, "name": nm, "prompt": pr, "on": bool(a.get("on", True)), "keywords": str(a.get("keywords", ""))[:200],
                    "vocab": str(a.get("vocab", ""))[:1500]})
    if not out:
        return J({"ok": False, "error": "חייב להישאר לפחות עוזר אחד"})
    ASSISTANTS[:] = out
    save_assistants()
    return J({"ok": True})


@app.route("/api/texts", methods=["POST"])
def api_texts():
    g = api_guard()
    if g:
        return g
    d = request.get_json(silent=True) or {}
    for k in TEXTS:
        if k in d and str(d[k]).strip():
            TEXTS[k] = clean_for_tts(str(d[k]))[:400] if k != "wait" else str(d[k])[:300]
    save_texts()
    return J({"ok": True})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    g = api_guard()
    if g:
        return g
    d = request.get_json(silent=True) or {}

    def num(key, lo, hi, default):
        try:
            return min(hi, max(lo, int(d.get(key, default) or 0)))
        except (ValueError, TypeError):
            return default
    SETTINGS["daily_limit"] = num("daily_limit", 0, 100000, 40)
    SETTINGS["mail_hour"] = num("mail_hour", 0, 23, 21)
    SETTINGS["record_max"] = num("record_max", 5, 120, 25)
    SETTINGS["unlimited_phones"] = re.sub(r"[^0-9,]", "", str(d.get("unlimited_phones", "")))
    SETTINGS["blocked_phones"] = re.sub(r"[^0-9,]", "", str(d.get("blocked_phones", "")))
    SETTINGS["announcement"] = clean_for_tts(str(d.get("announcement", "")))[:300]
    SETTINGS["tts"] = "on" if d.get("tts") == "on" else "off"
    SETTINGS["voices"] = ",".join(csv_list(str(d.get("voices", "")))) or DEFAULT_VOICES
    SETTINGS["model"] = re.sub(r"[^a-z0-9.\-]", "", str(d.get("model", "")).lower())[:60]
    SETTINGS["vocab"] = clean_for_tts(str(d.get("vocab", "")))[:1500]
    wm = str(d.get("wait_music", "trance"))
    SETTINGS["wait_music"] = "trance" if wm == "on" else (wm if wm in ("trance", "bells", "custom", "yemot", "off") else "trance")
    if "shabbat_mode" in d:
        SETTINGS["shabbat_mode"] = "off" if d.get("shabbat_mode") == "off" else "on"
    if "shabbat_city" in d:
        SETTINGS["shabbat_city"] = clean_for_tts(str(d.get("shabbat_city") or "בני ברק"))[:40] or "בני ברק"
    if "shabbat_before" in d:
        SETTINGS["shabbat_before"] = num("shabbat_before", 0, 120, 30)
    if "shabbat_after" in d:
        SETTINGS["shabbat_after"] = num("shabbat_after", 0, 180, 50)
    if "keep_rec_days" in d:
        SETTINGS["keep_rec_days"] = num("keep_rec_days", 0, 30, 3)
    _shabbat_cache["t"] = 0
    save_settings()
    return J({"ok": True})


@app.route("/api/user", methods=["POST"])
def api_user():
    g = api_guard()
    if g:
        return g
    d = request.get_json(silent=True) or {}
    phone, action = str(d.get("phone", "")), d.get("action")
    if action == "rename":
        nm = clean_for_tts(str(d.get("name", "")))[:30]
        if nm:
            names[phone] = nm
    elif action == "delete":
        names.pop(phone, None)
        notes.pop(phone, None)
        voices.pop(phone, None)
        warnings.pop(phone, None)
    elif action == "reset_warnings":
        warnings.pop(phone, None)
    elif action == "block":
        bl = csv_list(SETTINGS["blocked_phones"])
        if phone in bl:
            bl.remove(phone)
        elif phone not in OWNER_PHONES:
            bl.append(phone)
        SETTINGS["blocked_phones"] = ",".join(bl)
        save_settings()
    elif action == "unlimited":
        ul = csv_list(SETTINGS["unlimited_phones"])
        if phone in ul and phone not in OWNER_PHONES:
            ul.remove(phone)
        elif phone not in ul:
            ul.append(phone)
        SETTINGS["unlimited_phones"] = ",".join(ul)
        save_settings()
    elif action == "note":
        txt = clean_for_tts(str(d.get("note", "")))[:300]
        if txt:
            notes[phone] = {"text": txt, "created": now_str(), "heard": ""}
        else:
            notes.pop(phone, None)
    elif action == "note_again":
        if isinstance(notes.get(phone), dict):
            notes[phone]["heard"] = ""
            notes[phone]["created"] = now_str()
    elif action == "voice":
        try:
            voices[phone] = int(d.get("voice", 0))
        except (ValueError, TypeError):
            pass
    elif action == "add":
        nm = clean_for_tts(str(d.get("name", "")))[:30]
        ph = re.sub(r"[^0-9]", "", phone)
        if nm and ph:
            names[ph] = nm
    save_names()
    return J({"ok": True})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    g = api_guard()
    if g:
        return g
    d = request.get_json(silent=True) or {}
    with _lock:
        if d.get("what") == "calls":
            CALLS.clear()
        else:
            LOG.clear()
    save_log()
    return J({"ok": True})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    """מחיקה נקודתית מהיומן: הודעה אחת (msg), שיחה אחת (call), או כל השיחות של מתקשר (phone).
    ההקלטות של מה שנמחק נמחקות גם מימות"""
    g = api_guard()
    if g:
        return g
    d = request.get_json(silent=True) or {}
    msg = d.get("msg") if isinstance(d.get("msg"), dict) else None
    call = str(d.get("call") or "").strip()
    phone = str(d.get("phone") or "").strip()
    if not (msg or call or phone):
        return J({"ok": False, "error": "לא צוין מה למחוק"})
    removed = []
    with _lock:
        if msg:
            for i, l in enumerate(LOG):
                if l.get("time") == msg.get("time") and l.get("phone") == msg.get("phone") and l.get("q") == msg.get("q"):
                    removed.append(LOG.pop(i))
                    break
        elif call:
            removed = [l for l in LOG if l.get("call") == call]
            LOG[:] = [l for l in LOG if l.get("call") != call]
            CALLS[:] = [c for c in CALLS if c.get("call") != call]
        else:
            removed = [l for l in LOG if l.get("phone") == phone]
            LOG[:] = [l for l in LOG if l.get("phone") != phone]
            CALLS[:] = [c for c in CALLS if c.get("phone") != phone]
    for l in removed:
        if l.get("rec"):
            _bg(yemot_delete, l["rec"] + ".wav")
    save_log()
    return J({"ok": True, "removed": len(removed)})


@app.route("/api/sendmail", methods=["POST"])
def api_sendmail():
    g = api_guard()
    if g:
        return g
    day = today_str()
    return J({"ok": True, "result": send_mail("סיכום הקו ליום " + day, build_summary(day))})


@app.route("/api/log")
def api_log():
    """שורות יומן חדשות בלבד (לעדכון חי של מסך השיחות)"""
    g = api_guard()
    if g:
        return g
    try:
        after = int(request.args.get("after", "0"))
    except ValueError:
        after = 0
    with _lock:
        total = len(LOG)
        new = LOG[after:] if 0 <= after <= total else LOG[-50:]
        act = {st.get("call_id"): {"phone": st.get("phone"), "waiting": bool(st.get("pending")),
                                   "pending_q": st.get("pending_q", "") if st.get("pending") else "",
                                   "pending_a": st.get("pending_a", "") if st.get("pending") else "",
                                   "assistant": (assistant_by_id(st["assistant"])["name"] if st.get("assistant") else "")}
               for st in calls.values()}
    return J({"total": total, "new": new, "active": act, "time": now_str()})


@app.route("/api/export.csv")
def api_export():
    g = api_guard()
    if g:
        return g
    _, log, _, _ = snapshot()
    rows = ["\ufeffזמן,שם,טלפון,עוזר,שאלה,תשובה"]
    for l in log:
        rows.append(",".join('"%s"' % str(l.get(k, "")).replace('"', '""') for k in ("time", "name", "phone", "persona", "q", "a")))
    return Response("\n".join(rows), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=yemot-ai-log.csv"})


@app.route("/api/diag", methods=["POST"])
def api_diag():
    """בדיקת מערכת מלאה בלחיצה אחת: פייתון, Gemini, קול, ימות, כל מקורות החיפוש - עם זמנים ושגיאות מדויקות"""
    g = api_guard()
    if g:
        return g
    out = {"python": sys.version.split()[0], "models": list(MODELS), "preferred": SETTINGS.get("model", ""),
           "env": {"GEMINI_API_KEY": bool(os.environ.get("GEMINI_API_KEY")), "YEMOT_TOKEN": bool(YEMOT_TOKEN),
                   "ADMIN_KEY": bool(ADMIN_KEY), "PYTHON_VERSION": os.environ.get("PYTHON_VERSION", "")},
           "tts_libs": HAVE_TTS}
    t0 = time.time()
    try:
        r = gemini("ענה במילה אחת בעברית.", [{"role": "user", "parts": [{"text": "שלום, מה שלומך?"}]}])
        out["gemini"] = {"ok": bool(r), "answer": (r or "")[:60], "seconds": round(time.time() - t0, 1)}
    except Exception as e:
        out["gemini"] = {"ok": False, "error": str(e)[:200], "seconds": round(time.time() - t0, 1)}
    t0 = time.time()
    try:
        wav = call_with_deadline(lambda: make_tts("שלום, זו בדיקה", voice_list()[0]), 15)
        out["tts"] = {"ok": bool(wav), "bytes": len(wav or b""), "seconds": round(time.time() - t0, 1)}
    except Exception as e:
        out["tts"] = {"ok": False, "error": str(e)[:200], "seconds": round(time.time() - t0, 1)}
    t0 = time.time()
    try:
        yemot_write_text("ai_diag.txt", "ok " + now_str())
        time.sleep(1.5)
        txt = yemot_read_text("ai_diag.txt")
        out["yemot"] = {"ok": bool(txt and txt.startswith("ok")), "seconds": round(time.time() - t0, 1)}
    except Exception as e:
        out["yemot"] = {"ok": False, "error": str(e)[:200]}

    # כל מקורות החיפוש נבדקים במקביל, כדי שהבדיקה לא תיקח יותר מדי זמן
    def timed(fn):
        def run():
            s = time.time()
            try:
                r = fn()
                return {"ok": bool(r), "seconds": round(time.time() - s, 1), "answer": (r if isinstance(r, str) else "")[:60]}
            except Exception as e:
                return {"ok": False, "seconds": round(time.time() - s, 1), "error": str(e)[:200]}
        return run
    was_paused = SEARCH_STATE["ground_until"]
    SEARCH_STATE["ground_until"] = 0      # בבדיקה מנסים את חיפוש גוגל גם אם הוא בהפסקה
    SEARCH_MODEL_UNTIL.clear()            # ...ובכל המודלים, כדי לראות אם יש מודל שהחיפוש עדיין זמין בו
    res = run_parallel([
        timed(lambda: grounded_answer("ענה במשפט אחד קצר בעברית.", [{"role": "user", "parts": [{"text": "מה הכותרת הראשית בחדשות בישראל היום?"}]}])),
        timed(lambda: search_ddgs("חדשות היום", 3)),
        timed(lambda: search_bing("חדשות היום", 3)),
        timed(lambda: search_news("ישראל", 3)),
        timed(lambda: fetch_page_text("https://he.wikipedia.org/wiki/%D7%99%D7%A8%D7%95%D7%A9%D7%9C%D7%99%D7%9D")),
        timed(lambda: transit_lookup("1 | - | - | -")),
    ], 24)
    if not res[0] or not res[0].get("ok"):
        SEARCH_STATE["ground_until"] = max(was_paused, SEARCH_STATE["ground_until"])
    labels = ["google", "ddg", "bing", "news", "pages", "transit"]
    for k, r in zip(labels, res):
        err = (SEARCH_STATE["last"].get(k) or {}).get("error", "")
        out[k] = r or {"ok": False, "error": "לא ענה בזמן"}
        if not out[k].get("ok") and err and not out[k].get("error"):
            out[k]["error"] = err
    out["search"] = {"ok": any((out[k] or {}).get("ok") for k in labels),
                     "seconds": max([(out[k] or {}).get("seconds", 0) for k in labels] or [0]), "libs": HAVE_DDGS}
    t0 = time.time()
    out["weather"] = {"ok": bool(weather_lookup("Bnei Brak")), "seconds": round(time.time() - t0, 1)}
    t0 = time.time()
    out["wiki"] = {"ok": bool(wiki_search("ישי ריבו")), "seconds": round(time.time() - t0, 1)}
    try:
        nm, lat, lon = place_of(SETTINGS.get("shabbat_city", ""))
        ss = sun_time(il_now().date(), lat, lon, 0.833, False)
        out["zmanim"] = {"ok": bool(ss), "answer": "שקיעה היום ב%s: %s" % (nm, ss.strftime("%H:%M") if ss else "?")}
    except Exception as e:
        out["zmanim"] = {"ok": False, "error": str(e)[:200]}
    _shabbat_cache["t"] = 0
    st = shabbat_status()
    if st.get("enabled"):
        if st.get("error"):
            out["shabbat"] = {"ok": False, "error": st["error"]}
        else:
            msg = ("סגור עכשיו (%s) עד %s" % (st.get("name"), st.get("until"))) if st.get("closed") else \
                  ("ייסגר ב%s לכבוד %s" % (st.get("next_start", "?"), st.get("next_name", "")))
            out["shabbat"] = {"ok": True, "answer": msg}
            if not st.get("festivals"):
                out["shabbat"] = {"ok": False, "answer": msg, "error": "רק שבתות: חסרה הספרייה pyluach ב-requirements.txt, ולכן חגים לא מזוהים"}
    st = music_style()
    if st in WAIT_FILES:
        name = WAIT_FILES[st]
        MUSIC_READY[name] = music_exists(name)
        got = music_file()
        out["wait_music"] = {"ok": bool(got), "answer": got or ""}
        if not MUSIC_READY[name]:
            out["wait_music"]["error"] = "הקובץ %s.wav לא נמצא בשלוחה%s" % (name, ", מנגן טראנס במקומו" if got else "")
    out["model_status"] = {m: ("לא קיים" if MODEL_STATUS.get(m, {}).get("dead") else ("מכסה" if not model_ok(m) else "ok")) for m in MODELS}
    return J(out)


@app.route("/api/test_chat", methods=["POST"])
def api_test_chat():
    """צ'אט בדיקה מאתר הניהול: עונה בדיוק כמו הקו בטלפון (אותו עוזר, אותן הנחיות, אותו חיפוש).
    לא נרשם ביומן, לא נספר במכסה ולא מוסיף אזהרות"""
    g = api_guard()
    if g:
        return g
    d = request.get_json(silent=True) or {}
    text = str(d.get("text") or "").strip()[:1000]
    if not text:
        return J({"ok": False, "error": "צריך לכתוב שאלה"})
    assistant = assistant_by_id(str(d.get("assistant") or ""))
    history = []
    for h in (d.get("history") or [])[-6:]:
        if isinstance(h, dict) and h.get("q") and h.get("a"):
            history.append({"role": "user", "parts": [{"text": str(h["q"])[:1000]}]})
            history.append({"role": "model", "parts": [{"text": str(h["a"])[:1500]}]})
    info = {}
    t0 = time.time()
    try:
        transcript, action, answer = ask_ai_text(assistant, history, text, info)
    except Exception as e:
        print("test chat error:", e)
        return J({"ok": False, "error": str(e)[:200], "seconds": round(time.time() - t0, 1)})
    out = {"ok": True, "answer": answer, "action": action, "search": info.get("search", ""),
           "seconds": round(time.time() - t0, 1), "assistant": assistant["id"]}
    if action == "warn":
        try:
            n = max(1, min(WARN_MAX, int(d.get("warn_n") or 1)))
        except (ValueError, TypeError):
            n = 1
        out["answer"] = clean_for_tts(T("warning", n=n))
        out["warn"] = n
    elif action.startswith("switch:"):
        nxt = assistant_by_id(action.split(":", 1)[1])
        out["switch_to"] = nxt["id"]
        out["switch_name"] = nxt["name"]
    print("test chat: %.1fs, action=%s, search=%s" % (time.time() - t0, action, info.get("search", "")))
    return J(out)


@app.route("/api/say", methods=["POST"])
def api_say():
    """הקראת טקסט בקול של הקו, לשמיעה באתר הניהול (בלי להעלות לימות)"""
    g = api_guard()
    if g:
        return g
    d = request.get_json(silent=True) or {}
    text = clean_for_tts(str(d.get("text") or ""))
    if not text:
        return J({"error": "אין טקסט"}, 400)
    vl = voice_list()
    voice = str(d.get("voice") or "")
    if voice not in vl:
        voice = vl[0]
    try:
        wav = call_with_deadline(lambda: make_tts(text, voice), 20)
    except Exception as e:
        return J({"error": str(e)[:200]}, 502)
    if not wav:
        return J({"error": "הקול הטבעי לא זמין בשרת"}, 502)
    return Response(wav, mimetype="audio/wav", headers={"Cache-Control": "no-store"})


@app.route("/api/test_transit")
def api_test_transit():
    """בדיקה ידנית של מאגר התחבורה מהדפדפן, למשל: /api/test_transit?q=402 | בני ברק | ירושלים | -"""
    g = api_guard()
    if g:
        return g
    q = request.args.get("q", "402 | בני ברק | ירושלים | -")
    t0 = time.time()
    try:
        r = transit_lookup(q)
        return J({"ok": bool(r), "seconds": round(time.time() - t0, 1), "result": r or "", "parsed": parse_transit(q)})
    except Exception as e:
        return J({"ok": False, "seconds": round(time.time() - t0, 1), "error": str(e)[:300]})


@app.route("/api/rec")
def api_rec():
    """השמעת הקלטה של מתקשר באתר הניהול (מורידים מימות וממירים לפורמט שכל דפדפן מנגן)"""
    g = api_guard()
    if g:
        return g
    f = request.args.get("f", "")
    if not re.match(r"^ai_[0-9A-Za-z_]+$", f):
        return J({"error": "bad name"}, 400)
    try:
        data = yemot_download(f + ".wav")
    except Exception as e:
        return J({"error": str(e)[:200]}, 502)
    if len(data) < 500 or data[:40].lstrip().startswith(b"{"):
        return J({"error": "ההקלטה כבר נמחקה"}, 404)
    if HAVE_TTS:
        try:
            ff = imageio_ffmpeg.get_ffmpeg_exe()
            p = subprocess.run([ff, "-loglevel", "error", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le", "-f", "wav", "pipe:1"],
                               input=data, capture_output=True, timeout=20)
            if p.returncode == 0 and len(p.stdout) > 500:
                data = p.stdout
        except Exception as e:
            print("rec convert error:", str(e)[:100])
    return Response(data, mimetype="audio/wav", headers={"Cache-Control": "private, max-age=3600"})


@app.route("/api/test_zmanim")
def api_test_zmanim():
    """בדיקה מהדפדפן: /api/test_zmanim?city=ירושלים&day=מחר"""
    g = api_guard()
    if g:
        return g
    try:
        return J({"ok": True, "result": zmanim_text("%s | %s" % (request.args.get("city", "-"), request.args.get("day", "היום")))})
    except Exception as e:
        return J({"ok": False, "error": str(e)[:300]})


@app.route("/api/test_tts", methods=["POST"])
def api_test_tts():
    """בדיקה שהקול הטבעי עובד (בלי להעלות לימות)"""
    g = api_guard()
    if g:
        return g
    d = request.get_json(silent=True) or {}
    try:
        wav = make_tts(str(d.get("text") or "שלום, זו בדיקה של הקול"), str(d.get("voice") or voice_list()[0]))
        return J({"ok": bool(wav), "bytes": len(wav or b"")})
    except Exception as e:
        return J({"ok": False, "error": str(e)[:300]})


@app.route("/admin")
def admin():
    """אתר הניהול מוגש מהקובץ admin.html שנמצא ליד app.py"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")
    try:
        with open(path, encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html; charset=utf-8")
    except Exception:
        return Response(ADMIN_HTML, mimetype="text/html; charset=utf-8")


# גיבוי בלבד: מוצג רק אם הקובץ admin.html חסר ב-GitHub
ADMIN_HTML = """<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><title>ניהול הקו</title></head>
<body style="background:#000;color:#f5f2ed;font-family:Arial;text-align:center;padding:80px 20px">
<h1 style="color:#ff7a1a">הקו עובד</h1>
<p>הקובץ admin.html לא נמצא בשרת. יש להעלות אותו ל-GitHub, באותה תיקייה של app.py.</p>
</body></html>"""


if __name__ == "__main__":
    app.run()
