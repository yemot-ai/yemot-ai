from flask import Flask, request, Response
from yemot_flow.actions import build_id_list_message, build_read, build_go_to_folder, build_combined_action
from google import genai
from google.genai import types
import os
import re

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


def ask_ai(persona, history):
    system = PERSONAS.get(persona, PERSONAS["1"])
    last_error = None
    for model in MODELS:
        try:
            response = get_client().models.generate_content(
                model=model,
                contents=history,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=400,
                ),
            )
            if response.text:
                return clean_for_tts(response.text)
        except Exception as e:
            last_error = e
            continue
    print("Gemini error:", last_error)
    return "סליחה, יש בעיה זמנית. נסה שוב."


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


def listen(state, prefix=None):
    state["n"] += 1
    state["wait"] = "speech_%d" % state["n"]
    state["stage"] = "chat"
    read = build_read(
        [("text", "אני מקשיב")],
        mode="stt",
        val_name=state["wait"],
        lang="he-IL",
    )
    if prefix:
        return build_combined_action([build_id_list_message([("text", prefix)]), read])
    return read


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
        state = {"stage": "start", "n": 0, "wait": None, "persona": None, "history": []}
        calls[call_id] = state

    value = params.get(state["wait"], "") if state["wait"] else ""
    value = (value or "").strip()
    if value == "None":
        value = ""

    name = names.get(phone)

    # ---- התחלה: זיהוי או רישום ----
    if state["stage"] == "start":
        if name:
            resp = menu(state, name)
        else:
            state["n"] += 1
            state["wait"] = "name_%d" % state["n"]
            state["stage"] = "ask_name"
            resp = build_read(
                [("text", "שלום, זו הפעם הראשונה שלך בקו. אמור בבקשה את שמך הפרטי אחרי הצפצוף")],
                mode="stt",
                val_name=state["wait"],
                lang="he-IL",
            )
        return Response(resp, mimetype="text/plain; charset=utf-8")

    # ---- קבלת השם ----
    if state["stage"] == "ask_name":
        if not value:
            name = "אורח"
        else:
            name = clean_for_tts(value)[:30]
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
        if not value:
            resp = listen(state, prefix="לא שמעתי אותך")
            return Response(resp, mimetype="text/plain; charset=utf-8")

        low = value.lower()
        if "תפריט" in low or low.strip() == "חזרה":
            resp = menu(state, name)
            return Response(resp, mimetype="text/plain; charset=utf-8")
        if "סיים" in low or "ביי" in low or "להתראות" in low:
            return Response(goodbye(call_id, name), mimetype="text/plain; charset=utf-8")

        state["history"].append({"role": "user", "parts": [{"text": value}]})
        answer = ask_ai(state["persona"], state["history"])
        state["history"].append({"role": "model", "parts": [{"text": answer}]})
        state["history"] = state["history"][-10:]

        resp = listen(state, prefix=answer)
        return Response(resp, mimetype="text/plain; charset=utf-8")

    # מצב לא צפוי - חזרה לתפריט
    resp = menu(state, name)
    return Response(resp, mimetype="text/plain; charset=utf-8")


if __name__ == "__main__":
    app.run()
