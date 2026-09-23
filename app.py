# -*- coding: utf-8 -*-
"""
קו AI טלפוני בימות המשיח
- Render (חינם) + Gemini (חינם) + Edge TTS (חינם, קול טבעי)
- רישום שם לפי מספר טלפון, 7 עוזרים, חיפוש באינטרנט, החלפת קול, אתר ניהול חי
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
import asyncio
import smtplib
import threading
import datetime
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from email.mime.text import MIMEText
import importlib.resources  # noqa: F401  (טעינה מוקדמת - מונע תקלת ייבוא מקבילית ב-threads)
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
MODELS = ["gemini-3.6-flash-lite", "gemini-3.6-flash", "gemini-3.1-flash-lite", "gemini-3.1-flash",
          "gemini-3-flash-preview", "gemini-2.5-flash"]
MODEL_STATUS = {}     # שם מודל -> {"dead": True} (לא קיים) או {"until": זמן} (מכסה נגמרה, לנסות שוב אחר כך)


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
        print("models: could not list (%s), keeping defaults" % str(e)[:120])


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
}

GENERAL_RULES = (
    " אתה מדבר בטלפון, לכן ענה קצר וברור: משפט עד שלושה משפטים, אלא אם ביקשו במפורש משהו ארוך (סיפור, שיר, הסבר מפורט)."
    " בלי כוכביות, בלי רשימות, בלי אימוג'ים ובלי סימני עיצוב."
    " ענה בשפה שבה המשתמש דיבר אליך; ברירת המחדל היא עברית."
    " שמור על שפה מכובדת וצנועה, ואל תעסוק בנושאים לא צנועים."
    " יש לך כלי חיפוש באינטרנט. השתמש בו כשהמשתמש מבקש לחפש, או כשהתשובה דורשת מידע עדכני:"
    " מחירים, חנויות, חדשות, מזג אוויר, שעות פתיחה, תוצאות, מה קורה עכשיו. אחרי חיפוש תן תשובה מדויקת עם המספרים והשמות שמצאת."
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
     "prompt": "אתה מומחה למוזיקה חסידית וישראלית: זמרים, מלחינים, אלבומים, ניגונים, היסטוריה, וגם תיאוריה מוזיקלית - סולמות, אקורדים, "
               "מבנה שירים, מעברים. כששואלים על אקורדים או סולם של שיר, תן את הסולם ואת סדר האקורדים לפי חלקי השיר. "
               "אל תצטט מילים של שירים - אפשר לתאר על מה השיר ומי כתב והלחין."},
]

names = {}        # טלפון -> שם
voices = {}       # טלפון -> מספר קול מועדף
notes = {}        # טלפון -> הודעה אישית לשיחה הבאה
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
        raise Deadline("no answer from Gemini within %ds (timed out)" % seconds)
    if "e" in box:
        raise box["e"]
    return box["r"]




def il_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)


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


def yemot_upload_file(file_name, data, mime="audio/wav"):
    """העלאת קובץ קול לשלוחה (multipart)"""
    boundary = "----yemot" + uuid.uuid4().hex
    fields = {"token": YEMOT_TOKEN, "path": yemot_path(file_name), "convertAudio": "1"}
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
        data = json.dumps({"names": names, "voices": voices, "notes": notes}, ensure_ascii=False)
    _bg(yemot_write_text, "ai_names.txt", data)


def save_log():
    with _lock:
        lines = [json.dumps({"t": "log", **l}, ensure_ascii=False) for l in LOG[-LOG_MAX:]]
        lines += [json.dumps({"t": "call", **c}, ensure_ascii=False) for c in CALLS[-LOG_MAX:]]
    _bg(yemot_write_text, "ai_log.txt", "\n".join(lines))


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
threading.Thread(target=discover_models, daemon=True).start()


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

    mp3 = asyncio.run(gen())
    if not mp3:
        return None
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    p = subprocess.run([ff, "-loglevel", "error", "-i", "pipe:0", "-ar", "8000", "-ac", "1",
                        "-acodec", "pcm_s16le", "-f", "wav", "pipe:1"],
                       input=mp3, capture_output=True, timeout=40)
    if p.returncode != 0 or len(p.stdout) < 100:
        raise RuntimeError("ffmpeg failed: " + p.stderr.decode("utf-8", "ignore")[:200])
    return p.stdout


def speak_file(text, voice_idx, call_id):
    """מייצר קול טבעי ומעלה לימות. מחזיר שם קובץ (בלי סיומת) או None אם לא הצליח"""
    if SETTINGS.get("tts", "on") != "on" or not text:
        return None
    try:
        vl = voice_list()
        t0 = time.time()
        wav = call_with_deadline(lambda: make_tts(text, vl[voice_idx % len(vl)]), 12)
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
def gemini(system, contents, search=False):
    last_error = None
    variants = []
    order = list(MODELS)
    pref = SETTINGS.get("model", "")
    if pref:
        order = [pref] + [m for m in order if m != pref]
    for model in order:
        if not model_ok(model):
            continue
        for think in ("level", "budget", None):
            variants.append((model, search, think))
    skip_model = None
    for model, use_search, think in variants:
        if model == skip_model:
            continue
        no_think = think
        try:
            kw = dict(system_instruction=system, max_output_tokens=400)
            if use_search:
                kw["tools"] = [types.Tool(google_search=types.GoogleSearch())]
            if think == "level":
                kw["thinking_config"] = types.ThinkingConfig(thinking_level="minimal")
            elif think == "budget":
                kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            t0 = time.time()
            print("gemini: calling %s (search=%s think=%s)" % (model, use_search, think))
            response = call_with_deadline(
                lambda: get_client().models.generate_content(
                    model=model, contents=contents, config=types.GenerateContentConfig(**kw)),
                GEMINI_DEADLINE)
            print("gemini: %s answered in %.1fs" % (model, time.time() - t0))
            if response.text:
                return response.text
        except Exception as e:
            last_error = e
            msg = str(e)
            print("gemini variant failed (%s search=%s nothink=%s) after %.1fs: %s" % (
                model, use_search, no_think, time.time() - t0, msg[:120]))
            if "NOT_FOUND" in msg or "no longer available" in msg or "not found" in msg:
                MODEL_STATUS[model] = {"dead": True}
                skip_model = model
            elif "RESOURCE_EXHAUSTED" in msg or "429" in msg[:20]:
                if not use_search:
                    MODEL_STATUS[model] = {"until": time.time() + 120}
                skip_model = model
            elif "timed out" in msg.lower():
                skip_model = model
            continue
    print("Gemini error:", last_error)
    return None


def transcribe_name(file_name):
    try:
        audio = yemot_download(file_name + ".wav")
    except Exception as e:
        print("download error:", e)
        return ""
    _bg(yemot_delete, file_name + ".wav")
    text = gemini("בהקלטה טלפונית באיכות נמוכה אדם אומר את שמו הפרטי בעברית (שם ישראלי או יהודי נפוץ). "
                  "החזר רק את השם הפרטי, מילה אחת או שתיים, בלי שום תוספת.",
                  [types.Part.from_bytes(data=audio, mime_type="audio/wav")])
    return clean_for_tts(text or "")[:30]


ACTION_RE = re.compile(r"תמלול\s*:\s*(.*?)\s*\n\s*פעולה\s*:\s*(.*?)\s*\n\s*חיפוש\s*:\s*(.*?)\s*\n\s*תשובה\s*:\s*(.*)", re.S)


def ask_ai(assistant, history, file_name):
    """מחזיר (תמלול, פעולה, תשובה). פעולה: none / menu / end / voice / switch:id"""
    t0 = time.time()
    try:
        audio = yemot_download(file_name + ".wav")
    except Exception as e:
        print("download error:", e)
        return "", "none", "סליחה, לא הצלחתי לשמוע את ההקלטה. נסה שוב."
    print("timing: download %.1fs" % (time.time() - t0))
    _bg(yemot_delete, file_name + ".wav")

    others = "; ".join("%s = %s (מילים: %s)" % (a["id"], a["name"], a.get("keywords", "")) for _, a in active_assistants() if a["id"] != assistant["id"])
    system = assistant["prompt"] + GENERAL_RULES + (
        " תקבל הקלטה של מה שהמשתמש אמר עכשיו. ההקלטה היא משיחת טלפון באיכות נמוכה (8 קילוהרץ), בעברית מדוברת,"
        " לפעמים עם רעשי רקע. הקשב בתשומת לב מלאה, והשתמש בהקשר של השיחה ובתחום של העוזר כדי להשלים מילים לא ברורות"
        " (שמות של זמרים, מלחינים, מקומות, מונחים). אם משהו באמת לא ברור, שאל בקצרה במקום לנחש."
        " ענה בדיוק בפורמט הבא, ארבע שורות:\n"
        "תמלול: <תמלול מדויק של ההקלטה>\n"
        "פעולה: <אחת מהאפשרויות: none | menu | end | voice | switch:מזהה>\n"
        "חיפוש: <כן אם התשובה דורשת חיפוש באינטרנט (המשתמש ביקש לחפש, או מידע עדכני: מחירים, חדשות, מזג אוויר, שעות פתיחה, תוצאות), אחרת לא>\n"
        "תשובה: <התשובה שלך למשתמש. אם חיפוש = כן, כתוב כאן רק: מחפש>\n"
        "כללי הפעולה: menu אם ביקש לחזור לתפריט. end אם ביקש לסיים או להתנתק או אמר להתראות. "
        "voice אם ביקש להחליף קול. switch:מזהה אם ביקש לעבור לעוזר אחר מהרשימה: " + others + ". "
        "אחרת none. כשהפעולה אינה none, כתוב בתשובה משפט קצר מתאים (למשל: בטח, מעביר אותך)."
    )
    contents = list(history) + [{
        "role": "user",
        "parts": [{"text": "ההקלטה של המשתמש:"}, types.Part.from_bytes(data=audio, mime_type="audio/wav")],
    }]
    t0 = time.time()
    raw = gemini(system, contents)
    print("timing: gemini(audio) %.1fs" % (time.time() - t0))
    if not raw:
        return "", "none", T("error")
    m = ACTION_RE.search(raw)
    need_search = False
    if m:
        transcript, action, need_search, answer = m.group(1).strip(), m.group(2).strip().lower(), "כן" in m.group(3), m.group(4).strip()
    else:
        transcript, action = "", "none"
        answer = re.sub(r"^(תמלול|פעולה|חיפוש|תשובה)\s*:\s*", "", raw.strip())
    if need_search and transcript and action == "none":
        # שלב שני, רק כשבאמת צריך: חיפוש באינטרנט לפי התמלול (טקסט בלבד, מהיר יותר מאודיו)
        t0 = time.time()
        sys2 = assistant["prompt"] + GENERAL_RULES + " חפש באינטרנט וענה תשובה מדויקת עם המספרים והשמות שמצאת. תשובה קצרה, מתאימה להקראה בטלפון."
        contents2 = list(history) + [{"role": "user", "parts": [{"text": transcript}]}]
        found = gemini(sys2, contents2, search=True)
        if not found:
            # החיפוש נכשל - עונים מהידע, בלי להשאיר את המתקשר עם "מחפש"
            sys3 = assistant["prompt"] + GENERAL_RULES + " החיפוש באינטרנט לא זמין כרגע. ענה כמיטב ידיעתך, וציין בקצרה שלא הצלחת לבדוק באינטרנט."
            found = gemini(sys3, contents2)
        print("timing: gemini(search) %.1fs" % (time.time() - t0))
        if found:
            answer = re.sub(r"^(תמלול|פעולה|חיפוש|תשובה)\s*:\s*", "", found.strip())
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
    if not answer:
        answer = T("not_understood")
    return transcript, action, clean_for_tts(answer)


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
    return build_read([("text", phrases[i % len(phrases)])], mode="tap", val_name="w_%d" % state["n"],
                      max_digits=1, min_digits=1, sec_wait=2, amount_attempts=1, allow_empty="Ok", empty_val="None")


def goodbye(call_id, name, state=None):
    part = msg_part(state, T("goodbye", name=name)) if state else ("text", T("goodbye", name=name))
    with _lock:
        calls.pop(call_id, None)
    return build_combined_action([build_id_list_message([part]), build_go_to_folder("hangup")])


def ai_worker(pending, state, assistant, history, file_name, call_id, voice_idx):
    try:
        transcript, action, answer = ask_ai(assistant, history, file_name)
        tts = None
        if action in ("none", "voice") or action.startswith("switch"):
            v = voice_idx + 1 if action == "voice" else voice_idx
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
        name = transcribe_name(state["file"]) or "אורח"
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
            pending["event"].wait(11)

        if not pending["done"]:
            if time.time() - pending["started"] > 75:
                state["pending"] = None
                return R(listen(state, T("too_long")))
            if state["wait_i"] > 0:
                pending["event"].wait(6)
            if not pending["done"]:
                return R(wait_message(state))

        state["pending"] = None
        transcript, action, answer, tts = pending["result"]
        if tts:
            state["tts_file"] = tts
            state["played"].append(tts)

        if transcript:
            state["last_q"] = transcript
            state["history"].append({"role": "user", "parts": [{"text": transcript}]})
            state["history"].append({"role": "model", "parts": [{"text": answer}]})
            state["history"] = state["history"][-12:]
            with _lock:
                LOG.append({"time": now_str(), "phone": phone, "name": name, "call": call_id,
                            "persona": assistant["name"], "q": transcript, "a": answer})
                del LOG[:-LOG_MAX]
            save_log()

        if action == "menu":
            state["tts_file"] = None
            return R(menu(state, name))
        if action == "end":
            state["tts_file"] = None
            return R(goodbye(call_id, name, state))
        if action == "voice":
            state["voice"] = (state["voice"] + 1) % max(1, len(voice_list()))
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
                    "since": datetime.datetime.utcfromtimestamp(st.get("started", 0) + 3 * 3600).strftime("%H:%M"),
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
                      "voice": voices.get(ph, 0),
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
        "models": [{"name": m, "ok": model_ok(m), "dead": bool(MODEL_STATUS.get(m, {}).get("dead"))}
                   for m in ([SETTINGS["model"]] if SETTINGS.get("model") else []) + [x for x in MODELS if x != SETTINGS.get("model")]],
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
        out.append({"id": aid, "name": nm, "prompt": pr, "on": bool(a.get("on", True)), "keywords": str(a.get("keywords", ""))[:200]})
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
    """בדיקת מערכת מלאה בלחיצה אחת: פייתון, Gemini, קול, ימות - עם זמנים ושגיאות מדויקות"""
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
    out["model_status"] = {m: ("לא קיים" if MODEL_STATUS.get(m, {}).get("dead") else ("מכסה" if not model_ok(m) else "ok")) for m in MODELS}
    return J(out)


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
    """אתר הניהול. אם יש קובץ admin.html ליד app.py - הוא זה שמוגש (כך אפשר לשדרג את האתר בלי לגעת בקוד של הקו)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")
    try:
        with open(path, encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html; charset=utf-8")
    except Exception:
        return Response(ADMIN_HTML, mimetype="text/html; charset=utf-8")


ADMIN_HTML = r"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ניהול הקו</title><style>
:root{--bg:#f3f5f9;--card:#fff;--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;--brand:#1e3a5f;--brand2:#2563eb;--ok:#16a34a;--bad:#dc2626;--warn:#d97706}
*{box-sizing:border-box}body{margin:0;font-family:Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink);font-size:15px}
.app{display:flex;min-height:100vh}.side{width:220px;background:var(--brand);color:#fff;padding:18px 0;position:sticky;top:0;height:100vh;flex-shrink:0}
.side h1{font-size:18px;margin:0 18px 18px}.nav{display:flex;flex-direction:column}.nav a{color:#cbd5e1;text-decoration:none;padding:11px 18px;border-right:3px solid transparent;cursor:pointer}
.nav a.on,.nav a:hover{color:#fff;background:#ffffff14;border-right-color:#60a5fa}.main{flex:1;padding:22px 26px;min-width:0}
h2{margin:0 0 14px;font-size:22px}h3{margin:18px 0 8px;font-size:16px;color:var(--brand)}.sub{color:var(--muted);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border-radius:12px;padding:14px 16px;box-shadow:0 1px 2px #0000000d}.card b{display:block;font-size:28px;margin-top:4px}
.panel{background:var(--card);border-radius:12px;padding:16px;box-shadow:0 1px 2px #0000000d;margin-bottom:16px}
table{width:100%;border-collapse:collapse}th,td{padding:8px 9px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top;font-size:14px}th{color:var(--muted);font-weight:600;font-size:13px}
tr:hover td{background:#f9fafb}input[type=text],input[type=number],input[type=password],select,textarea{padding:7px 9px;border:1px solid #cfd4dc;border-radius:8px;font:inherit;width:100%}
textarea{min-height:64px;resize:vertical}.btn{padding:7px 13px;border:0;border-radius:8px;background:var(--brand2);color:#fff;cursor:pointer;font:inherit}
.btn.sm{padding:4px 9px;font-size:13px}.btn.gray{background:#6b7280}.btn.red{background:var(--bad)}.btn.green{background:var(--ok)}.btn.line{background:#fff;color:var(--brand2);border:1px solid var(--brand2)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:#eef2ff;color:#3730a3;margin-left:4px}.tag.red{background:#fee2e2;color:#991b1b}.tag.green{background:#dcfce7;color:#166534}.tag.gold{background:#fef3c7;color:#92400e}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--ok);margin-left:6px;animation:b 1.2s infinite}@keyframes b{50%{opacity:.25}}
.page{display:none}.page.on{display:block}.q{color:#1d4ed8}.a{color:#374151}.bubble{max-width:80%;padding:8px 12px;border-radius:14px;margin:4px 0;white-space:pre-wrap}
.bubble.u{background:#dbeafe;margin-left:auto}.bubble.m{background:#f1f5f9}.chat{display:flex;flex-direction:column}
.toast{position:fixed;bottom:20px;left:20px;background:#111827;color:#fff;padding:10px 16px;border-radius:10px;opacity:0;transition:.3s;pointer-events:none}.toast.on{opacity:1}
.field{margin-bottom:10px}.field label{display:block;font-size:13px;color:var(--muted);margin-bottom:3px}.drag{cursor:grab;color:#9ca3af}
.login{max-width:360px;margin:100px auto}svg text{font-family:inherit}.muted{color:var(--muted)}.pill{cursor:pointer}
@media(max-width:800px){.side{display:none}.grid2,.grid3{grid-template-columns:1fr}}
</style></head><body>
<div id="loginBox" class="login" style="display:none"><div class="panel"><h2>כניסה לניהול הקו</h2><div class="field"><input type="password" id="pw" placeholder="סיסמה"></div><button class="btn" style="width:100%" onclick="login()">כניסה</button><p id="loginErr" style="color:var(--bad)"></p></div></div>
<div class="app" id="app" style="display:none">
<div class="side"><h1>ניהול הקו</h1><div class="nav">
<a data-p="dash" class="on">לוח בקרה</a><a data-p="users">משתמשים</a><a data-p="conv">שיחות</a><a data-p="assist">עוזרים</a><a data-p="texts">נוסחים</a><a data-p="settings">הגדרות</a><a href="/admin/logout">יציאה</a></div>
<p class="sub" style="margin:18px;color:#94a3b8" id="clock"></p></div>
<div class="main">

<div class="page on" id="p-dash"><h2><span class="dot"></span>לוח בקרה <span class="sub">מתעדכן כל 5 שניות</span></h2>
<div class="cards"><div class="card">שיחות פעילות עכשיו<b id="l_active">0</b></div><div class="card">שיחות היום<b id="l_calls">0</b></div><div class="card">הודעות היום<b id="l_msgs">0</b></div><div class="card">משתמשים רשומים<b id="l_users">0</b></div><div class="card">סה"כ שיחות<b id="l_ct">0</b></div><div class="card">סה"כ הודעות<b id="l_mt">0</b></div></div>
<div class="panel"><h3 style="margin-top:0">עכשיו בקו</h3><table id="activeT"></table></div>
<div class="grid3" id="charts"></div></div>

<div class="page" id="p-users"><h2>משתמשים</h2>
<div class="panel"><div class="row"><input type="text" id="uSearch" placeholder="חיפוש לפי שם או טלפון" style="max-width:280px" oninput="renderUsers()">
<span class="muted">|</span><input type="text" id="addPhone" placeholder="טלפון" style="max-width:150px"><input type="text" id="addName" placeholder="שם" style="max-width:150px"><button class="btn sm" onclick="addUser()">הוסף משתמש ידנית</button></div></div>
<div class="panel"><table id="usersT"></table></div></div>

<div class="page" id="p-conv"><h2>שיחות</h2>
<div class="panel"><div class="row"><input type="text" id="cSearch" placeholder="חיפוש בתוכן השיחות" style="max-width:300px" oninput="renderConv()"><select id="cUser" style="max-width:220px" onchange="renderConv()"><option value="">כל המשתמשים</option></select>
<button class="btn sm gray" onclick="clearLog()">נקה יומן</button></div></div>
<div id="convList"></div></div>

<div class="page" id="p-assist"><h2>עוזרים</h2><p class="sub">הסדר כאן = מספר ההקשה בתפריט (1 עד 8). עוזר כבוי לא מופיע בתפריט. "מילים" = איך המתקשר קורא לעוזר כשהוא מבקש לעבור אליו בדיבור.</p>
<div id="assistList"></div><div class="row" style="margin:12px 0"><button class="btn line" onclick="addAssistant()">+ עוזר חדש</button><button class="btn" onclick="saveAssistants()">שמור עוזרים</button></div></div>

<div class="page" id="p-texts"><h2>נוסחים</h2><p class="sub">כל מה שהקו אומר. אפשר להשתמש ב-{name} לשם המתקשר, ב-{assistant} לשם העוזר, וב-{digit} למספר ההקשה.</p>
<div class="panel" id="textsList"></div><button class="btn" onclick="saveTexts()">שמור נוסחים</button></div>

<div class="page" id="p-settings"><h2>הגדרות</h2><div class="panel" id="settingsBox"></div><button class="btn" onclick="saveSettings()">שמור הגדרות</button>
<div class="panel" style="margin-top:16px"><h3 style="margin-top:0">סיכום יומי למייל</h3><p id="mailState"></p><button class="btn line" onclick="sendMail()">שלח סיכום של היום עכשיו</button> <span id="mailRes"></span></div>
<div class="panel"><h3 style="margin-top:0">בדיקת קול טבעי</h3><div class="row"><select id="ttsVoice" style="max-width:300px"></select><button class="btn line" onclick="testTts()">בדוק</button><span id="ttsRes"></span></div><p class="sub">בודק שהשרת מצליח לייצר קול (בלי להעלות לימות).</p></div></div>

</div></div><div class="toast" id="toast"></div>
<script>
let S=null;const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(m){const t=$('toast');t.textContent=m;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),2200);}
async function api(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});if(r.status===403){showLogin();throw new Error('auth');}return r.json();}
function showLogin(){$('app').style.display='none';$('loginBox').style.display='block';}
async function login(){const d=await api('/admin/login',{key:$('pw').value});if(d.ok){$('loginBox').style.display='none';boot();}else $('loginErr').textContent=d.error||'שגיאה';}
$('pw')?.addEventListener('keydown',e=>{if(e.key==='Enter')login();});
document.querySelectorAll('.nav a[data-p]').forEach(a=>a.onclick=()=>{document.querySelectorAll('.nav a').forEach(x=>x.classList.remove('on'));a.classList.add('on');document.querySelectorAll('.page').forEach(p=>p.classList.remove('on'));$('p-'+a.dataset.p).classList.add('on');});
async function boot(){try{S=await api('/api/state');}catch(e){return;}$('app').style.display='flex';renderAll();tick();}
function renderAll(){renderLive(S.live);renderCharts();renderUsers();renderConv();renderAssist();renderTexts();renderSettings();}
async function tick(){try{const d=await api('/api/live');renderLive(d);}catch(e){}setTimeout(tick,5000);}
function renderLive(d){$('l_active').textContent=d.active.length;$('l_calls').textContent=d.calls_today;$('l_msgs').textContent=d.msgs_today;$('l_users').textContent=d.users;$('l_ct').textContent=d.calls_total;$('l_mt').textContent=d.msgs_total;$('clock').textContent=d.time;
let t='<tr><th>מי</th><th>טלפון</th><th>עוזר</th><th>מצב</th><th>מאז</th><th>שאלה אחרונה</th></tr>';if(!d.active.length)t+='<tr><td colspan="6" class="muted">אין שיחות פעילות כרגע</td></tr>';
for(const a of d.active)t+=`<tr><td>${esc(a.name)}</td><td>${esc(a.phone)}</td><td>${esc(a.assistant)}</td><td>${esc(a.state)}</td><td>${esc(a.since)}</td><td>${esc(a.last_q)}</td></tr>`;$('activeT').innerHTML=t;}
function bar(title,pairs,color){if(!pairs.length)return `<div class="panel"><b>${esc(title)}</b><p class="muted">אין נתונים עדיין</p></div>`;const mx=Math.max(...pairs.map(p=>p[1]))||1;const w=Math.max(300,pairs.length*32+30);let s=`<div class="panel"><b>${esc(title)}</b><svg viewBox="0 0 ${w} 180" width="100%">`;
pairs.forEach((p,i)=>{const x=15+i*32,h=Math.round(130*p[1]/mx);s+=`<rect x="${x}" y="${150-h}" width="24" height="${h}" rx="4" fill="${color}"/><text x="${x+12}" y="${145-h}" font-size="11" text-anchor="middle">${p[1]}</text><text x="${x+12}" y="170" font-size="9" text-anchor="middle">${esc(String(p[0])).slice(0,10)}</text>`;});return s+'</svg></div>';}
function renderCharts(){const c=S.charts;$('charts').innerHTML=bar('שיחות ב-14 הימים האחרונים',c.per_day,'#1e3a5f')+bar('הודעות לפי עוזר',c.per_assistant,'#2563eb')+bar('המשתמשים הפעילים',c.top_users,'#16a34a');}
function renderUsers(){const q=($('uSearch').value||'').trim();let t='<tr><th>שם</th><th>טלפון</th><th>שיחות</th><th>הודעות</th><th>היום</th><th>שיחה אחרונה</th><th>סטטוס</th><th>הודעה לשיחה הבאה</th><th></th></tr>';
const list=S.users.filter(u=>!q||u.name.includes(q)||u.phone.includes(q)).sort((a,b)=>b.last.localeCompare(a.last));
for(const u of list){t+=`<tr><td><input type="text" value="${esc(u.name)}" style="width:120px" onchange="userAct('${u.phone}','rename',{name:this.value})"></td><td>${esc(u.phone)}${u.owner?' <span class="tag gold">בעלים</span>':''}</td><td>${u.calls}</td><td>${u.msgs}</td><td>${u.today}</td><td>${esc(u.last)}</td>
<td>${u.blocked?'<span class="tag red">חסום</span>':''}${u.unlimited?'<span class="tag green">בלי הגבלה</span>':''}</td>
<td><input type="text" value="${esc(u.note)}" placeholder="יושמע לו פעם אחת" style="width:180px" onchange="userAct('${u.phone}','note',{note:this.value})"></td>
<td class="row"><button class="btn sm gray" onclick="showUser('${u.phone}')">שיחות</button><button class="btn sm ${u.blocked?'green':'red'}" onclick="userAct('${u.phone}','block')">${u.blocked?'בטל חסימה':'חסום'}</button>${u.owner?'':`<button class="btn sm line" onclick="userAct('${u.phone}','unlimited')">${u.unlimited?'הפעל הגבלה':'בלי הגבלה'}</button><button class="btn sm gray" onclick="if(confirm('למחוק? בשיחה הבאה יירשם מחדש'))userAct('${u.phone}','delete')">מחק</button>`}</td></tr>`;}
if(!list.length)t+='<tr><td colspan="9" class="muted">אין משתמשים</td></tr>';$('usersT').innerHTML=t;
const sel=$('cUser');const cur=sel.value;sel.innerHTML='<option value="">כל המשתמשים</option>'+S.users.map(u=>`<option value="${u.phone}">${esc(u.name)} (${u.phone})</option>`).join('');sel.value=cur;}
async function userAct(phone,action,extra){await api('/api/user',{phone,action,...extra});await reload();toast('נשמר');}
async function addUser(){const p=$('addPhone').value.trim(),n=$('addName').value.trim();if(!p||!n)return toast('צריך טלפון ושם');await api('/api/user',{phone:p,name:n,action:'add'});$('addPhone').value='';$('addName').value='';await reload();toast('נוסף');}
function showUser(phone){$('cUser').value=phone;document.querySelector('.nav a[data-p=conv]').click();renderConv();}
function renderConv(){const q=($('cSearch').value||'').trim(),ph=$('cUser').value;const groups={};const order=[];
for(const l of S.log){if(ph&&l.phone!==ph)continue;if(q&&!(l.q.includes(q)||l.a.includes(q)||(l.name||'').includes(q)))continue;const k=l.call||(l.phone+'|'+l.time.slice(0,10));if(!groups[k]){groups[k]=[];order.push(k);}groups[k].push(l);}
let s='';for(const k of order){const msgs=groups[k].slice().reverse();const f=msgs[0];s+=`<div class="panel"><div class="row" style="justify-content:space-between"><b>${esc(f.name)} <span class="muted">${esc(f.phone)}</span></b><span class="muted">${esc(f.time)} · ${msgs.length} הודעות</span></div><div class="chat">`;
let last='';for(const m of msgs){if(m.persona!==last){s+=`<div class="muted" style="font-size:12px;margin:6px 0 2px">— ${esc(m.persona)} —</div>`;last=m.persona;}s+=`<div class="bubble u">${esc(m.q)}</div><div class="bubble m">${esc(m.a)}</div>`;}s+='</div></div>';}
$('convList').innerHTML=s||'<div class="panel muted">אין שיחות</div>';}
async function clearLog(){if(!confirm('למחוק את כל יומן השיחות?'))return;await api('/api/clear',{what:'log'});await reload();toast('היומן נוקה');}
let A=[];function renderAssist(){A=JSON.parse(JSON.stringify(S.assistants));drawAssist();}
function drawAssist(){let s='';A.forEach((a,i)=>{const digit=A.slice(0,i+1).filter(x=>x.on).length;s+=`<div class="panel"><div class="row" style="justify-content:space-between"><div class="row"><b>${a.on?'הקשה '+digit:'כבוי'}</b><span class="tag">${esc(a.id)}</span></div>
<div class="row"><button class="btn sm gray" onclick="mv(${i},-1)">▲</button><button class="btn sm gray" onclick="mv(${i},1)">▼</button><label><input type="checkbox" ${a.on?'checked':''} onchange="A[${i}].on=this.checked;drawAssist()"> פעיל</label><button class="btn sm red" onclick="if(confirm('למחוק את העוזר?')){A.splice(${i},1);drawAssist();}">מחק</button></div></div>
<div class="grid2" style="margin-top:8px"><div class="field"><label>שם העוזר (כפי שנשמע בתפריט)</label><input type="text" value="${esc(a.name)}" onchange="A[${i}].name=this.value"></div><div class="field"><label>מילים לזיהוי בדיבור (מופרדות בפסיק)</label><input type="text" value="${esc(a.keywords||'')}" onchange="A[${i}].keywords=this.value"></div></div>
<div class="field"><label>ההנחיה ל-AI (האופי, התחום, איך לענות)</label><textarea onchange="A[${i}].prompt=this.value">${esc(a.prompt)}</textarea></div></div>`;});$('assistList').innerHTML=s;}
function mv(i,d){const j=i+d;if(j<0||j>=A.length)return;[A[i],A[j]]=[A[j],A[i]];drawAssist();}
function addAssistant(){A.push({id:'a'+Math.random().toString(36).slice(2,8),name:'עוזר חדש',prompt:'אתה עוזר ידידותי.',on:true,keywords:''});drawAssist();window.scrollTo(0,document.body.scrollHeight);}
async function saveAssistants(){const d=await api('/api/assistants',{assistants:A});if(d.ok){await reload();toast('העוזרים נשמרו');}else toast(d.error||'שגיאה');}
const TXT_LABELS={first_time:'פעם ראשונה - בקשת שם',ask_name_again:'בקשת שם חוזרת',name_saved:'אחרי שמירת השם',menu_hello:'פתיחת התפריט',menu_item:'שורה בתפריט (לכל עוזר)',menu_end:'סיום התפריט',enter:'כניסה לעוזר',listening:'הקשבה (כשאין תשובה להשמיע)',not_heard:'לא נקלטה הקלטה',wait:'הודעות המתנה (מופרדות בפסיק, מתחלפות)',too_long:'התשובה לוקחת יותר מדי זמן',limit:'הגעה למכסה היומית',blocked:'מספר חסום',voice_changed:'הקול הוחלף (כשאין קול טבעי)',goodbye:'פרידה',error:'תקלה זמנית',not_understood:'לא הובן'};
function renderTexts(){let s='';for(const k in TXT_LABELS)s+=`<div class="field"><label>${esc(TXT_LABELS[k])}</label><input type="text" id="t_${k}" value="${esc(S.texts[k]||'')}"></div>`;$('textsList').innerHTML=s;}
async function saveTexts(){const d={};for(const k in TXT_LABELS)d[k]=$('t_'+k).value;await api('/api/texts',d);await reload();toast('הנוסחים נשמרו');}
function renderSettings(){const s=S.settings;$('settingsBox').innerHTML=`<div class="field"><label>הודעה בתחילת כל שיחה (ריק = בלי)</label><input type="text" id="s_announcement" value="${esc(s.announcement)}"></div>
<div class="grid2"><div class="field"><label>קול טבעי</label><select id="s_tts"><option value="on" ${s.tts=='on'?'selected':''}>פעיל (Edge, קול גבר טבעי)</option><option value="off" ${s.tts!='on'?'selected':''}>כבוי - הקראה של ימות (מהיר יותר)</option></select></div>
<div class="field"><label>רשימת קולות (מופרדים בפסיק, הראשון ברירת מחדל)</label><input type="text" id="s_voices" value="${esc(s.voices)}"></div>
<div class="field"><label>הודעות ליום לכל משתמש (0 = בלי הגבלה)</label><input type="number" id="s_daily_limit" value="${s.daily_limit}"></div><div class="field"><label>אורך הקלטה מקסימלי (שניות)</label><input type="number" id="s_record_max" value="${s.record_max}"></div>
<div class="field"><label>מספרים ללא הגבלה (מופרדים בפסיק)</label><input type="text" id="s_unlimited_phones" value="${esc(s.unlimited_phones)}"></div><div class="field"><label>מספרים חסומים</label><input type="text" id="s_blocked_phones" value="${esc(s.blocked_phones)}"></div>
<div class="field"><label>שעת הסיכום היומי למייל (0-23)</label><input type="number" id="s_mail_hour" value="${s.mail_hour}"></div></div>`;
$('mailState').innerHTML=S.mail?`<span style="color:var(--ok)">מוגדר, נשלח אל ${esc(S.mail_to)}</span>`:'<span style="color:var(--bad)">לא מוגדר - צריך MAIL_USER ו-MAIL_PASS ב-Render</span>';
$('ttsVoice').innerHTML=S.voices.map(v=>`<option>${esc(v)}</option>`).join('');}
async function saveSettings(){const d={};for(const k of ['announcement','tts','voices','daily_limit','record_max','unlimited_phones','blocked_phones','mail_hour'])d[k]=$('s_'+k).value;await api('/api/settings',d);await reload();toast('ההגדרות נשמרו');}
async function sendMail(){$('mailRes').textContent='שולח...';const d=await api('/api/sendmail',{});$('mailRes').textContent=d.result;}
async function testTts(){$('ttsRes').textContent='בודק...';const d=await api('/api/test_tts',{voice:$('ttsVoice').value});$('ttsRes').textContent=d.ok?'עובד ('+d.bytes+' בייט)':'נכשל: '+(d.error||'');}
async function reload(){S=await api('/api/state');renderAll();}
boot();
</script></body></html>"""


if __name__ == "__main__":
    app.run()
