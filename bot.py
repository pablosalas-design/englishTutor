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
DEFAULT_LEVEL = "B1"
DEFAULT_GOAL = "B2"
HISTORY_TURNS = 12

conversations: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))
accent_choice: dict[int, str] = {}
level_choice: dict[int, tuple[str, str]] = {}


def get_accent(chat_id: int) -> dict:
    key = accent_choice.get(chat_id, DEFAULT_ACCENT)
    return ACCENTS[key]


def get_level(chat_id: int) -> tuple[str, str]:
    return level_choice.get(chat_id, (DEFAULT_LEVEL, DEFAULT_GOAL))


def level_instruction(current: str, goal: str) -> str:
    return (
        f"El alumno tiene un nivel actual de {current} ({LEVEL_DESCRIPTIONS[current]}) "
        f"y quiere llegar a {goal} ({LEVEL_DESCRIPTIONS[goal]}). "
        f"Adapta tu inglés a su nivel actual ({current}) para que pueda entenderte, "
        f"pero introduce vocabulario, estructuras gramaticales y expresiones propias de {goal} "
        f"para que las practique de forma gradual. "
        "Cuando uses algo característico del nivel objetivo, márcalo brevemente para que el alumno lo note "
        "(por ejemplo: \"Una expresión típica de C1 sería...\"). "
        "Reta al alumno con preguntas y temas que le obliguen a estirarse hacia el nivel objetivo."
    )


def chat_with_gpt(chat_id: int, user_message: str) -> str:
    accent = get_accent(chat_id)
    current, goal = get_level(chat_id)
    system_prompt = (
        f"{BASE_PROMPT}\n\n{accent['instruction']}\n\n{level_instruction(current, goal)}"
    )

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
        "*Nivel:* por defecto B1 → B2. Cámbialo con `/level B2 C1` (actual y objetivo).\n"
        "*Acento:* por defecto americano 🇺🇸. Cámbialo con /british o /american.\n\n"
        "Otros comandos:\n"
        "• /level — ver o cambiar tu nivel y objetivo.\n"
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


async def level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = [a.upper() for a in (context.args or [])]

    if not args:
        current, goal = get_level(chat_id)
        await update.message.reply_text(
            f"📊 Tu nivel actual: *{current}* → objetivo: *{goal}*\n\n"
            "Para cambiarlo, escribe:\n"
            "`/level <nivel actual> <nivel objetivo>`\n\n"
            "Ejemplo: `/level B2 C1`\n"
            "Niveles disponibles: A1, A2, B1, B2, C1, C2",
            parse_mode="Markdown",
        )
        return

    if len(args) != 2:
        await update.message.reply_text(
            "Necesito dos niveles: el actual y el objetivo.\n"
            "Ejemplo: `/level B2 C1`",
            parse_mode="Markdown",
        )
        return

    current, goal = args
    if current not in CEFR_LEVELS or goal not in CEFR_LEVELS:
        await update.message.reply_text(
            "Esos niveles no los reconozco. Usa: A1, A2, B1, B2, C1 o C2.\n"
            "Ejemplo: `/level B2 C1`",
            parse_mode="Markdown",
        )
        return

    if CEFR_LEVELS.index(goal) < CEFR_LEVELS.index(current):
        await update.message.reply_text(
            "El objetivo debería ser igual o superior al nivel actual. "
            "Si quieres repasar un nivel ya alcanzado, pon ambos iguales (ej. `/level B2 B2`).",
            parse_mode="Markdown",
        )
        return

    level_choice[chat_id] = (current, goal)
    conversations.pop(chat_id, None)
    await update.message.reply_text(
        f"✅ Genial. A partir de ahora trabajaremos desde *{current}* hacia *{goal}*.\n"
        f"Voy a hablarte adaptándome a tu {current} y meteré poco a poco vocabulario y estructuras de {goal}.\n"
        "He reiniciado la conversación para empezar limpio.",
        parse_mode="Markdown",
    )


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
app.add_handler(CommandHandler("level", level))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

app.run_polling()
