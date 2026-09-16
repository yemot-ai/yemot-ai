from flask import Flask, request, Response
from yemot_flow.actions import (
    build_id_list_message,
    build_read,
    build_go_to_folder,
    build_combined_action,
)
from google import genai
from google.genai import types

import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


app = Flask(__name__)


# ============================================================
# הגדרות
# ============================================================

YEMOT_API = "https://www.call2all.co.il/ym/api/"

YEMOT_TOKEN = os.environ.get(
    "YEMOT_TOKEN",
    ""
).strip()

GEMINI_API_KEY = os.environ.get(
    "GEMINI_API_KEY",
    ""
).strip()

ADMIN_KEY = os.environ.get(
    "ADMIN_KEY",
    ""
).strip()


# שלוחות קול.
# לדוגמה:
# VOICE_EXTS=1,2,3
VOICE_EXTS = [
    x.strip().strip("/")
    for x in os.environ.get(
        "VOICE_EXTS",
        "1"
    ).split(",")
    if x.strip()
]

if not VOICE_EXTS:
    VOICE_EXTS = ["1"]

DATA_EXT = VOICE_EXTS[0]


# מודל ראשי מהיר.
# מודל גיבוי רק במקרה שהמודל הראשי נכשל.
AI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]


# אורך הקלטה מרבי.
MAX_AUDIO_SECONDS = 25


# גודל WAV מרבי.
MAX_AUDIO_BYTES = 7 * 1024 * 1024


# כמה זוגות שאלות/תשובות לשמור בזיכרון בזמן השיחה בלבד.
MAX_HISTORY_MESSAGES = 6


# כמה זמן שיחה שלא קיבלה בקשה תישאר בזיכרון.
CALL_TTL_SECONDS = 45 * 60


# מכסה יומית.
DEFAULT_DAILY_LIMIT = max(
    0,
    int(
        os.environ.get(
            "DAILY_LIMIT",
            "40"
        ) or 0
    )
)


# משתמשים ללא הגבלה.
OWNER_PHONES = {
    "0527661756",
    "0527609296",
}


# ============================================================
# מצב זמני בזיכרון בלבד
# ============================================================

names = {}

persona_names = {}
persona_prompts = {}

calls = {}

usage = {}

state_lock = threading.RLock()
client_lock = threading.Lock()

_client = None


# ============================================================
# כללים כלליים
# ============================================================

GENERAL_RULES = (
    " אתה מדבר בטלפון, לכן ענה קצר וברור, "
    "בלי כוכביות, בלי רשימות, בלי אימוג'ים "
    "ובלי סימני עיצוב."
    " ענה בשפה שבה המשתמש דיבר אליך; "
    "ברירת המחדל היא עברית."
    " שמור על שפה מכובדת וצנועה."
    " כשאתה מספק מספרים או מחירים, "
    "כתוב אותם במילים בעברית ולא בספרות."
)


# ============================================================
# PERSONAS
# ============================================================

DEFAULT_PERSONAS = {
    "1": (
        "העוזר הכללי",
        "אתה עוזר כללי ידידותי ומועיל."
    ),

    "2": (
        "העוזר הלימודי",
        "אתה עוזר לימודי ומכובד. "
        "ענה בצורה ברורה ומסודרת."
    ),

    "3": (
        "העוזר החוצפן",
        "אתה עוזר חוצפני וסרקסטי עם הומור. "
        "היה משעשע אבל אל תעליב באמת."
    ),

    "4": (
        "העוזר היצירתי",
        "אתה עוזר יצירתי. "
        "הצע רעיונות, סיפורים קצרים, "
        "בדיחות ותוכן יצירתי."
    ),

    "5": (
        "העוזר הטכני",
        "אתה עוזר טכני. "
        "הסבר מחשבים, אינטרנט וטלפונים בפשטות."
    ),

    "6": (
        "החבר",
        "אתה חבר קרוב, חמוד וזורם. "
        "דבר בחום ובשפה קלילה."
    ),

    "7": (
        "העוזר המוזיקלי",
        "אתה עוזר מוזיקלי. "
        "אתה מומחה למוזיקה, אקורדים, "
        "מושגים, סגנונות ואמנים."
    ),
}


for key, (name, prompt) in DEFAULT_PERSONAS.items():
    persona_names[key] = name
    persona_prompts[key] = prompt


# ============================================================
# אזור זמן ישראל
# ============================================================

try:
    ISRAEL_TZ = ZoneInfo(
        "Asia/Jerusalem"
    )
except Exception:
    ISRAEL_TZ = dt.timezone(
        dt.timedelta(hours=3)
    )


def il_now():
    return dt.datetime.now(
        ISRAEL_TZ
    )


# ============================================================
# כלי טקסט
# ============================================================

def clean_for_tts(text, limit=650):
    text = str(text or "")

    text = re.sub(
        r"[*_#`>\[\]{}]",
        "",
        text
    )

    text = re.sub(
        r"https?://\S+|www\.\S+",
        "",
        text
    )

    text = text.replace(
        "\r",
        " "
    )

    text = text.replace(
        "\n",
        ", "
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()

    return text[:limit]


def normalize_phone(phone):
    phone = re.sub(
        r"\D",
        "",
        str(phone or "")
    )

    return phone or "unknown"


# ============================================================
# ניקוי שיחות ישנות
# ============================================================

def cleanup_calls():
    now_mono = time.monotonic()

    with state_lock:

        expired = [
            call_id
            for call_id, state in calls.items()
            if (
                now_mono
                - state.get(
                    "last_seen",
                    0
                )
                > CALL_TTL_SECONDS
            )
        ]

        for call_id in expired:
            calls.pop(
                call_id,
                None
            )

        today = il_now().date().isoformat()

        expired_usage = [
            phone
            for phone, item in usage.items()
            if item.get("day") != today
        ]

        for phone in expired_usage:
            usage.pop(
                phone,
                None
            )


# ============================================================
# מכסת הודעות
# ============================================================

def over_limit(phone):
    if phone in OWNER_PHONES:
        return False

    if DEFAULT_DAILY_LIMIT <= 0:
        return False

    today = il_now().date().isoformat()

    with state_lock:

        item = usage.get(
            phone
        )

        if not item:
            return False

        if item.get("day") != today:
            return False

        return int(
            item.get(
                "count",
                0
            )
        ) >= DEFAULT_DAILY_LIMIT


def consume_message(phone):
    if phone in OWNER_PHONES:
        return True

    if DEFAULT_DAILY_LIMIT <= 0:
        return True

    today = il_now().date().isoformat()

    with state_lock:

        item = usage.get(
            phone
        )

        if (
            not item
            or item.get("day") != today
        ):
            item = {
                "day": today,
                "count": 0,
            }

            usage[phone] = item

        if item["count"] >= DEFAULT_DAILY_LIMIT:
            return False

        item["count"] += 1

        return True


# ============================================================
# ימות המשיח - הורדת קובץ
# ============================================================

def yemot_download(
    ext,
    file_name
):
    if not YEMOT_TOKEN:
        raise RuntimeError(
            "YEMOT_TOKEN is missing"
        )

    url = (
        YEMOT_API
        + "DownloadFile?"
        + urllib.parse.urlencode({
            "token": YEMOT_TOKEN,
            "path": (
                "ivr2:/%s/%s.wav"
                % (
                    ext,
                    file_name
                )
            ),
        })
    )

    with urllib.request.urlopen(
        url,
        timeout=20
    ) as response:

        data = response.read(
            MAX_AUDIO_BYTES + 1
        )

    if not data:
        raise RuntimeError(
            "empty audio"
        )

    if len(data) > MAX_AUDIO_BYTES:
        raise RuntimeError(
            "audio file too large"
        )

    return data


# ============================================================
# ימות המשיח - מחיקת קובץ
# נעשה ברקע כדי לא לעכב את השיחה
# ============================================================

def yemot_delete(
    ext,
    file_name
):
    if not YEMOT_TOKEN:
        return

    try:

        url = (
            YEMOT_API
            + "FileAction?"
            + urllib.parse.urlencode({
                "token": YEMOT_TOKEN,
                "action": "delete",
                "what": (
                    "ivr2:/%s/%s.wav"
                    % (
                        ext,
                        file_name
                    )
                ),
            })
        )

        urllib.request.urlopen(
            url,
            timeout=8
        ).read()

    except Exception as exc:

        print(
            "delete error:",
            repr(exc)
        )


def delete_audio_async(
    ext,
    file_name
):
    threading.Thread(
        target=yemot_delete,
        args=(
            ext,
            file_name
        ),
        daemon=True
    ).start()


# ============================================================
# קבצי מידע קבועים
# רק שמות והגדרות.
# שום היסטוריית שיחה לא נשמרת.
# ============================================================

def yemot_read_text(
    file_name
):
    if not YEMOT_TOKEN:
        return None

    try:

        url = (
            YEMOT_API
            + "DownloadFile?"
            + urllib.parse.urlencode({
                "token": YEMOT_TOKEN,
                "path": (
                    "ivr2:/%s/%s"
                    % (
                        DATA_EXT,
                        file_name
                    )
                ),
            })
        )

        with urllib.request.urlopen(
            url,
            timeout=12
        ) as response:

            data = response.read(
                512 * 1024
            ).decode(
                "utf-8",
                "ignore"
            )

        if data.lstrip().startswith(
            '{"responseStatus'
        ):
            return None

        return data

    except Exception as exc:

        print(
            "read text error:",
            repr(exc)
        )

        return None


def yemot_write_text(
    file_name,
    text
):
    if not YEMOT_TOKEN:
        return False

    try:

        body = urllib.parse.urlencode({
            "token": YEMOT_TOKEN,
            "what": (
                "ivr2:/%s/%s"
                % (
                    DATA_EXT,
                    file_name
                )
            ),
            "contents": text,
        }).encode(
            "utf-8"
        )

        req = urllib.request.Request(
            YEMOT_API + "UploadTextFile",
            data=body,
            method="POST"
        )

        urllib.request.urlopen(
            req,
            timeout=15
        ).read()

        return True

    except Exception as exc:

        print(
            "write text error:",
            repr(exc)
        )

        return False


def save_names():
    with state_lock:
        data = json.dumps(
            names,
            ensure_ascii=False
        )

    threading.Thread(
        target=yemot_write_text,
        args=(
            "ai_names.txt",
            data
        ),
        daemon=True
    ).start()


def save_personas():
    with state_lock:

        data = json.dumps(
            {
                "names": persona_names,
                "prompts": persona_prompts,
            },
            ensure_ascii=False
        )

    threading.Thread(
        target=yemot_write_text,
        args=(
            "ai_personas.txt",
            data
        ),
        daemon=True
    ).start()


def load_names():
    text = yemot_read_text(
        "ai_names.txt"
    )

    if not text:
        return

    try:
        data = json.loads(
            text
        )
    except Exception:
        return

    if not isinstance(
        data,
        dict
    ):
        return

    with state_lock:

        for phone, name in data.items():

            phone = normalize_phone(
                phone
            )

            name = clean_for_tts(
                name,
                30
            )

            if phone and name:
                names[phone] = name


def load_personas():
    text = yemot_read_text(
        "ai_personas.txt"
    )

    if not text:
        return

    try:
        data = json.loads(
            text
        )
    except Exception:
        return

    if not isinstance(
        data,
        dict
    ):
        return

    with state_lock:

        loaded_names = data.get(
            "names"
        )

        loaded_prompts = data.get(
            "prompts"
        )

        if isinstance(
            loaded_names,
            dict
        ):

            for key, value in loaded_names.items():

                if (
                    key in persona_names
                    and value
                ):
                    persona_names[key] = clean_for_tts(
                        value,
                        40
                    )

        if isinstance(
            loaded_prompts,
            dict
        ):

            for key, value in loaded_prompts.items():

                if (
                    key in persona_prompts
                    and value
                ):
                    persona_prompts[key] = clean_for_tts(
                        value,
                        1200
                    )


if YEMOT_TOKEN:
    load_names()
    load_personas()


# ============================================================
# GEMINI CLIENT
# ============================================================

def get_client():
    global _client

    if _client is not None:
        return _client

    with client_lock:

        if _client is not None:
            return _client

        if not GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY is missing"
            )

        _client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        return _client


# ============================================================
# בחירת פרסונה
# ============================================================

def get_persona_prompt(
    persona_key
):
    with state_lock:

        prompt = persona_prompts.get(
            persona_key,
            persona_prompts["1"]
        )

    return prompt


# ============================================================
# ה-AI הראשי
#
# חשוב:
# אין תמלול נפרד.
#
# Gemini מקבל:
# 1. היסטוריה
# 2. אודיו חדש
# 3. הוראות
#
# ומחזיר:
# תשובה + טקסט שהובן + פקודה אם קיימת.
# ============================================================

def ai_audio_turn(
    persona_key,
    history,
    audio_bytes
):
    now = il_now()

    current_date = now.strftime(
        "%d/%m/%Y"
    )

    current_time = now.strftime(
        "%H:%M"
    )

    persona = get_persona_prompt(
        persona_key
    )

    system_instruction = (
        persona
        + GENERAL_RULES
        + (
            f" [תאריך בישראל: {current_date}; "
            f"שעה: {current_time}]"
        )
        + """
 אתה מקבל הקלטה קולית של משתמש בשיחת טלפון.

 המטרה היא לענות למשתמש במהירות ובטבעיות.

 יש לבצע את המשימה בתוך קריאת AI אחת.

 תחילה הבן מה המשתמש אמר.
 לאחר מכן:
 אם מדובר בשאלה רגילה, החזר תשובה קצרה.
 אם המשתמש מבקש מידע עדכני או מידע שיכול להשתנות,
 ניתן להשתמש ב-Google Search.
 אל תחפש באינטרנט כשאין בכך צורך.

 יש גם שלוש פקודות מיוחדות:
 אם המשתמש אומר להחליף קול, החזר COMMAND|CHANGE_VOICE
 אם המשתמש אומר תפריט או חזרה, החזר COMMAND|MENU
 אם המשתמש אומר סיים, ביי או להתראות, החזר COMMAND|HANGUP

 עבור שאלה רגילה יש להחזיר בדיוק בפורמט:

TRANSCRIPT|הטקסט שהמשתמש אמר
ANSWER|התשובה שלך

אין להחזיר שום שורה נוספת.

 עבור פקודה יש להחזיר בדיוק שורה אחת:

COMMAND|CHANGE_VOICE

או:

COMMAND|MENU

או:

COMMAND|HANGUP

התשובה מיועדת להשמעה בטלפון.
היא חייבת להיות קצרה.
אין להשתמש בכוכביות.
אין להשתמש ברשימות.
אין להשתמש באימוג'ים.
אין לדבר על הפורמט הזה.
"""
    )

    contents = []

    # היסטוריה זמנית של השיחה בלבד
    for item in history:
        contents.append(
            item
        )

    # האודיו החדש
    contents.append({
        "role": "user",
        "parts": [
            types.Part.from_bytes(
                data=audio_bytes,
                mime_type="audio/wav"
            ),
            types.Part.from_text(
                text=(
                    "האזן להקלטה ופעל "
                    "לפי ההוראות."
                )
            ),
        ],
    })

    for model_name in AI_MODELS:

        try:

            config_args = {
                "system_instruction": system_instruction,
                "max_output_tokens": 220,
                "tools": [
                    types.Tool(
                        google_search=types.GoogleSearch()
                    )
                ],
            }

            # Gemini 3.x:
            # מינימום reasoning לצורך latency נמוך
            if model_name.startswith(
                "gemini-3."
            ):

                config_args[
                    "thinking_config"
                ] = types.ThinkingConfig(
                    thinking_level="minimal"
                )

            config = types.GenerateContentConfig(
                **config_args
            )

            result = get_client().models.generate_content(
                model=model_name,
                contents=contents,
                config=config
            )

            text = getattr(
                result,
                "text",
                None
            )

            if text:

                text = text.strip()

                print(
                    "Gemini success:",
                    model_name
                )

                return text

            print(
                "Gemini empty response:",
                model_name
            )

        except Exception as exc:

            print(
                "Gemini error:",
                model_name,
                repr(exc)
            )

            continue

    return ""


# ============================================================
# פענוח תשובת Gemini
# ============================================================

def parse_ai_result(
    raw
):
    raw = str(
        raw or ""
    ).strip()

    if not raw:
        return {
            "type": "error",
            "transcript": "",
            "answer": "",
        }

    # פקודות
    upper = raw.upper()

    if "COMMAND|CHANGE_VOICE" in upper:
        return {
            "type": "change_voice",
            "transcript": "",
            "answer": "",
        }

    if "COMMAND|MENU" in upper:
        return {
            "type": "menu",
            "transcript": "",
            "answer": "",
        }

    if "COMMAND|HANGUP" in upper:
        return {
            "type": "hangup",
            "transcript": "",
            "answer": "",
        }

    # תשובה רגילה
    transcript_match = re.search(
        r"TRANSCRIPT\|(.*?)(?:\n|$)",
        raw,
        re.IGNORECASE
    )

    answer_match = re.search(
        r"ANSWER\|(.*)",
        raw,
        re.IGNORECASE | re.DOTALL
    )

    transcript = (
        transcript_match.group(1).strip()
        if transcript_match
        else ""
    )

    answer = (
        answer_match.group(1).strip()
        if answer_match
        else ""
    )

    # במקרה של פורמט חלקי
    if not answer:

        lines = [
            line.strip()
            for line in raw.splitlines()
            if line.strip()
        ]

        if lines:

            answer = lines[-1]

            answer = re.sub(
                r"^(ANSWER|TRANSCRIPT)\s*\|\s*",
                "",
                answer,
                flags=re.IGNORECASE
            )

    answer = clean_for_tts(
        answer,
        700
    )

    transcript = clean_for_tts(
        transcript,
        500
    )

    return {
        "type": "answer"
        if answer
        else "error",
        "transcript": transcript,
        "answer": answer,
    }


# ============================================================
# הודעה אחת
# ============================================================

def ask_ai(
    persona_key,
    history,
    ext,
    file_name
):
    try:

        audio = yemot_download(
            ext,
            file_name
        )

    except Exception as exc:

        print(
            "Yemot download error:",
            repr(exc)
        )

        return {
            "type": "error",
            "transcript": "",
            "answer": (
                "סליחה, לא הצלחתי לקבל "
                "את ההקלטה. נסה שוב."
            ),
        }

    # לא לעכב את השיחה על מחיקת הקובץ
    delete_audio_async(
        ext,
        file_name
    )

    raw = ai_audio_turn(
        persona_key,
        history,
        audio
    )

    result = parse_ai_result(
        raw
    )

    if result["type"] == "error":

        result["answer"] = (
            "סליחה, יש בעיה זמנית "
            "בחיבור ל-AI. נסה שוב."
        )

    return result


# ============================================================
# בניית תפריט
# ============================================================

def menu(
    state,
    name,
    prefix=None
):
    state["n"] += 1

    state["wait"] = (
        "choice_%d"
        % state["n"]
    )

    state["stage"] = "menu"

    text = (
        "שלום %s. "
        "הקש 1 לעוזר כללי, "
        "2 לעוזר לימודי, "
        "3 לעוזר החוצפן, "
        "4 לעוזר היצירתי, "
        "5 לעוזר טכני, "
        "6 לחבר, "
        "7 לעוזר המוזיקלי, "
        "או 9 לסיום."
        % name
    )

    read = build_read(
        [("text", text)],
        mode="tap",
        val_name=state["wait"],
        max_digits=1,
        min_digits=1,
        digits_allowed="12345679",
        sec_wait=10,
    )

    if prefix:

        return build_combined_action([
            build_id_list_message([
                ("text", prefix)
            ]),
            read,
        ])

    return read


# ============================================================
# הקלטה
# ============================================================

def record(
    state,
    val_prefix,
    prompt,
    prefix=None
):
    state["n"] += 1

    state["wait"] = (
        "%s_%d"
        % (
            val_prefix,
            state["n"]
        )
    )

    safe_call_id = re.sub(
        r"[^0-9a-zA-Z_-]",
        "",
        state["call_id"]
    )

    if not safe_call_id:
        safe_call_id = "call"

    file_name = (
        "ai_%s_%d"
        % (
            safe_call_id[-18:],
            state["n"]
        )
    )

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
        max_length=MAX_AUDIO_SECONDS,
    )

    if prefix:

        return build_combined_action([
            build_id_list_message([
                ("text", prefix)
            ]),
            read,
        ])

    return read


# ============================================================
# האזנה
# ============================================================

def listen(
    state,
    prefix=None,
    first=False
):
    state["stage"] = "chat"

    if first:

        return record(
            state,
            "speech",
            (
                (
                    prefix + ". "
                )
                if prefix
                else ""
            )
            + (
                "דבר אחרי הצפצוף, "
                "ובסיום הקש סולמית"
            )
        )

    return record(
        state,
        "speech",
        prefix or "אני מקשיב"
    )


# ============================================================
# סיום
# ============================================================

def goodbye(
    call_id,
    name
):
    with state_lock:

        # ברגע שמסתיימת השיחה
        # כל ההיסטוריה נמחקת
        calls.pop(
            call_id,
            None
        )

    return build_combined_action([
        build_id_list_message([
            (
                "text",
                "להתראות %s"
                % name
            )
        ]),
        build_go_to_folder(
            "hangup"
        ),
    ])


# ============================================================
# ENDPOINT ראשי
# ============================================================

@app.route(
    "/",
    methods=["GET", "POST"]
)
def yemot():

    cleanup_calls()

    params = request.values.to_dict()

    call_id = (
        params.get(
            "ApiCallId",
            ""
        )
        or ""
    ).strip()

    if not call_id:

        return Response(
            "ok",
            mimetype=(
                "text/plain; "
                "charset=utf-8"
            ),
        )

    if params.get(
        "hangup"
    ) == "yes":

        with state_lock:
            calls.pop(
                call_id,
                None
            )

        return Response(
            "noop",
            mimetype=(
                "text/plain; "
                "charset=utf-8"
            ),
        )

    phone = normalize_phone(
        params.get(
            "ApiPhone",
            "unknown"
        )
    )

    ext = (
        params.get(
            "ApiExtension",
            ""
        )
        or VOICE_EXTS[0]
    ).strip("/")

    if not ext:
        ext = VOICE_EXTS[0]

    if (
        VOICE_EXTS
        and ext not in VOICE_EXTS
    ):
        ext = VOICE_EXTS[0]

    with state_lock:

        state = calls.get(
            call_id
        )

        if state is None:

            state = {
                "stage": "start",
                "n": 0,
                "wait": None,
                "persona": None,
                "history": [],
                "call_id": call_id,
                "file": None,
                "resume": None,
                "voice_ext": ext,
                "last_seen": time.monotonic(),
                "message_count": 0,
                "lock": threading.Lock(),
            }

            calls[call_id] = state

        else:

            state["last_seen"] = (
                time.monotonic()
            )

            state["voice_ext"] = ext

    # מונע התנגשות בין שתי בקשות
    # של אותה שיחה.
    with state["lock"]:

        return handle_call(
            state,
            params,
            phone,
            ext
        )


# ============================================================
# טיפול בשיחה
# ============================================================

def handle_call(
    state,
    params,
    phone,
    ext
):
    has_value = (
        bool(state["wait"])
        and state["wait"]
        in params
    )

    value = (
        (
            params.get(
                state["wait"],
                ""
            )
            or ""
        ).strip()
        if has_value
        else ""
    )

    if value == "None":
        value = ""

    with state_lock:

        name = names.get(
            phone
        )

    # ========================================================
    # אחרי החלפת קול
    # ========================================================

    if state.get("resume"):

        mode = state["resume"]

        state["resume"] = None

        if mode == "chat":

            return Response(
                listen(
                    state,
                    prefix="הקול הוחלף"
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        return Response(
            menu(
                state,
                name or "אורח",
                prefix="הקול הוחלף"
            ),
            mimetype=(
                "text/plain; "
                "charset=utf-8"
            ),
        )

    # ========================================================
    # התחלה
    # ========================================================

    if state["stage"] == "start":

        if name:

            return Response(
                menu(
                    state,
                    name
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        state["stage"] = "ask_name"

        return Response(
            record(
                state,
                "name",
                (
                    "שלום, זו הפעם הראשונה שלך בקו. "
                    "אמור את שמך הפרטי, "
                    "ובסיום הקש סולמית"
                )
            ),
            mimetype=(
                "text/plain; "
                "charset=utf-8"
            ),
        )

    # ========================================================
    # קבלת שם
    # ========================================================

    if state["stage"] == "ask_name":

        if not has_value:

            return Response(
                record(
                    state,
                    "name",
                    (
                        "אמור את שמך הפרטי, "
                        "ובסיום הקש סולמית"
                    )
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        try:

            audio = yemot_download(
                ext,
                state["file"]
            )

        except Exception as exc:

            print(
                "name download error:",
                repr(exc)
            )

            return Response(
                record(
                    state,
                    "name",
                    (
                        "לא הצלחתי לקבל את ההקלטה. "
                        "אמור שוב את שמך הפרטי, "
                        "ובסיום הקש סולמית"
                    )
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        delete_audio_async(
            ext,
            state["file"]
        )

        # קריאת AI אחת בלבד גם כאן.
        name_prompt = (
            "החזר רק את השם הפרטי "
            "שנאמר בהקלטה. "
            "בלי הסברים ובלי מילים נוספות."
        )

        try:

            config = types.GenerateContentConfig(
                system_instruction=name_prompt,
                max_output_tokens=20,
                thinking_config=types.ThinkingConfig(
                    thinking_level="minimal"
                ),
            )

            result = get_client().models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[
                    types.Part.from_bytes(
                        data=audio,
                        mime_type="audio/wav"
                    )
                ],
                config=config
            )

            new_name = clean_for_tts(
                getattr(
                    result,
                    "text",
                    ""
                ),
                30
            )

        except Exception as exc:

            print(
                "name AI error:",
                repr(exc)
            )

            new_name = ""

        new_name = re.sub(
            r"[^\u0590-\u05FF\- ]",
            "",
            new_name
        ).strip()

        if not new_name:
            new_name = "אורח"

        with state_lock:
            names[phone] = new_name

        save_names()

        return Response(
            menu(
                state,
                new_name,
                prefix=(
                    "נעים להכיר %s, "
                    "השם נשמר"
                    % new_name
                )
            ),
            mimetype=(
                "text/plain; "
                "charset=utf-8"
            ),
        )

    name = name or "אורח"

    # ========================================================
    # תפריט
    # ========================================================

    if state["stage"] == "menu":

        if value == "9":

            return Response(
                goodbye(
                    state["call_id"],
                    name
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        if value in persona_names:

            state["persona"] = value
            state["history"] = []
            state["message_count"] = 0

            with state_lock:
                persona = persona_names[value]

            return Response(
                listen(
                    state,
                    prefix=(
                        "אתה עכשיו עם %s. "
                        "אמור החלף קול "
                        "כדי להחליף קול, "
                        "תפריט כדי לחזור, "
                        "או סיים כדי לסיים"
                    )
                    % persona,
                    first=True
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        return Response(
            menu(
                state,
                name
            ),
            mimetype=(
                "text/plain; "
                "charset=utf-8"
            ),
        )

    # ========================================================
    # צ'אט
    # ========================================================

    if state["stage"] == "chat":

        if not has_value:

            return Response(
                listen(
                    state,
                    prefix="לא שמעתי אותך"
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        if over_limit(phone):

            if state.get("file"):
                delete_audio_async(
                    ext,
                    state["file"]
                )

            return Response(
                menu(
                    state,
                    name,
                    prefix=(
                        "הגעת למכסת "
                        "ההודעות היומית שלך. "
                        "אפשר לנסות שוב מחר"
                    )
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        if not consume_message(phone):

            if state.get("file"):
                delete_audio_async(
                    ext,
                    state["file"]
                )

            return Response(
                menu(
                    state,
                    name,
                    prefix=(
                        "הגעת למכסת "
                        "ההודעות היומית שלך. "
                        "אפשר לנסות שוב מחר"
                    )
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        result = ask_ai(
            state["persona"],
            state["history"],
            ext,
            state["file"]
        )

        result_type = result.get(
            "type"
        )

        transcript = result.get(
            "transcript",
            ""
        )

        answer = result.get(
            "answer",
            ""
        )

        # ====================================================
        # החלפת קול
        # ====================================================

        if result_type == "change_voice":

            if len(VOICE_EXTS) < 2:

                return Response(
                    listen(
                        state,
                        prefix=(
                            "אין קולות נוספים "
                            "להחלפה"
                        )
                    ),
                    mimetype=(
                        "text/plain; "
                        "charset=utf-8"
                    ),
                )

            current = (
                ext
                if ext in VOICE_EXTS
                else VOICE_EXTS[0]
            )

            next_ext = VOICE_EXTS[
                (
                    VOICE_EXTS.index(
                        current
                    )
                    + 1
                )
                % len(VOICE_EXTS)
            ]

            state["resume"] = "chat"
            state["wait"] = None
            state["voice_ext"] = next_ext

            return Response(
                build_go_to_folder(
                    "/" + next_ext
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        # ====================================================
        # תפריט
        # ====================================================

        if result_type == "menu":

            return Response(
                menu(
                    state,
                    name
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        # ====================================================
        # סיום
        # ====================================================

        if result_type == "hangup":

            return Response(
                goodbye(
                    state["call_id"],
                    name
                ),
                mimetype=(
                    "text/plain; "
                    "charset=utf-8"
                ),
            )

        # ====================================================
        # תשובה רגילה
        # ====================================================

        if not answer:

            answer = (
                "סליחה, לא הצלחתי "
                "להכין תשובה. נסה שוב."
            )

        # היסטוריה נשמרת בזיכרון בלבד
        # ורק בתוך השיחה הנוכחית.
        if transcript:

            state["history"].append({
                "role": "user",
                "parts": [
                    {
                        "text": transcript
                    }
                ],
            })

            state["history"].append({
                "role": "model",
                "parts": [
                    {
                        "text": answer
                    }
                ],
            })

            state["history"] = state[
                "history"
            ][
                -(
                    MAX_HISTORY_MESSAGES * 2
                ):
            ]

        state["message_count"] += 1

        return Response(
            listen(
                state,
                prefix=answer
            ),
            mimetype=(
                "text/plain; "
                "charset=utf-8"
            ),
        )

    # ========================================================
    # fallback
    # ========================================================

    return Response(
        menu(
            state,
            name
        ),
        mimetype=(
            "text/plain; "
            "charset=utf-8"
        ),
    )


# ============================================================
# ADMIN - ניהול מינימלי
# אין כאן יומן שיחות.
# ============================================================

ADMIN_CSS = """
<style>
body{
    font-family:Arial,sans-serif;
    direction:rtl;
    background:#f4f6f9;
    margin:0;
    color:#222
}

.wrap{
    max-width:1000px;
    margin:0 auto;
    padding:20px
}

.card{
    background:#fff;
    border-radius:12px;
    padding:18px;
    margin-bottom:18px;
    box-shadow:0 2px 8px #0001
}

table{
    width:100%;
    border-collapse:collapse;
    background:#fff
}

th,td{
    padding:9px;
    border-bottom:1px solid #eee;
    text-align:right;
    vertical-align:top
}

th{
    background:#2d3e50;
    color:#fff
}

input,textarea{
    width:100%;
    box-sizing:border-box;
    font:inherit;
    padding:7px;
    border:1px solid #ccc;
    border-radius:7px
}

textarea{
    min-height:90px
}

button{
    padding:8px 14px;
    border:0;
    border-radius:7px;
    background:#2d3e50;
    color:#fff;
    cursor:pointer
}

.red{
    background:#c0392b
}

.note{
    background:#fff8d9;
    border-radius:8px;
    padding:12px;
}
</style>
"""


def admin_session_token():

    return hmac.new(
        ADMIN_KEY.encode(
            "utf-8"
        ),
        b"yby-ai-session-v2",
        hashlib.sha256
    ).hexdigest()


def is_admin():

    if not ADMIN_KEY:
        return False

    supplied = request.cookies.get(
        "admin_session",
        ""
    )

    expected = admin_session_token()

    return (
        bool(supplied)
        and hmac.compare_digest(
            supplied,
            expected
        )
    )


def admin_login_page(
    message=""
):
    page = (
        ADMIN_CSS
        + """
        <div class="wrap"
             style="max-width:400px;
                    margin-top:80px">

            <div class="card">

                <h2>
                    כניסה לניהול הקו
                </h2>

                %s

                <form
                    method="post"
                    action="/admin/login">

                    <input
                        type="password"
                        name="key"
                        placeholder="סיסמה"
                        style="margin-bottom:10px">

                    <button>
                        כניסה
                    </button>

                </form>

            </div>

        </div>
        """
        % (
            (
                "<p style='color:#c0392b'>%s</p>"
                % html.escape(message)
            )
            if message
            else ""
        )
    )

    return Response(
        page,
        mimetype=(
            "text/html; "
            "charset=utf-8"
        ),
    )


@app.route(
    "/admin/login",
    methods=["POST"]
)
def admin_login():

    key = request.form.get(
        "key",
        ""
    )

    if not ADMIN_KEY:

        return admin_login_page(
            "לא הוגדרה ADMIN_KEY ב-Render"
        )

    if not hmac.compare_digest(
        key,
        ADMIN_KEY
    ):

        return admin_login_page(
            "סיסמה שגויה"
        )

    response = Response(
        "",
        status=302,
        headers={
            "Location": "/admin"
        },
    )

    response.set_cookie(
        "admin_session",
        admin_session_token(),
        max_age=24 * 60 * 60,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure
    )

    return response


@app.route(
    "/admin",
    methods=["GET"]
)
def admin():

    if not is_admin():

        return admin_login_page()

    with state_lock:

        names_snapshot = dict(
            names
        )

        persona_names_snapshot = dict(
            persona_names
        )

        persona_prompts_snapshot = dict(
            persona_prompts
        )

        active_calls = len(calls)

    user_rows = []

    for phone, name in sorted(
        names_snapshot.items(),
        key=lambda item: item[1]
    ):

        user_rows.append(
            """
            <tr>

                <td>%s</td>
                <td>%s</td>

                <td>

                    <form
                        method="post"
                        action="/admin/rename">

                        <input
                            type="hidden"
                            name="phone"
                            value="%s">

                        <input
                            type="text"
                            name="name"
                            value="%s">

                        <button>
                            שמור
                        </button>

                    </form>

                    <form
                        method="post"
                        action="/admin/delete"
                        style="margin-top:6px"
                        onsubmit="return confirm('למחוק את המשתמש?')">

                        <input
                            type="hidden"
                            name="phone"
                            value="%s">

                        <button class="red">
                            מחק
                        </button>

                    </form>

                </td>

            </tr>
            """
            % (
                html.escape(name),
                html.escape(phone),
                html.escape(phone),
                html.escape(name),
                html.escape(phone),
            )
        )

    if not user_rows:

        user_rows.append(
            """
            <tr>
                <td colspan="3">
                    אין משתמשים רשומים
                </td>
            </tr>
            """
        )

    persona_rows = []

    for key in sorted(
        persona_names_snapshot
    ):

        persona_rows.append(
            """
            <tr>

                <td>%s</td>

                <td>
                    <input
                        type="text"
                        name="name_%s"
                        value="%s">
                </td>

                <td>
                    <textarea
                        name="prompt_%s">%s</textarea>
                </td>

            </tr>
            """
            % (
                key,
                key,
                html.escape(
                    persona_names_snapshot[key]
                ),
                key,
                html.escape(
                    persona_prompts_snapshot[key]
                ),
            )
        )

    page = (
        ADMIN_CSS
        + """
        <div class="wrap">

            <div class="card">

                <h1>
                    ניהול קו AI
                </h1>

                <p>
                    שיחות פעילות כרגע:
                    <b>%d</b>
                </p>

                <div class="note">
                    היסטוריית השיחות אינה נשמרת.
                    היא קיימת רק בזיכרון בזמן
                    השיחה הפעילה ונמחקת
                    כשהשיחה מסתיימת או מתיישנת.
                </div>

            </div>


            <div class="card">

                <h2>
                    משתמשים ושמות
                </h2>

                <table>

                    <tr>
                        <th>שם</th>
                        <th>טלפון</th>
                        <th>פעולות</th>
                    </tr>

                    %s

                </table>

            </div>


            <div class="card">

                <h2>
                    עוזרים
                </h2>

                <form
                    method="post"
                    action="/admin/personas">

                    <table>

                        <tr>
                            <th>מספר</th>
                            <th>שם</th>
                            <th>אופי והנחיה</th>
                        </tr>

                        %s

                    </table>

                    <p>

                        <button>
                            שמור עוזרים
                        </button>

                    </p>

                </form>

            </div>


            <div class="card">

                <a href="/admin/logout">
                    יציאה
                </a>

            </div>

        </div>
        """
        % (
            active_calls,
            "".join(user_rows),
            "".join(persona_rows),
        )
    )

    return Response(
        page,
        mimetype=(
            "text/html; "
            "charset=utf-8"
        ),
    )


@app.route(
    "/admin/rename",
    methods=["POST"]
)
def admin_rename():

    if not is_admin():
        return admin_login_page()

    phone = normalize_phone(
        request.form.get(
            "phone",
            ""
        )
    )

    name = clean_for_tts(
        request.form.get(
            "name",
            ""
        ),
        30
    )

    if phone and name:

        with state_lock:
            names[phone] = name

        save_names()

    return Response(
        "",
        status=302,
        headers={
            "Location": "/admin"
        },
    )


@app.route(
    "/admin/delete",
    methods=["POST"]
)
def admin_delete():

    if not is_admin():
        return admin_login_page()

    phone = normalize_phone(
        request.form.get(
            "phone",
            ""
        )
    )

    with state_lock:
        names.pop(
            phone,
            None
        )

    save_names()

    return Response(
        "",
        status=302,
        headers={
            "Location": "/admin"
        },
    )


@app.route(
    "/admin/personas",
    methods=["POST"]
)
def admin_personas():

    if not is_admin():
        return admin_login_page()

    with state_lock:

        for key in persona_names:

            name = clean_for_tts(
                request.form.get(
                    "name_" + key,
                    ""
                ),
                40
            )

            prompt = clean_for_tts(
                request.form.get(
                    "prompt_" + key,
                    ""
                ),
                1200
            )

            if name:
                persona_names[key] = name

            if prompt:
                persona_prompts[key] = prompt

    save_personas()

    return Response(
        "",
        status=302,
        headers={
            "Location": "/admin"
        },
    )


@app.route(
    "/admin/logout"
)
def admin_logout():

    response = Response(
        "",
        status=302,
        headers={
            "Location": "/admin"
        },
    )

    response.set_cookie(
        "admin_session",
        "",
        max_age=0,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure
    )

    return response


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return Response(
        "ok",
        mimetype=(
            "text/plain; "
            "charset=utf-8"
        ),
    )


# ============================================================
# ERROR HANDLER
# ============================================================

@app.errorhandler(Exception)
def unexpected_error(exc):

    print(
        "Unhandled exception:",
        repr(exc)
    )

    return Response(
        "סליחה, אירעה תקלה זמנית במערכת. נסה שוב.",
        status=200,
        mimetype=(
            "text/plain; "
            "charset=utf-8"
        ),
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
