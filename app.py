from flask import Flask, request, Response
from yemot_flow.actions import build_id_list_message, build_read, build_go_to_folder, build_combined_action
from google import genai
from google.genai import types
import os
import re
import json
import urllib.request
import urllib.parse

app = Flask(__name__)

# שמות לפי מספר טלפון (נשמר כל עוד השרת ער)
names = {}

# מצב של כל שיחה פעילה (נמחק בסיום השיחה - אין היסטוריה)
calls = {}

GENERAL_RULES = (
    " אתה מדבר בטלפון, לכן ענה קצר וברור, בלי כוכביות, בלי רשימות, בלי אימוג'ים ובלי סימני עיצוב."
    " ענה בשפה שבה המשתמש דיבר אליך; ברירת המחדל היא עברית."
    " שמור על שפה מכובדת וצנועה, ואל תעסוק בנושאים לא צנועים."
)

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
YEMOT_EXT = os.environ.get("YEMOT_EXT", "1").strip("/")   # השלוחה שבה מוגדר ה-API
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


def yemot_download(file_name):
    """הורדת הקלטה מהמערכת של ימות המשיח"""
    url = YEMOT_API + "DownloadFile?" + urllib.parse.urlencode({
        "token": YEMOT_TOKEN,
        "path": "ivr2:/%s/%s.wav" % (YEMOT_EXT, file_name),
    })
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()


def yemot_delete(file_name):
    """מחיקת ההקלטה אחרי השימוש (לא קריטי אם נכשל)"""
    try:
        url = YEMOT_API + "FileAction?" + urllib.parse.urlencode({
            "token": YEMOT_TOKEN,
            "action": "delete",
            "what": "ivr2:/%s/%s.wav" % (YEMOT_EXT, file_name),
        })
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as e:
        print("delete error:", e)


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


def transcribe_name(file_name):
    """המרת הקלטת השם לטקסט"""
    try:
        audio = yemot_download(file_name)
    except Exception as e:
        print("download error:", e)
        return ""
    yemot_delete(file_name)
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


def ask_ai(persona, history, file_name):
    """מוריד את ההקלטה, מתמלל ועונה בקריאה אחת. מחזיר (תמלול, תשובה)"""
    try:
        audio = yemot_download(file_name)
    except Exception as e:
        print("download error:", e)
        return "", "סליחה, לא הצלחתי לשמוע את ההקלטה. נסה שוב."
    yemot_delete(file_name)

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


def listen(state, prefix=None):
    state["stage"] = "chat"
    return record(state, "speech", "דבר עכשיו, ובסיום הקש סולמית", prefix=prefix)


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
    state = calls.get(call_id)
    if state is None:
        state = {"stage": "start", "n": 0, "wait": None, "persona": None, "history": [], "call_id": call_id, "file": None}
        calls[call_id] = state

    has_value = bool(state["wait"]) and state["wait"] in params
    value = (params.get(state["wait"], "") or "").strip() if has_value else ""
    if value == "None":
        value = ""

    name = names.get(phone)

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
        name = transcribe_name(state["file"]) or "אורח"
        names[phone] = name
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
                prefix="אתה עכשיו עם %s. דבר אחרי הצפצוף. אמור תפריט כדי לחזור, או סיים כדי לסיים." % PERSONA_NAMES[value],
            )
        else:
            resp = menu(state, name)
        return Response(resp, mimetype="text/plain; charset=utf-8")

    # ---- שיחה עם ה-AI ----
    if state["stage"] == "chat":
        if not has_value:
            resp = listen(state, prefix="לא שמעתי אותך")
            return Response(resp, mimetype="text/plain; charset=utf-8")

        transcript, answer = ask_ai(state["persona"], state["history"], state["file"])

        low = transcript.lower()
        if "תפריט" in low or low.strip() == "חזרה":
            resp = menu(state, name)
            return Response(resp, mimetype="text/plain; charset=utf-8")
        if low.strip() in ("סיים", "ביי", "להתראות", "סיים.", "ביי.", "להתראות."):
            return Response(goodbye(call_id, name), mimetype="text/plain; charset=utf-8")

        if transcript:
            state["history"].append({"role": "user", "parts": [{"text": transcript}]})
            state["history"].append({"role": "model", "parts": [{"text": answer}]})
        state["history"] = state["history"][-10:]

        resp = listen(state, prefix=answer)
        return Response(resp, mimetype="text/plain; charset=utf-8")

    # מצב לא צפוי - חזרה לתפריט
    resp = menu(state, name)
    return Response(resp, mimetype="text/plain; charset=utf-8")


if __name__ == "__main__":
    app.run()
