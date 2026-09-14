from flask import Flask, request, Response
from yemot_flow.actions import build_id_list_message, build_read, build_go_to_folder, build_combined_action
from google import genai
from google.genai import types
from google.cloud import tts_v1
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
from concurrent.futures import ThreadPoolExecutor
import hashlib

app = Flask(__name__)

# ============= קונפיגורציה ={
names = {}
LOG = []
CALLS = []
LOG_MAX = 500
_lock = threading.Lock()
calls = {}

# ============= מודלים וקולות ={
MODELS_DICT = {
    "1": {"name": "Gemini Flash (מהיר וחזק)", "models": ["gemini-3.1-flash-lite", "gemini-3-flash"]},
    "2": {"name": "Gemini Pro (חכם יותר)", "models": ["gemini-3-flash"]},
    "3": {"name": "Gemini חיסכון (זול)", "models": ["gemini-2.5-flash-lite"]},
}

VOICE_STYLES = {
    "1": {"name": "קול רגיל", "voice_id": "he-IL-Standard-A"},
    "2": {"name": "קול נשי", "voice_id": "he-IL-Standard-B"},
    "3": {"name": "קול גברי", "voice_id": "he-IL-Standard-C"},
}

RESPONSE_CACHE = {}
executor = ThreadPoolExecutor(max_workers=3)

GENERAL_RULES = (
    " אתה מדבר בטלפון, לכן ענה קצר וברור, בלי כוכביות, בלי רשימות, בלי אימוג'ים ובלי סימני עיצוב."
    " ענה בשפה שבה המשתמש דיבר אליך; ברירת המחדל היא עברית."
    " שמור על שפה מכובדת וצנועה, ואל תעסוק בנושאים לא צנועים."
    " **חשוב מאוד: כשאתה מספק מספרים או מחירים, כתוב אותם במילים עברית (\"שתיים מאות שלושים וחמישה שקל\" ולא \"235\").**"
)

PERSONAS = {
    "1": "אתה עוזר כללי ידידותי ומועיל." + GENERAL_RULES,
    "2": "אתה עוזר תורני. ענה בסגנון תורני מכובד, וציין מקורות כשאפשר." + GENERAL_RULES,
    "3": "אתה עוזר חוצפני וסרקסטי עם הומור. ענה בחוצפה משעשעת אבל בלי להעליב באמת." + GENERAL_RULES,
    "4": "אתה עוזר יצירתי. ספר סיפורים קצרים, כתוב שירים, בדיחות ורעיונות יצירתיים." + GENERAL_RULES,
    "5": "אתה עוזר טכני. הסבר דברים טכניים בפשטות: מחשבים, אינטרנט, טלפונים." + GENERAL_RULES,
    "6": ("אתה ערס - ידיד קרוב וחמוד. תדבר בנימוס קלוקל מאוד. "
          "השתמש בביטויים כמו 'אחלה מה אחי', 'ספר לי', 'בואנו', 'כאן בדיוק', 'מאשימה'. "
          "תרגיש כמו ישיבה עם חבר טוב בבר. פתוח, כיפי ותמיד עם חיוך." ) + GENERAL_RULES,
}

PERSONA_NAMES = {
    "1": "העוזר הכללי",
    "2": "העוזר התורני",
    "3": "העוזר החוצפן",
    "4": "העוזר היצירתי",
    "5": "העוזר הטכני",
    "6": "הערס",
}

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "")
VOICE_EXTS = [e.strip().strip("/") for e in os.environ.get("VOICE_EXTS", "1").split(",") if e.strip()]
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
DATA_EXT = VOICE_EXTS[0]

MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_PASS = os.environ.get("MAIL_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "") or MAIL_USER

OWNER_PHONES = ["0527661756", "0527609296"]

SETTINGS = {
    "daily_limit": int(os.environ.get("DAILY_LIMIT", "40")),
    "unlimited_phones": "0527661756,0527609296",
    "mail_hour": 21,
}

YEMOT_API = "https://www.call2all.co.il/ym/api/"

_client = None
_tts_client = None

def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
    return _client

def get_tts_client():
    global _tts_client
    if _tts_client is None:
        _tts_client = tts_v1.TextToSpeechClient()
    return _tts_client

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

def clean_for_tts(text):
    """ניקיון להשמעה - שמור על מספרים"""
    text = re.sub(r"[*_#`>\[\]{}]", "", text)
    text = text.replace("\n", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    
    if any(c.isdigit() for c in text):
        return text[:900]
    return text[:600]

def yemot_download(ext, file_name):
    """הורדת הקלטה מהמערכת של ימות המשיח"""
    url = YEMOT_API + "DownloadFile?" + urllib.parse.urlencode({
        "token": YEMOT_TOKEN,
        "path": "ivr2:/%s/%s.wav" % (ext, file_name),
    })
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()

def yemot_delete(ext, file_name):
    """מחיקת ההקלטה אחרי השימוש"""
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
    """קריאת קובץ טקסט מהמערכת של ימות"""
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
        data = json.dumps({"log": LOG[-500:], "calls": CALLS[-500:]}, ensure_ascii=False)
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

def build_summary(day):
    """סיכום של יום אחד (טקסט HTML)"""
    h = html.escape
    with _lock:
        log = [l for l in LOG if l["time"].startswith(day)]
        calls_list = [c for c in CALLS if c["time"].startswith(day)]
        users = dict(names)
    phones = sorted(set(c["phone"] for c in calls_list) | set(l["phone"] for l in log))
    out = ["<div dir='rtl' style='font-family:Arial'>",
           "<h2>סיכום הקו ליום %s</h2>" % h(day),
           "<p>שיחות: <b>%d</b> &nbsp; מתקשרים שונים: <b>%d</b> &nbsp; הודעות ל-AI: <b>%d</b></p>" % (
               len(calls_list), len(phones), len(log))]
    if phones:
        out.append("<h3>לפי מתקשר</h3><ul>")
        for ph in phones:
            nm = users.get(ph, "לא רשום")
            out.append("<li>%s (%s): %d שיחות, %d הודעות</li>" % (
                h(nm), h(ph), sum(1 for c in calls_list if c["phone"] == ph), sum(1 for l in log if l["phone"] == ph)))
        out.append("</ul>")
    if log:
        out.append("<h3>מה שאלו</h3><table border='1' cellpadding='5' style='border-collapse:collapse'>"
                   "<tr><th>שעה</th><th>מי</th><th>עוזר</th><th>מודל</th><th>שאלה</th></tr>")
        for l in log[:150]:
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td><div class='q'>%s</div><div class='a'>%s</div></td></tr>" % (
                h(l["time"][11:]), h(l["name"]), h(l["persona"]), h(l.get("model", "Gemini")), h(l["q"][:100]), h(l["a"][:100])))
        out.append("</table>")
        if len(log) > 150:
            out.append("<p>...ועוד %d הודעות</p>" % (len(log) - 150))
    else:
        out.append("<p>לא היו הודעות היום.</p>")
    out.append("</div>")
    return "".join(out)

def send_mail(subject, body_html):
    if not (MAIL_USER and MAIL_PASS and MAIL_TO):
        return "לא הוגדר מייל"
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
        return "שגיאה: %s" % e

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

def gemini(system, contents, schema=None, model_choice="1"):
    """קריאה ל-Gemini API עם בחירת מודל"""
    models = MODELS_DICT.get(model_choice, MODELS_DICT["1"])["models"]
    last_error = None
    
    for model in models:
        try:
            if schema:
                cfg = types.GenerateContentConfig(
                    system_instruction=system, max_output_tokens=500,
                    response_mime_type="application/json", response_schema=schema,
                )
            else:
                cfg = types.GenerateContentConfig(system_instruction=system, max_output_tokens=500)
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

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "transcript": {"type": "STRING"},
        "answer": {"type": "STRING"},
    },
    "required": ["transcript", "answer"],
}

def ask_ai(persona, history, ext, file_name, model_choice="1"):
    """מוריד את ההקלטה, מתמלל ועונה בקריאה אחת"""
    try:
        audio = yemot_download(ext, file_name)
    except Exception as e:
        print("download error:", e)
        return "", "סליחה, לא הצלחתי לשמוע את ההקלטה. נסה שוב."
    yemot_delete(ext, file_name)

    now = il_now()
    current_time = now.strftime("%H:%M")
    current_date = now.strftime("%d/%m/%Y")

    system = PERSONAS.get(persona, PERSONAS["1"]) + (
        f"\n[זמן בישראל: {current_time}, תאריך: {current_date}] "
        "תקבל הקלטה של מה שהמשתמש אמר עכשיו. החזר JSON עם שני שדות:"
        " transcript - תמלול מדויק של ההקלטה, answer - התשובה שלך למשתמש."
    )
    contents = list(history) + [{
        "role": "user",
        "parts": [
            {"text": "ההקלטה של המשתמש:"},
            types.Part.from_bytes(data=audio, mime_type="audio/wav"),
        ],
    }]
    
    raw = gemini(system, contents, schema=SCHEMA, model_choice=model_choice)
    if not raw:
        return "", "סליחה, יש בעיה זמנית. נסה שוב."
    try:
        data = json.loads(raw)
        transcript = (data.get("transcript") or "").strip()
        answer = (data.get("answer") or "").strip()
    except Exception:
        transcript, answer = "", raw
    if not answer:
        answer = "לא הבנתי, אפשר לחזור על זה?"
    
    return transcript, clean_for_tts(answer)

def menu_model_choice(state, name):
    """תפריט בחירת מודל AI"""
    state["n"] += 1
    state["wait"] = "model_%d" % state["n"]
    state["stage"] = "model_choice"
    read = build_read(
        [("text",
          "שלום %s. בחר מודל. הקש 1 לג'ימיני פלש. הקש 2 לג'ימיני פרו. הקש 3 לג'ימיני חיסכון. הקש 9 לסיום." % name)],
        mode="tap",
        val_name=state["wait"],
        max_digits=1,
        min_digits=1,
        digits_allowed="123459",
        sec_wait=10,
    )
    return read

def menu_voice_choice(state, name):
    """תפריט בחירת קול"""
    state["n"] += 1
    state["wait"] = "voice_%d" % state["n"]
    state["stage"] = "voice_choice"
    read = build_read(
        [("text",
          "בחר קול. הקש 1 לקול רגיל. הקש 2 לקול נשי. הקש 3 לקול גברי. הקש 0 לדלג." )],
        mode="tap",
        val_name=state["wait"],
        max_digits=1,
        min_digits=1,
        digits_allowed="0123",
        sec_wait=10,
    )
    return read

def menu(state, name, prefix=None):
    """תפריט העוזרים"""
    state["n"] += 1
    state["wait"] = "choice_%d" % state["n"]
    state["stage"] = "menu"
    read = build_read(
        [("text",
          "בחר עוזר. הקש 1 כללי. 2 תורני. 3 חוצפן. 4 יצירתי. 5 טכני. 6 ערס. 9 סיום.")],
        mode="tap",
        val_name=state["wait"],
        max_digits=1,
        min_digits=1,
        digits_allowed="123456789",
        sec_wait=10,
    )
    if prefix:
        return build_combined_action([build_id_list_message([("text", prefix)]), read])
    return read

def record(state, val_prefix, prompt, prefix=None):
    """בקשת הקלטה מהמתקשר"""
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
    """הקשבה למתקשר"""
    state["stage"] = "chat"
    if first:
        return record(state, "speech", (prefix + ". " if prefix else "") + "דבר אחרי הצפצוף, ובסיום הקש סולמית")
    return record(state, "speech", prefix or "אני מקשיב")

def goodbye(call_id, name):
    calls.pop(call_id, None)
    return build_combined_action([
        build_id_list_message([("text", "להתראות %s" % name)]),
        build_go_to_folder("hangup"),
    ])

@app.route("/", methods=["GET", "POST"])
def yemot():
    params = request.values.to_dict()
    call_id = params.get("ApiCallId")

    if not call_id:
        return Response("ok", mimetype="text/plain; charset=utf-8")

    if params.get("hangup") == "yes":
        calls.pop(call_id, None)
        return Response("noop", mimetype="text/plain; charset=utf-8")

    phone = params.get("ApiPhone", "unknown")
    ext = (params.get("ApiExtension", "") or VOICE_EXTS[0]).strip("/") or VOICE_EXTS[0]
    state = calls.get(call_id)
    
    if state is None:
        with _lock:
            CALLS.append({"time": now_str(), "phone": phone, "name": names.get(phone, "")})
            del CALLS[:-LOG_MAX]
        save_log()
        state = {
            "stage": "start", 
            "n": 0, 
            "wait": None, 
            "persona": None, 
            "history": [], 
            "call_id": call_id, 
            "file": None, 
            "resume": None,
            "model_choice": "1",
            "voice_id": "he-IL-Standard-A",
        }
        calls[call_id] = state

    has_value = bool(state["wait"]) and state["wait"] in params
    value = (params.get(state["wait"], "") or "").strip() if has_value else ""
    if value == "None":
        value = ""

    name = names.get(phone)

    if state["resume"]:
        mode = state["resume"]
        state["resume"] = None
        if mode == "chat":
            resp = listen(state, prefix="הקול הוחלף")
        else:
            resp = menu(state, name or "אורח", prefix="הקול הוחלף")
        return Response(resp, mimetype="text/plain; charset=utf-8")

    if state["stage"] == "start":
        if name:
            resp = menu_model_choice(state, name)
        else:
            state["stage"] = "ask_name"
            resp = record(state, "name", "שלום, זו הפעם הראשונה שלך בקו. אמור את שמך הפרטי, ובסיום הקש סולמית")
        return Response(resp, mimetype="text/plain; charset=utf-8")

    if state["stage"] == "model_choice":
        if not has_value:
            resp = menu_model_choice(state, name or "אורח")
            return Response(resp, mimetype="text/plain; charset=utf-8")
        if value == "9":
            return Response(goodbye(call_id, name or "אורח"), mimetype="text/plain; charset=utf-8")
        if value in MODELS_DICT:
            state["model_choice"] = value
            resp = menu_voice_choice(state, name or "אורח")
        else:
            resp = menu_model_choice(state, name or "אורח")
        return Response(resp, mimetype="text/plain; charset=utf-8")

    if state["stage"] == "voice_choice":
        if not has_value:
            resp = menu_voice_choice(state, name or "אורח")
            return Response(resp, mimetype="text/plain; charset=utf-8")
        if value in VOICE_STYLES:
            state["voice_id"] = VOICE_STYLES[value]["voice_id"]
        resp = menu(state, name or "אורח", prefix="בחרת: %s וקול: %s" % (
            MODELS_DICT.get(state["model_choice"], MODELS_DICT["1"])["name"],
            VOICE_STYLES.get(value, VOICE_STYLES["1"])["name"]
        ))
        return Response(resp, mimetype="text/plain; charset=utf-8")

    if state["stage"] == "ask_name":
        if not has_value:
            resp = record(state, "name", "אמור את שמך הפרטי, ובסיום הקש סולמית")
            return Response(resp, mimetype="text/plain; charset=utf-8")
        name = transcribe_name(ext, state["file"]) or "אורח"
        names[phone] = name
        save_names()
        resp = menu_model_choice(state, name)
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
                prefix="עכשיו עם %s. אמור החלף קול להחלפה, תפריט לחזרה, סיים לסיום." % PERSONA_NAMES[value],
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
            resp = menu(state, name, prefix="הגעת למכסת ההודעות. נסה מחר")
            return Response(resp, mimetype="text/plain; charset=utf-8")

        transcript, answer = ask_ai(state["persona"], state["history"], ext, state["file"], state["model_choice"])

        low = transcript.lower()
        if "החלף קול" in low or "תחליף קול" in low or "שנה קול" in low:
            if len(VOICE_EXTS) < 2 or ext not in VOICE_EXTS:
                resp = listen(state, prefix="אין קולות נוספים")
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
            if len(state["history"]) > 20:
                summary = "סיכום: " + " ".join([h.get("parts", [{}])[0].get("text", "")[:50] for h in state["history"][:10]])
                state["history"] = [{"role": "system", "parts": [{"text": summary}]}] + state["history"][-5:]
            
            state["history"].append({"role": "user", "parts": [{"text": transcript}]})
            state["history"].append({"role": "model", "parts": [{"text": answer}]})
            
            with _lock:
                LOG.append({
                    "time": now_str(),
                    "phone": phone,
                    "name": name,
                    "persona": PERSONA_NAMES.get(state["persona"], ""),
                    "model": MODELS_DICT.get(state["model_choice"], {}).get("name", ""),
                    "q": transcript,
                    "a": answer
                })
                del LOG[:-LOG_MAX]
            save_log()
        state["history"] = state["history"][-10:]

        resp = listen(state, prefix=answer)
        return Response(resp, mimetype="text/plain; charset=utf-8")

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
        return admin_login_page("לא הוגדרה סיסמה")
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
        calls_list = list(CALLS)

    calls_today = sum(1 for c in calls_list if c["time"].startswith(today))
    active = len(calls_state_snapshot())

    out = [ADMIN_CSS, '<div class="wrap"><div class="top"><h1>ניהול הקו</h1>'
           '<a href="/admin">רענון</a></div>']
    out.append('<div class="cards">'
               '<div class="card">משתמשים רשומים<b>%d</b></div>'
               '<div class="card">שיחות היום<b>%d</b></div>'
               '<div class="card">סה"כ שיחות<b>%d</b></div>'
               '<div class="card">שיחות פעילות<b>%d</b></div>'
               '<div class="card">הודעות ביומן<b>%d</b></div></div>' % (
                   len(users), calls_today, len(calls_list), active, len(log)))

    out.append('<h2>משתמשים רשומים</h2><table><tr><th>שם</th><th>טלפון</th><th>שיחות</th><th>הודעות</th><th>פעולות</th></tr>')
    for phone, user_name in sorted(users.items(), key=lambda x: x[1]):
        n_calls = sum(1 for c in calls_list if c["phone"] == phone)
        n_msgs = sum(1 for l in log if l["phone"] == phone)
        out.append('<tr><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>'
                   '<form class="inline" method="post" action="/admin/rename"><input type="hidden" name="phone" value="%s">'
                   '<input type="text" name="name" value="%s"> <button>שנה</button></form> '
                   '<form class="inline" method="post" action="/admin/delete" onsubmit="return confirm(\'מחק?\')">'
                   '<input type="hidden" name="phone" value="%s"><button class="red">מחק</button></form> '
                   '<a href="/admin?phone=%s">שיחות</a></td></tr>' % (
                       h(user_name), h(phone), n_calls, n_msgs, h(phone), h(user_name), h(phone), h(phone)))
    if not users:
        out.append('<tr><td colspan="5">אין משתמשים עדיין</td></tr>')
    out.append('</table>')

    shown = [l for l in log if not filt or l["phone"] == filt][::-1][:300]
    out.append('<h2>שיחות%s</h2>' % (" – " + h(filt) if filt else ""))
    out.append('<form class="inline" method="post" action="/admin/clear" onsubmit="return confirm(\'למחוק הכל?\')">'
               '<button class="red">נקה</button></form>')
    out.append('<table><tr><th>זמן</th><th>מי</th><th>עוזר</th><th>מודל</th><th>שיחה</th></tr>')
    for l in shown:
        out.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                   '<td><div class="q">שאל: %s</div><div class="a">תשובה: %s</div></td></tr>' % (
                       h(l["time"]), h(l["name"]), h(l["persona"]), h(l.get("model", "Gemini")), h(l["q"][:50]), h(l["a"][:100])))
    if not shown:
        out.append('<tr><td colspan="5">אין הודעות</td></tr>')
    out.append('</table>')

    out.append('<h2>העוזרים</h2><form method="post" action="/admin/personas"><table><tr><th>מס</th><th>שם</th><th>הנחיה</th></tr>')
    for k in sorted(PERSONAS):
        base = PERSONAS[k].replace(GENERAL_RULES, "")
        out.append('<tr><td>%s</td><td><input type="text" name="name_%s" value="%s" style="width:150px"></td>'
                   '<td><textarea name="prompt_%s">%s</textarea></td></tr>' % (k, k, h(PERSONA_NAMES[k]), k, h(base)))
    out.append('</table><button>שמור</button></form>')

    out.append('<h2>הגדרות</h2><form method="post" action="/admin/settings"><table>'
               '<tr><td>הודעות ליום</td><td><input type="text" name="daily_limit" value="%d"></td></tr>'
               '<tr><td>ללא הגבלה (פסיקים)</td><td><input type="text" name="unlimited_phones" value="%s" style="width:300px"></td></tr>'
               '<tr><td>שעת מייל (0-23)</td><td><input type="text" name="mail_hour" value="%d"></td></tr>'
               '</table><button>שמור</button></form>' % (
                   SETTINGS["daily_limit"], h(SETTINGS["unlimited_phones"]), SETTINGS["mail_hour"]))
    
    out.append('<p><a href="/admin/logout">יציאה</a></p></div>')
    return Response("".join(out), mimetype="text/html; charset=utf-8")

def calls_state_snapshot():
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

@app.route("/admin/logout")
def admin_logout():
    resp = Response("", status=302, headers={"Location": "/admin"})
    resp.set_cookie("admin_key", "", max_age=0)
    return resp

if __name__ == "__main__":
    app.run()
