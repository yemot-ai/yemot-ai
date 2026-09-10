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

# מצב של כל שיחה פעילה (נמחק בסיום השיחה - אין היסטוריה)
calls = {}

GENERAL_RULES = (
    " אתה מדבר בטלפון, לכן ענה קצר וברור, בלי כוכביות, בלי רשימות, בלי אימוג'ים ובלי סימני עיצוב."
    " ענה בשפה שבה המשתמש דיבר אליך; ברירת המחדל היא עברית."
    " שמור על שפה מכובדת וצנועה, ואל תעסוק בנושאים לא צנועים."
    " אם שואלים מי יצר את הקו או אותך, ענה שהקו נוצר על ידי וואי בי וואי."
)

CREDIT = "הקו נוצר על ידי וואי בי וואי"

PERSONAS = {
    "1": "אתה עוזר כללי ידידותי ומועיל." + GENERAL_RULES,
    "2": "אתה עוזר תורני. ענה בסגנון תורני מכובד, וציין מקורות כשאפשר." + GENERAL_RULES,
    "3": "אתה עוזר חוצפני וסרקסטי עם הומור. ענה בחוצפה משעשעת אבל בלי להעליב באמת." + GENERAL_RULES,
    "4": "אתה עוזר יצירתי. ספר סיפורים קצרים, כתוב שירים, בדיחות ורעיונות יצירתיים." + GENERAL_RULES,
    "5": "אתה עוזר טכני. הסבר דברים טכניים בפשטות: מחשבים, אינטרנט, טלפונים." + GENERAL_RULES,
}

PERSONA_NAMES = {
    "1": "העוזר הכללי",
    "2": "העוזר התורני",
    "3": "העוזר החוצפן",
    "4": "העוזר היצירתי",
    "5": "העוזר הטכני",
}

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "")          # מספר המערכת:סיסמה
# רשימת השלוחות של הקו, כל אחת עם קול אחר (מוגדר ב-ext.ini של השלוחה בימות). מופרד בפסיקים.
VOICE_EXTS = [e.strip().strip("/") for e in os.environ.get("VOICE_EXTS", "1").split(",") if e.strip()]
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")                # סיסמה לאתר הניהול
DATA_EXT = VOICE_EXTS[0]                                   # השלוחה שבה נשמרים קבצי הנתונים
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


def now_str():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%d/%m/%Y %H:%M")


def gemini(system, contents, schema=None):
    last_error = None
    for model in MODELS:
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


def ask_ai(persona, history, ext, file_name):
    """מוריד את ההקלטה, מתמלל ועונה בקריאה אחת. מחזיר (תמלול, תשובה)"""
    try:
        audio = yemot_download(ext, file_name)
    except Exception as e:
        print("download error:", e)
        return "", "סליחה, לא הצלחתי לשמוע את ההקלטה. נסה שוב."
    yemot_delete(ext, file_name)

    system = PERSONAS.get(persona, PERSONAS["1"]) + (
        " תקבל הקלטה של מה שהמשתמש אמר עכשיו. החזר JSON עם שני שדות:"
        " transcript - תמלול מדויק של ההקלטה, answer - התשובה שלך למשתמש."
    )
    contents = list(history) + [{
        "role": "user",
        "parts": [
            {"text": "ההקלטה של המשתמש:"},
            types.Part.from_bytes(data=audio, mime_type="audio/wav"),
        ],
    }]
    raw = gemini(system, contents, schema=SCHEMA)
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


def menu(state, name, prefix=None):
    state["n"] += 1
    state["wait"] = "choice_%d" % state["n"]
    state["stage"] = "menu"
    read = build_read(
        [("text",
          "שלום %s. הקש 1 לעוזר כללי. הקש 2 לעוזר תורני. הקש 3 לעוזר חוצפן. "
          "הקש 4 לעוזר יצירתי. הקש 5 לעוזר טכני. הקש 9 לסיום." % name)],
        mode="tap",
        val_name=state["wait"],
        max_digits=1,
        min_digits=1,
        digits_allowed="123459",
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

    # פנייה בלי פרטי שיחה (למשל שירות שמשאיר את השרת ער)
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
        state = {"stage": "start", "n": 0, "wait": None, "persona": None, "history": [], "call_id": call_id, "file": None, "resume": None}
        calls[call_id] = state

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
            resp = menu(state, name, prefix=CREDIT)
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
        resp = menu(state, name, prefix="נעים להכיר %s, השם נשמר. %s" % (name, CREDIT))
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
        if not has_value:
            resp = listen(state, prefix="לא שמעתי אותך")
            return Response(resp, mimetype="text/plain; charset=utf-8")

        transcript, answer = ask_ai(state["persona"], state["history"], ext, state["file"])

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
    data = json.dumps({"names": PERSONA_NAMES, "prompts": {k: PERSONAS[k].replace(GENERAL_RULES, "") for k in PERSONAS}}, ensure_ascii=False)
    threading.Thread(target=yemot_write_text, args=("ai_personas.txt", data), daemon=True).start()
    return Response("", status=302, headers={"Location": "/admin"})


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
