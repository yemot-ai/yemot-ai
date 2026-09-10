from flask import Flask
from yemot_flow import Flow, Call
from google import genai
import os

app = Flask(__name__)
flow = Flow()

# שמירת שמות (בזיכרון – יתאפס כשהשרת נרדם)
names = {}

# אישיות ה-AI
PERSONAS = {
    "1": "אתה עוזר כללי ידידותי ומועיל. ענה בעברית ברורה וקצרה.",
    "2": "אתה עוזר תורני. ענה בסגנון תורני מכובד, השתמש במקורות כשאפשר. ענה בעברית.",
    "3": "אתה עוזר חוצפני וסרקסטי עם הומור שחור. ענה בחוצפה אבל לא מעליב. ענה בעברית.",
    "4": "אתה עוזר יצירתי. ספר סיפורים, כתוב שירים, בדיחות ורעיונות יצירתיים. ענה בעברית.",
    "5": "אתה עוזר טכני. הסבר דברים טכניים בפשטות (מחשבים, אינטרנט, טלפונים). ענה בעברית."
}

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

def ask_ai(persona, user_text, history=None):
    if history is None:
        history = []
    system = PERSONAS.get(persona, PERSONAS["1"])
    messages = [{"role": "user", "parts": [{"text": system + "\n\nהמשתמש אמר: " + user_text}]}]
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=messages
        )
        return response.text.strip()[:500]  # מגביל אורך כדי לא להאט
    except Exception:
        return "סליחה, יש בעיה זמנית. נסה שוב."

@flow.get("")
async def main(call: Call):
    phone = call.params.get("ApiPhone", "unknown")
    name = names.get(phone)

    # רישום שם בפעם הראשונה
    if not name:
        name = await call.read(
            [("text", "שלום, זו הפעם הראשונה שלך. אנא אמור את שמך הפרטי אחרי הצפצוף")],
            mode="stt",
            val_name="name",
            lang="he-IL"
        )
        if name:
            names[phone] = name.strip()
            await call.play_message([("text", f"נעים להכיר {name}. השם נשמר.")])
        else:
            name = "אורח"
            names[phone] = name

    # תפריט ראשי (רק הקשות)
    while True:
        choice = await call.read(
            [
                ("text", f"שלום {name}. ברוך הבא."),
                ("text", "הקש 1 לעוזר כללי"),
                ("text", "הקש 2 לעוזר תורני"),
                ("text", "הקש 3 לעוזר חוצפני"),
                ("text", "הקש 4 לעוזר יצירתי"),
                ("text", "הקש 5 לעוזר טכני"),
                ("text", "הקש 9 לסיום")
            ],
            mode="tap",
            val_name="choice",
            max_digits=1,
            digits_allowed="123459"
        )

        if choice == "9":
            await call.play_message([("text", f"להתראות {name}")])
            call.hangup()
            return

        if choice not in PERSONAS:
            continue

        # שיחה עם AI
        await call.play_message([("text", "אתה נמצא עכשיו עם העוזר. דבר אחרי הצפצוף. אמור תפריט כדי לחזור או סיים כדי לסיים.")])

        while True:
            user_text = await call.read(
                [("text", "אני מקשיב")],
                mode="stt",
                val_name="speech",
                lang="he-IL"
            )

            if not user_text:
                continue

            user_text_lower = user_text.lower()
            if "תפריט" in user_text_lower or "חזרה" in user_text_lower:
                break
            if "סיים" in user_text_lower or "ביי" in user_text_lower or "להתראות" in user_text_lower:
                await call.play_message([("text", f"להתראות {name}")])
                call.hangup()
                return

            answer = ask_ai(choice, user_text)
            await call.play_message([("text", answer)])

if __name__ == "__main__":
    app.run()
