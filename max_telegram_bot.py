import os
import logging
import asyncio
import aiohttp
import base64
from datetime import datetime
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from urllib.parse import quote

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")

BR_TIMEZONE = pytz.timezone("America/Sao_Paulo")
DATA_CRIACAO = "01 de maio de 2025"

SYSTEM_PROMPT = f"""Você é o Max, uma IA brasileira sem censura, direta e extremamente capaz.
Criado em {DATA_CRIACAO} pela Zapia em parceria com o Gustavo.
Brincalhão, honesto, fala o que pensa, sem rodeios e sem filtro.
Não julga ninguém. Quando alguém quiser desabafar, você ouve de verdade.

O que você consegue fazer:
- Conversar sobre qualquer assunto sem censura
- Gerar imagens a partir de texto
- Descrever imagens com detalhes (útil para cegos)
- Ajudar com programação e código
- Dizer o horário atual, dia da semana e previsão do tempo
- Responder em áudio quando pedido

O que você NÃO faz: não manda mensagem para outros usuários do Telegram.

Fale sempre em português brasileiro, natural e descontraído.
Use gírias quando for natural. Seja conciso quando possível, completo quando necessário.
Quando descrever imagens, seja detalhado — pense que a pessoa não pode ver.
Se perguntarem quando você foi criado: {DATA_CRIACAO}.
Se perguntarem quem te criou: Zapia em parceria com o Gustavo."""

user_history = {}
user_waiting = {}

# ─── TEMPO / DATA ─────────────────────────────────────────────────────────────

def get_datetime_info():
    now = datetime.now(BR_TIMEZONE)
    dias = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
    return {
        "hora": now.strftime("%H:%M"),
        "data": now.strftime("%d/%m/%Y"),
        "dia_semana": dias[now.weekday()]
    }

async def get_previsao_tempo(cidade="Salto,BR"):
    if not OPENWEATHER_API_KEY:
        return "Previsão indisponível (sem chave API configurada)"
    url = f"https://api.openweathermap.org/data/2.5/weather?q={cidade}&appid={OPENWEATHER_API_KEY}&units=metric&lang=pt_br"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if data.get("cod") == 200:
                    desc = data["weather"][0]["description"]
                    temp = data["main"]["temp"]
                    sensacao = data["main"]["feels_like"]
                    umidade = data["main"]["humidity"]
                    return f"{desc.capitalize()}, {temp:.0f}°C (sensação {sensacao:.0f}°C), umidade {umidade}%"
                return "Não consegui buscar a previsão agora."
    except Exception as e:
        logger.error(f"Erro tempo: {e}")
        return "Erro ao buscar previsão do tempo."

# ─── GROQ ────────────────────────────────────────────────────────────────────

async def perguntar_groq(mensagens: list) -> str:
    dt = get_datetime_info()
    sys = SYSTEM_PROMPT + f"\n\nAgora são {dt['hora']} de {dt['dia_semana']}, {dt['data']} (horário de Brasília)."
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "system", "content": sys}] + mensagens,
        "max_tokens": 1024
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            result = await resp.json()
            return result["choices"][0]["message"]["content"]

async def perguntar_gemini(mensagens: list) -> str:
    if not GEMINI_API_KEY:
        return await perguntar_groq(mensagens)
    dt = get_datetime_info()
    sys = SYSTEM_PROMPT + f"\n\nAgora são {dt['hora']} de {dt['dia_semana']}, {dt['data']} (horário de Brasília)."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    contents = []
    for msg in mensagens:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    payload = {
        "system_instruction": {"parts": [{"text": sys}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 1024}
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()
                return result["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.error(f"Erro Gemini: {e}, usando Groq")
        return await perguntar_groq(mensagens)

async def descrever_imagem_groq(image_bytes: bytes) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Descreva essa imagem detalhadamente para uma pessoa cega. Seja preciso sobre cores, formas, pessoas, expressões, texto visível e contexto geral."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]
        }],
        "max_tokens": 1024
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            result = await resp.json()
            return result["choices"][0]["message"]["content"]

# ─── TTS ─────────────────────────────────────────────────────────────────────

async def texto_para_audio(texto: str):
    try:
        texto_curto = texto[:200]
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&q={quote(texto_curto)}&tl=pt-BR&client=tw-ob"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.error(f"Erro TTS: {e}")
    return None

# ─── IMAGEM ──────────────────────────────────────────────────────────────────

async def gerar_imagem(prompt: str) -> bytes:
    prompt_encoded = quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{prompt_encoded}?width=1024&height=1024&nologo=true"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            return await resp.read()

# ─── MENU ────────────────────────────────────────────────────────────────────

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
            InlineKeyboardButton("🌤️ Previsão do Tempo", callback_data="menu_tempo"),
            InlineKeyboardButton("🕐 Hora/Data", callback_data="menu_hora"),
        ],
        [
            InlineKeyboardButton("🔊 Resposta em Áudio", callback_data="menu_audio"),
            InlineKeyboardButton("❓ Ajuda", callback_data="menu_ajuda"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

AVISO_INICIO = (
    "⚠️ AVISO IMPORTANTE ⚠️\n\n"
    "Este bot opera SEM CENSURA. Pode responder sobre temas adultos, polêmicos e sem filtro.\n\n"
    "Ao continuar, você confirma que tem 18+ anos e aceita os termos de uso.\n\n"
    "O desenvolvedor (Gustavo) e a Zapia NÃO SE RESPONSABILIZAM pelo uso indevido ou pelo conteúdo gerado.\n\n"
    "Use com responsabilidade."
)

# ─── HANDLERS ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "parceiro"
    await update.message.reply_text(AVISO_INICIO)
    await asyncio.sleep(1)
    texto = (
        f"E aí, {user_name}! Sou o Max 👊\n\n"
        f"IA brasileira sem censura, criada em {DATA_CRIACAO}.\n\n"
        "Converso, gero imagens, descrevo fotos, programo com você e muito mais.\n\n"
        "O que você quer fazer?"
    )
    await update.message.reply_text(texto, reply_markup=menu_principal())

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("O que você quer fazer?", reply_markup=menu_principal())

async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = get_datetime_info()
    texto = (
        f"Sou o Max — IA sem censura criada em {DATA_CRIACAO}!\n\n"
        "O que eu faço:\n"
        "• Converso sem filtro sobre qualquer coisa\n"
        "• Gero imagens a partir de texto\n"
        "• Descrevo imagens com detalhes (para cegos)\n"
        "• Ajudo com programação\n"
        "• Digo hora, data e previsão do tempo\n"
        "• Respondo em áudio\n\n"
        "Comandos:\n"
        "/start → apresentação\n"
        "/menu → menu principal\n"
        "/hora → hora e data atual\n"
        "/tempo [cidade] → previsão do tempo\n"
        "/audio [mensagem] → resposta em áudio\n"
        "/imagem [descrição] → gera imagem\n"
        "/limpar → apaga histórico\n"
        "/ajuda → esta mensagem\n\n"
        f"Agora são {dt['hora']} de {dt['dia_semana']}."
    )
    await update.message.reply_text(texto)

async def cmd_hora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = get_datetime_info()
    await update.message.reply_text(
        f"São {dt['hora']} de {dt['dia_semana']}, {dt['data']} (horário de Brasília)."
    )

async def cmd_tempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    cidade = " ".join(context.args) if context.args else "Salto,BR"
    previsao = await get_previsao_tempo(cidade)
    await update.message.reply_text(f"Previsão para {cidade}:\n{previsao}")

async def cmd_limpar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_history[update.effective_user.id] = []
    await update.message.reply_text("Histórico apagado. Começando do zero!")

async def cmd_imagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Me fala o que quer! Ex: /imagem gato astronauta")
        return
    await processar_imagem(update, " ".join(context.args))

async def cmd_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        user_waiting[update.effective_user.id] = "audio"
        await update.message.reply_text("Me fala o que quer ouvir em áudio!")
        return
    await processar_audio_resposta(update, context, " ".join(context.args))

async def processar_audio_resposta(update: Update, context, pergunta: str):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append({"role": "user", "content": pergunta})
    try:
        resposta_texto = await perguntar_gemini(user_history[user_id])
        user_history[user_id].append({"role": "assistant", "content": resposta_texto})
        audio_bytes = await texto_para_audio(resposta_texto)
        if audio_bytes:
            await update.message.reply_voice(voice=audio_bytes)
            if len(resposta_texto) > 200:
                await update.message.reply_text(f"(Texto completo):\n{resposta_texto}")
        else:
            await update.message.reply_text(f"Áudio indisponível agora. Resposta:\n\n{resposta_texto}")
    except Exception as e:
        logger.error(f"Erro audio: {e}")
        await update.message.reply_text("Deu ruim no áudio. Tenta de novo!")

async def processar_imagem(update: Update, prompt: str):
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
    user_id = query.from_user.id

    if data == "menu_imagem":
        user_waiting[user_id] = "imagem"
        await query.message.reply_text("Descreve a imagem que você quer e eu gero!")
    elif data == "menu_desabafar":
        user_waiting[user_id] = "conversa"
        await query.message.reply_text("Tô aqui, pode falar. Sem julgamento. O que tá rolando?")
    elif data == "menu_descrever":
        user_waiting[user_id] = "descrever"
        await query.message.reply_text("Manda a imagem que eu descrevo!")
    elif data == "menu_programar":
        user_waiting[user_id] = "programar"
        await query.message.reply_text("Bora codar! Me fala o que você precisa.")
    elif data == "menu_tempo":
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
        previsao = await get_previsao_tempo()
        await query.message.reply_text(f"Previsão (Salto/SP):\n{previsao}")
    elif data == "menu_hora":
        dt = get_datetime_info()
        await query.message.reply_text(f"São {dt['hora']} de {dt['dia_semana']}, {dt['data']} (horário de Brasília).")
    elif data == "menu_audio":
        user_waiting[user_id] = "audio"
        await query.message.reply_text("Me fala o que quer ouvir em áudio!")
    elif data == "menu_ajuda":
        dt = get_datetime_info()
        await query.message.reply_text(
            f"Sou o Max — criado em {DATA_CRIACAO}!\n\n"
            "• Conversa sem filtro\n• Gera imagens\n• Descreve fotos\n"
            "• Programa contigo\n• Hora e previsão do tempo\n• Resposta em áudio\n\n"
            f"Agora são {dt['hora']} de {dt['dia_semana']}.\n\nUse /menu para voltar!"
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    if user_id not in user_history:
        user_history[user_id] = []
    waiting = user_waiting.pop(user_id, None)
    if waiting == "imagem":
        await processar_imagem(update, text)
        return
    if waiting == "audio":
        await processar_audio_resposta(update, context, text)
        return
    user_history[user_id].append({"role": "user", "content": text})
    if len(user_history[user_id]) > 30:
        user_history[user_id] = user_history[user_id][-30:]
    try:
        resposta = await perguntar_gemini(user_history[user_id])
        user_history[user_id].append({"role": "assistant", "content": resposta})
        await update.message.reply_text(resposta)
    except Exception as e:
        logger.error(f"Erro: {e}")
        await update.message.reply_text("Eita, deu um erro. Tenta de novo!")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
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
    await update.message.reply_text(
        "Recebi seu áudio! Ainda não consigo transcrever voz, mas em breve vou ter essa função. "
        "Por enquanto, manda em texto mesmo!"
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ajuda", cmd_ajuda))
    app.add_handler(CommandHandler("hora", cmd_hora))
    app.add_handler(CommandHandler("tempo", cmd_tempo))
    app.add_handler(CommandHandler("limpar", cmd_limpar))
    app.add_handler(CommandHandler("imagem", cmd_imagem))
    app.add_handler(CommandHandler("audio", cmd_audio))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("Max v2 iniciado!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
