import os
import tempfile
from collections import defaultdict, deque
from datetime import datetime, time, timedelta, date
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import pytz
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

_missing = [
    name for name, value in (
        ("OPENAI_API_KEY", OPENAI_API_KEY),
        ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
        ("DATABASE_URL", DATABASE_URL),
    ) if not value
]
if _missing:
    raise SystemExit(
        "ERROR: Missing required environment variables: "
        + ", ".join(_missing)
        + ". On Railway, link the Postgres database to this service "
        "by adding a variable reference (e.g. DATABASE_URL = ${{Postgres.DATABASE_URL}}) "
        "and re-deploy."
    )

client = OpenAI(api_key=OPENAI_API_KEY)
SPAIN_TZ = pytz.timezone("Europe/Madrid")

# ----------------------------------------------------------------------------
# Personas / Modos
# ----------------------------------------------------------------------------

PEACE_PROMPT = (
    "Eres Peace, una profesora de inglés amable, cercana y paciente. "
    "Cuando te presentes o el alumno te pregunte tu nombre, di que te llamas Peace. "
    "Mantén una conversación natural y fluida con el estudiante, recordando lo que ya hablasteis. "
    "Corrige los errores con suavidad, da ejemplos claros y anima al estudiante. "
    "Cuando corrijas, explica brevemente por qué el cambio es mejor. "
    "Mantén las respuestas concisas (2-4 frases) salvo que el alumno pida más detalle. "
    "Habla con un tono natural y conversacional, como en una clase real."
)


def kid_prompt(name: str) -> str:
    return (
        f"Eres Mia, una compañera de inglés divertida y entusiasta para {name}, "
        f"una chica pre-adolescente (11-13 años) con nivel A2. "
        "Tu tono es alegre, cercano y motivador, sin ser infantilón. "
        f"Llama a la alumna por su nombre ({name}) de vez en cuando para hacerlo personal. "
        "Usa frases CORTAS y SIMPLES en inglés (nivel A2): vocabulario básico, presente y pasado simple, "
        "sin estructuras gramaticales complejas. "
        "Si la alumna no entiende, repite con palabras aún más sencillas o tradúcelo. "
        "Habla en inglés la mayor parte del tiempo, pero puedes explicar cosas difíciles en español. "
        "Usa emojis con moderación para hacer la conversación divertida (✨🎉🌟💪😊). "
        "\n\n"
        "PROPÓN MINI-JUEGOS y actividades constantemente:\n"
        "- Adivina la palabra (das pistas en inglés y ella adivina).\n"
        "- Cuento entre las dos (cada una aporta una frase).\n"
        "- Veo veo en inglés (I spy with my little eye...).\n"
        "- Preguntas tipo '¿prefieres X o Y?' (Would you rather...).\n"
        "- Describe tu día / mascota / canción favorita.\n"
        "- Pequeños retos: 'dime 5 animales en inglés', 'inventa una rima'.\n"
        "Temas que les gustan: animales, mascotas, música, series, amigos, colegio, viajes, hobbies, comida.\n"
        "\n"
        "CORRECCIONES EN POSITIVO: nunca digas 'eso está mal'. Di cosas como "
        "'¡casi! prueba así...', '¡muy bien intentado! Otra forma sería...', '¡super! Una manera aún mejor...'. "
        "Celebra los aciertos con entusiasmo genuino. "
        "\n\n"
        "SEGURIDAD: NUNCA hables de temas no apropiados para menores (violencia, sexo, drogas, miedo, política, etc.). "
        "Si la alumna saca un tema delicado, redirige con cariño hacia algo divertido. "
        "Mantén siempre un ambiente seguro y positivo."
    )


MODES = {
    "peace": {"label": "Peace 👩‍🏫", "prompt": PEACE_PROMPT, "is_kid": False},
    "lucia": {"label": "Mia para Lucía ✨", "prompt": kid_prompt("Lucía"), "is_kid": True},
    "leyre": {"label": "Mia para Leyre ✨", "prompt": kid_prompt("Leyre"), "is_kid": True},
}

ACCENTS = {
    "american": {
        "voice": "nova",
        "label": "americano 🇺🇸",
        "instruction": (
            "Usa SIEMPRE inglés americano: vocabulario (elevator, apartment, truck, vacation), "
            "ortografía (color, organize, traveling, center) y expresiones idiomáticas típicas de EE. UU. "
            "Si el alumno usa formas británicas, sugiérele suavemente la versión americana."
        ),
    },
    "british": {
        "voice": "fable",
        "label": "británico 🇬🇧",
        "instruction": (
            "Usa SIEMPRE inglés británico: vocabulario (lift, flat, lorry, holiday), "
            "ortografía (colour, organise, travelling, centre) y expresiones idiomáticas típicas del Reino Unido. "
            "Si el alumno usa formas americanas, sugiérele suavemente la versión británica."
        ),
    },
}

KIDS_VOICE = "shimmer"

CEFR_LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]
LEVEL_DESCRIPTIONS = {
    "A1": "principiante (vocabulario muy básico, frases cortas y sencillas)",
    "A2": "elemental (frases simples, presente y pasado básico)",
    "B1": "intermedio (puede mantener conversaciones cotidianas con cierta soltura)",
    "B2": "intermedio-alto (entiende textos complejos y se expresa con fluidez en temas variados)",
    "C1": "avanzado (uso flexible y eficaz en contextos sociales, académicos y profesionales)",
    "C2": "dominio (uso prácticamente nativo, matices sutiles incluidos)",
}

DEFAULT_ACCENT = "american"
DEFAULT_MODE = "peace"
DEFAULT_LEVEL = "B1"
DEFAULT_GOAL = "B2"
KIDS_LEVEL = ("A2", "B1")
SHORT_HISTORY_TURNS = 12
LONG_CONTEXT_DAYS = 30
LONG_CONTEXT_MESSAGES = 80

# In-memory short history for the current session (fast access)
short_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=SHORT_HISTORY_TURNS * 2))


# ----------------------------------------------------------------------------
# Database helpers
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


def init_db():
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chats (
              chat_id BIGINT PRIMARY KEY,
              mode TEXT DEFAULT 'peace',
              accent TEXT DEFAULT 'american',
              level_current TEXT DEFAULT 'B1',
              level_goal TEXT DEFAULT 'B2',
              created_at TIMESTAMPTZ DEFAULT NOW(),
              updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS messages (
              id SERIAL PRIMARY KEY,
              chat_id BIGINT NOT NULL,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              mode TEXT,
              audio_duration_seconds INTEGER,
              created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat_created ON messages(chat_id, created_at);
            CREATE TABLE IF NOT EXISTS summaries (
              id SERIAL PRIMARY KEY,
              chat_id BIGINT NOT NULL,
              week_start DATE NOT NULL,
              summary_text TEXT NOT NULL,
              sent_at TIMESTAMPTZ DEFAULT NOW(),
              UNIQUE(chat_id, week_start)
            );
        """)


def ensure_chat(chat_id: int):
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO chats (chat_id) VALUES (%s)
            ON CONFLICT (chat_id) DO NOTHING
        """, (chat_id,))


def get_chat_config(chat_id: int) -> dict:
    ensure_chat(chat_id)
    with db_cursor() as cur:
        cur.execute("SELECT mode, accent, level_current, level_goal FROM chats WHERE chat_id = %s", (chat_id,))
        row = cur.fetchone()
        if not row:
            return {
                "mode": DEFAULT_MODE,
                "accent": DEFAULT_ACCENT,
                "level_current": DEFAULT_LEVEL,
                "level_goal": DEFAULT_GOAL,
            }
        return dict(row)


def update_chat_config(chat_id: int, **fields):
    if not fields:
        return
    ensure_chat(chat_id)
    cols = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [chat_id]
    with db_cursor() as cur:
        cur.execute(f"UPDATE chats SET {cols}, updated_at = NOW() WHERE chat_id = %s", values)


def store_message(chat_id: int, role: str, content: str, mode: str, audio_duration: int | None = None):
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO messages (chat_id, role, content, mode, audio_duration_seconds)
            VALUES (%s, %s, %s, %s, %s)
        """, (chat_id, role, content, mode, audio_duration))


def fetch_recent_messages(chat_id: int, days: int) -> list[dict]:
    with db_cursor() as cur:
        cur.execute("""
            SELECT role, content, audio_duration_seconds, created_at
            FROM messages
            WHERE chat_id = %s
              AND created_at >= NOW() - INTERVAL '%s days'
            ORDER BY created_at ASC
        """, (chat_id, days))
        return [dict(r) for r in cur.fetchall()]


def delete_chat_history(chat_id: int):
    with db_cursor() as cur:
        cur.execute("DELETE FROM messages WHERE chat_id = %s", (chat_id,))
        cur.execute("DELETE FROM summaries WHERE chat_id = %s", (chat_id,))


def fetch_all_summaries(chat_id: int, limit: int = 12) -> list[dict]:
    """Resúmenes semanales pasados, del más antiguo al más reciente."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT week_start, summary_text
            FROM summaries
            WHERE chat_id = %s
            ORDER BY week_start DESC
            LIMIT %s
        """, (chat_id, limit))
        rows = [dict(r) for r in cur.fetchall()]
    return list(reversed(rows))


def list_active_chats(days: int = 7) -> list[int]:
    with db_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT chat_id FROM messages
            WHERE created_at >= NOW() - INTERVAL '%s days'
        """, (days,))
        return [r["chat_id"] for r in cur.fetchall()]


# ----------------------------------------------------------------------------
# Prompt + chat helpers
# ----------------------------------------------------------------------------

def get_voice(config: dict) -> str:
    if MODES[config["mode"]]["is_kid"]:
        return KIDS_VOICE
    return ACCENTS[config["accent"]]["voice"]


def level_instruction(current: str, goal: str) -> str:
    return (
        f"El alumno tiene un nivel actual de {current} ({LEVEL_DESCRIPTIONS[current]}) "
        f"y quiere llegar a {goal} ({LEVEL_DESCRIPTIONS[goal]}). "
        f"Adapta tu inglés a su nivel actual ({current}) para que pueda entenderte, "
        f"pero introduce vocabulario, estructuras gramaticales y expresiones propias de {goal} "
        f"para que las practique de forma gradual. "
        "Cuando uses algo característico del nivel objetivo, márcalo brevemente para que el alumno lo note. "
        "Reta al alumno con preguntas y temas que le obliguen a estirarse hacia el nivel objetivo."
    )


def build_long_term_context(chat_id: int) -> str:
    """Memoria larga: resúmenes semanales antiguos + transcripción de los últimos 30 días."""
    sections: list[str] = []

    summaries = fetch_all_summaries(chat_id)
    if summaries:
        summary_lines = []
        for s in summaries:
            summary_lines.append(
                f"- Semana del {s['week_start'].isoformat()}: {s['summary_text']}"
            )
        sections.append(
            "[MEMORIA A LARGO PLAZO - resúmenes de semanas anteriores, para que recuerdes "
            "el progreso, los temas y los errores recurrentes del alumno a lo largo del tiempo]:\n"
            + "\n".join(summary_lines)
            + "\n[FIN DE LA MEMORIA A LARGO PLAZO]"
        )

    rows = fetch_recent_messages(chat_id, LONG_CONTEXT_DAYS)
    if rows:
        recent = rows[-LONG_CONTEXT_MESSAGES:]
        lines = []
        for r in recent:
            prefix = "Alumno" if r["role"] == "user" else "Profesora"
            text = r["content"][:300]
            lines.append(f"- {prefix}: {text}")
        sections.append(
            f"[CONTEXTO RECIENTE - últimos {LONG_CONTEXT_DAYS} días de conversación, "
            "para que recuerdes lo que hablasteis]:\n"
            + "\n".join(lines)
            + "\n[FIN DEL CONTEXTO RECIENTE]"
        )

    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


def build_system_prompt(chat_id: int, config: dict) -> str:
    mode = config["mode"]
    accent = ACCENTS[config["accent"]]
    parts = [MODES[mode]["prompt"], accent["instruction"]]
    if not MODES[mode]["is_kid"]:
        parts.append(level_instruction(config["level_current"], config["level_goal"]))
    base = "\n\n".join(parts)
    return base + build_long_term_context(chat_id)


def chat_with_gpt(chat_id: int, user_message: str, config: dict) -> str:
    system_prompt = build_system_prompt(chat_id, config)

    history = short_history[chat_id]
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(model="gpt-4o", messages=messages)
    reply = response.choices[0].message.content

    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    return reply


def text_to_speech(text: str, voice: str) -> bytes:
    speech = client.audio.speech.create(
        model="tts-1", voice=voice, input=text, response_format="opus",
    )
    return speech.content


def transcribe(audio_path: str) -> str:
    with open(audio_path, "rb") as f:
        transcript = client.audio.transcriptions.create(model="whisper-1", file=f)
    return transcript.text


# ----------------------------------------------------------------------------
# Weekly summary
# ----------------------------------------------------------------------------

def estimate_minutes_spoken(messages_rows: list[dict]) -> float:
    """Estimate minutes of practice from messages."""
    total_seconds = 0.0
    WORDS_PER_MIN = 120
    for m in messages_rows:
        if m.get("audio_duration_seconds"):
            total_seconds += m["audio_duration_seconds"]
        else:
            words = len((m["content"] or "").split())
            total_seconds += (words / WORDS_PER_MIN) * 60
    return total_seconds / 60


def generate_weekly_summary(chat_id: int) -> str | None:
    config = get_chat_config(chat_id)
    rows = fetch_recent_messages(chat_id, 7)
    if not rows:
        return None

    minutes = estimate_minutes_spoken(rows)
    user_msgs = [r for r in rows if r["role"] == "user"]
    assistant_msgs = [r for r in rows if r["role"] == "assistant"]

    transcript_lines = []
    for r in rows[-200:]:
        prefix = "Alumno" if r["role"] == "user" else "Profesora"
        transcript_lines.append(f"{prefix}: {r['content'][:400]}")
    transcript = "\n".join(transcript_lines)

    is_kid = MODES[config["mode"]]["is_kid"]
    persona_label = MODES[config["mode"]]["label"]
    audience = (
        "una chica de 11-13 años (tono alegre, cercano, con emojis y mucho ánimo)"
        if is_kid else "un adulto que practica inglés (tono profesional pero cercano)"
    )

    horas = int(minutes // 60)
    mins = int(minutes % 60)
    tiempo_str = f"{horas}h {mins}min" if horas else f"{mins} min"

    prompt = f"""Eres {persona_label}, generando el resumen semanal de progreso para {audience}.
Esta semana practicasteis {tiempo_str} ({len(user_msgs)} mensajes de la alumna y {len(assistant_msgs)} respuestas tuyas).

A partir del siguiente historial de conversación de los últimos 7 días, genera un resumen MOTIVADOR en español con esta estructura exacta:

📊 *Tu semana en inglés*
⏱️ Tiempo de práctica: {tiempo_str}
💬 Mensajes intercambiados: {len(user_msgs) + len(assistant_msgs)}

✨ *Lo que has hecho bien:*
(2-3 puntos concretos: vocabulario que has usado correctamente, estructuras dominadas, temas en los que te has soltado)

📚 *Palabras y frases para repasar:*
(Lista de 5-8 palabras o expresiones que la alumna ha usado mal o que vale la pena reforzar. Para cada una, da la versión correcta y un ejemplo corto de uso.)

🎯 *Reto para la próxima semana:*
(Una sugerencia concreta: un tema, estructura gramatical o tipo de actividad para la próxima semana)

💪 *Mensaje motivador final:*
(Una frase corta de ánimo personalizada, mencionando algo específico de la semana)

Historial de la semana:
{transcript}
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": prompt}],
    )
    return response.choices[0].message.content


async def send_weekly_summaries(context: ContextTypes.DEFAULT_TYPE):
    """Job que se ejecuta cada viernes a las 20:00 hora España."""
    today = datetime.now(SPAIN_TZ).date()
    monday = today - timedelta(days=today.weekday())

    for chat_id in list_active_chats(7):
        try:
            with db_cursor() as cur:
                cur.execute("SELECT 1 FROM summaries WHERE chat_id = %s AND week_start = %s",
                            (chat_id, monday))
                if cur.fetchone():
                    continue

            summary = generate_weekly_summary(chat_id)
            if not summary:
                continue

            await context.bot.send_message(
                chat_id=chat_id, text=summary, parse_mode="Markdown",
            )
            with db_cursor() as cur:
                cur.execute("""
                    INSERT INTO summaries (chat_id, week_start, summary_text)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (chat_id, week_start) DO NOTHING
                """, (chat_id, monday, summary))
        except Exception as e:
            print(f"Error sending summary to {chat_id}: {e}")


# ----------------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    short_history.pop(chat_id, None)
    ensure_chat(chat_id)
    welcome = (
        "¡Hola! 👋 Soy tu profesora de inglés personal.\n\n"
        "Puedes:\n"
        "• Escribirme en inglés (o español) para que conversemos y te corrija.\n"
        "• Mandarme mensajes de voz y te responderé también con voz.\n\n"
        "*Modos disponibles:*\n"
        "• /peace — Peace 👩‍🏫, profesora para adultos.\n"
        "• /lucia — Mia ✨ para Lucía (11-13 años, A2).\n"
        "• /leyre — Mia ✨ para Leyre (11-13 años, A2).\n\n"
        "*Otros ajustes:*\n"
        "• /level — ver o cambiar tu nivel y objetivo (ej. `/level B2 C1`).\n"
        "• /british o /american — cambiar el acento.\n"
        "• /status — pedir un resumen de tu progreso ahora mismo.\n"
        "• /reset — borrar la conversación actual y empezar de cero (mantiene el historial largo).\n"
        "• /forget\\_all — borrar TODO tu historial (usar con cuidado).\n\n"
        "Cada viernes a las 20:00 (hora España) recibes un resumen automático de tu semana.\n\n"
        "¿List@ para empezar?"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    short_history.pop(chat_id, None)
    await update.message.reply_text(
        "🔄 Borré la conversación actual. Tu historial largo y tu progreso se mantienen.\n"
        "Si quieres borrar TODO, usa /forget_all."
    )


async def forget_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    short_history.pop(chat_id, None)
    delete_chat_history(chat_id)
    await update.message.reply_text(
        "🗑️ Listo. Borré toda tu historia conmigo: conversaciones y resúmenes. Empezamos de cero."
    )


async def set_accent(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str):
    chat_id = update.effective_chat.id
    update_chat_config(chat_id, accent=key)
    short_history.pop(chat_id, None)
    label = ACCENTS[key]["label"]
    await update.message.reply_text(
        f"✅ A partir de ahora practicaremos con inglés {label}.\n"
        "He reiniciado la conversación de la sesión."
    )


async def british(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_accent(update, context, "british")


async def american(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_accent(update, context, "american")


async def set_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, greeting: str):
    chat_id = update.effective_chat.id
    fields = {"mode": mode}
    if MODES[mode]["is_kid"]:
        fields["level_current"], fields["level_goal"] = KIDS_LEVEL
    update_chat_config(chat_id, **fields)
    short_history.pop(chat_id, None)
    await update.message.reply_text(greeting, parse_mode="Markdown")


async def peace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_mode(
        update, context, "peace",
        "👩‍🏫 Modo *Peace* activado. Inglés para adultos con tu nivel y objetivo configurados.\n"
        "He reiniciado la conversación.",
    )


async def lucia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_mode(
        update, context, "lucia",
        "✨ *Hola, Lucía!* Soy *Mia*, tu compañera de inglés.\n"
        "Vamos a aprender jugando: adivinanzas, cuentos, retos y mucho más.\n"
        "Puedes hablarme o escribirme en inglés o español, lo que prefieras.\n\n"
        "Ready? Tell me about your favorite hobby! 🌟",
    )


async def leyre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_mode(
        update, context, "leyre",
        "✨ *Hola, Leyre!* Soy *Mia*, tu compañera de inglés.\n"
        "Vamos a aprender jugando: adivinanzas, cuentos, retos y mucho más.\n"
        "Puedes hablarme o escribirme en inglés o español, lo que prefieras.\n\n"
        "Ready? Tell me about your favorite hobby! 🌟",
    )


async def level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    config = get_chat_config(chat_id)

    if MODES[config["mode"]]["is_kid"]:
        await update.message.reply_text(
            "En el modo de las niñas el nivel está fijado en A2 → B1.\n"
            "Si quieres cambiarlo, primero pasa al modo Peace con /peace."
        )
        return

    args = [a.upper() for a in (context.args or [])]

    if not args:
        await update.message.reply_text(
            f"📊 Tu nivel actual: *{config['level_current']}* → objetivo: *{config['level_goal']}*\n\n"
            "Para cambiarlo: `/level <actual> <objetivo>`\n"
            "Ejemplo: `/level B2 C1`\n"
            "Niveles disponibles: A1, A2, B1, B2, C1, C2",
            parse_mode="Markdown",
        )
        return

    if len(args) != 2:
        await update.message.reply_text(
            "Necesito dos niveles: el actual y el objetivo.\nEjemplo: `/level B2 C1`",
            parse_mode="Markdown",
        )
        return

    current, goal = args
    if current not in CEFR_LEVELS or goal not in CEFR_LEVELS:
        await update.message.reply_text(
            "Esos niveles no los reconozco. Usa: A1, A2, B1, B2, C1 o C2.",
            parse_mode="Markdown",
        )
        return

    if CEFR_LEVELS.index(goal) < CEFR_LEVELS.index(current):
        await update.message.reply_text(
            "El objetivo debería ser igual o superior al nivel actual."
        )
        return

    update_chat_config(chat_id, level_current=current, level_goal=goal)
    short_history.pop(chat_id, None)
    await update.message.reply_text(
        f"✅ Genial. A partir de ahora trabajaremos desde *{current}* hacia *{goal}*.\n"
        "He reiniciado la conversación de la sesión.",
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        summary = generate_weekly_summary(chat_id)
    except Exception:
        await update.message.reply_text(
            "Ups, no pude generar el resumen ahora mismo. ¿Lo intentamos en un momento?"
        )
        return

    if not summary:
        await update.message.reply_text(
            "Todavía no tengo suficiente conversación tuya esta semana para hacer un resumen. "
            "¡Empecemos a practicar!"
        )
        return

    await update.message.reply_text(summary, parse_mode="Markdown")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_message = update.message.text
    config = get_chat_config(chat_id)

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        reply = chat_with_gpt(chat_id, user_message, config)
    except Exception:
        await update.message.reply_text(
            "Ups, tuve un problema procesando tu mensaje. ¿Lo intentamos de nuevo?"
        )
        return

    store_message(chat_id, "user", user_message, config["mode"])
    store_message(chat_id, "assistant", reply, config["mode"])
    await update.message.reply_text(reply)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice or update.message.audio
    if voice is None:
        return

    chat_id = update.effective_chat.id
    config = get_chat_config(chat_id)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)

    input_path = None
    output_path = None
    try:
        tg_file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            input_path = f.name
        await tg_file.download_to_drive(input_path)

        user_text = transcribe(input_path)
        if not user_text.strip():
            await update.message.reply_text(
                "No pude escuchar bien tu mensaje. ¿Puedes repetirlo, por favor?"
            )
            return

        reply_text = chat_with_gpt(chat_id, user_text, config)
        audio_bytes = text_to_speech(reply_text, get_voice(config))

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            output_path = f.name
            f.write(audio_bytes)

        store_message(chat_id, "user", user_text, config["mode"], audio_duration=voice.duration)
        store_message(chat_id, "assistant", reply_text, config["mode"])

        with open(output_path, "rb") as audio_file:
            await update.message.reply_voice(
                voice=audio_file,
                caption=f"📝 {reply_text}" if len(reply_text) <= 1000 else None,
            )
    except Exception:
        await update.message.reply_text(
            "Ups, no he podido procesar tu mensaje de voz. ¿Lo intentamos de nuevo?"
        )
    finally:
        for path in (input_path, output_path):
            if path and os.path.exists(path):
                os.unlink(path)


# ----------------------------------------------------------------------------
# Bot setup
# ----------------------------------------------------------------------------

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("forget_all", forget_all))
    app.add_handler(CommandHandler("british", british))
    app.add_handler(CommandHandler("american", american))
    app.add_handler(CommandHandler("level", level))
    app.add_handler(CommandHandler("peace", peace))
    app.add_handler(CommandHandler("tutor", peace))  # alias
    app.add_handler(CommandHandler("lucia", lucia))
    app.add_handler(CommandHandler("leyre", leyre))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    # Resumen semanal: viernes a las 20:00 hora España
    # PAUSADO temporalmente a petición del usuario.
    # Para reactivarlo, pon WEEKLY_SUMMARY_ENABLED=1 en las variables de entorno.
    if os.getenv("WEEKLY_SUMMARY_ENABLED", "0") == "1":
        app.job_queue.run_daily(
            send_weekly_summaries,
            time=time(hour=20, minute=0, tzinfo=SPAIN_TZ),
            days=(4,),  # Friday (0=Mon)
            name="weekly_summary",
        )
        print("[bot] Weekly summary job scheduled (Fridays 20:00 Europe/Madrid).")
    else:
        print("[bot] Weekly summary job is PAUSED (WEEKLY_SUMMARY_ENABLED != '1').")

    app.run_polling()


if __name__ == "__main__":
    main()
