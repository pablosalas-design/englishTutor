"""Realtime voice webapp for the English tutor.

Sirve una página web sencilla donde el alumno habla con la profesora
en tiempo real (WebRTC + OpenAI Realtime API). Comparte la base de datos
con el bot de Telegram para mantener memoria larga y resúmenes semanales.
"""
import json
import os
import random
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
            -- Lecciones de "Practicar este tema": varias por día (una por tema),
            -- separadas de la lección automática del día (is_practice = FALSE).
            ALTER TABLE grammar_lessons
              ADD COLUMN IF NOT EXISTS is_practice BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE grammar_lessons
              DROP CONSTRAINT IF EXISTS grammar_lessons_chat_id_lesson_date_key;
            -- La lección automática del día es única por (chat_id, lesson_date);
            -- las de práctica son únicas por (chat_id, lesson_date, topic). Dos
            -- índices parciales evitan que una colisione/sobrescriba a la otra.
            DROP INDEX IF EXISTS uq_grammar_lessons_chat_date_topic;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_grammar_lessons_daily
              ON grammar_lessons(chat_id, lesson_date)
              WHERE is_practice = FALSE;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_grammar_lessons_practice
              ON grammar_lessons(chat_id, lesson_date, topic)
              WHERE is_practice = TRUE;
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


def init_db_vocab():
    """Crea las tablas de la pestaña Vocabulario (phrasal verbs + SRS) si no existen."""
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS phrasal_verbs (
              id SERIAL PRIMARY KEY,
              level TEXT NOT NULL,            -- 'B2-C1' o 'A2-B1'
              phrasal TEXT NOT NULL,          -- ej. "look forward to"
              meaning_es TEXT NOT NULL,       -- significado en español
              meaning_en TEXT NOT NULL,       -- definición corta en inglés
              examples JSONB NOT NULL,        -- [{"en": "...", "es": "..."}, ...]
              created_at TIMESTAMPTZ DEFAULT NOW(),
              UNIQUE(level, phrasal)
            );
            CREATE INDEX IF NOT EXISTS idx_phrasal_verbs_level
              ON phrasal_verbs(level);

            CREATE TABLE IF NOT EXISTS phrasal_progress (
              id SERIAL PRIMARY KEY,
              chat_id BIGINT NOT NULL,
              phrasal_id INTEGER NOT NULL REFERENCES phrasal_verbs(id) ON DELETE CASCADE,
              box INTEGER NOT NULL DEFAULT 1, -- 1..5 (Leitner)
              times_seen INTEGER NOT NULL DEFAULT 0,
              times_correct INTEGER NOT NULL DEFAULT 0,
              first_seen_at TIMESTAMPTZ DEFAULT NOW(),
              last_seen_at TIMESTAMPTZ,
              next_due_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              UNIQUE(chat_id, phrasal_id)
            );
            CREATE INDEX IF NOT EXISTS idx_phrasal_progress_chat
              ON phrasal_progress(chat_id, next_due_at);

            CREATE TABLE IF NOT EXISTS user_words (
              id SERIAL PRIMARY KEY,
              chat_id BIGINT NOT NULL,        -- web_chat_id por perfil (-1001/-1002/-1003)
              word TEXT NOT NULL,             -- forma normalizada en minúsculas (para deduplicar)
              display TEXT NOT NULL,          -- tal como la escribió el usuario
              meaning_es TEXT NOT NULL,       -- significado en español
              definition_en TEXT,             -- definición corta en inglés
              examples JSONB NOT NULL DEFAULT '[]', -- [{"en": "...", "es": "..."}, ...]
              box INTEGER NOT NULL DEFAULT 1, -- 1..5 (Leitner)
              times_seen INTEGER NOT NULL DEFAULT 0,
              times_correct INTEGER NOT NULL DEFAULT 0,
              source TEXT NOT NULL DEFAULT 'telegram',
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              first_seen_at TIMESTAMPTZ DEFAULT NOW(),
              last_seen_at TIMESTAMPTZ,
              next_due_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              UNIQUE(chat_id, word)
            );
            CREATE INDEX IF NOT EXISTS idx_user_words_chat
              ON user_words(chat_id, next_due_at);
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
    """La lección AUTOMÁTICA del día (no las de 'Practicar este tema')."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, topic, level, lang, title, explanation, examples, exercises
            FROM grammar_lessons
            WHERE chat_id = %s AND lesson_date = %s AND is_practice = FALSE
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, today),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def insert_lesson(chat_id: int, mode: str, today: date, lesson: dict, is_practice: bool = False) -> int:
    # Cada tipo de lección usa su propio índice parcial para que la del día y las
    # de práctica nunca se sobrescriban entre sí (ver init_db_grammar).
    if is_practice:
        conflict_clause = "ON CONFLICT (chat_id, lesson_date, topic) WHERE is_practice = TRUE"
    else:
        conflict_clause = "ON CONFLICT (chat_id, lesson_date) WHERE is_practice = FALSE"
    with db_cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO grammar_lessons
              (chat_id, mode, lesson_date, topic, level, lang, title, explanation, examples, exercises, is_practice)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            {conflict_clause} DO UPDATE SET
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
                is_practice,
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
- exercise_plan: { "num_mc": N, "num_fill": M, "total": N+M } — how many exercises to produce

Your job:
1. Pick ONE grammar topic. The "topic" field MUST be EXACTLY one of the slugs in available_topics.
   - If the input contains a non-empty "forced_topic", you MUST use EXACTLY that slug as
     the topic and build the whole lesson on it. Ignore the selection priority below.
   - Otherwise choose by this priority:
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
    // EXACTLY exercise_plan.total exercises in this order:
    // first exercise_plan.num_mc multiple choice, then exercise_plan.num_fill fill-in-the-blank.
    {
      "type": "mc",
      "question": "<English sentence with ___ where the answer goes, OR a question>",
      "options": ["<a>", "<b>", "<c>", "<d>"],
      "correct": "<one of the options, EXACT text>",
      "explanation": "<brief feedback in explanation_lang>"
    },
    ... more "mc" up to num_mc ...
    {
      "type": "fill",
      "question": "<English sentence with ___ where the answer goes>",
      "correct": "<the exact word(s) that fill the blank, lowercase, no punctuation>",
      "accept": ["<optional alternative spellings or contractions, lowercase>"],
      "explanation": "<brief feedback in explanation_lang>"
    },
    ... more "fill" up to num_fill ...
  ]
}

Rules:
- Exercises and example sentences are in ENGLISH.
- Example translations always go to native_lang (Spanish).
- Lesson explanation and exercise feedback follow explanation_lang.
- Difficulty must match the level.
- Each "fill" answer must be 1-3 words, unambiguous, and lowercase.
- Never include the answer inside the question.
- Vary the exercises so they all test the same topic from different angles.
"""


REGEN_EXERCISES_SYSTEM_PROMPT = """You are an expert English grammar coach.
The student has just finished a grammar lesson and wants to practise the SAME topic
again with a NEW set of exercises.

Inputs:
- topic, title, explanation, level, explanation_lang, native_lang, tone
- previous_exercises: the exercises the student already saw (DO NOT repeat them
  — change the sentences, vocabulary and contexts).
- exercise_plan: { "num_mc": N, "num_fill": M, "total": N+M }

Output STRICT JSON with this schema (no markdown, no extra text):
{
  "exercises": [
    // EXACTLY exercise_plan.total exercises in this order:
    // first exercise_plan.num_mc multiple choice, then exercise_plan.num_fill fill-in-the-blank.
    {
      "type": "mc",
      "question": "<English sentence with ___ where the answer goes, OR a question>",
      "options": ["<a>", "<b>", "<c>", "<d>"],
      "correct": "<one of the options, EXACT text>",
      "explanation": "<brief feedback in explanation_lang>"
    },
    {
      "type": "fill",
      "question": "<English sentence with ___ where the answer goes>",
      "correct": "<the exact word(s) that fill the blank, lowercase, no punctuation>",
      "accept": ["<optional alternative spellings or contractions, lowercase>"],
      "explanation": "<brief feedback in explanation_lang>"
    }
  ]
}

Rules:
- Exercises and example sentences are in ENGLISH.
- Feedback follows explanation_lang.
- Difficulty must match the level.
- Each "fill" answer must be 1-3 words, unambiguous, and lowercase.
- Never include the answer inside the question.
- The new exercises MUST be clearly different from previous_exercises (different sentences,
  different contexts, different vocabulary). Same grammar topic, fresh content.
"""


# Temario por nivel: el modelo SOLO puede elegir de esta lista.
# Editable sin tocar nada más.
LEVEL_CURRICULUM: dict[str, list[str]] = {
    "B2-C1": [
        # Condicionales
        "second_conditional",
        "third_conditional",
        "mixed_conditionals",
        "conditionals_unless_provided_as_long_as",   # unless / provided that / as long as
        "wish_if_only",
        # Estilo indirecto
        "reported_speech",
        "indirect_questions",                        # Could you tell me where...
        # Voz pasiva y causativos
        "passive_voice",
        "causative_have_get",                        # have/get something done
        # Modales
        "may_might_possibility_permission",          # It may rain / May I come in? / may vs might
        "modals_of_deduction_present",               # must / might / can't be
        "modals_of_deduction_past",                  # must / might / can't have + past participle
        # Relative Clauses
        "relative_clauses_defining",
        "relative_clauses_non_defining",
        "reduced_relative_clauses",                  # the man sitting there / the book written by...
        # Verb Patterns
        "gerunds_vs_infinitives",
        "used_to_be_used_to_get_used_to",
        # Subjuntivo y estructuras formales
        "subjunctive_suggest_recommend_insist",      # I suggest (that) he be / go...
        # Inversiones y énfasis
        "inversion_negative_adverbials",             # hardly... when, no sooner, never have I...
        "conditional_inversion",                     # Had I known... / Were I to... / Should you need...
        "cleft_sentences",                           # It was X that..., What I need is...
        "emphasis_do_does_did",                      # I do like it
        # Tiempos verbales
        "present_perfect_simple_vs_continuous",       # I've worked vs I've been working
        "future_continuous",                         # I'll be working at 5pm
        "future_perfect",
        "past_perfect_continuous",
        # Conectores
        "linkers_advanced",                          # whereas, although, despite, however
        "linkers_purpose_result",                    # so that, in order to, such... that, so... that
        # Intensificadores y cuantificadores
        "so_such_too_enough",
        "quantifiers_advanced",                      # few/a few, little/a little, plenty of, hardly any
        # Preguntas y conversación
        "question_tags",
        # Participios
        "participle_clauses",                        # Walking home, I saw...
        # Artículos y comparación
        "articles_advanced",
        "comparison_advanced",                       # the more... the more, far/much + comparative
        # Phrasal Verbs
        "phrasal_verbs_separable",
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


# Ejercicios por perfil. Peace (adulto): 15 (9 mc + 6 fill).
# Niñas (Lucía/Leyre): 5 (3 mc + 2 fill).
EXERCISE_PLAN_BY_MODE: dict[str, dict[str, int]] = {
    "peace":  {"num_mc": 9, "num_fill": 6},
    "lucia":  {"num_mc": 3, "num_fill": 2},
    "leyre":  {"num_mc": 3, "num_fill": 2},
}


def exercise_plan_for(mode: str) -> dict:
    plan = EXERCISE_PLAN_BY_MODE.get(mode, {"num_mc": 3, "num_fill": 2})
    return {**plan, "total": plan["num_mc"] + plan["num_fill"]}


def expected_order(plan: dict) -> list[str]:
    return ["mc"] * plan["num_mc"] + ["fill"] * plan["num_fill"]


def validate_exercises_list(exs, plan: dict) -> tuple[bool, str]:
    """Valida solo la lista de ejercicios contra el plan."""
    expected = expected_order(plan)
    if not (isinstance(exs, list) and len(exs) == plan["total"]):
        return False, f"need exactly {plan['total']} exercises, got {len(exs) if isinstance(exs, list) else 'n/a'}"
    types = [e.get("type") for e in exs]
    if types != expected:
        return False, f"exercise order wrong: got {types}, expected {expected}"
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


def validate_lesson_payload(data: dict, plan: dict) -> tuple[bool, str]:
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
    return validate_exercises_list(data.get("exercises"), plan)


def normalize_fill_answers(exs: list) -> None:
    """Normaliza las respuestas de tipo fill in-place para comparación robusta."""
    for e in exs:
        if e.get("type") == "fill":
            e["correct"] = str(e.get("correct", "")).strip().lower()
            e["accept"] = [str(a).strip().lower() for a in e.get("accept", []) if isinstance(a, str)]


def generate_lesson(chat_id: int, mode: str, forced_topic: str | None = None) -> dict:
    if not openai_client:
        raise HTTPException(status_code=500, detail="Falta OPENAI_API_KEY")

    cfg = MODES[mode]
    user_lines = fetch_user_lines(chat_id, limit=60)
    past_topics = fetch_past_lesson_topics(chat_id, limit=20)
    tone = "warm" if cfg["is_kid"] else "neutral"
    available_topics = LEVEL_CURRICULUM.get(cfg["level"], [])
    plan = exercise_plan_for(mode)

    user_payload = {
        "student_name": cfg["student_name"],
        "level": cfg["level"],
        "explanation_lang": cfg["explanation_lang"],
        "native_lang": "es",
        "tone": tone,
        "recent_user_lines": user_lines,
        "available_topics": available_topics,
        "past_topics": past_topics,
        "exercise_plan": plan,
    }
    if forced_topic:
        user_payload["forced_topic"] = forced_topic

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

        ok, err = validate_lesson_payload(data, plan)
        if ok and forced_topic:
            # Forzamos el slug exacto aunque el modelo desvíe el nombre.
            data["topic"] = forced_topic
        if ok and available_topics and data["topic"] not in available_topics:
            ok, err = False, f"topic '{data['topic']}' not in curriculum for {cfg['level']}"
        if ok:
            data["level"] = cfg["level"]
            data["lang"] = cfg["explanation_lang"]
            normalize_fill_answers(data["exercises"])
            return data
        last_error = err

    raise HTTPException(status_code=502, detail=f"Generated lesson invalid: {last_error}")


def create_practice_lesson(chat_id: int, mode: str, topic: str) -> dict:
    """Genera (y guarda) una lección para un tema concreto del temario del perfil.
    Se guarda como lección de práctica (is_practice=TRUE), separada de la del día."""
    cfg = MODES[mode]
    available_topics = LEVEL_CURRICULUM.get(cfg["level"], [])
    if topic not in available_topics:
        raise HTTPException(status_code=400, detail="Tema no válido para este nivel")

    lesson = generate_lesson(chat_id, mode, forced_topic=topic)
    lesson_id = insert_lesson(chat_id, mode, utc_today(), lesson, is_practice=True)
    lesson["lesson_id"] = lesson_id
    return lesson


# Etiquetas legibles (es) para cada slug del temario. Si falta una, se genera
# a partir del slug. Editable en un único sitio.
TOPIC_LABELS: dict[str, str] = {
    # B2-C1
    "second_conditional": "Segundo condicional",
    "third_conditional": "Tercer condicional",
    "mixed_conditionals": "Condicionales mixtos",
    "conditionals_unless_provided_as_long_as": "Condicionales: unless / provided / as long as",
    "reported_speech": "Estilo indirecto",
    "indirect_questions": "Preguntas indirectas",
    "passive_voice": "Voz pasiva",
    "may_might_possibility_permission": "may / might (posibilidad y permiso)",
    "modals_of_deduction_present": "Modales de deducción (presente)",
    "modals_of_deduction_past": "Modales de deducción (pasado)",
    "relative_clauses_defining": "Relativas especificativas",
    "relative_clauses_non_defining": "Relativas explicativas",
    "reduced_relative_clauses": "Relativas reducidas",
    "gerunds_vs_infinitives": "Gerundio vs infinitivo",
    "subjunctive_suggest_recommend_insist": "Subjuntivo (suggest/recommend/insist)",
    "inversion_negative_adverbials": "Inversión con adverbios negativos",
    "conditional_inversion": "Inversión condicional (Had I known…)",
    "cleft_sentences": "Oraciones enfáticas (cleft)",
    "emphasis_do_does_did": "Énfasis con do/does/did",
    "wish_if_only": "wish / if only",
    "used_to_be_used_to_get_used_to": "used to / be used to / get used to",
    "causative_have_get": "Causativo (have/get something done)",
    "present_perfect_simple_vs_continuous": "Presente perfecto simple vs continuo",
    "future_continuous": "Futuro continuo",
    "future_perfect": "Futuro perfecto",
    "past_perfect_continuous": "Pasado perfecto continuo",
    "phrasal_verbs_separable": "Phrasal verbs separables",
    "linkers_advanced": "Conectores avanzados",
    "linkers_purpose_result": "Conectores de propósito y resultado",
    "so_such_too_enough": "so / such / too / enough",
    "quantifiers_advanced": "Cuantificadores avanzados (few/a few, little/a little…)",
    "question_tags": "Question tags",
    "participle_clauses": "Cláusulas de participio",
    "articles_advanced": "Artículos (avanzado)",
    "comparison_advanced": "Comparación (avanzado)",
    # A2-B1
    "present_simple": "Presente simple",
    "present_continuous": "Presente continuo",
    "present_simple_vs_continuous": "Presente simple vs continuo",
    "have_got": "have got",
    "past_simple_regular_irregular": "Pasado simple (regular/irregular)",
    "past_continuous": "Pasado continuo",
    "used_to_past_habits": "used to (hábitos del pasado)",
    "past_perfect_basic": "Pasado perfecto (básico)",
    "present_perfect_basic": "Presente perfecto (básico)",
    "future_will_vs_going_to": "Futuro: will vs going to",
    "zero_conditional": "Condicional cero",
    "first_conditional": "Primer condicional",
    "time_conjunctions_when_while_before_after": "Conjunciones de tiempo (when/while/before/after)",
    "modals_can_could": "Modales: can / could",
    "modals_must_should": "Modales: must / should",
    "have_to_dont_have_to": "have to / don't have to",
    "comparatives_superlatives": "Comparativos y superlativos",
    "articles_a_an_the": "Artículos: a / an / the",
    "countable_uncountable": "Contables e incontables",
    "some_any_much_many_a_lot_of": "some / any / much / many / a lot of",
    "both_either_neither": "both / either / neither",
    "prepositions_of_time": "Preposiciones de tiempo",
    "prepositions_of_place": "Preposiciones de lugar",
    "prepositions_of_movement": "Preposiciones de movimiento",
    "object_pronouns": "Pronombres de objeto",
    "possessive_adjectives_vs_pronouns": "Posesivos: adjetivos vs pronombres",
    "reflexive_pronouns": "Pronombres reflexivos",
    "demonstratives_this_that_these_those": "Demostrativos (this/that/these/those)",
    "possessive_s_vs_of": "Posesivo 's vs of",
    "adverbs_of_frequency": "Adverbios de frecuencia",
    "there_is_there_are": "there is / there are",
    "question_words": "Palabras interrogativas",
    "linkers_basic": "Conectores básicos",
    "so_neither_agreement": "Acuerdo: so do I / neither do I",
    "like_love_hate_ing": "like / love / hate + -ing",
}


def grammar_topic_label(slug: str) -> str:
    return TOPIC_LABELS.get(slug) or slug.replace("_", " ").capitalize()


# Categorías del temario B2-C1 para el mapa de progreso.
# El orden visible lo marca LEVEL_CURRICULUM; esto solo agrupa por sección.
TOPIC_CATEGORY: dict[str, str] = {
    "second_conditional": "Condicionales",
    "third_conditional": "Condicionales",
    "mixed_conditionals": "Condicionales",
    "conditionals_unless_provided_as_long_as": "Condicionales",
    "wish_if_only": "Condicionales",
    "reported_speech": "Estilo indirecto",
    "indirect_questions": "Estilo indirecto",
    "passive_voice": "Voz pasiva y causativos",
    "causative_have_get": "Voz pasiva y causativos",
    "may_might_possibility_permission": "Modales",
    "modals_of_deduction_present": "Modales",
    "modals_of_deduction_past": "Modales",
    "relative_clauses_defining": "Relative Clauses",
    "relative_clauses_non_defining": "Relative Clauses",
    "reduced_relative_clauses": "Relative Clauses",
    "gerunds_vs_infinitives": "Verb Patterns",
    "used_to_be_used_to_get_used_to": "Verb Patterns",
    "subjunctive_suggest_recommend_insist": "Subjuntivo y estructuras formales",
    "inversion_negative_adverbials": "Inversiones y énfasis",
    "conditional_inversion": "Inversiones y énfasis",
    "cleft_sentences": "Inversiones y énfasis",
    "emphasis_do_does_did": "Inversiones y énfasis",
    "present_perfect_simple_vs_continuous": "Tiempos verbales",
    "future_continuous": "Tiempos verbales",
    "future_perfect": "Tiempos verbales",
    "past_perfect_continuous": "Tiempos verbales",
    "phrasal_verbs_separable": "Phrasal Verbs",
    "linkers_advanced": "Conectores",
    "linkers_purpose_result": "Conectores",
    "so_such_too_enough": "Intensificadores y cuantificadores",
    "quantifiers_advanced": "Intensificadores y cuantificadores",
    "question_tags": "Preguntas y conversación",
    "participle_clauses": "Participios",
    "articles_advanced": "Artículos y comparación",
    "comparison_advanced": "Artículos y comparación",
}


# Umbrales para considerar un tema "dominado" en el mapa de progreso.
GRAMMAR_MASTERY_MIN_ATTEMPTS = 6
GRAMMAR_MASTERY_MIN_ACCURACY = 0.8


def build_grammar_progress(chat_id: int, mode: str) -> dict:
    """Mapa de progreso del temario del nivel del perfil.

    Cada tema queda en uno de tres estados:
      - not_started: nunca ha salido en una lección de este perfil.
      - in_progress: ya lo ha visto pero aún no lo domina.
      - mastered: suficientes intentos y precisión alta.
    """
    cfg = MODES[mode]
    level = cfg["level"]
    slugs = LEVEL_CURRICULUM.get(level, [])

    stats: dict[str, dict] = {}
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT l.topic,
                   COUNT(a.id) AS attempts,
                   COALESCE(SUM(CASE WHEN a.is_correct THEN 1 ELSE 0 END), 0) AS correct,
                   COUNT(DISTINCT l.id) AS lessons
            FROM grammar_lessons l
            LEFT JOIN grammar_attempts a ON a.lesson_id = l.id
            WHERE l.chat_id = %s
            GROUP BY l.topic
            """,
            (chat_id,),
        )
        for r in cur.fetchall():
            stats[r["topic"]] = dict(r)

    topics = []
    counts = {"mastered": 0, "in_progress": 0, "not_started": 0}
    for slug in slugs:
        s = stats.get(slug)
        attempts = int(s["attempts"]) if s else 0
        correct = int(s["correct"]) if s else 0
        accuracy = round(correct / attempts * 100) if attempts else 0
        if not s:
            status = "not_started"
        elif attempts >= GRAMMAR_MASTERY_MIN_ATTEMPTS and (correct / attempts) >= GRAMMAR_MASTERY_MIN_ACCURACY:
            status = "mastered"
        else:
            status = "in_progress"
        counts[status] += 1
        topics.append({
            "slug": slug,
            "label": grammar_topic_label(slug),
            "category": TOPIC_CATEGORY.get(slug),
            "status": status,
            "attempts": attempts,
            "correct": correct,
            "accuracy": accuracy,
        })

    return {
        "level": level,
        "mode": mode,
        "summary": {"total": len(slugs), **counts},
        "topics": topics,
    }


def regenerate_exercises_for_lesson(chat_id: int, mode: str, lesson_id: int) -> dict:
    """Genera un nuevo set de ejercicios para una lección existente (mismo tema).
    Actualiza la fila en BD y borra los intentos anteriores para esa lección.
    Devuelve la lección con los ejercicios nuevos."""
    if not openai_client:
        raise HTTPException(status_code=500, detail="Falta OPENAI_API_KEY")

    cfg = MODES[mode]
    plan = exercise_plan_for(mode)

    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, topic, level, lang, title, explanation, examples, exercises
            FROM grammar_lessons
            WHERE id = %s AND chat_id = %s
            """,
            (lesson_id, chat_id),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Lección no encontrada para este perfil")

    tone = "warm" if cfg["is_kid"] else "neutral"
    user_payload = {
        "topic": row["topic"],
        "title": row["title"],
        "explanation": row["explanation"],
        "level": row["level"],
        "explanation_lang": row["lang"],
        "native_lang": "es",
        "tone": tone,
        "previous_exercises": row["exercises"],
        "exercise_plan": plan,
    }

    last_error = ""
    new_exercises = None
    for attempt in range(2):
        completion = openai_client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            temperature=0.85 if attempt == 0 else 0.6,
            messages=[
                {"role": "system", "content": REGEN_EXERCISES_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        )
        try:
            data = json.loads(completion.choices[0].message.content)
        except Exception as e:
            last_error = f"JSON parse: {e}"
            continue
        exs = data.get("exercises") if isinstance(data, dict) else None
        ok, err = validate_exercises_list(exs, plan)
        if ok:
            new_exercises = exs
            break
        last_error = err

    if new_exercises is None:
        raise HTTPException(status_code=502, detail=f"Regenerated exercises invalid: {last_error}")

    normalize_fill_answers(new_exercises)

    with db_cursor() as cur:
        cur.execute(
            "UPDATE grammar_lessons SET exercises = %s WHERE id = %s AND chat_id = %s",
            (json.dumps(new_exercises), lesson_id, chat_id),
        )
        # Reseteamos los intentos de esta lección para que el "repetir" sea limpio.
        cur.execute(
            "DELETE FROM grammar_attempts WHERE lesson_id = %s AND chat_id = %s",
            (lesson_id, chat_id),
        )

    return {
        "lesson_id": lesson_id,
        "topic": row["topic"],
        "level": row["level"],
        "lang": row["lang"],
        "title": row["title"],
        "explanation": row["explanation"],
        "examples": row["examples"],
        "exercises": new_exercises,
    }


def get_or_create_today_lesson(chat_id: int, mode: str) -> dict:
    today = utc_today()
    existing = fetch_today_lesson(chat_id, today)
    plan = exercise_plan_for(mode)
    if existing:
        existing["lesson_id"] = existing.pop("id")
        # Auto-curación: si la lección cacheada se hizo con un número de ejercicios
        # distinto al plan actual del perfil, regenera los ejercicios al vuelo
        # manteniendo el mismo tema, título y explicación.
        exs = existing.get("exercises")
        if not isinstance(exs, list) or len(exs) != plan["total"]:
            try:
                fixed = regenerate_exercises_for_lesson(chat_id, mode, existing["lesson_id"])
                return fixed
            except Exception as e:
                print(f"[grammar] auto-heal exercises failed: {e}")
                # Si falla, devolvemos la lección antigua igualmente para no romper la pantalla.
        return existing

    lesson = generate_lesson(chat_id, mode)
    lesson_id = insert_lesson(chat_id, mode, today, lesson)
    lesson["lesson_id"] = lesson_id
    return lesson


def normalize_fill_text(s: str) -> str:
    """Normaliza una respuesta de rellenar: minúsculas, apóstrofos tipográficos
    (´ ` ’ ‘ …) unificados a recto ', espacios colapsados y puntuación externa fuera."""
    if not s:
        return ""
    s = str(s).lower()
    for ch in ["´", "`", "’", "‘", "ʼ", "‛", "\u2032"]:
        s = s.replace(ch, "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(".,!?;:\"()[]").strip()


def evaluate_answer(exercise: dict, user_answer: str | None) -> bool:
    """Comprueba en el servidor si la respuesta del alumno es correcta."""
    if user_answer is None:
        return False
    raw = str(user_answer).strip()
    if exercise.get("type") == "mc":
        return raw == exercise.get("correct")
    # fill: tolerante a apóstrofos tipográficos, espacios y a escribir la frase entera
    norm = normalize_fill_text(raw)
    accepted = [normalize_fill_text(exercise.get("correct", ""))]
    accepted += [normalize_fill_text(a) for a in exercise.get("accept", [])]
    accepted = [a for a in accepted if a]
    if norm in accepted:
        return True
    # Leniencia: el alumno escribió toda la frase en vez de solo la palabra del hueco.
    for a in accepted:
        if re.search(r"(^|[^\w'])" + re.escape(a) + r"([^\w']|$)", norm):
            return True
    return False


# ----------------------------------------------------------------------------
# Vocabulary (phrasal verbs con repetición espaciada Leitner)
# ----------------------------------------------------------------------------

# Mapeo de modo → nivel de phrasal verbs
MODE_TO_VOCAB_LEVEL = {
    "peace": "B2-C1",
    "lucia": "A2-B1",
    "leyre": "A2-B1",
}

# Cuántos phrasal verbs nuevos por sesión (perfil) y cuántos repasos máximo.
VOCAB_PLAN_BY_MODE = {
    "peace": {"new": 5, "max_reviews": 10, "target_exercises": 15},
    "lucia": {"new": 3, "max_reviews": 6},
    "leyre": {"new": 3, "max_reviews": 6},
}

# Intervalos Leitner (días) por caja: caja 1 = mañana, caja 5 = en un mes.
LEITNER_INTERVALS_DAYS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}

# Tamaño de la "tanda" inicial de seed por nivel y de cada ampliación.
VOCAB_SEED_BATCH = 40
VOCAB_REFILL_THRESHOLD = 8   # si quedan menos de N nuevos para este alumno, generar más
VOCAB_REFILL_BATCH = 20


def vocab_plan_for(mode: str) -> dict:
    return VOCAB_PLAN_BY_MODE.get(mode, VOCAB_PLAN_BY_MODE["peace"])


def vocab_level_for(mode: str) -> str:
    return MODE_TO_VOCAB_LEVEL.get(mode, "B2-C1")


VOCAB_SEED_SYSTEM_PROMPT = (
    "You are an English curriculum designer. Generate a list of useful, common phrasal verbs "
    "for a Spanish-speaking learner. Each phrasal verb must include: the phrasal verb itself "
    "(in lowercase, no parentheses, just the words; if it is separable use the canonical form "
    "like 'pick up'), a short Spanish meaning (3-7 words), a short English definition (5-12 words), "
    "and exactly two example sentences. Each example has the English sentence and a natural Spanish "
    "translation. Sentences must be short, natural and conversational, not textbook-style. "
    "Avoid offensive, sexual, violent, political or scary content. "
    "Do NOT include phrasal verbs that are too informal, vulgar, or regional. "
    "Return ONLY valid JSON: an object with key 'items', whose value is an array of objects "
    "with keys: phrasal, meaning_es, meaning_en, examples (each example: {en, es})."
)


def _vocab_seed_user_prompt(level: str, n: int, exclude: list[str]) -> str:
    if level == "B2-C1":
        audience = (
            "an adult Spanish speaker at level B2-C1 who works in technology, business and design. "
            "Choose phrasal verbs that are genuinely common in everyday adult conversation, work "
            "meetings, films and news. Mix everyday ones (run out of, get along with) with slightly "
            "more advanced ones (pull off, weigh in, brush up on)."
        )
    else:
        audience = (
            "a Spanish girl aged 11-13 at level A2-B1. Choose simple, very common phrasal verbs "
            "useful for school, family, friends, hobbies, animals, food and daily routines. "
            "Avoid anything mature, complex, or work-related."
        )

    exclude_block = ""
    if exclude:
        exclude_block = (
            "\n\nDo NOT include any of these phrasal verbs (already in the system):\n- "
            + "\n- ".join(sorted(exclude))
        )

    return (
        f"Generate exactly {n} phrasal verbs for {audience}"
        + exclude_block
        + "\n\nReturn JSON only, with the schema described in the system message."
    )


def fetch_existing_phrasals(level: str) -> list[str]:
    with db_cursor() as cur:
        cur.execute("SELECT phrasal FROM phrasal_verbs WHERE level = %s", (level,))
        return [r["phrasal"] for r in cur.fetchall()]


def _validate_vocab_items(items) -> list[dict]:
    if not isinstance(items, list):
        return []
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        phrasal = (it.get("phrasal") or "").strip().lower()
        meaning_es = (it.get("meaning_es") or "").strip()
        meaning_en = (it.get("meaning_en") or "").strip()
        examples = it.get("examples") or []
        if not (phrasal and meaning_es and meaning_en and isinstance(examples, list)):
            continue
        ex_clean = []
        for ex in examples:
            if not isinstance(ex, dict):
                continue
            en = (ex.get("en") or "").strip()
            es = (ex.get("es") or "").strip()
            if en and es:
                ex_clean.append({"en": en, "es": es})
        if len(ex_clean) < 2:
            continue
        clean.append({
            "phrasal": phrasal,
            "meaning_es": meaning_es,
            "meaning_en": meaning_en,
            "examples": ex_clean[:2],
        })
    return clean


def generate_phrasal_batch(level: str, n: int) -> list[dict]:
    """Pide a OpenAI una tanda de phrasal verbs para `level`, excluyendo los ya existentes."""
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY no configurado")
    existing = fetch_existing_phrasals(level)
    user_prompt = _vocab_seed_user_prompt(level, n, existing)
    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": VOCAB_SEED_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.8,
    )
    data = json.loads(resp.choices[0].message.content)
    items = _validate_vocab_items(data.get("items"))
    inserted = []
    with db_cursor() as cur:
        for it in items:
            cur.execute(
                """
                INSERT INTO phrasal_verbs (level, phrasal, meaning_es, meaning_en, examples)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (level, phrasal) DO NOTHING
                RETURNING id, phrasal, meaning_es, meaning_en, examples
                """,
                (level, it["phrasal"], it["meaning_es"], it["meaning_en"], json.dumps(it["examples"])),
            )
            row = cur.fetchone()
            if row:
                inserted.append(dict(row))
    print(f"[vocab] Generated {len(items)} items for {level}, inserted {len(inserted)} new ones.")
    return inserted


def _vocab_lock_key(level: str) -> int:
    """Clave estable y determinista por nivel para pg_advisory_lock (cabe en bigint)."""
    import hashlib
    digest = hashlib.sha1(f"vocab:{level}".encode("utf-8")).digest()
    # Usamos 4 bytes (32 bits sin signo) y nos quedamos con un int positivo de hasta 31 bits
    # para que encaje sin riesgo en cualquier interpretación de bigint.
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def ensure_vocab_pool_for_user(chat_id: int, level: str):
    """Asegura que haya phrasal verbs nuevos disponibles (no vistos por este alumno).

    Usa un advisory lock por nivel para que dos peticiones concurrentes no disparen
    dos llamadas a OpenAI a la vez.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM phrasal_verbs pv
            LEFT JOIN phrasal_progress pp
              ON pp.phrasal_id = pv.id AND pp.chat_id = %s
            WHERE pv.level = %s AND pp.id IS NULL
            """,
            (chat_id, level),
        )
        unseen = cur.fetchone()["c"]

    if unseen >= VOCAB_REFILL_THRESHOLD:
        return

    lock_key = _vocab_lock_key(level)
    with db_cursor() as cur:
        # Espera (bloqueante) al lock por nivel — la otra petición saldrá del with
        # y aquí re-evaluamos si todavía hace falta generar.
        cur.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        try:
            cur.execute(
                """
                SELECT COUNT(*) AS c
                FROM phrasal_verbs pv
                LEFT JOIN phrasal_progress pp
                  ON pp.phrasal_id = pv.id AND pp.chat_id = %s
                WHERE pv.level = %s AND pp.id IS NULL
                """,
                (chat_id, level),
            )
            unseen2 = cur.fetchone()["c"]
            if unseen2 >= VOCAB_REFILL_THRESHOLD:
                # Otra petición ya rellenó mientras esperábamos.
                return
            cur.execute("SELECT COUNT(*) AS c FROM phrasal_verbs WHERE level = %s", (level,))
            total = cur.fetchone()["c"]
            batch = VOCAB_SEED_BATCH if total == 0 else VOCAB_REFILL_BATCH
            try:
                generate_phrasal_batch(level, batch)
            except Exception as e:
                print(f"[vocab] generate_phrasal_batch failed: {e}")
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


def fetch_due_reviews(chat_id: int, level: str, limit: int) -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT pv.id, pv.phrasal, pv.meaning_es, pv.meaning_en, pv.examples,
                   pp.box, pp.times_seen, pp.times_correct
            FROM phrasal_progress pp
            JOIN phrasal_verbs pv ON pv.id = pp.phrasal_id
            WHERE pp.chat_id = %s
              AND pv.level = %s
              AND pp.next_due_at <= NOW()
            ORDER BY pp.next_due_at ASC
            LIMIT %s
            """,
            (chat_id, level, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_new_for_user(chat_id: int, level: str, limit: int) -> list[dict]:
    """Phrasal verbs que este alumno aún no ha visto."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT pv.id, pv.phrasal, pv.meaning_es, pv.meaning_en, pv.examples
            FROM phrasal_verbs pv
            LEFT JOIN phrasal_progress pp
              ON pp.phrasal_id = pv.id AND pp.chat_id = %s
            WHERE pv.level = %s AND pp.id IS NULL
            ORDER BY pv.id ASC
            LIMIT %s
            """,
            (chat_id, level, limit),
        )
        return [dict(r) for r in cur.fetchall()]


GENERIC_DISTRACTORS = [
    "Hacer una compra",
    "Olvidar algo importante",
    "Cambiar de tema",
    "Dormir profundamente",
    "Llegar tarde a un sitio",
    "Hablar más bajo",
    "Pedir un favor",
    "Esconder algo",
    "Abrir una ventana",
    "Recordar el pasado",
]


def fetch_distractor_meanings(
    level: str,
    exclude_ids: list[int],
    exclude_meanings: list[str],
    n: int = 3,
) -> list[str]:
    """Significados (es) de OTROS phrasal verbs del mismo nivel, para usar como distractores.

    Filtra significados que coinciden con los de los items en juego (incluida la
    respuesta correcta) para que ningún distractor sea ambiguo o duplicado.
    """
    safe_exclude_ids = list(exclude_ids) if exclude_ids else [-1]
    safe_exclude_meanings = [m for m in (exclude_meanings or []) if m]
    if not safe_exclude_meanings:
        safe_exclude_meanings = [""]
    with db_cursor() as cur:
        # En vez de SELECT DISTINCT + ORDER BY random() (no válido en PG), tomamos
        # una muestra aleatoria amplia y deduplicamos en Python.
        cur.execute(
            """
            SELECT meaning_es FROM phrasal_verbs
            WHERE level = %s
              AND id <> ALL(%s)
              AND meaning_es <> ALL(%s)
            ORDER BY random()
            LIMIT %s
            """,
            (level, safe_exclude_ids, safe_exclude_meanings, max(n * 3, 12)),
        )
        rows = [r["meaning_es"] for r in cur.fetchall()]
        seen: set[str] = set()
        out: list[str] = []
        for m in rows:
            if m not in seen:
                seen.add(m)
                out.append(m)
            if len(out) >= n:
                break
        return out


def normalize_phrasal_text(s: str) -> str:
    """Normaliza para comparar: minúsculas, sin signos de puntuación, espacios colapsados."""
    if not s:
        return ""
    s = s.lower().replace("-", " ").replace("'", "'").replace("’", "'")
    # Unifica apóstrofos tipográficos (´ ` ’ ‘ …) a recto '
    for ap in ["´", "`", "’", "‘", "ʼ", "‛", "\u2032"]:
        s = s.replace(ap, "'")
    # Quita puntuación común
    for ch in [".", ",", ";", ":", "!", "?", '"', "(", ")"]:
        s = s.replace(ch, " ")
    return re.sub(r"\s+", " ", s).strip()


def make_cloze_for_phrasal(example_en: str, phrasal: str) -> str | None:
    """Devuelve la frase en inglés con el phrasal sustituido por '_____', o None si no se encuentra.

    Acepta formas conjugadas comunes del primer verbo (s, ed, ing) y la base.
    No cubre todos los irregulares; en ese caso devuelve None y se usará otro tipo de ejercicio.
    """
    if not example_en or not phrasal:
        return None
    parts = phrasal.lower().split()
    if not parts:
        return None
    verb = parts[0]
    rest = parts[1:]
    if len(verb) < 2:
        return None
    forms: set[str] = {verb, verb + "s", verb + "ed", verb + "ing"}
    if verb.endswith("e"):
        forms.add(verb[:-1] + "ing")  # take -> taking
        forms.add(verb + "d")          # take -> taked? mejor no, pero coge "made", etc.
    if verb.endswith("y"):
        forms.add(verb[:-1] + "ies")
        forms.add(verb[:-1] + "ied")
    pattern_verb = "(" + "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)) + ")"
    pattern_rest = ""
    if rest:
        pattern_rest = r"\s+" + r"\s+".join(re.escape(p) for p in rest)
    pattern = re.compile(rf"\b{pattern_verb}{pattern_rest}\b", re.IGNORECASE)
    m = pattern.search(example_en)
    if not m:
        return None
    return example_en[:m.start()] + "_____" + example_en[m.end():]


def build_meaning_mc_exercise(it: dict, level: str, item_ids: list[int]) -> dict:
    """Ejercicio de elección múltiple: dado el phrasal, elige el significado en español."""
    correct = it["meaning_es"]
    candidates = fetch_distractor_meanings(
        level, item_ids, exclude_meanings=[correct], n=8
    )
    distractors: list[str] = []
    seen = {correct}
    for c in candidates:
        if c not in seen:
            distractors.append(c)
            seen.add(c)
        if len(distractors) >= 3:
            break
    for g in GENERIC_DISTRACTORS:
        if len(distractors) >= 3:
            break
        if g not in seen:
            distractors.append(g)
            seen.add(g)
    options = [correct] + distractors[:3]
    random.shuffle(options)
    question_en = (
        f"What does \"{it['phrasal']}\" mean?"
        if level == "B2-C1"
        else f"¿Qué significa \"{it['phrasal']}\"?"
    )
    return {
        "phrasal_id": it["id"],
        "phrasal": it["phrasal"],
        "type": "meaning_mc",
        "question": question_en,
        "options": options,
        "correct": correct,
        "explanation": it.get("meaning_en", ""),
        "examples": it.get("examples", []),
    }


def build_phrasal_write_exercise(it: dict, level: str) -> dict | None:
    """Ejercicio de escritura: ver pista (significado en es + frase en inglés con hueco) y escribir el phrasal."""
    examples = it.get("examples") or []
    cloze = None
    chosen_example = None
    for ex in examples:
        en = (ex or {}).get("en", "") if isinstance(ex, dict) else ""
        c = make_cloze_for_phrasal(en, it["phrasal"])
        if c:
            cloze = c
            chosen_example = ex
            break
    if not cloze:
        return None  # señalamos al caller que use otro tipo
    instruction_es = "Escribe el phrasal verb que falta. Pista en español:"
    return {
        "phrasal_id": it["id"],
        "phrasal": it["phrasal"],
        "type": "phrasal_write",
        "instruction": instruction_es,
        "hint_es": it["meaning_es"],
        "cloze_en": cloze,
        "correct": it["phrasal"],
        "explanation": it.get("meaning_en", ""),
        "examples": examples,
    }


def build_vocab_exercises(items: list[dict], level: str, target: int | None = None) -> list[dict]:
    """Construye ejercicios mezclando elección múltiple y escritura.

    Genera ambas variantes (MC + escritura, si hay hueco posible) por cada phrasal,
    y devuelve hasta `target` ejercicios. Si hay menos variantes únicas que `target`,
    repite phrasals para completar (útil para sesiones cortas con pocos items nuevos).
    Si `target` es None, devuelve 1 ejercicio por phrasal (comportamiento clásico).
    """
    if not items:
        return []
    item_ids = [it["id"] for it in items]

    if target is None:
        exercises: list[dict] = []
        for idx, it in enumerate(items):
            prefer_write = (idx % 2 == 1)
            ex = build_phrasal_write_exercise(it, level) if prefer_write else None
            if ex is None:
                ex = build_meaning_mc_exercise(it, level, item_ids)
            exercises.append(ex)
        random.shuffle(exercises)
        return exercises

    # Pool con ambas variantes por phrasal (sin duplicar phrasal+tipo).
    pool: list[dict] = []
    for it in items:
        pool.append(build_meaning_mc_exercise(it, level, item_ids))
        wr = build_phrasal_write_exercise(it, level)
        if wr:
            pool.append(wr)
    random.shuffle(pool)

    if len(pool) >= target:
        return pool[:target]

    # No hay suficientes variantes únicas: completamos repitiendo del pool.
    out = list(pool)
    if pool:
        while len(out) < target:
            out.append(random.choice(pool))
    random.shuffle(out)
    return out[:target]


def build_today_vocab_session(chat_id: int, mode: str) -> dict:
    """Devuelve {study: [...], reviews: [...], exercises: [...]} para la sesión de hoy."""
    level = vocab_level_for(mode)
    plan = vocab_plan_for(mode)

    ensure_vocab_pool_for_user(chat_id, level)

    new_items = fetch_new_for_user(chat_id, level, plan["new"])
    reviews = fetch_due_reviews(chat_id, level, plan["max_reviews"])

    # Mezclamos new + reviews para los ejercicios; el bloque "study" sólo lleva los nuevos.
    practice_items = new_items + reviews
    target = plan.get("target_exercises")
    exercises = build_vocab_exercises(practice_items, level, target=target)

    return {
        "level": level,
        "mode": mode,
        "study": new_items,         # tarjetas nuevas que ver antes de practicar
        "reviews_count": len(reviews),
        "exercises": exercises,
        "totals": {
            "new": len(new_items),
            "reviews": len(reviews),
            "exercises": len(exercises),
        },
    }


def evaluate_vocab_answer(exercise: dict, user_answer: str | None) -> bool:
    if user_answer is None:
        return False
    return user_answer.strip() == (exercise.get("correct") or "").strip()


def update_phrasal_progress(chat_id: int, phrasal_id: int, is_correct: bool) -> dict:
    """Actualiza la caja Leitner y la próxima fecha. Devuelve el progreso resultante."""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO phrasal_progress
              (chat_id, phrasal_id, box, times_seen, times_correct, last_seen_at, next_due_at)
            VALUES (%s, %s, %s, 1, %s, NOW(), NOW() + (%s || ' days')::interval)
            ON CONFLICT (chat_id, phrasal_id) DO UPDATE SET
              box = LEAST(5, GREATEST(1,
                CASE WHEN %s THEN phrasal_progress.box + 1 ELSE 1 END
              )),
              times_seen = phrasal_progress.times_seen + 1,
              times_correct = phrasal_progress.times_correct + CASE WHEN %s THEN 1 ELSE 0 END,
              last_seen_at = NOW(),
              next_due_at = NOW() + (
                CASE LEAST(5, GREATEST(1,
                  CASE WHEN %s THEN phrasal_progress.box + 1 ELSE 1 END
                ))
                  WHEN 1 THEN INTERVAL '1 day'
                  WHEN 2 THEN INTERVAL '3 days'
                  WHEN 3 THEN INTERVAL '7 days'
                  WHEN 4 THEN INTERVAL '14 days'
                  ELSE INTERVAL '30 days'
                END
              )
            RETURNING box, times_seen, times_correct, next_due_at
            """,
            (
                chat_id, phrasal_id,
                2 if is_correct else 1,
                1 if is_correct else 0,
                LEITNER_INTERVALS_DAYS[2 if is_correct else 1],
                is_correct, is_correct, is_correct,
            ),
        )
        return dict(cur.fetchone())


# ----------------------------------------------------------------------------
# "Mis palabras" — lista personal de vocabulario (alimentada desde Telegram)
# ----------------------------------------------------------------------------

def fetch_user_words_due(chat_id: int, limit: int) -> list[dict]:
    """Palabras cuyo repaso ya vence (next_due_at <= ahora)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, word, display, meaning_es, definition_en, examples,
                   box, times_seen, times_correct
            FROM user_words
            WHERE chat_id = %s AND times_seen > 0 AND next_due_at <= NOW()
            ORDER BY next_due_at ASC
            LIMIT %s
            """,
            (chat_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_user_words_new(chat_id: int, limit: int) -> list[dict]:
    """Palabras recién añadidas que aún no se han practicado (times_seen = 0)."""
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, word, display, meaning_es, definition_en, examples,
                   box, times_seen, times_correct
            FROM user_words
            WHERE chat_id = %s AND times_seen = 0
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (chat_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def count_user_words(chat_id: int) -> dict:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE next_due_at <= NOW()) AS due,
              COUNT(*) FILTER (WHERE times_seen = 0) AS new
            FROM user_words WHERE chat_id = %s
            """,
            (chat_id,),
        )
        row = cur.fetchone()
        return {
            "total": int(row["total"]),
            "due": int(row["due"]),
            "new": int(row["new"]),
        }


def fetch_user_word_distractors(
    chat_id: int, exclude_ids: list[int], exclude_meanings: list[str], n: int = 3
) -> list[str]:
    """Significados (es) de OTRAS palabras del mismo usuario, como distractores."""
    safe_ids = list(exclude_ids) if exclude_ids else [-1]
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT meaning_es FROM user_words
            WHERE chat_id = %s AND id <> ALL(%s)
            ORDER BY random()
            LIMIT 30
            """,
            (chat_id, safe_ids),
        )
        rows = [r["meaning_es"] for r in cur.fetchall()]
    seen = {m for m in (exclude_meanings or []) if m}
    out: list[str] = []
    for m in rows:
        if m and m not in seen:
            out.append(m)
            seen.add(m)
        if len(out) >= n:
            break
    return out


def build_myword_mc_exercise(it: dict, chat_id: int, item_ids: list[int]) -> dict:
    correct = it["meaning_es"]
    distractors = fetch_user_word_distractors(chat_id, item_ids, [correct], n=3)
    seen = {correct, *distractors}
    for g in GENERIC_DISTRACTORS:
        if len(distractors) >= 3:
            break
        if g not in seen:
            distractors.append(g)
            seen.add(g)
    options = [correct] + distractors[:3]
    random.shuffle(options)
    return {
        "word_id": it["id"],
        "word": it["display"],
        "type": "meaning_mc",
        "question": f"What does \"{it['display']}\" mean?",
        "options": options,
        "correct": correct,
        "explanation": it.get("definition_en", "") or "",
        "examples": it.get("examples", []) or [],
    }


def build_myword_write_exercise(it: dict) -> dict:
    examples = it.get("examples") or []
    cloze = None
    for ex in examples:
        en = (ex or {}).get("en", "") if isinstance(ex, dict) else ""
        c = make_cloze_for_phrasal(en, it["word"])
        if c:
            cloze = c
            break
    return {
        "word_id": it["id"],
        "word": it["display"],
        "type": "word_write",
        "instruction": "Escribe la palabra o expresión en inglés. Pista en español:",
        "hint_es": it["meaning_es"],
        "cloze_en": cloze or "",
        "correct": it["display"],
        "explanation": it.get("definition_en", "") or "",
        "examples": examples,
    }


def build_mywords_exercises(items: list[dict], chat_id: int, target: int | None = None) -> list[dict]:
    """Mezcla ejercicios de elección múltiple y de escritura para la lista personal."""
    if not items:
        return []
    item_ids = [it["id"] for it in items]

    if target is None:
        exercises: list[dict] = []
        for idx, it in enumerate(items):
            ex = (
                build_myword_write_exercise(it)
                if idx % 2 == 1
                else build_myword_mc_exercise(it, chat_id, item_ids)
            )
            exercises.append(ex)
        random.shuffle(exercises)
        return exercises

    pool: list[dict] = []
    for it in items:
        pool.append(build_myword_mc_exercise(it, chat_id, item_ids))
        pool.append(build_myword_write_exercise(it))
    random.shuffle(pool)
    if len(pool) >= target:
        return pool[:target]
    out = list(pool)
    if pool:
        while len(out) < target:
            out.append(random.choice(pool))
    random.shuffle(out)
    return out[:target]


def build_today_mywords_session(chat_id: int, mode: str) -> dict:
    """Sesión de hoy de Mis palabras: nuevas para estudiar + repasos vencidos + ejercicios."""
    plan = vocab_plan_for(mode)
    counts = count_user_words(chat_id)

    new_items = fetch_user_words_new(chat_id, plan["new"])
    reviews = fetch_user_words_due(chat_id, plan["max_reviews"])
    seen_ids = {it["id"] for it in new_items}
    reviews = [r for r in reviews if r["id"] not in seen_ids]

    practice_items = new_items + reviews
    target = plan.get("target_exercises")
    exercises = build_mywords_exercises(practice_items, chat_id, target=target)

    return {
        "mode": mode,
        "study": new_items,
        "reviews_count": len(reviews),
        "exercises": exercises,
        "totals": {
            "new": len(new_items),
            "reviews": len(reviews),
            "exercises": len(exercises),
            "all": counts["total"],
        },
    }


def update_user_word_progress(chat_id: int, word_id: int, is_correct: bool) -> dict:
    """Actualiza la caja Leitner de una palabra personal. Devuelve el progreso."""
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE user_words SET
              box = LEAST(5, GREATEST(1, CASE WHEN %s THEN box + 1 ELSE 1 END)),
              times_seen = times_seen + 1,
              times_correct = times_correct + CASE WHEN %s THEN 1 ELSE 0 END,
              last_seen_at = NOW(),
              next_due_at = NOW() + (
                CASE LEAST(5, GREATEST(1, CASE WHEN %s THEN box + 1 ELSE 1 END))
                  WHEN 1 THEN INTERVAL '1 day'
                  WHEN 2 THEN INTERVAL '3 days'
                  WHEN 3 THEN INTERVAL '7 days'
                  WHEN 4 THEN INTERVAL '14 days'
                  ELSE INTERVAL '30 days'
                END
              )
            WHERE chat_id = %s AND id = %s
            RETURNING box, times_seen, times_correct, next_due_at
            """,
            (is_correct, is_correct, is_correct, chat_id, word_id),
        )
        row = cur.fetchone()
        return dict(row) if row else {}


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
    try:
        init_db_vocab()
    except Exception as e:
        print(f"[startup] init_db_vocab failed: {e}")


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


class RegenerateItem(BaseModel):
    lesson_id: int


class PracticeItem(BaseModel):
    topic: str


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


@app.post("/api/grammar/regenerate")
async def grammar_regenerate(mode: str, item: RegenerateItem):
    """Regenera el set de ejercicios de una lección (mismo tema, ejercicios nuevos)."""
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    chat_id = web_chat_id(mode)
    try:
        return regenerate_exercises_for_lesson(chat_id, mode, item.lesson_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudieron regenerar los ejercicios: {e}")


@app.get("/api/grammar/progress")
async def grammar_progress(mode: str):
    """Mapa de progreso del temario para el perfil (semáforo + resumen)."""
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    chat_id = web_chat_id(mode)
    ensure_chat(chat_id)
    try:
        return build_grammar_progress(chat_id, mode)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo calcular el progreso: {e}")


@app.post("/api/grammar/practice")
async def grammar_practice(mode: str, item: PracticeItem):
    """Genera y devuelve una lección para un tema concreto del temario."""
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    chat_id = web_chat_id(mode)
    ensure_chat(chat_id)
    try:
        return create_practice_lesson(chat_id, mode, item.topic)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo preparar la práctica: {e}")


# ---------------------------- Vocab endpoints ------------------------------

class VocabAnswerItem(BaseModel):
    phrasal_id: int
    user_answer: str | None = None
    exercise_type: str | None = "meaning_mc"  # "meaning_mc" o "phrasal_write"


@app.get("/api/vocab/today")
async def vocab_today(mode: str):
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    chat_id = web_chat_id(mode)
    ensure_chat(chat_id)
    try:
        session = build_today_vocab_session(chat_id, mode)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo preparar la sesión: {e}")
    return session


@app.post("/api/vocab/answer")
async def vocab_answer(mode: str, item: VocabAnswerItem):
    """Evalúa la respuesta y actualiza la caja Leitner del alumno para ese phrasal."""
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    chat_id = web_chat_id(mode)
    level = vocab_level_for(mode)

    with db_cursor() as cur:
        cur.execute(
            "SELECT id, phrasal, meaning_es FROM phrasal_verbs WHERE id = %s AND level = %s",
            (item.phrasal_id, level),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Phrasal verb no encontrado para este nivel")

    user_ans = (item.user_answer or "").strip()
    if (item.exercise_type or "meaning_mc") == "phrasal_write":
        is_correct = normalize_phrasal_text(user_ans) == normalize_phrasal_text(row["phrasal"])
    else:
        # meaning_mc: comparar contra el significado en español
        is_correct = user_ans == (row["meaning_es"] or "").strip()

    progress = update_phrasal_progress(chat_id, item.phrasal_id, is_correct)
    progress["next_due_at"] = progress["next_due_at"].isoformat()
    return {
        "ok": True,
        "is_correct": is_correct,
        "correct_meaning": row["meaning_es"],
        "correct_phrasal": row["phrasal"],
        "progress": progress,
    }


# ---------------------------- Mis palabras endpoints -----------------------

class MyWordAnswerItem(BaseModel):
    word_id: int
    user_answer: str | None = None
    exercise_type: str | None = "meaning_mc"  # "meaning_mc" o "word_write"


@app.get("/api/mywords/today")
async def mywords_today(mode: str):
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    chat_id = web_chat_id(mode)
    ensure_chat(chat_id)
    try:
        return build_today_mywords_session(chat_id, mode)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo preparar la sesión: {e}")


@app.get("/api/mywords/list")
async def mywords_list(mode: str):
    """Resumen + lista completa de las palabras personales del perfil."""
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    chat_id = web_chat_id(mode)
    ensure_chat(chat_id)
    counts = count_user_words(chat_id)
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, display, meaning_es, box, times_seen, times_correct,
                   next_due_at, created_at
            FROM user_words
            WHERE chat_id = %s
            ORDER BY created_at DESC
            LIMIT 500
            """,
            (chat_id,),
        )
        words = []
        for r in cur.fetchall():
            d = dict(r)
            d["next_due_at"] = d["next_due_at"].isoformat() if d.get("next_due_at") else None
            d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
            words.append(d)
    return {"totals": counts, "words": words}


@app.post("/api/mywords/answer")
async def mywords_answer(mode: str, item: MyWordAnswerItem):
    """Evalúa en el servidor y actualiza la caja Leitner de esa palabra personal."""
    if mode not in MODES:
        raise HTTPException(status_code=400, detail="Modo desconocido")
    chat_id = web_chat_id(mode)

    with db_cursor() as cur:
        cur.execute(
            "SELECT id, word, display, meaning_es FROM user_words WHERE id = %s AND chat_id = %s",
            (item.word_id, chat_id),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Palabra no encontrada para este perfil")

    user_ans = (item.user_answer or "").strip()
    if (item.exercise_type or "meaning_mc") == "word_write":
        is_correct = normalize_phrasal_text(user_ans) == normalize_phrasal_text(row["word"])
    else:
        is_correct = user_ans == (row["meaning_es"] or "").strip()

    progress = update_user_word_progress(chat_id, item.word_id, is_correct)
    if progress.get("next_due_at"):
        progress["next_due_at"] = progress["next_due_at"].isoformat()
    return {
        "ok": True,
        "is_correct": is_correct,
        "correct_meaning": row["meaning_es"],
        "correct_word": row["display"],
        "progress": progress,
    }


# Servir estáticos al final para que las rutas API tengan prioridad
app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webapp:app", host="0.0.0.0", port=PORT, log_level="info")
