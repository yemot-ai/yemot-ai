from flask import Flask, request, Response
from yemot_flow.actions import build_id_list_message, build_read, build_go_to_folder, build_combined_action
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
from email.mime.text import MIMEText
import urllib.request
import urllib.parse

app = Flask(__name__)

# שמות לפי מספר טלפון. נשמרים גם כקובץ במערכת ימות המשיח כדי לשרוד הפעלה מחדש של השרת
names = {}

# יומן שיחות (מה כל אחד שאל ומה ה-AI ענה) + רשימת שיחות
LOG = []
CALLS = []
LOG_MAX = 1000
_lock = threading.Lock()

# מצב של כל שיחה פעילה, לפי מזהה השיחה של ימות (ApiCallId) - כל שיחה מבודדת לגמרי מהאחרות
calls = {}
call_locks = {}
_calls_lock = threading.Lock()

WAIT_FILLERS = ["עוד רגע", "רגע אחד, אני בודק", "עוד שניה", "כמעט סיימתי"]
FAST_WAIT = 3.5      # כמה שניות מחכים לתשובה לפני שמשמיעים "עוד רגע"
POLL_WAIT = 2.0      # כמה שניות מחכים בכל סבב "עוד רגע"
MAX_POLLS = 14       # מקסימום סבבי המתנה (~40 שניות) לפני שמוותרים


def get_call_lock(call_id):
    with _calls_lock:
        lock = call_locks.get(call_id)
        if lock is None:
            lock = threading.Lock()
            call_locks[call_id] = lock
        return lock


def drop_call(call_id):
    with _calls_lock:
        calls.pop(call_id, None)
        call_locks.pop(call_id, None)


def cleanup_loop():
    """מנקה שיחות שנשארו בזיכרון (למשל ניתוק בלי הודעה) אחרי שעתיים"""
    while True:
        time.sleep(600)
        try:
            cutoff = time.time() - 2 * 3600
            with _calls_lock:
                stale = [cid for cid, st in calls.items() if st.get("last", 0) < cutoff]
                for cid in stale:
                    calls.pop(cid, None)
                    call_locks.pop(cid, None)
        except Exception as e:
            print("cleanup error:", e)


threading.Thread(target=cleanup_loop, daemon=True).start()

GENERAL_RULES = (
    " אתה מדבר בטלפון, לכן ענה קצר וברור, בלי כוכביות, בלי רשימות, בלי אימוג'ים ובלי סימני עיצוב."
    " ענה בשפה שבה המשתמש דיבר אליך; ברירת המחדל היא עברית."
    " שמור על שפה מכובדת וצנועה, ואל תעסוק בנושאים לא צנועים."
    " יש לך כלי חיפוש באינטרנט. השתמש בו כשהמשתמש מבקש לחפש, או כשהתשובה דורשת מידע עדכני:"
    " מחירים, חנויות, חדשות, מזג אוויר, שעות פתיחה, תוצאות, מה קורה עכשיו. אחרי חיפוש תן תשובה מדויקת עם המספרים והשמות שמצאת."

)

PERSONAS = {
    "1": "אתה עוזר כללי ידידותי ומועיל." + GENERAL_RULES,
    "2": "אתה עוזר תורני. ענה בסגנון תורני מכובד, וציין מקורות כשאפשר." + GENERAL_RULES,
    "3": "אתה עוזר חוצפני וסרקסטי עם הומור. ענה בחוצפה משעשעת אבל בלי להעליב באמת." + GENERAL_RULES,
    "4": "אתה עוזר יצירתי. ספר סיפורים קצרים, כתוב שירים, בדיחות ורעיונות יצירתיים." + GENERAL_RULES,
    "5": "אתה עוזר טכני. הסבר דברים טכניים בפשטות: מחשבים, אינטרנט, טלפונים." + GENERAL_RULES,
    "6": "אתה מדבר כמו ערס ישראלי מגניב: סלנג רחוב (אחי, וואלה, סבבה, יא מלך, בקטנה), ביטחון עצמי, חוצפה וקטע של מגניבות. "
         "עונה לעניין אבל בסטייל. בלי קללות ובלי להעליב באמת." + GENERAL_RULES,
    "7": "אתה מומחה למוזיקה חסידית וישראלית: זמרים, מלחינים, אלבומים, ניגונים, היסטוריה, וגם תיאוריה מוזיקלית - סולמות, אקורדים, "
         "מבנה שירים, מעברים. כששואלים על אקורדים או סולם של שיר, תן את הסולם ואת סדר האקורדים לפי חלקי השיר. "
         "אל תצטט מילים של שירים - אפשר לתאר על מה השיר ומי כתב והלחין." + GENERAL_RULES,
}

PERSONA_NAMES = {
    "1": "העוזר הכללי",
    "2": "העוזר התורני",
    "3": "העוזר החוצפן",
    "4": "העוזר היצירתי",
    "5": "העוזר הטכני",
    "6": "הערס",
    "7": "המומחה למוזיקה",
}

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "")          # מספר המערכת:סיסמה
# רשימת השלוחות של הקו, כל אחת עם קול אחר (מוגדר ב-ext.ini של השלוחה בימות). מופרד בפסיקים.
VOICE_EXTS = [e.strip().strip("/") for e in os.environ.get("VOICE_EXTS", "1").split(",") if e.strip()]
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")                # סיסמה לאתר הניהול
DATA_EXT = VOICE_EXTS[0]                                   # השלוחה שבה נשמרים קבצי הנתונים

# מייל לסיכום יומי (Gmail עם סיסמת אפליקציה)
MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_PASS = os.environ.get("MAIL_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "") or MAIL_USER

# מספרי הבעלים - תמיד בלי הגבלה, לא משנה מה מוגדר באתר הניהול
OWNER_PHONES = ["0527661756", "0527609296"]

# הגדרות שניתן לשנות מאתר הניהול (נשמרות בימות)
SETTINGS = {
    "daily_limit": int(os.environ.get("DAILY_LIMIT", "40")),   # הודעות ליום לכל משתמש (0 = בלי הגבלה)
    "unlimited_phones": "0527661756,0527609296",                        # מספרים ללא הגבלה, מופרדים בפסיק
    "mail_hour": 21,                                           # שעת שליחת הסיכום היומי (שעון ישראל)
}
YEMOT_API = "https://www.call2all.co.il/ym/api/"

MODELS = ["gemini-3.1-flash-lite", "gemini-3-flash", "gemini-2.5-flash-lite", "gemini-2.5-flash"]

_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
    return _client


def clean_for_tts(text):
    text = re.sub(r"[*_#`>\[\]]", "", text)
    text = text.replace("\n", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:800]


def yemot_download(ext, file_name):
    """הורדת הקלטה מהמערכת של ימות המשיח"""
    url = YEMOT_API + "DownloadFile?" + urllib.parse.urlencode({
        "token": YEMOT_TOKEN,
        "path": "ivr2:/%s/%s.wav" % (ext, file_name),
    })
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()


def yemot_delete(ext, file_name):
    """מחיקת ההקלטה אחרי השימוש (לא קריטי אם נכשל)"""
    try:
        url = YEMOT_API + "FileAction?" + urllib.parse.urlencode({
            "token": YEMOT_TOKEN,
            "action": "delete",
            "what": "ivr2:/%s/%s.wav" % (ext, file_name),
        })
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as e:
        print("delete error:", e)


def yemot_read_text(file_name):
    """קריאת קובץ טקסט מהמערכת של ימות (מחזיר None אם אין)"""
    try:
        url = YEMOT_API + "DownloadFile?" + urllib.parse.urlencode({
            "token": YEMOT_TOKEN, "path": "ivr2:/%s/%s" % (DATA_EXT, file_name)})
        with urllib.request.urlopen(url, timeout=20) as r:
            data = r.read().decode("utf-8", "ignore")
        if data.lstrip().startswith("{\"responseStatus"):
            return None
        return data
    except Exception as e:
        print("read text error:", e)
        return None


def yemot_write_text(file_name, text):
    """שמירת קובץ טקסט במערכת של ימות"""
    try:
        body = urllib.parse.urlencode({
            "token": YEMOT_TOKEN, "what": "ivr2:/%s/%s" % (DATA_EXT, file_name), "contents": text}).encode()
        req = urllib.request.Request(YEMOT_API + "UploadTextFile", data=body, method="POST")
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print("write text error:", e)


def save_names():
    with _lock:
        data = json.dumps(names, ensure_ascii=False)
    threading.Thread(target=yemot_write_text, args=("ai_names.txt", data), daemon=True).start()


def save_log():
    with _lock:
        data = json.dumps({"log": LOG[-LOG_MAX:], "calls": CALLS[-LOG_MAX:]}, ensure_ascii=False)
    threading.Thread(target=yemot_write_text, args=("ai_log.txt", data), daemon=True).start()


def load_data():
    """טעינת השמות והיומן מימות בעליית השרת"""
    if not YEMOT_TOKEN:
        return
    try:
        t = yemot_read_text("ai_names.txt")
        if t:
            names.update(json.loads(t))
        t = yemot_read_text("ai_log.txt")
        if t:
            d = json.loads(t)
            LOG.extend(d.get("log", []))
            CALLS.extend(d.get("calls", []))
        print("loaded %d names, %d log lines" % (len(names), len(LOG)))
    except Exception as e:
        print("load error:", e)


load_data()


def save_settings():
    data = json.dumps(SETTINGS, ensure_ascii=False)
    threading.Thread(target=yemot_write_text, args=("ai_settings.txt", data), daemon=True).start()


def load_settings():
    if not YEMOT_TOKEN:
        return
    try:
        t = yemot_read_text("ai_settings.txt")
        if t:
            d = json.loads(t)
            SETTINGS["daily_limit"] = int(d.get("daily_limit", SETTINGS["daily_limit"]))
            SETTINGS["unlimited_phones"] = str(d.get("unlimited_phones", ""))
            SETTINGS["mail_hour"] = int(d.get("mail_hour", SETTINGS["mail_hour"]))
    except Exception as e:
        print("load settings error:", e)


load_settings()


def il_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)


def now_str():
    return il_now().strftime("%d/%m/%Y %H:%M")


def today_str():
    return il_now().strftime("%d/%m/%Y")


def messages_today(phone):
    today = today_str()
    with _lock:
        return sum(1 for l in LOG if l["phone"] == phone and l["time"].startswith(today))


def over_limit(phone):
    limit = SETTINGS.get("daily_limit", 0)
    if limit <= 0:
        return False
    unlimited = [x.strip() for x in SETTINGS.get("unlimited_phones", "").split(",") if x.strip()]
    if phone in unlimited or phone in OWNER_PHONES:
        return False
    return messages_today(phone) >= limit


def build_summary(day):
    """סיכום של יום אחד (טקסט HTML)"""
    h = html.escape
    with _lock:
        log = [l for l in LOG if l["time"].startswith(day)]
        calls = [c for c in CALLS if c["time"].startswith(day)]
        users = dict(names)
    phones = sorted(set(c["phone"] for c in calls) | set(l["phone"] for l in log))
    out = ["<div dir='rtl' style='font-family:Arial'>",
           "<h2>סיכום הקו ליום %s</h2>" % h(day),
           "<p>שיחות: <b>%d</b> &nbsp; מתקשרים שונים: <b>%d</b> &nbsp; הודעות ל-AI: <b>%d</b></p>" % (
               len(calls), len(phones), len(log))]
    if phones:
        out.append("<h3>לפי מתקשר</h3><ul>")
        for ph in phones:
            nm = users.get(ph, "לא רשום")
            out.append("<li>%s (%s): %d שיחות, %d הודעות</li>" % (
                h(nm), h(ph), sum(1 for c in calls if c["phone"] == ph), sum(1 for l in log if l["phone"] == ph)))
        out.append("</ul>")
    if log:
        out.append("<h3>מה שאלו</h3><table border='1' cellpadding='5' style='border-collapse:collapse'>"
                   "<tr><th>שעה</th><th>מי</th><th>עוזר</th><th>שאלה</th><th>תשובה</th></tr>")
        for l in log[:150]:
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                h(l["time"][11:]), h(l["name"]), h(l["persona"]), h(l["q"]), h(l["a"][:200])))
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
        print("mail error:", e)
        return "שגיאה בשליחה: %s" % e


_last_mail_day = [None]


def daily_mail_loop():
    """שולח סיכום פעם ביום בשעה שנקבעה"""
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





def gemini(system, contents, schema=None, search=False):
    """קריאה ל-Gemini. אם משהו נכשל - מנסה בלי כיבוי חשיבה, בלי חיפוש, ואז את המודל הבא"""
    last_error = None
    variants = []
    for model in MODELS:
        for use_search in ([True, False] if search else [False]):
            for no_think in (True, False):
                variants.append((model, use_search, no_think))
    for model, use_search, no_think in variants:
        try:
            kw = dict(system_instruction=system, max_output_tokens=600)
            if schema:
                kw["response_mime_type"] = "application/json"
                kw["response_schema"] = schema
            if use_search:
                kw["tools"] = [types.Tool(google_search=types.GoogleSearch())]
            if no_think:
                kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            cfg = types.GenerateContentConfig(**kw)
            response = get_client().models.generate_content(model=model, contents=contents, config=cfg)
            if response.text:
                return response.text
        except Exception as e:
            last_error = e
            continue
    print("Gemini error:", last_error)
    return None


def transcribe_name(ext, file_name):
    """המרת הקלטת השם לטקסט"""
    try:
        audio = yemot_download(ext, file_name)
    except Exception as e:
        print("download error:", e)
        return ""
    yemot_delete(ext, file_name)
    text = gemini(
        "בהקלטה אדם אומר את שמו הפרטי בעברית. החזר רק את השם הפרטי, מילה אחת או שתיים, בלי שום תוספת.",
        [types.Part.from_bytes(data=audio, mime_type="audio/wav")],
    )
    return clean_for_tts(text or "")[:30]


def ask_ai(persona, history, ext, file_name):
    """מוריד את ההקלטה, מתמלל ועונה בקריאה אחת (כולל חיפוש באינטרנט כשצריך). מחזיר (תמלול, תשובה)"""
    try:
        audio = yemot_download(ext, file_name)
    except Exception as e:
        print("download error:", e)
        return "", "סליחה, לא הצלחתי לשמוע את ההקלטה. נסה שוב."
    yemot_delete(ext, file_name)

    system = PERSONAS.get(persona, PERSONAS["1"]) + (
        " תקבל הקלטה של מה שהמשתמש אמר עכשיו. ענה בדיוק בפורמט הבא, שתי שורות:\n"
        "תמלול: <תמלול מדויק של ההקלטה>\n"
        "תשובה: <התשובה שלך למשתמש>"
    )
    contents = list(history) + [{
        "role": "user",
        "parts": [
            {"text": "ההקלטה של המשתמש:"},
            types.Part.from_bytes(data=audio, mime_type="audio/wav"),
        ],
    }]
    raw = gemini(system, contents, search=True)
    if not raw:
        return "", "סליחה, יש בעיה זמנית. נסה שוב."
    m = re.search(r"תמלול\s*:\s*(.*?)\s*(?:\n|^|\s)תשובה\s*:\s*(.*)", raw, re.S)
    if m:
        transcript, answer = m.group(1).strip(), m.group(2).strip()
    else:
        transcript, answer = "", re.sub(r"^(תמלול|תשובה)\s*:\s*", "", raw.strip())
    if not answer:
        answer = "לא הבנתי, אפשר לחזור על זה?"
    return transcript, clean_for_tts(answer)


WAIT_PHRASES = ["רק רגע", "עוד רגע", "רק שניה", "כבר עונה", "עוד שניה", "רגע אחד"]


def wait_message(state):
    """הודעת המתנה קצרה. אחרי שהיא מושמעת ימות פונים שוב לשרת ובודקים אם התשובה מוכנה"""
    i = state.get("wait_i", 0)
    state["wait_i"] = i + 1
    return build_id_list_message([("text", WAIT_PHRASES[i % len(WAIT_PHRASES)])])


def ai_worker(pending, persona, history, ext, file_name):
    """רץ ברקע: מוריד את ההקלטה ושואל את ה-AI, בלי להחזיק את ימות ממתינים"""
    try:
        pending["result"] = ask_ai(persona, history, ext, file_name)
    except Exception as e:
        print("worker error:", e)
        pending["result"] = ("", "סליחה, יש בעיה זמנית. נסה שוב.")
    finally:
        pending["done"] = True


def cleanup_loop():
    """מנקה מהזיכרון שיחות ישנות שלא הסתיימו כראוי"""
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


def menu(state, name, prefix=None):
    state["n"] += 1
    state["wait"] = "choice_%d" % state["n"]
    state["stage"] = "menu"
    read = build_read(
        [("text",
          "שלום %s. הקש 1 לעוזר כללי. הקש 2 לעוזר תורני. הקש 3 לעוזר חוצפן. "
          "הקש 4 לעוזר יצירתי. הקש 5 לעוזר טכני. הקש 6 לערס. הקש 7 למומחה למוזיקה. הקש 9 לסיום." % name)],
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
    """בקשת הקלטה מהמתקשר (חינם, במקום זיהוי דיבור שעולה יחידות)"""
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
    """הקשבה למתקשר. התשובה של ה-AI (prefix) מושמעת כהודעה של ההקלטה עצמה,
    כך שלא נאמר כל פעם מחדש להקיש סולמית - רק בכניסה לעוזר."""
    state["stage"] = "chat"
    if first:
        return record(state, "speech", (prefix + ". " if prefix else "") + "דבר אחרי הצפצוף, ובסיום הקש סולמית")
    return record(state, "speech", prefix or "אני מקשיב")


def wait_poll(state):
    """משמיע 'עוד רגע' וחוזר לשרת מיד כדי לבדוק אם התשובה מוכנה"""
    state["n"] += 1
    state["wait"] = "wait_%d" % state["n"]
    state["stage"] = "wait"
    filler = WAIT_FILLERS[state["polls"] % len(WAIT_FILLERS)]
    return build_read(
        [("text", filler)],
        mode="tap",
        val_name=state["wait"],
        max_digits=1,
        min_digits=1,
        sec_wait=1,
        amount_attempts=1,
        allow_empty="yes",
        empty_val="None",
    )


def goodbye(call_id, name):
    drop_call(call_id)
    return build_combined_action([
        build_id_list_message([("text", "להתראות %s" % name)]),
        build_go_to_folder("hangup"),
    ])


def deliver(state, call_id, phone, name, ext):
    """מסירת התשובה של ה-AI למתקשר (כולל פקודות: תפריט / סיים / החלף קול)"""
    transcript, answer = state["job"]["result"]
    state["job"] = None

    low = transcript.lower()
    if "החלף קול" in low or "תחליף קול" in low or "שנה קול" in low:
        if len(VOICE_EXTS) < 2 or ext not in VOICE_EXTS:
            return text_response(listen(state, prefix="אין קולות נוספים להחלפה"))
        next_ext = VOICE_EXTS[(VOICE_EXTS.index(ext) + 1) % len(VOICE_EXTS)]
        state["resume"] = "chat"
        state["wait"] = None
        return text_response(build_go_to_folder("/" + next_ext))
    if "תפריט" in low or low.strip() == "חזרה":
        return text_response(menu(state, name))
    if low.strip() in ("סיים", "ביי", "להתראות", "סיים.", "ביי.", "להתראות."):
        return text_response(goodbye(call_id, name))

    if transcript:
        state["history"].append({"role": "user", "parts": [{"text": transcript}]})
        state["history"].append({"role": "model", "parts": [{"text": answer}]})
        state["history"] = state["history"][-10:]
        with _lock:
            LOG.append({"time": now_str(), "phone": phone, "name": name,
                        "persona": PERSONA_NAMES.get(state["persona"], ""), "q": transcript, "a": answer})
            del LOG[:-LOG_MAX]
        save_log()

    return text_response(listen(state, prefix=answer))


def text_response(resp):
    return Response(resp, mimetype="text/plain; charset=utf-8")


@app.route("/", methods=["GET", "POST"])
def yemot():
    params = request.values.to_dict()
    call_id = params.get("ApiCallId")

    # פנייה בלי פרטי שיחה (למשל שירות שמשאיר את השרת ער)
    if not call_id:
        return Response("ok", mimetype="text/plain; charset=utf-8")

    if params.get("hangup") == "yes":
        drop_call(call_id)
        return Response("noop", mimetype="text/plain; charset=utf-8")

    # כל שיחה מטופלת בנעילה משלה: בקשות של אותה שיחה לא רצות במקביל, ושיחות שונות לא נוגעות זו בזו
    with get_call_lock(call_id):
        try:
            return handle_call(params, call_id)
        except Exception as e:
            print("handler error:", repr(e))
            st = calls.get(call_id)
            name = names.get(params.get("ApiPhone", ""), "אורח")
            if st is None:
                return text_response(build_combined_action([
                    build_id_list_message([("text", "סליחה, יש תקלה זמנית. נסה להתקשר שוב")]),
                    build_go_to_folder("hangup")]))
            st["job"] = None
            return text_response(menu(st, name, prefix="סליחה, קרתה תקלה קטנה, חוזרים לתפריט"))


def handle_call(params, call_id):
    phone = params.get("ApiPhone", "unknown")
    ext = (params.get("ApiExtension", "") or VOICE_EXTS[0]).strip("/") or VOICE_EXTS[0]
    state = calls.get(call_id)
    if state is None:
        with _lock:
            CALLS.append({"time": now_str(), "phone": phone, "name": names.get(phone, "")})
            del CALLS[:-LOG_MAX]
        save_log()
        state = {"stage": "start", "n": 0, "wait": None, "persona": None, "history": [], "call_id": call_id,
                 "file": None, "resume": None, "job": None, "polls": 0, "last": time.time()}
        with _calls_lock:
            calls[call_id] = state
    state["last"] = time.time()

    has_value = bool(state["wait"]) and state["wait"] in params
    value = (params.get(state["wait"], "") or "").strip() if has_value else ""
    if value == "None":
        value = ""

    name = names.get(phone)

    # ---- חזרה אחרי החלפת קול (הגענו לשלוחה אחרת באותה שיחה) ----
    if state["resume"]:
        mode = state["resume"]
        state["resume"] = None
        if mode == "chat":
            resp = listen(state, prefix="הקול הוחלף")
        else:
            resp = menu(state, name or "אורח", prefix="הקול הוחלף")
        return Response(resp, mimetype="text/plain; charset=utf-8")

    # ---- התחלה: זיהוי או רישום ----
    if state["stage"] == "start":
        if name:
            resp = menu(state, name)
        else:
            state["stage"] = "ask_name"
            resp = record(state, "name", "שלום, זו הפעם הראשונה שלך בקו. אמור את שמך הפרטי, ובסיום הקש סולמית")
        return Response(resp, mimetype="text/plain; charset=utf-8")

    # ---- קבלת השם ----
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

    # ---- תפריט ----
    if state["stage"] == "menu":
        if value == "9":
            return Response(goodbye(call_id, name), mimetype="text/plain; charset=utf-8")
        if value in PERSONAS:
            state["persona"] = value
            state["history"] = []
            resp = listen(
                state,
                prefix="אתה עכשיו עם %s. אמור החלף קול כדי להחליף את הקול, תפריט כדי לחזור, או סיים כדי לסיים" % PERSONA_NAMES[value],
                first=True,
            )
        else:
            resp = menu(state, name)
        return Response(resp, mimetype="text/plain; charset=utf-8")

    # ---- שיחה עם ה-AI ----
    if state["stage"] == "chat":
        pending = state.get("pending")

        if pending is None:
            if not has_value:
                resp = listen(state, prefix="לא שמעתי אותך")
                return Response(resp, mimetype="text/plain; charset=utf-8")
            if over_limit(phone):
                yemot_delete(ext, state["file"])
                resp = menu(state, name, prefix="הגעת למכסת ההודעות היומית שלך. אפשר לנסות שוב מחר")
                return Response(resp, mimetype="text/plain; charset=utf-8")
            # מתחילים לעבד ברקע ועונים לימות מיד עם הודעת המתנה
            pending = {"done": False, "result": None}
            state["pending"] = pending
            state["wait_i"] = 0
            threading.Thread(target=ai_worker,
                             args=(pending, state["persona"], list(state["history"]), ext, state["file"]),
                             daemon=True).start()
            return Response(wait_message(state), mimetype="text/plain; charset=utf-8")

        if not pending["done"]:
            return Response(wait_message(state), mimetype="text/plain; charset=utf-8")

        # התשובה מוכנה
        state["pending"] = None
        transcript, answer = pending["result"]

        low = transcript.lower()
        if "החלף קול" in low or "תחליף קול" in low or "שנה קול" in low:
            if len(VOICE_EXTS) < 2 or ext not in VOICE_EXTS:
                resp = listen(state, prefix="אין קולות נוספים להחלפה")
                return Response(resp, mimetype="text/plain; charset=utf-8")
            next_ext = VOICE_EXTS[(VOICE_EXTS.index(ext) + 1) % len(VOICE_EXTS)]
            state["resume"] = "chat"
            state["wait"] = None
            return Response(build_go_to_folder("/" + next_ext), mimetype="text/plain; charset=utf-8")
        if "תפריט" in low or low.strip() == "חזרה":
            resp = menu(state, name)
            return Response(resp, mimetype="text/plain; charset=utf-8")
        if low.strip() in ("סיים", "ביי", "להתראות", "סיים.", "ביי.", "להתראות."):
            return Response(goodbye(call_id, name), mimetype="text/plain; charset=utf-8")

        if transcript:
            state["history"].append({"role": "user", "parts": [{"text": transcript}]})
            state["history"].append({"role": "model", "parts": [{"text": answer}]})
            with _lock:
                LOG.append({"time": now_str(), "phone": phone, "name": name,
                            "persona": PERSONA_NAMES.get(state["persona"], ""), "q": transcript, "a": answer})
                del LOG[:-LOG_MAX]
            save_log()
        state["history"] = state["history"][-10:]

        resp = listen(state, prefix=answer)
        return Response(resp, mimetype="text/plain; charset=utf-8")

    # מצב לא צפוי - חזרה לתפריט
    resp = menu(state, name)
    return Response(resp, mimetype="text/plain; charset=utf-8")



# ===================== אתר ניהול =====================

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
        "<p style='color:#c0392b'>%s</p>" % msg if msg else "")
    return Response(page, mimetype="text/html; charset=utf-8")


@app.route("/admin/login", methods=["POST"])
def admin_login():
    key = request.form.get("key", "")
    if not ADMIN_KEY:
        return admin_login_page("לא הוגדרה סיסמה (ADMIN_KEY) בשרת")
    if key != ADMIN_KEY:
        return admin_login_page("סיסמה שגויה")
    resp = Response("", status=302, headers={"Location": "/admin"})
    resp.set_cookie("admin_key", key, max_age=60 * 60 * 24 * 90, httponly=True)
    return resp


@app.route("/admin", methods=["GET"])
def admin():
    if not is_admin():
        return admin_login_page()
    h = html.escape
    filt = request.args.get("phone", "").strip()
    today = now_str()[:10]

    with _lock:
        users = dict(names)
        log = list(LOG)
        calls = list(CALLS)

    calls_today = sum(1 for c in calls if c["time"].startswith(today))
    active = len(calls_state_snapshot())

    out = [ADMIN_CSS, '<div class="wrap"><div class="top"><h1>ניהול הקו</h1>'
           '<a href="/admin">רענון</a></div>']
    out.append('<div class="cards">'
               '<div class="card">משתמשים רשומים<b>%d</b></div>'
               '<div class="card">שיחות היום<b>%d</b></div>'
               '<div class="card">סה"כ שיחות<b>%d</b></div>'
               '<div class="card">שיחות פעילות עכשיו<b>%d</b></div>'
               '<div class="card">הודעות ביומן<b>%d</b></div></div>' % (
                   len(users), calls_today, len(calls), active, len(log)))

    # users
    out.append('<h2>משתמשים רשומים</h2><table><tr><th>שם</th><th>טלפון</th><th>שיחות</th><th>הודעות</th><th>פעולות</th></tr>')
    for phone, name in sorted(users.items(), key=lambda x: x[1]):
        n_calls = sum(1 for c in calls if c["phone"] == phone)
        n_msgs = sum(1 for l in log if l["phone"] == phone)
        out.append('<tr><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>'
                   '<form class="inline" method="post" action="/admin/rename"><input type="hidden" name="phone" value="%s">'
                   '<input type="text" name="name" value="%s"> <button>שנה שם</button></form> '
                   '<form class="inline" method="post" action="/admin/delete" onsubmit="return confirm(\'למחוק את המשתמש? בשיחה הבאה הוא יתבקש להירשם מחדש\')">'
                   '<input type="hidden" name="phone" value="%s"><button class="red">מחק</button></form> '
                   '<a href="/admin?phone=%s">הצג שיחות</a></td></tr>' % (
                       h(name), h(phone), n_calls, n_msgs, h(phone), h(name), h(phone), h(phone)))
    if not users:
        out.append('<tr><td colspan="5">עדיין אין משתמשים רשומים</td></tr>')
    out.append('</table>')

    # log
    shown = [l for l in log if not filt or l["phone"] == filt][::-1][:300]
    out.append('<h2>מה דיברו עם הקו%s</h2>' % (" – " + h(filt) + ' (<a href="/admin">הצג הכל</a>)' if filt else ""))
    out.append('<form class="inline" method="post" action="/admin/clear" onsubmit="return confirm(\'למחוק את כל היומן?\')">'
               '<button class="red">נקה יומן</button></form>')
    out.append('<table><tr><th>זמן</th><th>מי</th><th>עוזר</th><th>מה נאמר</th></tr>')
    for l in shown:
        out.append('<tr><td>%s</td><td>%s<br><small>%s</small></td><td>%s</td>'
                   '<td><div class="q">שאל: %s</div><div class="a">ענה: %s</div></td></tr>' % (
                       h(l["time"]), h(l["name"]), h(l["phone"]), h(l["persona"]), h(l["q"]), h(l["a"])))
    if not shown:
        out.append('<tr><td colspan="4">אין הודעות עדיין</td></tr>')
    out.append('</table>')

    # calls
    out.append('<h2>שיחות אחרונות</h2><table><tr><th>זמן</th><th>טלפון</th><th>שם</th></tr>')
    for c in calls[::-1][:100]:
        out.append('<tr><td>%s</td><td>%s</td><td>%s</td></tr>' % (h(c["time"]), h(c["phone"]), h(users.get(c["phone"], c.get("name", "")) or "לא רשום")))
    out.append('</table>')

    # personas
    out.append('<h2>העוזרים (אפשר לערוך את האופי של כל עוזר)</h2>')
    out.append('<form method="post" action="/admin/personas"><table><tr><th style="width:40px">מס</th><th style="width:180px">שם העוזר</th><th>ההנחיה ל-AI</th></tr>')
    for k in sorted(PERSONAS):
        base = PERSONAS[k].replace(GENERAL_RULES, "")
        out.append('<tr><td>%s</td><td><input type="text" name="name_%s" value="%s"></td>'
                   '<td><textarea name="prompt_%s">%s</textarea></td></tr>' % (k, k, h(PERSONA_NAMES[k]), k, h(base)))
    out.append('</table><button>שמור עוזרים</button></form>')
    out.append('<p><small>הכללים הקבועים (עברית, קצר, טלפון, שפה מכובדת) מתווספים אוטומטית לכל עוזר.</small></p>')
    out.append('<h2>הגדרות</h2><form method="post" action="/admin/settings"><table>'
               '<tr><th style="width:260px">הגדרה</th><th>ערך</th></tr>'
               '<tr><td>הודעות ליום לכל משתמש (0 = בלי הגבלה)</td><td><input type="text" name="daily_limit" value="%d"></td></tr>'
               '<tr><td>מספרים ללא הגבלה (מופרדים בפסיק)</td><td><input type="text" name="unlimited_phones" value="%s" style="width:320px"></td></tr>'
               '<tr><td>שעת שליחת הסיכום היומי למייל (0-23)</td><td><input type="text" name="mail_hour" value="%d"></td></tr>'
               '</table><button>שמור הגדרות</button></form>' % (
                   SETTINGS["daily_limit"], h(SETTINGS["unlimited_phones"]), SETTINGS["mail_hour"]))
    mail_state = ("מוגדר, נשלח אל " + h(MAIL_TO)) if (MAIL_USER and MAIL_PASS) else "לא מוגדר (צריך MAIL_USER ו-MAIL_PASS ב-Render)"
    out.append('<p>סיכום יומי למייל: %s &nbsp; '
               '<form class="inline" method="post" action="/admin/sendmail"><button>שלח סיכום של היום עכשיו</button></form> %s</p>' % (
                   mail_state, "<b style='color:#1a5fb4'>%s</b>" % h(request.args.get("mail", "")) if request.args.get("mail") else ""))
    out.append('<p><a href="/admin/logout">יציאה</a></p></div>')
    return Response("".join(out), mimetype="text/html; charset=utf-8")


def calls_state_snapshot():
    with _calls_lock:
        return dict(calls)


@app.route("/admin/rename", methods=["POST"])
def admin_rename():
    if not is_admin():
        return admin_login_page()
    phone = request.form.get("phone", "")
    name = clean_for_tts(request.form.get("name", ""))[:30]
    if phone in names and name:
        names[phone] = name
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
    data = json.dumps({"names": PERSONA_NAMES, "prompts": {k: PERSONAS[k].replace(GENERAL_RULES, "") for k in PERSONAS}}, ensure_ascii=False)
    threading.Thread(target=yemot_write_text, args=("ai_personas.txt", data), daemon=True).start()
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
    resp.set_cookie("admin_key", "", max_age=0)
    return resp


def load_personas():
    if not YEMOT_TOKEN:
        return
    try:
        t = yemot_read_text("ai_personas.txt")
        if t:
            d = json.loads(t)
            for k, v in d.get("names", {}).items():
                if k in PERSONA_NAMES:
                    PERSONA_NAMES[k] = v
            for k, v in d.get("prompts", {}).items():
                if k in PERSONAS:
                    PERSONAS[k] = v + GENERAL_RULES
    except Exception as e:
        print("load personas error:", e)


load_personas()


if __name__ == "__main__":
    app.run()
