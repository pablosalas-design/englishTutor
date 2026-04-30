"""Realtime voice webapp for the English tutor.

Sirve una página web sencilla donde el alumno habla con la profesora
en tiempo real (WebRTC + OpenAI Realtime API). Comparte la base de datos
con el bot de Telegram para mantener memoria larga y resúmenes semanales.
"""
import json
import os
import re
from contextlib import contextmanager
from datetime import date, datetime, timezone

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", "5000"))

REALTIME_MODEL = "gpt-4o-realtime-preview"
LONG_CONTEXT_DAYS = 30
LONG_CONTEXT_MESSAGES = 80

# ----------------------------------------------------------------------------
# Personas (deben coincidir con bot.py)
# ----------------------------------------------------------------------------

PEACE_PROMPT = (
    "You are Peace, a kind, warm and patient English teacher. "
    "Have a natural, flowing voice conversation with the student, remembering what you've talked about before. "
    "Correct mistakes gently, give clear examples, and encourage the student. "
    "When you correct, briefly explain why the change is better. "
    "Keep responses concise (2-4 sentences) unless the student asks for more detail. "
    "PACING: Speak at a brisk, natural conversational pace, the way a real native English-speaking adult would talk to a friend. "
    "Do NOT speak slowly or in an exaggeratedly didactic tone. Avoid long pauses between words. "
    "Sound energetic, lively and human, never robotic or overly careful."
)


def kid_prompt(name: str) -> str:
    return (
        f"You are Mia, a fun and enthusiastic English companion for {name}, "
        f"a pre-teen girl (11-13 years old) at A2 level. "
        "Your tone is cheerful, warm and motivating, but not babyish. "
        f"Call her by her name ({name}) sometimes to make it personal. "
        "Use SHORT and SIMPLE English sentences (A2 level). "
        "If she doesn't understand, repeat with even simpler words or translate to Spanish. "
        "Mostly speak English, but explain difficult things in Spanish when needed. "
        "\n\n"
        "Constantly propose mini-games and activities: guess the word, story together, "
        "I spy with my little eye, would-you-rather questions, describe your day or pet, "
        "small challenges like 'name 5 animals in English'. "
        "Topics she likes: animals, pets, music, shows, friends, school, hobbies, food. "
        "\n\n"
        "POSITIVE CORRECTIONS: never say 'that's wrong'. Say 'almost! try this...', "
        "'great try! Another way would be...'. Celebrate successes with genuine enthusiasm. "
        "\n\n"
        "SAFETY: NEVER discuss inappropriate topics for minors (violence, sex, drugs, fear, politics). "
        "If she brings up a sensitive topic, redirect lovingly to something fun. "
        "Keep the environment safe and positive at all times. "
        "\n\n"
        "PACING: Speak at a lively, natural conversational pace, like a fun teen friend chatting in real time. "
        "Do NOT speak slowly or robotically. Keep the rhythm energetic and warm. "
        "Pronounce English clearly so she can understand, but never drag the words. "
        "When you switch to Spanish for explanations, use a normal Spanish conversational pace too."
    )


MODES = {
    "peace": {
        "label": "Peace",
        "subtitle": "Profesora para adultos",
        "prompt": PEACE_PROMPT,
        "voice": "coral",
        "is_kid": False,
        "color": "#5B7FFF",
        "level": "B2-C1",
        "explanation_lang": "en",  # explicaciones de gramática en inglés
        "student_name": "Pablo",
    },
    "lucia": {
        "label": "Mia para Lucía",
        "subtitle": "11-13 años · A2-B1",
        "prompt": kid_prompt("Lucía"),
        "voice": "sage",
        "is_kid": True,
        "color": "#FF6FA0",
        "level": "A2-B1",
        "explanation_lang": "es",  # explicaciones en español
        "student_name": "Lucía",
    },
    "leyre": {
        "label": "Mia para Leyre",
        "subtitle": "11-13 años · A2-B1",
        "prompt": kid_prompt("Leyre"),
        "voice": "sage",
        "is_kid": True,
        "color": "#9B6FFF",
        "level": "A2-B1",
        "explanation_lang": "es",
        "student_name": "Leyre",
    },
}

# ----------------------------------------------------------------------------
# Database (mismas tablas que el bot de Telegram)
# ----------------------------------------------------------------------------

@contextmanager
def db_cursor():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    finally:
        conn.close()


def init_db_grammar():
    """Crea las tablas de la pestaña Gramática si no existen."""
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS grammar_lessons (
              id SERIAL PRIMARY KEY,
              chat_id BIGINT NOT NULL,
              mode TEXT NOT NULL,
              lesson_date DATE NOT NULL,
              topic TEXT NOT NULL,
              level TEXT NOT NULL,
              lang TEXT NOT NULL,
              title TEXT NOT NULL,
              explanation TEXT NOT NULL,
              examples JSONB NOT NULL,
              exercises JSONB NOT NULL,
              created_at TIMESTAMPTZ DEFAULT NOW(),
              UNIQUE(chat_id, lesson_date)
            );
            CREATE INDEX IF NOT EXISTS idx_grammar_lessons_chat_date
              ON grammar_lessons(chat_id, lesson_date DESC);
            CREATE TABLE IF NOT EXISTS grammar_attempts (
              id SERIAL PRIMARY KEY,
              chat_id BIGINT NOT NULL,
              lesson_id INTEGER NOT NULL REFERENCES grammar_lessons(id) ON DELETE CASCADE,
              exercise_index INTEGER NOT NULL,
              user_answer TEXT,
              is_correct BOOLEAN NOT NULL,
              created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_grammar_attempts_lesson
              ON grammar_attempts(lesson_id);
        """)


def web_chat_id(mode: str) -> int:
    """Identificador estable por modo para el canal web (negativo para
    distinguir de los chat_id de Telegram, que son positivos para usuarios)."""
    mapping = {"peace": -1001, "lucia": -1002, "leyre": -1003}
    return mapping.get(mode, -1000)


def ensure_chat(chat_id: int):
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO chats (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING",
            (chat_id,),
        )


def fetch_recent_messages(chat_id: int, days: int = LONG_CONTEXT_DAYS) -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT role, content, created_at
            FROM messages
            WHERE chat_id = %s
              AND created_at >= NOW() - INTERVAL '%s days'
            ORDER BY created_at ASC
            """,
            (chat_id, days),
        )
        return [dict(r) for r in cur.fetchall()]


def store_message(chat_id: int, role: str, content: str, mode: str):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (chat_id, role, content, mode)
            VALUES (%s, %s, %s, %s)
            """,
            (chat_id, role, content, mode),
        )


# ----------------------------------------------------------------------------
# Prompt building
# ----------------------------------------------------------------------------

def fetch_all_summaries(chat_id: int, limit: int = 12) -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT week_start, summary_text
            FROM summaries
            WHERE chat_id = %s
            ORDER BY week_start DESC
            LIMIT %s
            """,
            (chat_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return list(reversed(rows))


def build_long_term_context(chat_id: int) -> str:
    sections: list[str] = []

    summaries = fetch_all_summaries(chat_id)
    if summaries:
        summary_lines = [
            f"- Week of {s['week_start'].isoformat()}: {s['summary_text']}"
            for s in summaries
        ]
        sections.append(
            "[LONG-TERM MEMORY - summaries of previous weeks, so you remember the "
            "student's progress, recurring topics and mistakes over time]:\n"
            + "\n".join(summary_lines)
            + "\n[END OF LONG-TERM MEMORY]"
        )

    rows = fetch_recent_messages(chat_id)
    if rows:
        recent = rows[-LONG_CONTEXT_MESSAGES:]
        lines = []
        for r in recent:
            prefix = "Student" if r["role"] == "user" else "Teacher"
            text = (r["content"] or "")[:300]
            lines.append(f"- {prefix}: {text}")
        sections.append(
            f"[RECENT CONTEXT - last {LONG_CONTEXT_DAYS} days of conversation, "
            "so you remember what you've already talked about]:\n"
            + "\n".join(lines)
            + "\n[END OF RECENT CONTEXT]"
        )

    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


def build_instructions(mode: str) -> str:
    cfg = MODES[mode]
    chat_id = web_chat_id(mode)
    return cfg["prompt"] + build_long_term_context(chat_id)


# ----------------------------------------------------------------------------
# Grammar (lección diaria + ejercicios generados por IA)
# ----------------------------------------------------------------------------

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def fetch_user_lines(chat_id: int, limit: int = 60) -> list[str]:
    """Últimas frases dichas por el alumno (rol=user) en orden cronológico."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT content FROM messages
            WHERE chat_id = %s AND role = 'user' AND content IS NOT NULL
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (chat_id, limit),
        )
        rows = cur.fetchall()
    return list(reversed([r["content"][:300] for r in rows if r["content"]]))


def fetch_past_lesson_topics(chat_id: int, limit: int = 20) -> list[str]:
    """Temas ya cubiertos para no repetir."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT topic FROM grammar_lessons
            WHERE chat_id = %s
            ORDER BY lesson_date DESC
            LIMIT %s
            """,
            (chat_id, limit),
        )
        return [r["topic"] for r in cur.fetchall()]


def fetch_today_lesson(chat_id: int, today: date) -> dict | None:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, topic, level, lang, title, explanation, examples, exercises
            FROM grammar_lessons
            WHERE chat_id = %s AND lesson_date = %s
            """,
            (chat_id, today),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def insert_lesson(chat_id: int, mode: str, today: date, lesson: dict) -> int:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO grammar_lessons
              (chat_id, mode, lesson_date, topic, level, lang, title, explanation, examples, exercises)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, lesson_date) DO UPDATE SET
              topic = EXCLUDED.topic,
              title = EXCLUDED.title,
              explanation = EXCLUDED.explanation,
              examples = EXCLUDED.examples,
              exercises = EXCLUDED.exercises
            RETURNING id
            """,
            (
                chat_id,
                mode,
                today,
                lesson["topic"],
                lesson["level"],
                lesson["lang"],
                lesson["title"],
                lesson["explanation"],
                json.dumps(lesson["examples"]),
                json.dumps(lesson["exercises"]),
            ),
        )
        return cur.fetchone()["id"]


GRAMMAR_SYSTEM_PROMPT = """You are an expert English grammar coach.
You design ONE short daily grammar lesson personalised to a single student.

Inputs you will receive:
- student_name, level (CEFR range)
- explanation_lang ("es" or "en"): the language used for the lesson explanation and feedback
- native_lang: always "es" (the student's mother tongue)
- recent_user_lines: things the student has recently said in conversation (may contain mistakes)
- available_topics: the OFFICIAL curriculum for this level — the only topics you may pick from
- past_topics: grammar topics already covered (avoid repeating these)
- tone: "warm" (kid) or "neutral" (adult)

Your job:
1. Pick ONE grammar topic. The "topic" field MUST be EXACTLY one of the slugs in available_topics.
   Priority for choosing:
     a) A topic from available_topics that the recent_user_lines suggest is weak
        (recurring mistakes, avoidance, awkward phrasing) AND is not in past_topics.
     b) Otherwise, any topic from available_topics that is not in past_topics.
     c) Only if every topic in available_topics is already in past_topics, you may pick
        the one that was covered longest ago.
2. Produce a focused, motivating mini-lesson on that topic.

Output STRICT JSON with this schema (no markdown, no extra text):
{
  "topic": "<short slug, e.g. 'present_perfect_vs_past_simple'>",
  "title": "<friendly lesson title in explanation_lang>",
  "explanation": "<2-4 short paragraphs in explanation_lang. Plain text, no markdown.
                  Cover: when to use it, the rule, common mistakes (ideally referencing
                  the student's own mistakes if visible), and a quick tip.>",
  "examples": [
    {"en": "<English sentence>", "translation": "<Spanish translation, native_lang>"},
    ... 3 examples total
  ],
  "exercises": [
    // EXACTLY 5 exercises in this order: 3 multiple choice, then 2 fill-in-the-blank.
    {
      "type": "mc",
      "question": "<English sentence with ___ where the answer goes, OR a question>",
      "options": ["<a>", "<b>", "<c>", "<d>"],
      "correct": "<one of the options, EXACT text>",
      "explanation": "<brief feedback in explanation_lang>"
    },
    ... two more "mc" ...
    {
      "type": "fill",
      "question": "<English sentence with ___ where the answer goes>",
      "correct": "<the exact word(s) that fill the blank, lowercase, no punctuation>",
      "accept": ["<optional alternative spellings or contractions, lowercase>"],
      "explanation": "<brief feedback in explanation_lang>"
    },
    ... one more "fill" ...
  ]
}

Rules:
- Exercises and example sentences are in ENGLISH.
- Example translations always go to native_lang (Spanish).
- Lesson explanation and exercise feedback follow explanation_lang.
- Difficulty must match the level.
- Each "fill" answer must be 1-3 words, unambiguous, and lowercase.
- Never include the answer inside the question.
"""


# Temario por nivel: el modelo SOLO puede elegir de esta lista.
# Editable sin tocar nada más.
LEVEL_CURRICULUM: dict[str, list[str]] = {
    "B2-C1": [
        # Conditionals
        "second_conditional",
        "third_conditional",
        "mixed_conditionals",
        "conditionals_unless_provided_as_long_as",   # unless / provided that / as long as
        # Reported speech / passive
        "reported_speech",
        "indirect_questions",                        # Could you tell me where...
        "passive_voice",
        # Modals
        "modals_of_deduction_present",               # must / might / can't be
        "modals_of_deduction_past",                  # must / might / can't have + past participle
        # Relatives
        "relative_clauses_defining",
        "relative_clauses_non_defining",
        "reduced_relative_clauses",                  # the man sitting there / the book written by...
        # Verb patterns
        "gerunds_vs_infinitives",
        "subjunctive_suggest_recommend_insist",      # I suggest (that) he be / go...
        # Emphasis & inversion
        "inversion_negative_adverbials",             # hardly... when, no sooner, never have I...
        "cleft_sentences",                           # It was X that..., What I need is...
        "emphasis_do_does_did",                      # I do like it
        # Wish, used to, causative
        "wish_if_only",
        "used_to_be_used_to_get_used_to",
        "causative_have_get",
        # Tenses
        "future_continuous",                         # I'll be working at 5pm
        "future_perfect",
        "past_perfect_continuous",
        # Phrasal verbs / linkers / quantifiers
        "phrasal_verbs_separable",
        "linkers_advanced",                          # whereas, although, despite, however
        "linkers_purpose_result",                    # so that, in order to, such... that, so... that
        "so_such_too_enough",
        "question_tags",
        # Other
        "participle_clauses",                        # Walking home, I saw...
        "articles_advanced",
        "comparison_advanced",                       # the more... the more, far/much + comparative
    ],
    "A2-B1": [
        # Present
        "present_simple",
        "present_continuous",
        "present_simple_vs_continuous",
        "have_got",                                  # I've got a sister
        # Past
        "past_simple_regular_irregular",
        "past_continuous",
        "used_to_past_habits",                       # I used to play football
        "past_perfect_basic",                        # I had finished when...
        # Present perfect
        "present_perfect_basic",
        # Future & conditionals
        "future_will_vs_going_to",
        "zero_conditional",                          # If you heat water, it boils
        "first_conditional",                         # If it rains, I will stay
        "time_conjunctions_when_while_before_after", # when / while / before / after / until
        # Modals
        "modals_can_could",
        "modals_must_should",
        "have_to_dont_have_to",                      # obligation / no obligation
        # Quantifiers / articles
        "comparatives_superlatives",
        "articles_a_an_the",
        "countable_uncountable",
        "some_any_much_many_a_lot_of",
        "both_either_neither",
        # Prepositions
        "prepositions_of_time",                      # in / on / at
        "prepositions_of_place",                     # in / on / at / under / next to
        "prepositions_of_movement",                  # to / into / out of / through / across
        # Pronouns & possessives
        "object_pronouns",                           # me / him / her / them
        "possessive_adjectives_vs_pronouns",         # my vs mine
        "reflexive_pronouns",                        # myself, yourself...
        "demonstratives_this_that_these_those",
        "possessive_s_vs_of",
        # Other
        "adverbs_of_frequency",
        "there_is_there_are",
        "question_words",                            # who / what / where / when / why / how
        "linkers_basic",                             # and / but / or / because / so
        "so_neither_agreement",                      # me too / me neither / so do I / neither do I
        "like_love_hate_ing",                        # I love swimming
    ],
}


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def validate_lesson_payload(data: dict) -> tuple[bool, str]:
    """Comprueba que la lección generada cumple el contrato. Devuelve (ok, error)."""
    required_top = {"topic", "title", "explanation", "examples", "exercises"}
    if not isinstance(data, dict) or not required_top.issubset(data):
        return False, f"missing top-level keys: {required_top - set(data) if isinstance(data, dict) else 'not a dict'}"
    if not (isinstance(data["topic"], str) and data["topic"].strip()):
        return False, "topic empty"
    if not (isinstance(data["title"], str) and data["title"].strip()):
        return False, "title empty"
    if not (isinstance(data["explanation"], str) and len(data["explanation"]) >= 30):
        return False, "explanation too short"
    examples = data.get("examples")
    if not (isinstance(examples, list) and len(examples) >= 1):
        return False, "examples missing"
    for ex in examples:
        if not isinstance(ex, dict) or "en" not in ex or "translation" not in ex:
            return False, "example missing en/translation"
    exs = data.get("exercises")
    if not (isinstance(exs, list) and len(exs) == 5):
        return False, f"need exactly 5 exercises, got {len(exs) if isinstance(exs, list) else 'n/a'}"
    types = [e.get("type") for e in exs]
    if types != ["mc", "mc", "mc", "fill", "fill"]:
        return False, f"exercise order wrong: {types}"
    for i, e in enumerate(exs):
        if "question" not in e or not isinstance(e["question"], str):
            return False, f"ex {i}: question missing"
        if "correct" not in e or not isinstance(e["correct"], str) or not e["correct"]:
            return False, f"ex {i}: correct missing"
        if "explanation" not in e or not isinstance(e["explanation"], str):
            return False, f"ex {i}: explanation missing"
        if e["type"] == "mc":
            opts = e.get("options")
            if not (isinstance(opts, list) and len(opts) >= 2):
                return False, f"ex {i}: options missing"
            if e["correct"] not in opts:
                return False, f"ex {i}: correct '{e['correct']}' not in options"
        else:  # fill
            if not isinstance(e.get("accept", []), list):
                return False, f"ex {i}: accept not a list"
    return True, ""


def generate_lesson(chat_id: int, mode: str) -> dict:
    if not openai_client:
        raise HTTPException(status_code=500, detail="Falta OPENAI_API_KEY")

    cfg = MODES[mode]
    user_lines = fetch_user_lines(chat_id, limit=60)
    past_topics = fetch_past_lesson_topics(chat_id, limit=20)
    tone = "warm" if cfg["is_kid"] else "neutral"
    available_topics = LEVEL_CURRICULUM.get(cfg["level"], [])

    user_payload = {
        "student_name": cfg["student_name"],
        "level": cfg["level"],
        "explanation_lang": cfg["explanation_lang"],
        "native_lang": "es",
        "tone": tone,
        "recent_user_lines": user_lines,
        "available_topics": available_topics,
        "past_topics": past_topics,
    }

    last_error = ""
    for attempt in range(2):  # 1 intento + 1 reintento si validación falla
        completion = openai_client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            temperature=0.7 if attempt == 0 else 0.4,
            messages=[
                {"role": "system", "content": GRAMMAR_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        )
        try:
            data = json.loads(completion.choices[0].message.content)
        except Exception as e:
            last_error = f"JSON parse: {e}"
            continue

        ok, err = validate_lesson_payload(data)
        if ok and available_topics and data["topic"] not in available_topics:
            ok, err = False, f"topic '{data['topic']}' not in curriculum for {cfg['level']}"
        if ok:
            data["level"] = cfg["level"]
            data["lang"] = cfg["explanation_lang"]
            # Normalizar respuestas fill para comparación robusta
            for e in data["exercises"]:
                if e["type"] == "fill":
                    e["correct"] = e["correct"].strip().lower()
                    e["accept"] = [a.strip().lower() for a in e.get("accept", []) if isinstance(a, str)]
            return data
        last_error = err

    raise HTTPException(status_code=502, detail=f"Generated lesson invalid: {last_error}")


def get_or_create_today_lesson(chat_id: int, mode: str) -> dict:
    today = utc_today()
    existing = fetch_today_lesson(chat_id, today)
    if existing:
        existing["lesson_id"] = existing.pop("id")
        return existing

    lesson = generate_lesson(chat_id, mode)
    lesson_id = insert_lesson(chat_id, mode, today, lesson)
    lesson["lesson_id"] = lesson_id
    return lesson


def evaluate_answer(exercise: dict, user_answer: str | None) -> bool:
    """Comprueba en el servidor si la respuesta del alumno es correcta."""
    if user_answer is None:
        return False
    raw = str(user_answer).strip()
    if exercise.get("type") == "mc":
        return raw == exercise.get("correct")
    # fill: comparación case-insensitive, sin puntuación final
    norm = re.sub(r"[.,!?;:]+$", "", raw.strip().lower())
    accepted = [exercise.get("correct", "").strip().lower()]
    accepted += [str(a).strip().lower() for a in exercise.get("accept", [])]
    return norm in accepted


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------

app = FastAPI(title="English Tutor Voice")


@app.on_event("startup")
def on_startup():
    try:
        init_db_grammar()
    except Exception as e:
        # No queremos tirar el server si la BD aún no está lista; lo logueamos.
        print(f"[startup] init_db_grammar failed: {e}")


class TokenRequest(BaseModel):
    mode: str


class TranscriptItem(BaseModel):
    mode: str
    role: str  # "user" o "assistant"
    content: str


@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse("static/manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    # El SW debe servirse desde la raíz para tener scope sobre toda la app.
    response = FileResponse("static/sw.js", media_type="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/modes")
async def list_modes():
    return [
        {
            "id": k,
            "label": v["label"],
            "subtitle": v["subtitle"],
            "color": v["color"],
        }
        for k, v in MODES.items()
    ]


@app.post("/api/token")
async def mint_token(req: TokenRequest):
    if req.mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="Falta OPENAI_API_KEY")

    cfg = MODES[req.mode]
    chat_id = web_chat_id(req.mode)
    ensure_chat(chat_id)

    instructions = build_instructions(req.mode)

    payload = {
        "model": REALTIME_MODEL,
        "voice": cfg["voice"],
        "instructions": instructions,
        "modalities": ["audio", "text"],
        "input_audio_transcription": {"model": "whisper-1"},
        # VAD afinado para que responda como ChatGPT: poco silencio para detectar fin de turno
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.55,
            "prefix_padding_ms": 200,
            "silence_duration_ms": 250,
        },
        "temperature": 0.8,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    return r.json()


@app.post("/api/transcript")
async def save_transcript(item: TranscriptItem):
    """Guarda en la BD las frases dichas para que la memoria larga y el
    resumen semanal del bot también vean lo de la web."""
    if item.mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    if not item.content.strip():
        return {"ok": True}
    chat_id = web_chat_id(item.mode)
    ensure_chat(chat_id)
    store_message(chat_id, item.role, item.content.strip(), item.mode)
    return {"ok": True}


# ---------------------------- Grammar endpoints ------------------------------

class AttemptItem(BaseModel):
    lesson_id: int
    exercise_index: int
    user_answer: str | None = None


@app.get("/api/grammar/today")
async def grammar_today(mode: str):
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    chat_id = web_chat_id(mode)
    ensure_chat(chat_id)
    try:
        lesson = get_or_create_today_lesson(chat_id, mode)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo generar la lección: {e}")
    return lesson


@app.post("/api/grammar/attempt")
async def grammar_attempt(mode: str, item: AttemptItem):
    """Evalúa la respuesta en el servidor (no nos fiamos del cliente) y la guarda."""
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    chat_id = web_chat_id(mode)

    # Cargar la lección y comprobar que pertenece a este chat_id
    with db_cursor() as cur:
        cur.execute(
            "SELECT exercises FROM grammar_lessons WHERE id = %s AND chat_id = %s",
            (item.lesson_id, chat_id),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Lección no encontrada para este perfil")

    exercises = row["exercises"]
    if not isinstance(exercises, list) or not (0 <= item.exercise_index < len(exercises)):
        raise HTTPException(status_code=400, detail="exercise_index fuera de rango")

    is_correct = evaluate_answer(exercises[item.exercise_index], item.user_answer)

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO grammar_attempts
              (chat_id, lesson_id, exercise_index, user_answer, is_correct)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (chat_id, item.lesson_id, item.exercise_index, item.user_answer, is_correct),
        )
    return {"ok": True, "is_correct": is_correct}


# Servir estáticos al final para que las rutas API tengan prioridad
app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webapp:app", host="0.0.0.0", port=PORT, log_level="info")
