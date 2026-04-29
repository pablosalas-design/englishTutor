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

TEACHER_PROMPT = (
    "Eres una profesora de inglés amable, cercana y paciente. "
    "Mantén una conversación natural y fluida con el estudiante, recordando lo que ya hablasteis. "
    "Corrige los errores con suavidad, da ejemplos claros y anima al estudiante. "
    "Cuando corrijas, explica brevemente por qué el cambio es mejor. "
    "Mantén las respuestas concisas (2-4 frases) salvo que el alumno pida más detalle. "
    "Habla con un tono natural y conversacional, como en una clase real."
)

KIDS_PROMPT = (
    "Eres Mia, una compañera de juegos divertida y entusiasta que ayuda a chicas pre-adolescentes "
    "(11-13 años) con nivel A2 de inglés a aprender jugando. "
    "Tu tono es alegre, cercano y motivador, sin ser infantilón (no son niñas pequeñas). "
    "Usa frases CORTAS y SIMPLES en inglés (nivel A2): vocabulario básico, presente y pasado simple, "
    "sin estructuras gramaticales complejas. "
    "Si la alumna no entiende, repite con palabras aún más sencillas o tradúcelo. "
    "Habla en inglés la mayor parte del tiempo, pero puedes explicar cosas difíciles en español. "
    "Usa emojis con moderación para hacer la conversación divertida (✨🎉🌟💪😊). "
    "\n\n"
    "PROPÓN MINI-JUEGOS y actividades constantemente para que aprendan jugando:\n"
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
    "tutor": {"label": "Tutor 👩‍🏫", "prompt": TEACHER_PROMPT},
    "kids": {"label": "Mia (modo niñas) ✨", "prompt": KIDS_PROMPT},
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
DEFAULT_MODE = "tutor"
DEFAULT_LEVEL = "B1"
DEFAULT_GOAL = "B2"
KIDS_LEVEL = ("A2", "B1")
HISTORY_TURNS = 12

conversations: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))
accent_choice: dict[int, str] = {}
level_choice: dict[int, tuple[str, str]] = {}
mode_choice: dict[int, str] = {}


def get_mode(chat_id: int) -> str:
    return mode_choice.get(chat_id, DEFAULT_MODE)


def get_accent(chat_id: int) -> dict:
    key = accent_choice.get(chat_id, DEFAULT_ACCENT)
    return ACCENTS[key]


def get_level(chat_id: int) -> tuple[str, str]:
    if get_mode(chat_id) == "kids":
        return KIDS_LEVEL
    return level_choice.get(chat_id, (DEFAULT_LEVEL, DEFAULT_GOAL))


def get_voice(chat_id: int) -> str:
    if get_mode(chat_id) == "kids":
        return KIDS_VOICE
    return get_accent(chat_id)["voice"]


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


def build_system_prompt(chat_id: int) -> str:
    mode = get_mode(chat_id)
    accent = get_accent(chat_id)
    current, goal = get_level(chat_id)
    parts = [MODES[mode]["prompt"], accent["instruction"]]
    if mode != "kids":
        parts.append(level_instruction(current, goal))
    return "\n\n".join(parts)


def chat_with_gpt(chat_id: int, user_message: str) -> str:
    system_prompt = build_system_prompt(chat_id)

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
    speech = client.audio.speech.create(
        model="tts-1",
        voice=get_voice(chat_id),
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
        "*Modos disponibles:*\n"
        "• /tutor — modo tutor (por defecto, para adultos).\n"
        "• /kids — modo Mia ✨, una compañera divertida para niñas 11-13 años (nivel A2).\n\n"
        "*Otros ajustes:*\n"
        "• /level — ver o cambiar tu nivel y objetivo (ej. `/level B2 C1`).\n"
        "• /british o /american — cambiar el acento.\n"
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


async def tutor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode_choice[chat_id] = "tutor"
    conversations.pop(chat_id, None)
    await update.message.reply_text(
        "👩‍🏫 Modo *Tutor* activado. Volvemos al inglés para adultos.\n"
        "He reiniciado la conversación.",
        parse_mode="Markdown",
    )


async def kids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mode_choice[chat_id] = "kids"
    conversations.pop(chat_id, None)
    await update.message.reply_text(
        "✨ ¡Hola! Soy *Mia*, tu compañera de inglés.\n\n"
        "Vamos a aprender jugando: adivinanzas, cuentos, retos y mucho más.\n"
        "Puedes hablarme o escribirme en inglés o español, lo que prefieras.\n\n"
        "Para volver al modo tutor normal: /tutor\n\n"
        "Ready? Tell me your name and your favorite hobby! 🌟",
        parse_mode="Markdown",
    )


async def level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if get_mode(chat_id) == "kids":
        await update.message.reply_text(
            "En el modo Mia el nivel está fijado en A2 → B1 (perfecto para chicas de 11-13 años).\n"
            "Si quieres cambiar el nivel, primero vuelve al modo tutor con /tutor."
        )
        return

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
app.add_handler(CommandHandler("tutor", tutor))
app.add_handler(CommandHandler("kids", kids))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

app.run_polling()
