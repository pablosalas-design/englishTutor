"""Realtime voice webapp for the English tutor.

Sirve una página web sencilla donde el alumno habla con la profesora
en tiempo real (WebRTC + OpenAI Realtime API). Comparte la base de datos
con el bot de Telegram para mantener memoria larga y resúmenes semanales.
"""
import os
from contextlib import contextmanager
from datetime import datetime

import httpx
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
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


# URLs de los avatares 3D Ready Player Me (configurables por env, sin redeploy)
AVATAR_PEACE_URL = os.getenv("AVATAR_PEACE_URL", "/static/avatars/peace.glb")
AVATAR_MIA_URL = os.getenv("AVATAR_MIA_URL", "")  # Mia es la misma para Lucía y Leyre

MODES = {
    "peace": {
        "label": "Peace",
        "subtitle": "Profesora para adultos",
        "prompt": PEACE_PROMPT,
        "voice": "coral",
        "is_kid": False,
        "color": "#5B7FFF",
        "avatar_url": AVATAR_PEACE_URL,
    },
    "lucia": {
        "label": "Mia para Lucía",
        "subtitle": "11-13 años · A2",
        "prompt": kid_prompt("Lucía"),
        "voice": "sage",
        "is_kid": True,
        "color": "#FF6FA0",
        "avatar_url": AVATAR_MIA_URL,
    },
    "leyre": {
        "label": "Mia para Leyre",
        "subtitle": "11-13 años · A2",
        "prompt": kid_prompt("Leyre"),
        "voice": "sage",
        "is_kid": True,
        "color": "#9B6FFF",
        "avatar_url": AVATAR_MIA_URL,
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
# FastAPI app
# ----------------------------------------------------------------------------

app = FastAPI(title="English Tutor Voice")


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
            "avatar_url": v.get("avatar_url", ""),
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


# Servir estáticos al final para que las rutas API tengan prioridad
app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webapp:app", host="0.0.0.0", port=PORT, log_level="info")
