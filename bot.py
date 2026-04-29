import os
import tempfile
from collections import defaultdict, deque

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

client = OpenAI(api_key=OPENAI_API_KEY)

BASE_PROMPT = (
    "Eres una profesora de inglés amable, cercana y paciente. "
    "Mantén una conversación natural y fluida con el estudiante, recordando lo que ya hablasteis. "
    "Corrige los errores con suavidad, da ejemplos claros y anima al estudiante. "
    "Cuando corrijas, explica brevemente por qué el cambio es mejor. "
    "Mantén las respuestas concisas (2-4 frases) salvo que el alumno pida más detalle. "
    "Habla con un tono natural y conversacional, como en una clase real."
)

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

DEFAULT_ACCENT = "american"
HISTORY_TURNS = 12

conversations: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))
accent_choice: dict[int, str] = {}


def get_accent(chat_id: int) -> dict:
    key = accent_choice.get(chat_id, DEFAULT_ACCENT)
    return ACCENTS[key]


def chat_with_gpt(chat_id: int, user_message: str) -> str:
    accent = get_accent(chat_id)
    system_prompt = f"{BASE_PROMPT}\n\n{accent['instruction']}"

    history = conversations[chat_id]
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
    )
    reply = response.choices[0].message.content

    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    return reply


def text_to_speech(chat_id: int, text: str) -> bytes:
    accent = get_accent(chat_id)
    speech = client.audio.speech.create(
        model="tts-1",
        voice=accent["voice"],
        input=text,
        response_format="opus",
    )
    return speech.content


def transcribe(audio_path: str) -> str:
    with open(audio_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )
    return transcript.text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversations.pop(update.effective_chat.id, None)
    welcome = (
        "¡Hola! 👋 Soy tu profesora de inglés personal.\n\n"
        "Puedes:\n"
        "• Escribirme en inglés (o español) para que conversemos y te corrija.\n"
        "• Mandarme mensajes de voz y te responderé también con voz.\n\n"
        "*Acento:* por defecto practicamos con inglés americano 🇺🇸.\n"
        "Cámbialo cuando quieras con /british o /american.\n\n"
        "Otros comandos:\n"
        "• /reset — borrar la conversación y empezar de cero.\n\n"
        "¿List@ para empezar? Mándame tu primer mensaje."
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversations.pop(update.effective_chat.id, None)
    await update.message.reply_text(
        "🔄 Borré nuestra conversación. Empezamos de cero — dime qué quieres practicar."
    )


async def set_accent(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str):
    accent_choice[update.effective_chat.id] = key
    conversations.pop(update.effective_chat.id, None)
    label = ACCENTS[key]["label"]
    await update.message.reply_text(
        f"✅ A partir de ahora practicaremos con inglés {label}.\n"
        "He reiniciado la conversación para empezar limpio."
    )


async def british(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_accent(update, context, "british")


async def american(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_accent(update, context, "american")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    try:
        reply = chat_with_gpt(update.effective_chat.id, user_message)
    except Exception:
        await update.message.reply_text(
            "Ups, tuve un problema procesando tu mensaje. ¿Lo intentamos de nuevo?"
        )
        return
    await update.message.reply_text(reply)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice or update.message.audio
    if voice is None:
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE
    )

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

        reply_text = chat_with_gpt(update.effective_chat.id, user_text)
        audio_bytes = text_to_speech(update.effective_chat.id, reply_text)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            output_path = f.name
            f.write(audio_bytes)

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


app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("reset", reset))
app.add_handler(CommandHandler("british", british))
app.add_handler(CommandHandler("american", american))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

app.run_polling()
