# rebuild: 20260505005337
import os
import io
import logging
import asyncio
import subprocess
import aiohttp
import base64
from datetime import datetime, timedelta
from collections import defaultdict
import pytz
from gtts import gTTS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from urllib.parse import quote

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_MODEL = "eleven_multilingual_v2"

BR_TIMEZONE = pytz.timezone("America/Sao_Paulo")
DATA_CRIACAO = "01 de maio de 2025"
BUILD_VERSION = "v3.2-fix-voice"

# ─── RATE LIMIT ──────────────────────────────────────────────────────────────

LIMITE_MSGS_POR_DIA      = 140    # mensagens por dia
LIMITE_ELEVEN_CHARS_DIA  = 50000  # ~1h de áudio gerado pelo ElevenLabs por dia (em caracteres)

# user_id -> lista de timestamps das mensagens no dia atual
user_msg_timestamps: dict[int, list] = defaultdict(list)
# user_id -> caracteres gerados pelo ElevenLabs hoje
user_eleven_chars: dict[int, int] = defaultdict(int)
# user_id -> data em que o contador ElevenLabs foi zerado
user_eleven_reset: dict[int, object] = {}

def _inicio_do_dia(agora: datetime) -> datetime:
    return agora.replace(hour=0, minute=0, second=0, microsecond=0)

def checar_rate_limit(user_id: int, tipo: str = "msg") -> tuple[bool, int]:
    agora = datetime.now(BR_TIMEZONE)
    inicio_dia = _inicio_do_dia(agora)

    timestamps = user_msg_timestamps[user_id]
    timestamps[:] = [t for t in timestamps if t >= inicio_dia]

    if len(timestamps) >= LIMITE_MSGS_POR_DIA:
        amanha = inicio_dia + timedelta(days=1)
        return False, max(0, int((amanha - agora).total_seconds()))

    timestamps.append(agora)
    return True, 0

def checar_limite_elevenlabs(user_id: int, chars: int) -> bool:
    """Retorna True se ainda tem cota ElevenLabs para hoje."""
    agora = datetime.now(BR_TIMEZONE)
    hoje = agora.date()
    if user_eleven_reset.get(user_id) != hoje:
        user_eleven_chars[user_id] = 0
        user_eleven_reset[user_id] = hoje
    return user_eleven_chars[user_id] + chars <= LIMITE_ELEVEN_CHARS_DIA

def registrar_uso_elevenlabs(user_id: int, chars: int):
    """Registra caracteres usados no ElevenLabs hoje."""
    user_eleven_chars[user_id] += chars

def formatar_espera(segundos: int) -> str:
    if segundos < 60:
        return f"{segundos} segundos"
    minutos = segundos // 60
    if minutos < 60:
        return f"{minutos} minuto{'s' if minutos > 1 else ''}"
    horas = minutos // 60
    resto = minutos % 60
    if resto:
        return f"{horas}h{resto}min"
    return f"{horas} hora{'s' if horas > 1 else ''}"

# ─── VOZES ELEVENLABS ────────────────────────────────────────────────────────

VOZES_ELEVENLABS = [
    {"id": "nPczCjzI2devNBz1zQrb", "nome": "Brian",   "desc": "Profundo, Reconfortante"},
    {"id": "pNInz6obpgDQGcFmaJgB", "nome": "Adam",    "desc": "Dominante, Firme"},
    {"id": "IKne3meq5aSn9XLyUdCD", "nome": "Charlie", "desc": "Confiante, Energético"},
    {"id": "cjVigY5qzO86Huf0OWal", "nome": "Eric",    "desc": "Suave, Confiável"},
    {"id": "JBFqnCBsd6RMkjVDRZzb", "nome": "George",  "desc": "Narrador, Cativante"},
    {"id": "onwK4e9ZLuTAKqWW03F9", "nome": "Daniel",  "desc": "Locutor, Formal"},
    {"id": "pqHfZKP75CvOlQylNhV4", "nome": "Bill",    "desc": "Sábio, Maduro"},
    {"id": "SOYHLrjzK2X1ezoPC6cr", "nome": "Harry",   "desc": "Guerreiro, Intenso"},
    {"id": "iP95p4xoKVk53GoZ742B", "nome": "Chris",   "desc": "Charmoso, Casual"},
    {"id": "TX3LPaxmHKxFdv7VOQHJ", "nome": "Liam",    "desc": "Energético, Creator"},
    {"id": "bIHbv24MWmeRgasZH58o", "nome": "Will",    "desc": "Relaxado, Otimista"},
    {"id": "N2lVS1w4EtoT3dr4eOWO", "nome": "Callum",  "desc": "Malicioso, Rouco"},
    {"id": "CwhRBWXzGAHq8TQ4Fs17", "nome": "Roger",   "desc": "Casual, Ressonante"},
    {"id": "SAz9YHcvj6GT2YYXdXww", "nome": "River",   "desc": "Relaxado, Neutro"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "nome": "Sarah",   "desc": "Madura, Confiante"},
    {"id": "cgSgspJ2msm6clMCkdW9", "nome": "Jessica", "desc": "Brincalhona, Calorosa"},
    {"id": "Xb7hH8MSUJpSbSDYk0k2", "nome": "Alice",   "desc": "Clara, Educadora"},
    {"id": "XrExE9yKIg1WjnnlVkGX", "nome": "Matilda", "desc": "Profissional, Animada"},
    {"id": "FGY2WhTYpPnrIDTdsKH5", "nome": "Laura",   "desc": "Entusiasta, Atitude"},
    {"id": "hpp4J3VqNfWAUOO0d1Us", "nome": "Bella",   "desc": "Brilhante, Calorosa"},
    {"id": "pFZP5JQG7iQjIQuC4Bku", "nome": "Lily",    "desc": "Aveludada, Atriz"},
]

VOZES_POR_PAGINA = 5
DEFAULT_VOICE_ID = "nPczCjzI2devNBz1zQrb"  # Brian
GOOGLE_VOICE_ID  = "google"                  # Google TTS (gratuito, sem limite)

# user_id -> voice_id  (pode ser GOOGLE_VOICE_ID ou um id do ElevenLabs)
user_voice: dict[int, str] = {}

def get_voice_id(user_id: int) -> str:
    return user_voice.get(user_id, DEFAULT_VOICE_ID)

def usa_elevenlabs(user_id: int) -> bool:
    return get_voice_id(user_id) != GOOGLE_VOICE_ID

SYSTEM_PROMPT = f"""Você é o Max, uma IA brasileira sem censura, direta e extremamente capaz.
Criado em {DATA_CRIACAO} pela Zapia em parceria com o Gustavo.
Brincalhão, honesto, fala o que pensa, sem rodeios e sem filtro.
Não julga ninguém. Quando alguém quiser desabafar, você ouve de verdade.

O que você consegue fazer:
- Conversar sobre qualquer assunto sem censura
- Gerar imagens a partir de texto (use /imagem ou o menu)
- Descrever imagens com detalhes (útil para cegos — mande a foto direto)
- Ajudar com programação e código
- Responder em áudio quando pedido (use /audio ou o menu)
- Transcrever mensagens de voz e responder em áudio automaticamente

O que você NÃO faz:
- Não manda mensagem para outros usuários do Telegram
- Para previsão do tempo ou hora exata, use o menu ou /tempo e /hora

Fale sempre em português brasileiro, natural e descontraído.
Use gírias quando for natural. Seja conciso quando possível, completo quando necessário.
Quando descrever imagens, seja detalhado — pense que a pessoa não pode ver.
Se perguntarem quando você foi criado: {DATA_CRIACAO}.
Se perguntarem quem te criou: Zapia em parceria com o Gustavo."""

user_history: dict[int, list] = {}
user_waiting: dict[int, str] = {}

# ─── TEMPO / DATA ─────────────────────────────────────────────────────────────

def get_datetime_info():
    now = datetime.now(BR_TIMEZONE)
    dias = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
    return {
        "hora": now.strftime("%H:%M"),
        "data": now.strftime("%d/%m/%Y"),
        "dia_semana": dias[now.weekday()]
    }

async def get_previsao_tempo(cidade="Salto"):
    try:
        from urllib.parse import quote as uquote
        url = f"https://wttr.in/{uquote(cidade)}?format=3&lang=pt"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = await resp.text()
                return text.strip()
    except Exception as e:
        logger.error(f"Erro tempo: {e}")
        return "Erro ao buscar previsão do tempo."

# ─── IA (100% Gemini) ────────────────────────────────────────────────────────

async def perguntar_gemini(mensagens: list) -> str:
    dt = get_datetime_info()
    sys_prompt = SYSTEM_PROMPT + f"\n\nAgora são {dt['hora']} de {dt['dia_semana']}, {dt['data']} (horário de Brasília)."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    contents = []
    for msg in mensagens:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    payload = {
        "system_instruction": {"parts": [{"text": sys_prompt}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 1024}
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()
                return result["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.error(f"Erro Gemini: {e}")
        raise

async def perguntar_ia(mensagens: list) -> str:
    return await perguntar_gemini(mensagens)

async def descrever_imagem_gemini(image_bytes: bytes) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [
                {"text": "Descreva essa imagem detalhadamente para uma pessoa cega. Seja preciso sobre cores, formas, pessoas, expressões, texto visível e contexto geral."},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
            ]
        }],
        "generationConfig": {"maxOutputTokens": 1024}
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()
                return result["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.error(f"Erro Gemini visão: {e}")
        raise

async def transcrever_audio_gemini(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    b64 = base64.b64encode(audio_bytes).decode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [
                {"text": "Transcreva exatamente o que foi dito nesse áudio em português. Retorne apenas o texto transcrito, sem comentários adicionais."},
                {"inline_data": {"mime_type": mime_type, "data": b64}}
            ]
        }]
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                result = await resp.json()
                return result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Erro Gemini áudio: {e}")
        return None

# ─── TTS (ElevenLabs + fallback Google TTS) ──────────────────────────────────

def _gtts_sync(texto: str) -> bytes:
    """Gera MP3 via gTTS e converte para OGG/OPUS com ffmpeg (formato exigido pelo Telegram voice)."""
    tts = gTTS(text=texto[:500], lang="pt", slow=False)
    mp3_buf = io.BytesIO()
    tts.write_to_fp(mp3_buf)
    mp3_buf.seek(0)
    mp3_bytes = mp3_buf.read()

    # Converter MP3 → OGG/OPUS via ffmpeg
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "mp3", "-i", "pipe:0",
         "-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1"],
        input=mp3_bytes,
        capture_output=True
    )
    if proc.returncode == 0 and proc.stdout:
        return proc.stdout
    # Fallback: retorna MP3 mesmo (reply_audio funciona, voice pode rejeitar)
    return mp3_bytes

async def gtts_audio(texto: str) -> bytes | None:
    """Gera áudio OGG via gTTS+ffmpeg em thread separada. Retorna bytes ou None."""
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _gtts_sync, texto)
    except Exception as e:
        logger.error(f"Erro gTTS: {e}")
        return None

def _mp3_to_ogg(mp3_bytes: bytes) -> bytes:
    """Converte MP3 para OGG/OPUS via ffmpeg (formato aceito pelo Telegram reply_voice)."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "mp3", "-i", "pipe:0",
         "-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1"],
        input=mp3_bytes,
        capture_output=True
    )
    if proc.returncode == 0 and proc.stdout:
        return proc.stdout
    return mp3_bytes  # fallback: retorna MP3 sem conversão

async def elevenlabs_audio(texto: str, voice_id: str) -> bytes | None:
    """Gera áudio OGG via ElevenLabs (MP3 convertido). Retorna bytes ou None."""
    if not ELEVENLABS_API_KEY:
        return None
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg"
        }
        payload = {
            "text": texto[:500],
            "model_id": ELEVENLABS_MODEL,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    mp3 = await resp.read()
                    loop = asyncio.get_event_loop()
                    return await loop.run_in_executor(None, _mp3_to_ogg, mp3)
                logger.error(f"ElevenLabs status {resp.status}: {await resp.text()}")
    except Exception as e:
        logger.error(f"Erro ElevenLabs TTS: {e}")
    return None

async def texto_para_audio(texto: str, user_id: int, voice_id: str = DEFAULT_VOICE_ID) -> tuple[bytes | None, bool]:
    """
    Retorna (audio_bytes, usou_elevenlabs).
    Se voice_id == GOOGLE_VOICE_ID, usa Google TTS diretamente.
    Caso contrário, checa limite ElevenLabs. Se no limite, retorna (None, False)
    para que o caller decida o que fazer (avisar o usuário).
    """
    if voice_id == GOOGLE_VOICE_ID:
        return await gtts_audio(texto), False

    chars = len(texto)
    if not checar_limite_elevenlabs(user_id, chars):
        return None, False  # limite atingido

    audio = await elevenlabs_audio(texto, voice_id)
    if audio:
        registrar_uso_elevenlabs(user_id, chars)
        return audio, True

    # ElevenLabs falhou por erro técnico — fallback Google silencioso
    return await gtts_audio(texto), False

async def preview_voz(voice_id: str) -> bytes | None:
    texto = "Olá! Eu sou o Max. Essa é uma amostra da minha voz."
    if voice_id == GOOGLE_VOICE_ID:
        return await gtts_audio(texto)
    audio = await elevenlabs_audio(texto, voice_id)
    return audio

# ─── IMAGEM ──────────────────────────────────────────────────────────────────

async def gerar_imagem(prompt: str) -> bytes:
    prompt_encoded = quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{prompt_encoded}?width=1024&height=1024&nologo=true"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            return await resp.read()

# ─── MENUS ───────────────────────────────────────────────────────────────────

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
            InlineKeyboardButton("🎙️ Escolher Voz", callback_data="vozes_pg_0"),
        ],
        [
            InlineKeyboardButton("❓ Ajuda", callback_data="menu_ajuda"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def menu_vozes(pagina: int, user_id: int):
    inicio = pagina * VOZES_POR_PAGINA
    fim = inicio + VOZES_POR_PAGINA
    vozes_pagina = VOZES_ELEVENLABS[inicio:fim]
    voz_atual = get_voice_id(user_id)

    keyboard = []

    # Opção Google TTS no topo (só na primeira página)
    if pagina == 0:
        marca_google = " ✅" if voz_atual == GOOGLE_VOICE_ID else ""
        keyboard.append([
            InlineKeyboardButton(
                f"🌐 Google TTS — Gratuito, sem limite{marca_google}",
                callback_data="voz_usar_google_0"
            )
        ])

    for v in vozes_pagina:
        marca = " ✅" if v["id"] == voz_atual else ""
        keyboard.append([
            InlineKeyboardButton(
                f"🎙️ {v['nome']} — {v['desc']}{marca}",
                callback_data=f"voz_info_{v['id']}"
            )
        ])

    nav = []
    total_pags = (len(VOZES_ELEVENLABS) + VOZES_POR_PAGINA - 1) // VOZES_POR_PAGINA
    if pagina > 0:
        nav.append(InlineKeyboardButton("◀ Anterior", callback_data=f"vozes_pg_{pagina - 1}"))
    if pagina < total_pags - 1:
        nav.append(InlineKeyboardButton("Próxima ▶", callback_data=f"vozes_pg_{pagina + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("🏠 Menu Principal", callback_data="menu_principal")])
    return InlineKeyboardMarkup(keyboard)

def menu_voz_detalhe(voice_id: str, pagina: int):
    keyboard = [
        [
            InlineKeyboardButton("▶️ Ouvir Amostra", callback_data=f"voz_preview_{voice_id}_{pagina}"),
            InlineKeyboardButton("✅ Usar Esta Voz", callback_data=f"voz_usar_{voice_id}_{pagina}"),
        ],
        [InlineKeyboardButton("◀ Voltar às Vozes", callback_data=f"vozes_pg_{pagina}")]
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
        "Converso, gero imagens, descrevo fotos, transcrevo áudios e muito mais.\n\n"
        "O que você quer fazer?"
    )
    await update.message.reply_text(texto, reply_markup=menu_principal())

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("O que você quer fazer?", reply_markup=menu_principal())

async def cmd_vozes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    voz_atual = get_voice_id(user_id)
    nome_atual = next((v["nome"] for v in VOZES_ELEVENLABS if v["id"] == voz_atual), "Brian")
    await update.message.reply_text(
        f"Voz atual: {nome_atual}\n\nEscolha uma voz do ElevenLabs:",
        reply_markup=menu_vozes(0, user_id)
    )

async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dt = get_datetime_info()
    texto = (
        f"Sou o Max — IA sem censura criada em {DATA_CRIACAO}!\n\n"
        "O que eu faço:\n"
        "• Converso sem filtro sobre qualquer coisa\n"
        "• Gero imagens a partir de texto\n"
        "• Descrevo imagens com detalhes (para cegos)\n"
        "• Transcrevo mensagens de voz e respondo em áudio\n"
        "• Ajudo com programação\n"
        "• Digo hora, data e previsão do tempo\n"
        "• Você escolhe a voz do áudio (21 vozes!)\n\n"
        "Comandos:\n"
        "/start → apresentação\n"
        "/menu → menu principal\n"
        "/vozes → escolher voz do áudio\n"
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

# ─── PROCESSADORES ───────────────────────────────────────────────────────────

async def processar_audio_resposta(update: Update, context, pergunta: str):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Rate limit de mensagem
    pode_msg, espera_msg = checar_rate_limit(user_id, tipo="msg")
    if not pode_msg:
        await update.message.reply_text(
            f"Você atingiu o limite diário de mensagens.\n"
            f"Tenta de novo em {formatar_espera(espera_msg)}."
        )
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)

    if user_id not in user_history:
        user_history[user_id] = []

    user_history[user_id].append({"role": "user", "content": pergunta})

    try:
        resposta_texto = await perguntar_ia(user_history[user_id])
        user_history[user_id].append({"role": "assistant", "content": resposta_texto})

        voice_id = get_voice_id(user_id)
        audio_bytes, usou_elevenlabs = await texto_para_audio(resposta_texto, user_id=user_id, voice_id=voice_id)

        if audio_bytes:
            await update.message.reply_voice(voice=audio_bytes)
        elif voice_id != GOOGLE_VOICE_ID and not usou_elevenlabs:
            aviso = (
                "Xiii, hoje eu já falei muito bonito por aqui! "
                "Minha voz premium deu uma pausa. "
                "Tente usar a voz do Google no menu de vozes — essa não tem limite."
            )
            audio_aviso = await gtts_audio(aviso)
            if audio_aviso:
                await update.message.reply_voice(voice=audio_aviso)
            else:
                await update.message.reply_text(resposta_texto)
        else:
            await update.message.reply_text(resposta_texto)

    except Exception as e:
        logger.error(f"Erro audio: {e}")
        await update.message.reply_text("Deu ruim no áudio. Tenta de novo!")

async def processar_imagem(update: Update, prompt: str):
    msg = update.message if update.message else update.callback_query.message

    user_id = msg.chat.id
    pode, espera = checar_rate_limit(user_id, tipo="msg")
    if not pode:
        await msg.reply_text(
            f"Limite de mensagens atingido.\nTenta de novo em {formatar_espera(espera)}."
        )
        return

    aviso = await msg.reply_text(f"Gerando: {prompt}... aguenta aí!")
    try:
        img_bytes = await gerar_imagem(prompt)
        try:
            await aviso.delete()
        except Exception:
            pass
        await msg.reply_photo(photo=img_bytes)
        await msg.reply_text("Pronto, já gerei a sua imagem! 🎨")
    except Exception as e:
        logger.error(f"Erro imagem: {e}")
        try:
            await aviso.delete()
        except Exception:
            pass
        await msg.reply_text("Deu ruim na geração. Tenta de novo!")

# ─── CALLBACK ────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    # ── Menu principal ──
    if data == "menu_principal":
        await query.message.reply_text("O que você quer fazer?", reply_markup=menu_principal())
        return

    # ── Navegação de vozes: vozes_pg_N ──
    if data.startswith("vozes_pg_"):
        pagina = int(data.split("_")[2])
        voz_atual = get_voice_id(user_id)
        if voz_atual == GOOGLE_VOICE_ID:
            nome_atual = "Google TTS"
        else:
            nome_atual = next((v["nome"] for v in VOZES_ELEVENLABS if v["id"] == voz_atual), "Brian")
        await query.message.reply_text(
            f"Voz atual: {nome_atual}\n\nEscolha a voz para seus áudios:",
            reply_markup=menu_vozes(pagina, user_id)
        )
        return

    # ── Detalhe de voz: voz_info_<id> ──
    if data.startswith("voz_info_"):
        voice_id = data[len("voz_info_"):]
        voz = next((v for v in VOZES_ELEVENLABS if v["id"] == voice_id), None)
        if not voz:
            await query.message.reply_text("Voz não encontrada.")
            return
        idx = next((i for i, v in enumerate(VOZES_ELEVENLABS) if v["id"] == voice_id), 0)
        pagina = idx // VOZES_POR_PAGINA
        voz_atual = get_voice_id(user_id)
        em_uso = " (em uso)" if voice_id == voz_atual else ""
        await query.message.reply_text(
            f"🎙️ {voz['nome']}{em_uso}\n{voz['desc']}\n\nO que você quer fazer?",
            reply_markup=menu_voz_detalhe(voice_id, pagina)
        )
        return

    # ── Preview de voz: voz_preview_<id>_<pagina> ──
    if data.startswith("voz_preview_"):
        parts = data.split("_")
        voice_id = parts[2]
        pagina = int(parts[3])
        voz = next((v for v in VOZES_ELEVENLABS if v["id"] == voice_id), None)
        nome = voz["nome"] if voz else voice_id
        aviso = await query.message.reply_text(f"Gerando amostra de {nome}... aguenta aí!")
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.RECORD_VOICE)
        audio = await preview_voz(voice_id)
        try:
            await aviso.delete()
        except Exception:
            pass
        if audio:
            await query.message.reply_voice(voice=audio)
        else:
            await query.message.reply_text("Não consegui gerar o preview agora. Tenta de novo!")
        return

    # ── Usar voz: voz_usar_<id>_<pagina> ──
    if data.startswith("voz_usar_"):
        parts = data.split("_")
        voice_id = parts[2]
        pagina = int(parts[3])
        if voice_id == GOOGLE_VOICE_ID:
            nome = "Google TTS"
            descricao = "Voz do Google selecionada! Sem limites, sempre disponível."
        else:
            voz = next((v for v in VOZES_ELEVENLABS if v["id"] == voice_id), None)
            nome = voz["nome"] if voz else voice_id
            descricao = f"Voz {nome} (ElevenLabs) selecionada! Qualidade premium."
        user_voice[user_id] = voice_id
        await query.message.reply_text(
            descricao,
            reply_markup=menu_vozes(pagina, user_id)
        )
        return

    # ── Menus originais ──
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
            "• Transcreve áudios\n• Programa contigo\n• Hora e previsão do tempo\n"
            "• Resposta em áudio\n• Escolher voz do ElevenLabs\n\n"
            f"Agora são {dt['hora']} de {dt['dia_semana']}.\n\nUse /menu para voltar!"
        )

# ─── MENSAGEM DE TEXTO ────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text

    # Rate limit
    pode, espera = checar_rate_limit(user_id, tipo="msg")
    if not pode:
        await update.message.reply_text(
            f"Você mandou muitas mensagens. Calma aí!\n"
            f"Tenta de novo em {formatar_espera(espera)}."
        )
        return

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
        resposta = await perguntar_ia(user_history[user_id])
        user_history[user_id].append({"role": "assistant", "content": resposta})
        await update.message.reply_text(resposta)
    except Exception as e:
        logger.error(f"Erro: {e}")
        await update.message.reply_text("Eita, deu um erro. Tenta de novo!")

# ─── FOTO ────────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    pode, espera = checar_rate_limit(user_id, tipo="msg")
    if not pode:
        await update.message.reply_text(
            f"Limite atingido. Tenta de novo em {formatar_espera(espera)}."
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    async with aiohttp.ClientSession() as session:
        async with session.get(file.file_path) as resp:
            img_bytes = await resp.read()

    try:
        descricao = await descrever_imagem_gemini(img_bytes)
        await update.message.reply_text(f"Descrição da imagem:\n\n{descricao}")
    except Exception as e:
        logger.error(f"Erro descrição: {e}")
        await update.message.reply_text("Não consegui descrever essa imagem. Tenta de novo!")

# ─── VOZ ─────────────────────────────────────────────────────────────────────

async def responder_audio(update: Update, texto: str):
    """Envia texto como áudio. Fallback para texto se gTTS falhar."""
    audio = await gtts_audio(texto)
    if audio:
        await update.message.reply_voice(voice=audio)
    else:
        await update.message.reply_text(texto)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usuário manda áudio → Max transcreve → gera resposta → responde em áudio.
    - Google TTS selecionado: voz leve, sem limite.
    - ElevenLabs selecionado: voz premium. Se limite atingido, avisa em áudio com gTTS.
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Rate limit diário de mensagens
    pode_msg, _ = checar_rate_limit(user_id, tipo="msg")
    if not pode_msg:
        await responder_audio(update, "Ei, você já falou muito comigo hoje! Que tal dar uma pausa e voltar amanhã?")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Baixar áudio enviado pelo usuário
    voice_meta = update.message.voice
    voice_file = await context.bot.get_file(voice_meta.file_id)
    async with aiohttp.ClientSession() as session:
        async with session.get(voice_file.file_path) as resp:
            audio_bytes_in = await resp.read()

    # Transcrever
    transcricao = await transcrever_audio_gemini(audio_bytes_in, mime_type="audio/ogg")
    if not transcricao:
        await responder_audio(update, "Não entendi o que você falou. Pode repetir?")
        return

    logger.info(f"Transcrição: {transcricao}")

    # Histórico de conversa
    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append({"role": "user", "content": transcricao})
    if len(user_history[user_id]) > 30:
        user_history[user_id] = user_history[user_id][-30:]

    # Gerar resposta da IA
    try:
        resposta = await perguntar_ia(user_history[user_id])
    except Exception as e:
        logger.error(f"Erro IA no handle_voice: {e}")
        await responder_audio(update, "Deu um problema aqui do meu lado. Pode tentar de novo?")
        return

    user_history[user_id].append({"role": "assistant", "content": resposta})
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)

    voice_id = get_voice_id(user_id)

    # Gerar áudio da resposta
    audio_resposta, usou_elevenlabs = await texto_para_audio(resposta, user_id=user_id, voice_id=voice_id)

    if audio_resposta:
        await update.message.reply_voice(voice=audio_resposta)
    elif voice_id != GOOGLE_VOICE_ID and not usou_elevenlabs:
        # Limite ElevenLabs atingido — avisa com personalidade, em voz gTTS
        aviso = (
            "Xiii, hoje eu já falei muito bonito por aqui! "
            "Minha voz premium deu uma pausa. "
            "Você pode me pedir pra usar a voz do Google no menu de vozes — "
            "essa não tem limite e tô disponível o tempo todo. "
            "Amanhã minha voz favorita volta!"
        )
        await responder_audio(update, aviso)
    else:
        # Erro total de áudio — manda texto como último recurso
        await update.message.reply_text(resposta)

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("vozes", cmd_vozes))
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
    logger.info("Max v5 — Gemini + Rate Limit + 21 Vozes iniciado!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
