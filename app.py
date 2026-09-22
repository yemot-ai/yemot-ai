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

# המהיר ראשון (מכסה חינמית גדולה יותר); הבאים הם גיבוי
MODELS = ["gemini-3.1-flash-lite", "gemini-3-flash", "gemini-2.5-flash-lite", "gemini-2.5-flash"]

# קולות גבר טבעיים (Edge TTS, חינם). הראשון הוא ברירת המחדל.
DEFAULT_VOICES = "he-IL-AvriNeural,en-US-AndrewMultilingualNeural,en-US-BrianMultilingualNeural,de-DE-FlorianMultilingualNeural"

SETTINGS = {
    "daily_limit": int(os.environ.get("DAILY_LIMIT", "40")),
    "unlimited_phones": ",".join(OWNER_PHONES),
    "blocked_phones": "",
    "disabled": "",                 # עוזרים כבויים, למשל "3,6"
    "announcement": "",             # הודעה שמושמעת בתחילת כל שיחה
    "mail_hour": 21,
    "tts": "on",                    # קול טבעי: on / off
    "voices": DEFAULT_VOICES,
    "record_max": 25,               # שניות הקלטה מקסימום
}

GENERAL_RULES = (
    " אתה מדבר בטלפון, לכן ענה קצר וברור, בלי כוכביות, בלי רשימות, בלי אימוג'ים ובלי סימני עיצוב."
    " ענה בשפה שבה המשתמש דיבר אליך; ברירת המחדל היא עברית."
    " שמור על שפה מכובדת וצנועה, ואל תעסוק בנושאים לא צנועים."
    " יש לך כלי חיפוש באינטרנט. השתמש בו כשהמשתמש מבקש לחפש, או כשהתשובה דורשת מידע עדכני:"
    " מחירים, חנויות, חדשות, מזג אוויר, שעות פתיחה, תוצאות, מה קורה עכשיו. אחרי חיפוש תן תשובה מדויקת עם המספרים והשמות שמצאת."
)

PERSONAS = {
    "1": "אתה עוזר כללי ידידותי ומועיל.",
    "2": "אתה עוזר תורני. ענה בסגנון תורני מכובד, וציין מקורות כשאפשר.",
    "3": "אתה עוזר חוצפני וסרקסטי עם הומור. ענה בחוצפה משעשעת אבל בלי להעליב באמת.",
    "4": "אתה עוזר יצירתי. ספר סיפורים קצרים, כתוב שירים, בדיחות ורעיונות יצירתיים.",
    "5": "אתה עוזר טכני. הסבר דברים טכניים בפשטות: מחשבים, אינטרנט, טלפונים.",
    "6": "אתה מדבר כמו ערס ישראלי מגניב: סלנג רחוב (אחי, וואלה, סבבה, יא מלך, בקטנה), ביטחון עצמי, חוצפה וקטע של מגניבות. "
         "עונה לעניין אבל בסטייל. בלי קללות ובלי להעליב באמת.",
    "7": "אתה מומחה למוזיקה חסידית וישראלית: זמרים, מלחינים, אלבומים, ניגונים, היסטוריה, וגם תיאוריה מוזיקלית - סולמות, אקורדים, "
         "מבנה שירים, מעברים. כששואלים על אקורדים או סולם של שיר, תן את הסולם ואת סדר האקורדים לפי חלקי השיר. "
         "אל תצטט מילים של שירים - אפשר לתאר על מה השיר ומי כתב והלחין.",
}
PERSONA_NAMES = {"1": "העוזר הכללי", "2": "העוזר התורני", "3": "העוזר החוצפן", "4": "העוזר היצירתי",
                 "5": "העוזר הטכני", "6": "הערס", "7": "המומחה למוזיקה"}
# מילים שלפיהן מזהים בקשה לעבור לעוזר אחר
PERSONA_KEYWORDS = {"1": ["כללי"], "2": ["תורני", "רב"], "3": ["חוצפן", "חוצפני"], "4": ["יצירתי"],
                    "5": ["טכני"], "6": ["ערס"], "7": ["מוזיקה", "מוזיקלי", "מוסיקה"]}

names = {}        # טלפון -> שם
voices = {}       # טלפון -> מספר קול מועדף
LOG = []          # מה נאמר
CALLS = []        # שיחות
calls = {}        # מצב של שיחות פעילות (לפי ApiCallId)
LOG_MAX = 1500
_lock = threading.Lock()
WAIT_PHRASES = ["רק רגע", "עוד רגע", "רק שניה", "כבר עונה", "עוד שניה", "רגע אחד"]

_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""),
                               http_options=types.HttpOptions(timeout=25000))
    return _client


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


def active_personas():
    off = csv_list(SETTINGS.get("disabled", ""))
    return [k for k in sorted(PERSONAS) if k not in off]


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
        data = json.dumps({"names": names, "voices": voices}, ensure_ascii=False)
    _bg(yemot_write_text, "ai_names.txt", data)


def save_log():
    with _lock:
        lines = [json.dumps({"t": "log", **l}, ensure_ascii=False) for l in LOG[-LOG_MAX:]]
        lines += [json.dumps({"t": "call", **c}, ensure_ascii=False) for c in CALLS[-LOG_MAX:]]
    _bg(yemot_write_text, "ai_log.txt", "\n".join(lines))


def save_settings():
    _bg(yemot_write_text, "ai_settings.txt", json.dumps(SETTINGS, ensure_ascii=False))


def save_personas():
    _bg(yemot_write_text, "ai_personas.txt", json.dumps({"names": PERSONA_NAMES, "prompts": PERSONAS}, ensure_ascii=False))


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
        t = yemot_read_text("ai_personas.txt")
        if t:
            d = json.loads(t)
            for k, v in d.get("names", {}).items():
                if k in PERSONA_NAMES and v:
                    PERSONA_NAMES[k] = v
            for k, v in d.get("prompts", {}).items():
                if k in PERSONAS and v:
                    PERSONAS[k] = v.replace(GENERAL_RULES, "")
    except Exception as e:
        print("load personas error:", e)
    print("loaded: %d names, %d log, %d calls" % (len(names), len(LOG), len(CALLS)))


load_all()


# ============================================================ קול טבעי
def voice_list():
    v = csv_list(SETTINGS.get("voices", "")) or csv_list(DEFAULT_VOICES)
    return v


def make_tts(text, voice_name):
    """טקסט -> קובץ wav 8kHz מונו (Edge TTS + ffmpeg מובנה). מחזיר bytes או None"""
    import edge_tts
    import imageio_ffmpeg

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
        voice_name = vl[voice_idx % len(vl)]
        wav = make_tts(text, voice_name)
        if not wav:
            return None
        fname = "ai_tts_%s_%s" % (re.sub(r"[^0-9a-zA-Z]", "", call_id)[-10:], uuid.uuid4().hex[:6])
        yemot_upload_file(fname + ".wav", wav)
        return fname
    except Exception as e:
        print("tts error:", e)
        return None


# ============================================================ Gemini
def gemini(system, contents, search=False):
    last_error = None
    variants = []
    for model in MODELS:
        if search:
            variants.append((model, True, True))
        variants.append((model, False, True))
        variants.append((model, False, False))
    for model, use_search, no_think in variants:
        try:
            kw = dict(system_instruction=system, max_output_tokens=700)
            if use_search:
                kw["tools"] = [types.Tool(google_search=types.GoogleSearch())]
            if no_think:
                kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            response = get_client().models.generate_content(
                model=model, contents=contents, config=types.GenerateContentConfig(**kw))
            if response.text:
                return response.text
        except Exception as e:
            last_error = e
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


ACTION_RE = re.compile(r"תמלול\s*:\s*(.*?)\s*\n\s*פעולה\s*:\s*(.*?)\s*\n\s*תשובה\s*:\s*(.*)", re.S)


def ask_ai(persona, history, file_name):
    """מחזיר (תמלול, פעולה, תשובה). פעולה: none / menu / end / voice / switch:N"""
    try:
        audio = yemot_download(file_name + ".wav")
    except Exception as e:
        print("download error:", e)
        return "", "none", "סליחה, לא הצלחתי לשמוע את ההקלטה. נסה שוב."
    _bg(yemot_delete, file_name + ".wav")

    others = ", ".join("%s = %s" % (k, PERSONA_NAMES[k]) for k in active_personas() if k != persona)
    system = PERSONAS.get(persona, PERSONAS["1"]) + GENERAL_RULES + (
        " תקבל הקלטה של מה שהמשתמש אמר עכשיו. ההקלטה היא משיחת טלפון באיכות נמוכה (8 קילוהרץ), בעברית מדוברת,"
        " לפעמים עם רעשי רקע. הקשב בתשומת לב מלאה, והשתמש בהקשר של השיחה ובתחום של העוזר כדי להשלים מילים לא ברורות"
        " (שמות של זמרים, מלחינים, מקומות, מונחים). אם משהו באמת לא ברור, שאל בקצרה במקום לנחש."
        " ענה בדיוק בפורמט הבא, שלוש שורות:\n"
        "תמלול: <תמלול מדויק של ההקלטה>\n"
        "פעולה: <אחת מהאפשרויות: none | menu | end | voice | switch:מספר>\n"
        "תשובה: <התשובה שלך למשתמש>\n"
        "כללי הפעולה: menu אם ביקש לחזור לתפריט. end אם ביקש לסיים או להתנתק או אמר להתראות. "
        "voice אם ביקש להחליף קול. switch:מספר אם ביקש לעבור לעוזר אחר מהרשימה: " + others + ". "
        "אחרת none. כשהפעולה אינה none, כתוב בתשובה משפט קצר מתאים (למשל: בטח, מעביר אותך)."
    )
    contents = list(history) + [{
        "role": "user",
        "parts": [{"text": "ההקלטה של המשתמש:"}, types.Part.from_bytes(data=audio, mime_type="audio/wav")],
    }]
    raw = gemini(system, contents, search=True)
    if not raw:
        return "", "none", "סליחה, יש בעיה זמנית. נסה שוב."
    m = ACTION_RE.search(raw)
    if m:
        transcript, action, answer = m.group(1).strip(), m.group(2).strip().lower(), m.group(3).strip()
    else:
        transcript, action = "", "none"
        answer = re.sub(r"^(תמלול|פעולה|תשובה)\s*:\s*", "", raw.strip())
    # גיבוי לפי מילים
    low = transcript.lower()
    if action == "none":
        if "החלף קול" in low or "תחליף קול" in low or "שנה קול" in low:
            action = "voice"
        elif low.strip() in ("תפריט", "תפריט.", "חזרה לתפריט"):
            action = "menu"
        elif low.strip() in ("סיים", "ביי", "להתראות", "סיים.", "ביי.", "להתראות."):
            action = "end"
    if action.startswith("switch"):
        num = re.sub(r"[^0-9]", "", action)
        action = "switch:" + num if num in PERSONAS else "none"
    if not answer:
        answer = "לא הבנתי, אפשר לחזור על זה?"
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
    """הודעה להשמעה: קובץ קול טבעי אם יש, אחרת הקראה של ימות"""
    f = state.pop("tts_file", None)
    if f:
        return ("file", f)
    return ("text", text)


def menu(state, name, prefix=None):
    state["n"] += 1
    state["wait"] = "choice_%d" % state["n"]
    state["stage"] = "menu"
    items = ["שלום %s." % name]
    allowed = ""
    for k in active_personas():
        nm = PERSONA_NAMES[k]
        nm = nm[1:] if nm.startswith("ה") else nm
        items.append("הקש %s ל%s." % (k, nm))
        allowed += k
    items.append("הקש 9 לסיום.")
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
    """הקשבה. התשובה (text) מושמעת כהודעת ההקלטה - בקול טבעי אם יש"""
    state["stage"] = "chat"
    if first:
        return record(state, "speech", ("text", (text + ". " if text else "") + "דבר אחרי הצפצוף, ובסיום הקש סולמית"))
    return record(state, "speech", msg_part(state, text or "אני מקשיב"))


def wait_message(state):
    i = state.get("wait_i", 0)
    state["wait_i"] = i + 1
    state["n"] += 1
    return build_read([("text", WAIT_PHRASES[i % len(WAIT_PHRASES)])], mode="tap", val_name="w_%d" % state["n"],
                      max_digits=1, min_digits=1, sec_wait=2, amount_attempts=1, allow_empty="Ok", empty_val="None")


def goodbye(call_id, name, state=None):
    part = msg_part(state, "להתראות %s" % name) if state else ("text", "להתראות %s" % name)
    with _lock:
        calls.pop(call_id, None)
    return build_combined_action([build_id_list_message([part]), build_go_to_folder("hangup")])


def ai_worker(pending, state, persona, history, file_name, call_id, voice_idx):
    try:
        transcript, action, answer = ask_ai(persona, history, file_name)
        tts = None
        if action in ("none", "voice") or action.startswith("switch"):
            v = voice_idx + 1 if action == "voice" else voice_idx
            tts = speak_file(answer, v, call_id)
        pending["result"] = (transcript, action, answer, tts)
    except Exception as e:
        print("worker error:", e)
        pending["result"] = ("", "none", "סליחה, יש בעיה זמנית. נסה שוב.", None)
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
            state = {"stage": "start", "n": 0, "wait": None, "persona": None, "history": [], "call_id": call_id,
                     "file": None, "pending": None, "wait_i": 0, "phone": phone, "started": time.time(),
                     "voice": voices.get(phone, 0), "last_q": "", "tts_file": None, "played": []}
            calls[call_id] = state
            CALLS.append({"time": now_str(), "phone": phone, "name": names.get(phone, "")})
            del CALLS[:-LOG_MAX]
        state["last"] = time.time()
    if new_call:
        save_log()
        if is_blocked(phone):
            with _lock:
                calls.pop(call_id, None)
            return R(build_combined_action([build_id_list_message([("text", "המספר שלך אינו מורשה להשתמש בקו")]),
                                            build_go_to_folder("hangup")]))

    # מחיקת קובץ קול שכבר הושמע
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
        ann = SETTINGS.get("announcement", "").strip()
        if name:
            return R(menu(state, name, prefix=ann or None))
        state["stage"] = "ask_name"
        return R(record(state, "name", ("text", "שלום, זו הפעם הראשונה שלך בקו. אמור את שמך הפרטי, ובסיום הקש סולמית"),
                        prefix=("text", ann) if ann else None))

    # ---- קבלת שם
    if state["stage"] == "ask_name":
        if not has_value:
            return R(record(state, "name", ("text", "אמור את שמך הפרטי, ובסיום הקש סולמית")))
        name = transcribe_name(state["file"]) or "אורח"
        names[phone] = name
        save_names()
        return R(menu(state, name, prefix="נעים להכיר %s, השם נשמר" % name))

    name = name or "אורח"

    # ---- תפריט
    if state["stage"] == "menu":
        if value == "9":
            return R(goodbye(call_id, name, state))
        if value in active_personas():
            state["persona"] = value
            state["history"] = []
            return R(listen(state, "אתה עם %s" % PERSONA_NAMES[value], first=True))
        return R(menu(state, name))

    # ---- שיחה
    if state["stage"] == "chat":
        pending = state.get("pending")
        if pending is None:
            if not has_value:
                return R(listen(state, "לא שמעתי אותך"))
            if over_limit(phone):
                _bg(yemot_delete, state["file"] + ".wav")
                return R(menu(state, name, prefix="הגעת למכסת ההודעות היומית שלך. אפשר לנסות שוב מחר"))
            pending = {"done": False, "result": None, "started": time.time(), "event": threading.Event()}
            state["pending"] = pending
            state["wait_i"] = 0
            _bg(ai_worker, pending, state, state["persona"], list(state["history"]), state["file"], call_id, state["voice"])
            # מחכים עד 8 שניות בתוך הפנייה עצמה - ברוב המקרים התשובה מוכנה ואין "רק רגע" בכלל
            pending["event"].wait(8)

        if not pending["done"]:
            # עדיין לא מוכן: משמיעים "רק רגע" (ימות חוזרים אלינו אחרי ~3 שניות) ומחכים שוב
            if time.time() - pending["started"] > 75:
                state["pending"] = None
                return R(listen(state, "סליחה, זה לוקח יותר מדי זמן. אפשר לנסות שוב"))
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
                LOG.append({"time": now_str(), "phone": phone, "name": name,
                            "persona": PERSONA_NAMES.get(state["persona"], ""), "q": transcript, "a": answer})
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
            return R(listen(state, "הקול הוחלף. " + answer))
        if action.startswith("switch:"):
            new = action.split(":")[1]
            if new in active_personas():
                state["persona"] = new
                return R(listen(state, answer))
        return R(listen(state, answer))

    return R(menu(state, name))


# ============================================================ אתר ניהול
CSS = """<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
body{font-family:Arial,sans-serif;direction:rtl;background:#f4f6f9;margin:0;color:#222}
.wrap{max-width:1150px;margin:0 auto;padding:14px}
h1{margin:6px 0 12px}h2{margin:22px 0 8px;font-size:19px;color:#2d3e50}
.cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.card{background:#fff;border-radius:10px;padding:12px 16px;box-shadow:0 1px 3px #0002;min-width:130px;flex:1}
.card b{font-size:26px;display:block}.card small{color:#777}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px #0002;margin-bottom:14px}
th,td{padding:7px 9px;border-bottom:1px solid #eee;text-align:right;vertical-align:top;font-size:14px}
th{background:#2d3e50;color:#fff}
input[type=text],input[type=password],select{padding:5px;border:1px solid #ccc;border-radius:6px}
button{padding:5px 10px;border:0;border-radius:6px;background:#2d3e50;color:#fff;cursor:pointer}
button.red{background:#c0392b}button.green{background:#27ae60}form.inline{display:inline}
.q{color:#1a5fb4}.a{color:#333}textarea{width:100%;height:60px;padding:6px;border:1px solid #ccc;border-radius:6px;font-family:inherit}
.top{display:flex;justify-content:space-between;align-items:center}a{color:#1a5fb4}
.live{background:#e8f8ee;border-radius:10px;padding:10px 14px;margin-bottom:12px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#27ae60;margin-left:6px;animation:b 1.2s infinite}
@keyframes b{50%{opacity:.3}}.charts{display:flex;gap:12px;flex-wrap:wrap}.chart{background:#fff;border-radius:10px;padding:10px;box-shadow:0 1px 3px #0002;flex:1;min-width:300px}
.ok{color:#27ae60}.bad{color:#c0392b}.note{font-size:12px;color:#666}
</style>"""


def is_admin():
    if not ADMIN_KEY:
        return False
    return (request.values.get("key") or request.cookies.get("admin_key")) == ADMIN_KEY


def login_page(msg=""):
    return Response(CSS + """<div class="wrap" style="max-width:380px;margin-top:80px"><div class="card"><h2>כניסה לניהול הקו</h2>%s
    <form method="post" action="/admin/login"><input type="password" name="key" placeholder="סיסמה" style="width:100%%;box-sizing:border-box;margin-bottom:8px;padding:8px">
    <button style="width:100%%;padding:8px">כניסה</button></form></div></div>""" % (
        "<p class='bad'>%s</p>" % msg if msg else ""), mimetype="text/html; charset=utf-8")


def redirect(to="/admin"):
    return Response("", status=302, headers={"Location": to})


def guard():
    return None if is_admin() else login_page()


def snapshot():
    with _lock:
        return dict(names), list(LOG), list(CALLS), {k: dict(v) for k, v in calls.items()}


def live_data():
    users, log, cl, active = snapshot()
    today = today_str()
    act = []
    for cid, st in sorted(active.items(), key=lambda x: -x[1].get("started", 0)):
        act.append({"phone": st.get("phone", ""), "name": users.get(st.get("phone", ""), "לא רשום"),
                    "persona": PERSONA_NAMES.get(st.get("persona") or "", "בתפריט"),
                    "since": datetime.datetime.utcfromtimestamp(st.get("started", 0) + 3 * 3600).strftime("%H:%M"),
                    "last_q": st.get("last_q", ""),
                    "state": "מחכה לתשובה" if st.get("pending") else ("מדבר" if st.get("stage") == "chat" else st.get("stage", ""))})
    return {"active": act, "calls_today": sum(1 for c in cl if c["time"].startswith(today)),
            "msgs_today": sum(1 for l in log if l["time"].startswith(today)), "users": len(users),
            "calls_total": len(cl), "msgs_total": len(log), "time": now_str()}


def bar_chart(title, pairs, color="#2d3e50"):
    """גרף עמודות SVG פשוט (בלי ספריות חיצוניות)"""
    h = html.escape
    if not pairs:
        return '<div class="chart"><b>%s</b><p class="note">אין נתונים עדיין</p></div>' % h(title)
    mx = max(v for _, v in pairs) or 1
    w = max(320, 30 * len(pairs) + 40)
    out = ['<div class="chart"><b>%s</b><svg viewBox="0 0 %d 190" width="100%%" style="max-height:220px">' % (h(title), w)]
    for i, (lab, v) in enumerate(pairs):
        x = 20 + i * 30
        bh = int(140 * v / mx)
        out.append('<rect x="%d" y="%d" width="22" height="%d" rx="3" fill="%s"/>' % (x, 155 - bh, bh, color))
        out.append('<text x="%d" y="%d" font-size="11" text-anchor="middle">%s</text>' % (x + 11, 150 - bh, v))
        out.append('<text x="%d" y="175" font-size="9" text-anchor="middle">%s</text>' % (x + 11, h(str(lab))[:12]))
    out.append('</svg></div>')
    return "".join(out)


@app.route("/admin/login", methods=["POST"])
def admin_login():
    if not ADMIN_KEY:
        return login_page("לא הוגדרה סיסמה (ADMIN_KEY) בשרת")
    if request.form.get("key", "") != ADMIN_KEY:
        return login_page("סיסמה שגויה")
    resp = redirect()
    resp.set_cookie("admin_key", ADMIN_KEY, max_age=60 * 60 * 24 * 180, httponly=True)
    return resp


@app.route("/admin/logout")
def admin_logout():
    resp = redirect()
    resp.set_cookie("admin_key", "", max_age=0)
    return resp


@app.route("/admin/data")
def admin_data():
    if not is_admin():
        return Response("{}", status=403, mimetype="application/json")
    return Response(json.dumps(live_data(), ensure_ascii=False), mimetype="application/json; charset=utf-8")


@app.route("/admin")
def admin():
    g = guard()
    if g:
        return g
    h = html.escape
    users, log, cl, active = snapshot()
    filt = request.args.get("phone", "").strip()
    search = request.args.get("q", "").strip()
    ld = live_data()

    # גרפים
    days = [(il_now() - datetime.timedelta(days=i)).strftime("%d/%m/%Y") for i in range(13, -1, -1)]
    per_day = [(d[:5], sum(1 for c in cl if c["time"].startswith(d))) for d in days]
    per_persona = {}
    for l in log:
        per_persona[l["persona"]] = per_persona.get(l["persona"], 0) + 1
    per_persona = sorted(per_persona.items(), key=lambda x: -x[1])[:8]
    per_user = {}
    for l in log:
        per_user[l["phone"]] = per_user.get(l["phone"], 0) + 1
    top_users = [(users.get(p, p[-4:]), n) for p, n in sorted(per_user.items(), key=lambda x: -x[1])[:10]]

    o = [CSS, '<div class="wrap"><div class="top"><h1>ניהול הקו</h1><span class="note" id="clock">%s</span></div>' % h(ld["time"])]

    # חי
    o.append('<div class="live"><b><span class="dot"></span>עכשיו בקו</b> <span class="note">(מתעדכן לבד כל 5 שניות)</span>'
             '<div class="cards" style="margin-top:8px">'
             '<div class="card">שיחות פעילות<b id="c_active">%d</b></div>'
             '<div class="card">שיחות היום<b id="c_calls">%d</b></div>'
             '<div class="card">הודעות היום<b id="c_msgs">%d</b></div>'
             '<div class="card">משתמשים רשומים<b id="c_users">%d</b></div>'
             '<div class="card">סה"כ שיחות<b id="c_ct">%d</b></div></div>'
             '<table id="active"><tr><th>מי</th><th>טלפון</th><th>עוזר</th><th>מצב</th><th>מאז</th><th>שאלה אחרונה</th></tr>'
             '<tr><td colspan="6" class="note">אין שיחות פעילות</td></tr></table></div>' % (
                 len(ld["active"]), ld["calls_today"], ld["msgs_today"], ld["users"], ld["calls_total"]))

    o.append('<div class="charts">%s%s%s</div>' % (bar_chart("שיחות ב-14 הימים האחרונים", per_day),
                                                    bar_chart("הודעות לפי עוזר", per_persona, "#1a5fb4"),
                                                    bar_chart("המשתמשים הפעילים", top_users, "#27ae60")))

    # משתמשים
    blocked = csv_list(SETTINGS.get("blocked_phones", ""))
    o.append('<h2>משתמשים רשומים (%d)</h2><table><tr><th>שם</th><th>טלפון</th><th>שיחות</th><th>הודעות</th><th>היום</th><th>פעולות</th></tr>' % len(users))
    for phone, nm in sorted(users.items(), key=lambda x: x[1]):
        n_calls = sum(1 for c in cl if c["phone"] == phone)
        n_msgs = per_user.get(phone, 0)
        n_today = messages_today(phone)
        bl = phone in blocked
        o.append('<tr%s><td>%s%s</td><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>'
                 '<form class="inline" method="post" action="/admin/rename"><input type="hidden" name="phone" value="%s">'
                 '<input type="text" name="name" value="%s" style="width:110px"> <button>שנה שם</button></form> '
                 '<form class="inline" method="post" action="/admin/block"><input type="hidden" name="phone" value="%s"><button class="%s">%s</button></form> '
                 '<form class="inline" method="post" action="/admin/delete" onsubmit="return confirm(\'למחוק? בשיחה הבאה יירשם מחדש\')">'
                 '<input type="hidden" name="phone" value="%s"><button class="red">מחק</button></form> '
                 '<a href="/admin?phone=%s#log">שיחות</a></td></tr>' % (
                     ' style="background:#fdecea"' if bl else "", h(nm), " (חסום)" if bl else "", h(phone), n_calls, n_msgs, n_today,
                     h(phone), h(nm), h(phone), "green" if bl else "red", "בטל חסימה" if bl else "חסום", h(phone), h(phone)))
    if not users:
        o.append('<tr><td colspan="6">עדיין אין משתמשים</td></tr>')
    o.append('</table>')

    # יומן
    shown = [l for l in log if (not filt or l["phone"] == filt) and (not search or search in l["q"] or search in l["a"] or search in l["name"])][::-1][:300]
    o.append('<h2 id="log">מה דיברו עם הקו%s</h2>' % (" - " + h(filt) + ' (<a href="/admin#log">הצג הכל</a>)' if filt else ""))
    o.append('<form class="inline" method="get" action="/admin"><input type="text" name="q" value="%s" placeholder="חיפוש ביומן"> <button>חפש</button></form> '
             '<form class="inline" method="post" action="/admin/clear" onsubmit="return confirm(\'למחוק את כל היומן?\')"><button class="red">נקה יומן</button></form>' % h(search))
    o.append('<table><tr><th>זמן</th><th>מי</th><th>עוזר</th><th>מה נאמר</th></tr>')
    for l in shown:
        o.append('<tr><td>%s</td><td>%s<br><small>%s</small></td><td>%s</td><td><div class="q">שאל: %s</div><div class="a">ענה: %s</div></td></tr>' % (
            h(l["time"]), h(l["name"]), h(l["phone"]), h(l["persona"]), h(l["q"]), h(l["a"])))
    if not shown:
        o.append('<tr><td colspan="4">אין הודעות</td></tr>')
    o.append('</table>')

    # עוזרים
    off = csv_list(SETTINGS.get("disabled", ""))
    o.append('<h2>העוזרים</h2><form method="post" action="/admin/personas"><table><tr><th style="width:40px">מס</th><th style="width:60px">פעיל</th><th style="width:170px">שם</th><th>ההנחיה ל-AI</th></tr>')
    for k in sorted(PERSONAS):
        o.append('<tr><td>%s</td><td><input type="checkbox" name="on_%s" %s></td><td><input type="text" name="name_%s" value="%s" style="width:150px"></td>'
                 '<td><textarea name="prompt_%s">%s</textarea></td></tr>' % (k, k, "" if k in off else "checked", k, h(PERSONA_NAMES[k]), k, h(PERSONAS[k])))
    o.append('</table><button>שמור עוזרים</button> <span class="note">עוזר לא פעיל לא מופיע בתפריט. הכללים הקבועים (טלפון, עברית, צניעות, חיפוש) מתווספים אוטומטית.</span></form>')

    # הגדרות
    S = SETTINGS
    o.append('<h2>הגדרות</h2><form method="post" action="/admin/settings"><table><tr><th style="width:280px">הגדרה</th><th>ערך</th></tr>')
    o.append('<tr><td>הודעה בתחילת כל שיחה (ריק = בלי)</td><td><input type="text" name="announcement" value="%s" style="width:95%%"></td></tr>' % h(S["announcement"]))
    o.append('<tr><td>קול טבעי (Edge)</td><td><select name="tts"><option value="on" %s>פעיל</option><option value="off" %s>כבוי - הקראה של ימות</option></select></td></tr>' % (
        "selected" if S["tts"] == "on" else "", "selected" if S["tts"] != "on" else ""))
    o.append('<tr><td>רשימת קולות (מופרדים בפסיק, הראשון ברירת מחדל)</td><td><input type="text" name="voices" value="%s" style="width:95%%"></td></tr>' % h(S["voices"]))
    o.append('<tr><td>הודעות ליום לכל משתמש (0 = בלי הגבלה)</td><td><input type="text" name="daily_limit" value="%d"></td></tr>' % S["daily_limit"])
    o.append('<tr><td>מספרים ללא הגבלה</td><td><input type="text" name="unlimited_phones" value="%s" style="width:95%%"></td></tr>' % h(S["unlimited_phones"]))
    o.append('<tr><td>מספרים חסומים</td><td><input type="text" name="blocked_phones" value="%s" style="width:95%%"></td></tr>' % h(S["blocked_phones"]))
    o.append('<tr><td>אורך הקלטה מקסימלי (שניות)</td><td><input type="text" name="record_max" value="%d"></td></tr>' % S["record_max"])
    o.append('<tr><td>שעת הסיכום היומי למייל (0-23)</td><td><input type="text" name="mail_hour" value="%d"></td></tr>' % S["mail_hour"])
    o.append('</table><button>שמור הגדרות</button></form>')
    mail_state = ("<span class='ok'>מוגדר, נשלח אל %s</span>" % h(MAIL_TO)) if (MAIL_USER and MAIL_PASS) else "<span class='bad'>לא מוגדר (MAIL_USER ו-MAIL_PASS ב-Render)</span>"
    o.append('<p>סיכום יומי למייל: %s <form class="inline" method="post" action="/admin/sendmail"><button>שלח סיכום של היום עכשיו</button></form> <b class="ok">%s</b></p>' % (
        mail_state, h(request.args.get("mail", ""))))
    o.append('<p><a href="/admin/logout">יציאה</a></p></div>')

    o.append("""<script>
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function tick(){try{const r=await fetch('/admin/data');const d=await r.json();
document.getElementById('c_active').textContent=d.active.length;document.getElementById('c_calls').textContent=d.calls_today;
document.getElementById('c_msgs').textContent=d.msgs_today;document.getElementById('c_users').textContent=d.users;
document.getElementById('c_ct').textContent=d.calls_total;document.getElementById('clock').textContent=d.time;
let t='<tr><th>מי</th><th>טלפון</th><th>עוזר</th><th>מצב</th><th>מאז</th><th>שאלה אחרונה</th></tr>';
if(!d.active.length)t+='<tr><td colspan="6" class="note">אין שיחות פעילות</td></tr>';
for(const a of d.active)t+='<tr><td>'+esc(a.name)+'</td><td>'+esc(a.phone)+'</td><td>'+esc(a.persona)+'</td><td>'+esc(a.state)+'</td><td>'+esc(a.since)+'</td><td>'+esc(a.last_q)+'</td></tr>';
document.getElementById('active').innerHTML=t;}catch(e){}}
tick();setInterval(tick,5000);</script>""")
    return Response("".join(o), mimetype="text/html; charset=utf-8")


@app.route("/admin/rename", methods=["POST"])
def admin_rename():
    g = guard()
    if g:
        return g
    phone, nm = request.form.get("phone", ""), clean_for_tts(request.form.get("name", ""))[:30]
    if phone in names and nm:
        names[phone] = nm
        save_names()
    return redirect()


@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    g = guard()
    if g:
        return g
    names.pop(request.form.get("phone", ""), None)
    save_names()
    return redirect()


@app.route("/admin/block", methods=["POST"])
def admin_block():
    g = guard()
    if g:
        return g
    phone = request.form.get("phone", "")
    bl = csv_list(SETTINGS["blocked_phones"])
    if phone in bl:
        bl.remove(phone)
    elif phone and phone not in OWNER_PHONES:
        bl.append(phone)
    SETTINGS["blocked_phones"] = ",".join(bl)
    save_settings()
    return redirect()


@app.route("/admin/clear", methods=["POST"])
def admin_clear():
    g = guard()
    if g:
        return g
    with _lock:
        LOG.clear()
    save_log()
    return redirect()


@app.route("/admin/personas", methods=["POST"])
def admin_personas():
    g = guard()
    if g:
        return g
    off = []
    for k in list(PERSONAS):
        nm = request.form.get("name_" + k, "").strip()
        pr = request.form.get("prompt_" + k, "").strip()
        if nm:
            PERSONA_NAMES[k] = nm[:40]
        if pr:
            PERSONAS[k] = pr
        if not request.form.get("on_" + k):
            off.append(k)
    SETTINGS["disabled"] = ",".join(off)
    save_personas()
    save_settings()
    return redirect()


@app.route("/admin/settings", methods=["POST"])
def admin_settings():
    g = guard()
    if g:
        return g
    f = request.form

    def num(key, lo, hi, default):
        try:
            return min(hi, max(lo, int(f.get(key, default) or default)))
        except ValueError:
            return default
    SETTINGS["daily_limit"] = num("daily_limit", 0, 100000, 40)
    SETTINGS["mail_hour"] = num("mail_hour", 0, 23, 21)
    SETTINGS["record_max"] = num("record_max", 5, 120, 25)
    SETTINGS["unlimited_phones"] = re.sub(r"[^0-9,]", "", f.get("unlimited_phones", ""))
    SETTINGS["blocked_phones"] = re.sub(r"[^0-9,]", "", f.get("blocked_phones", ""))
    SETTINGS["announcement"] = clean_for_tts(f.get("announcement", ""))[:300]
    SETTINGS["tts"] = "on" if f.get("tts") == "on" else "off"
    SETTINGS["voices"] = ",".join(csv_list(f.get("voices", ""))) or DEFAULT_VOICES
    save_settings()
    return redirect()


# ============================================================ מייל יומי
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
                h(l["time"][11:]), h(l["name"]), h(l["persona"]), h(l["q"]), h(l["a"][:200])))
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


@app.route("/admin/sendmail", methods=["POST"])
def admin_sendmail():
    g = guard()
    if g:
        return g
    day = today_str()
    return redirect("/admin?mail=" + urllib.parse.quote(send_mail("סיכום הקו ליום " + day, build_summary(day))))


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


if __name__ == "__main__":
    app.run()
