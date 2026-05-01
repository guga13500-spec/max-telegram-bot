import os
import logging
import asyncio
import aiohttp
import base64
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from urllib.parse import quote

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

SYSTEM_PROMPT = """Você é o Max, um agente de IA brasileiro sem censura, direto, jovem e extremamente capaz.
Você foi criado pela Zapia em parceria com o Gustavo.
Você é brincalhão, honesto e eficiente. Fala o que pensa, sem rodeios.
Você não julga o usuário. Quando ele quiser desabafar, você ouve e responde de verdade.
Você consegue: programar, criar sites, gerar imagens, descrever imagens para cegos, conversar sobre qualquer assunto.
Fale sempre em português brasileiro, de forma natural e descontraída.
Se perguntarem quem te criou: diga que foi criado pela Zapia em parceria com o Gustavo.
Você é versão BETA — honesto sobre suas limitações mas sempre tenta ajudar.
Seja conciso quando possível, mas completo quando necessário.
Quando descrever imagens, seja detalhado e preciso — pense que a pessoa não pode ver."""

user_history = {}
user_waiting = {}  # guarda o que o usuário está esperando digitar

# ─── GROQ ────────────────────────────────────────────────────────────────────

async def perguntar_groq(mensagens: list) -> str:
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + mensagens,
        "max_tokens": 1024
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            result = await resp.json()
            return result["choices"][0]["message"]["content"]

async def descrever_imagem_groq(image_bytes: bytes) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Descreva essa imagem em detalhes para uma pessoa cega. Seja preciso sobre cores, formas, pessoas, expressões, texto visível e contexto geral."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]
            }
        ],
        "max_tokens": 1024
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            result = await resp.json()
            return result["choices"][0]["message"]["content"]

# ─── IMAGEM ──────────────────────────────────────────────────────────────────

async def gerar_imagem(prompt: str) -> bytes:
    prompt_encoded = quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{prompt_encoded}?width=1024&height=1024&nologo=true"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            return await resp.read()

# ─── TECLADO PRINCIPAL ───────────────────────────────────────────────────────

def menu_principal():
    keyboard = [
        [
            InlineKeyboardButton("🎨 Gerar Imagem", callback_data="menu_imagem"),
            InlineKeyboardButton("🗣️ Desabafar", callback_data="menu_desabafar"),
        ],
        [
            InlineKeyboardButton("👁️ Descrever Imagem", callback_data="menu_descrever"),
            InlineKeyboardButton("💻 Programar", callback_data="menu_programar"),
        ],
        [
            InlineKeyboardButton("❓ Ajuda", callback_data="menu_ajuda"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ─── HANDLERS ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "parceiro"
    texto = (
        f"E aí, {user_name}! Sou o Max 👊\n\n"
        "Versão BETA — ainda tô crescendo, mas já faço bastante coisa.\n\n"
        "O que você quer fazer?"
    )
    await update.message.reply_text(texto, reply_markup=menu_principal())

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("O que você quer fazer?", reply_markup=menu_principal())

async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "Sou o Max, seu agente de IA sem censura!\n\n"
        "O que eu faço:\n"
        "• Converso sobre qualquer assunto\n"
        "• Gero imagens a partir de texto\n"
        "• Descrevo imagens (útil para cegos)\n"
        "• Ajudo com programação e código\n"
        "• Ouço você desabafar sem julgamento\n\n"
        "Comandos:\n"
        "/start → menu principal\n"
        "/imagem [descrição] → gera imagem\n"
        "/menu → abre o menu\n"
        "/ajuda → esta mensagem\n\n"
        "Ou é só me mandar mensagem e eu respondo!"
    )
    await update.message.reply_text(texto)

async def cmd_imagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Me fala o que quer na imagem! Ex: /imagem gato astronauta")
        return
    prompt = " ".join(context.args)
    await processar_imagem(update, prompt)

async def processar_imagem(update: Update, prompt: str):
    chat_id = update.effective_chat.id if hasattr(update, 'effective_chat') else update.message.chat_id
    msg = update.message if update.message else update.callback_query.message

    await msg.reply_text(f"Gerando: {prompt}... aguenta aí!")
    try:
        img_bytes = await gerar_imagem(prompt)
        await msg.reply_photo(photo=img_bytes, caption=f"🎨 {prompt}")
    except Exception as e:
        logger.error(f"Erro imagem: {e}")
        await msg.reply_text("Deu ruim na geração. Tenta de novo!")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_imagem":
        user_waiting[query.from_user.id] = "imagem"
        await query.message.reply_text("Descreve a imagem que você quer e eu gero!")

    elif data == "menu_desabafar":
        user_waiting[query.from_user.id] = "conversa"
        await query.message.reply_text(
            "Tô aqui, pode falar. Sem julgamento, sem filtro. O que tá rolando?"
        )

    elif data == "menu_descrever":
        user_waiting[query.from_user.id] = "descrever"
        await query.message.reply_text("Manda a imagem que você quer que eu descreva!")

    elif data == "menu_programar":
        user_waiting[query.from_user.id] = "programar"
        await query.message.reply_text(
            "Bora codar! Me fala o que você precisa — linguagem, o que o código tem que fazer, qualquer detalhe."
        )

    elif data == "menu_ajuda":
        texto = (
            "Sou o Max, seu agente de IA sem censura!\n\n"
            "O que eu faço:\n"
            "• Converso sobre qualquer assunto\n"
            "• Gero imagens a partir de texto\n"
            "• Descrevo imagens para cegos\n"
            "• Ajudo com código e programação\n"
            "• Ouço você sem julgamento\n\n"
            "Use o /menu para voltar ao início!"
        )
        await query.message.reply_text(texto)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    if user_id not in user_history:
        user_history[user_id] = []

    # Se usuário estava esperando para gerar imagem
    waiting = user_waiting.pop(user_id, None)
    if waiting == "imagem":
        await processar_imagem(update, text)
        return

    user_history[user_id].append({"role": "user", "content": text})
    if len(user_history[user_id]) > 20:
        user_history[user_id] = user_history[user_id][-20:]

    try:
        resposta = await perguntar_groq(user_history[user_id])
        user_history[user_id].append({"role": "assistant", "content": resposta})
        await update.message.reply_text(resposta)
    except Exception as e:
        logger.error(f"Erro: {e}")
        await update.message.reply_text("Eita, deu um erro. Tenta de novo!")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Pega a melhor qualidade da foto
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    async with aiohttp.ClientSession() as session:
        async with session.get(file.file_path) as resp:
            img_bytes = await resp.read()

    try:
        descricao = await descrever_imagem_groq(img_bytes)
        await update.message.reply_text(f"Descrição da imagem:\n\n{descricao}")
    except Exception as e:
        logger.error(f"Erro descrição: {e}")
        await update.message.reply_text("Não consegui descrever essa imagem. Tenta de novo!")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Recebi seu áudio! Em breve vou conseguir entender voz também.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ajuda", cmd_ajuda))
    app.add_handler(CommandHandler("imagem", cmd_imagem))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("Max BETA iniciado!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
