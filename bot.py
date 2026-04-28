from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

client = OpenAI(api_key=OPENAI_API_KEY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "¡Hola! 👋 Soy tu profesor de inglés personal.\n\n"
        "Escríbeme en inglés (o en español si prefieres) y te ayudaré a:\n"
        "• Corregir errores de gramática y vocabulario\n"
        "• Darte ejemplos y explicaciones claras\n"
        "• Practicar conversación a tu ritmo\n\n"
        "¿List@ para empezar? Mándame tu primer mensaje."
    )
    await update.message.reply_text(welcome)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Eres un profesor de inglés amable y paciente. Corrige los errores suavemente, da ejemplos y anima al estudiante."},
            {"role": "user", "content": user_message}
        ]
    )
    reply = response.choices[0].message.content
    await update.message.reply_text(reply)

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app.run_polling()
