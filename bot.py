import os
import tempfile

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

SYSTEM_PROMPT = (
    "Eres una profesora de inglés amable, cercana y paciente. "
    "Corrige los errores con suavidad, da ejemplos claros y anima al estudiante. "
    "Habla con un tono natural y conversacional, como en una clase real. "
    "Cuando corrijas, explica brevemente por qué el cambio es mejor. "
    "Mantén las respuestas concisas (2-4 frases) salvo que el alumno pida más detalle."
)

VOICE_NAME = "nova"


def chat_with_gpt(user_message: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content


def text_to_speech(text: str) -> bytes:
    speech = client.audio.speech.create(
        model="tts-1",
        voice=VOICE_NAME,
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
    welcome = (
        "¡Hola! 👋 Soy tu profesora de inglés personal.\n\n"
        "Puedes:\n"
        "• Escribirme en inglés (o español) para que te corrija y conversemos.\n"
        "• Mandarme un mensaje de voz y te responderé también con voz, como en una clase real.\n\n"
        "¿List@ para empezar? Mándame tu primer mensaje."
    )
    await update.message.reply_text(welcome)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    try:
        reply = chat_with_gpt(user_message)
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

        reply_text = chat_with_gpt(user_text)
        audio_bytes = text_to_speech(reply_text)

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
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

app.run_polling()
