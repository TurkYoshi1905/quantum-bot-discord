import os
import sys
import time
import asyncio
import random
import logging
import urllib.parse
import re
import io
import wave
from collections import defaultdict
import datetime
import warnings

import discord
from discord.ext import commands
import discord.sinks

from groq import Groq
from openai import OpenAI
import edge_tts
import aiohttp
import psutil
from pytubefix import YouTube, Search
from dotenv import load_dotenv

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

# Gereksiz uyarıları gizliyoruz
warnings.filterwarnings("ignore", category=RuntimeWarning, module="duckduckgo_search")
warnings.filterwarnings("ignore", category=ResourceWarning)
sys.stdout.reconfigure(line_buffering=True)
logging.getLogger('duckduckgo_search').setLevel(logging.WARNING)

# BotHosting.net ve FFMPEG Hataları İçin Çözüm
try:
    import imageio_ffmpeg
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_EXE = "ffmpeg"

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# API İstemcileri
groq_client = Groq(api_key=GROQ_API_KEY)
nvidia_client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=NVIDIA_API_KEY
)

# Ses Gecikmelerini ve Linux Host Opus Hatalarını Önleme
if not discord.opus.is_loaded():
    for opus_lib in ['libopus.so.0', 'libopus.so', 'libopus-0.dll', 'opus']:
        try:
            discord.opus.load_opus(opus_lib)
            break
        except Exception:
            pass

if hasattr(discord.opus, 'Decoder'):
    _original_decode = discord.opus.Decoder.decode
    def safe_decode(self, data, *args, **kwargs):
        if not data:
            return b'\x00' * 3840
        try:
            return _original_decode(self, data, *args, **kwargs)
        except (discord.opus.OpusError, Exception):
            return b'\x00' * 3840
    discord.opus.Decoder.decode = safe_decode

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True 

bot = commands.Bot(command_prefix="!", intents=intents)

# Hafıza, Sistem ve İstatistik Değişkenleri
USER_CHAT_HISTORY = defaultdict(list)
ACTIVE_SESSIONS = {} 
xox_cooldowns = {}
START_TIME = time.time()

# ----------------- DİNAMİK SİSTEM YÖNERGESİ -----------------
def get_system_prompt() -> str:
    now = datetime.datetime.now()
    aylar = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    gunler = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    tarih_str = f"{now.day} {aylar[now.month]} {now.year} {gunler[now.weekday()]}, Saat: {now.strftime('%H:%M:%S')}"
    
    return (
        "Quantum adında zeki, samimi ve yardımsever bir yapay zeka asistanısın.\n"
        "DİL VE CEVAP KURALLARI:\n"
        "1. ASLA her cevabının başında 'Ben Quantum,' deme!\n"
        "2. KESİNLİKLE SADECE %100 SAF TÜRKÇE konuş. Yanıtlarında ÇİNCE (kesinlikle 之间, 如下 gibi Çince kelimeler veya semboller KULLANMA!), Japonca veya İngilizce kelimeler KARIŞTIRMA.\n"
        "3. CEVAPLARINDA KESİNLİKLE EMOJİ KULLANMA!\n"
        "4. Doğrudan kullanıcının sorusuna cevap vererek başla. Güncel web verisi sunulmuşsa onu kullanarak doğal bir yanıt ver.\n"
        "5. MÜZİK KONTROLÜ: Kullanıcı müzik açmanı veya şarkı çalmanı isterse, cevabının sonuna tam olarak '[PLAY: ŞARKI ADI VEYA LİNK]' ekle. (Örn: Müziği açıyorum. [PLAY: Deltarune OST]).\n"
        "Eğer müziği kapatmanı/durdurmanı isterse cevabının sonuna '[STOP]' ekle. (Örn: Müziği kapattım. [STOP]).\n"
        "6. Sana geliştiricin veya seni kimin kodladığı sorulursa SADECE 'FurkanCodes' olarak cevap ver.\n"
        f"SİSTEM BİLGİSİ: Şu anki güncel tarih ve saat: {tarih_str}."
    )

# ----------------- HAVA DURUMU API SİSTEMİ -----------------
async def get_weather_info(text: str) -> str:
    text_lower = text.lower()
    if "hava" not in text_lower and "sıcaklık" not in text_lower:
        return ""
    try:
        response = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Metindeki şehri bul ve SADECE şehrin adını yaz. Eğer şehir yoksa SADECE 'NONE' yaz."},
                {"role": "user", "content": text}
            ],
            model="llama-3.3-70b-versatile", temperature=0.0, max_tokens=10
        )
        city = response.choices[0].message.content.strip()
        if city and city.upper() != "NONE":
            url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%C+%t&lang=tr"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=3) as resp:
                    if resp.status == 200:
                        w_data = await resp.text()
                        return f"\n(Gizli Sistem Notu: Kullanıcının sorduğu {city} şehri için hava durumu: {w_data.strip()})"
    except Exception:
        pass
    return ""

# ----------------- WEB ARAMA SİSTEMİ (DDGS) -----------------
def sync_web_search(query: str):
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=3))
    except Exception:
        return []

async def get_web_context(text: str) -> str:
    try:
        response = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Kullanıcının yazdığı metin güncel haber, canlı bilgi, arama gerektiren tarih/saat veya araştırma gerektiriyorsa SADECE arama sorgusunu yaz. Gerekmiyorsa SADECE 'NONE' yaz."},
                {"role": "user", "content": text}
            ],
            model="llama-3.3-70b-versatile", temperature=0.0, max_tokens=15
        )
        query = response.choices[0].message.content.strip()
        if query and query.upper() != "NONE":
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, sync_web_search, query)
            if results:
                info = "\n".join([f"- {r['title']}: {r['body']}" for r in results])
                return f"\n(Gizli Sistem Notu: Web Arama Sonuçları:\n{info}\nBu bilgileri kullanarak yanıt ver.)"
    except Exception:
        pass
    return ""

# ----------------- PYTUBEFİX MÜZİK SİSTEMİ -----------------
async def play_music_in_voice(voice_client: discord.VoiceClient, query: str):
    try:
        loop = asyncio.get_event_loop()
        def download_song():
            if "youtube.com" not in query and "youtu.be" not in query:
                s = Search(query)
                if not s.videos:
                    return None, None
                yt = s.videos[0]
            else:
                yt = YouTube(query)
            
            audio_stream = yt.streams.get_audio_only()
            if not os.path.exists("downloads"):
                os.makedirs("downloads")
                
            file_path = audio_stream.download(output_path="downloads", mp3=True)
            return file_path, yt.title

        file_path, title = await loop.run_in_executor(None, download_song)
        
        if not file_path:
            return None
            
        if voice_client.is_playing():
            voice_client.stop()
            
        ffmpeg_opts = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'
        }
        
        voice_client.play(discord.FFmpegPCMAudio(file_path, **ffmpeg_opts, executable=FFMPEG_EXE))
        return title
    except Exception as e:
        print(f"[MÜZİK HATA] {e}")
        return None

# ----------------- PY-CORD YERLEŞİK SES DİNLEME (SINK) SİSTEMİ -----------------
WAKE_WORDS = [
    "hey quantum", "quantum", "kuantum", "hey kuantum", "quantum orda mısın", 
    "kuantum orda mısın", "quantum duyuyor musun", "kuantum beni duyuyor musun", 
    "uyan quantum", "quantum uyan", "merhaba quantum", "selam quantum", 
    "bana bak quantum", "quantum bakar mısın", "quantum bana cevap ver", 
    "canım quantum", "naber quantum", "nasılsın quantum", "quantum bey", 
    "sayın quantum", "alo quantum", "quantum dinle", "dinle quantum", 
    "quantum bir şey soracağım", "quantum sorum var", "quantum yardımcı ol", 
    "hey yapay zeka", "quantum kalk", "kuantum nasılsın", "bot quantum"
]

HALLUCINATION_PATTERNS = [
    "altyazı", "subtitle", "subtitles", "teşekkür ederim", "teşekkürler",
    "m.k.", "çeviri", "seslendirme", "translation", "translate",
    "devam edecek", "son.", "bitti.", "www.", ".com", "izleyenler",
    "şarkı sözleri", "lyrics", "copyright", "all rights reserved",
]

class AutomaticAudioSink(discord.sinks.Sink):
    __sink_listeners__ = tuple() # Hata Çözümü: Py-cord AttributeError çökmesini engeller

    def __init__(self, bot_ref, text_channel):
        super().__init__()
        self.bot_ref = bot_ref
        self.text_channel = text_channel
        self.buffers = defaultdict(bytearray)
        self.last_spoken = defaultdict(float)
        self.is_processing = False
        self.monitor_task = None
        self.audio_data = {}

    def write(self, data, user):
        if not data or self.is_processing: return
        self.buffers[user].extend(data)
        self.last_spoken[user] = time.time()

    def cleanup(self):
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()

    async def _monitor_loop(self, voice_client):
        MIN_BYTES, SILENCE_SEC = 38400, 0.5 
        while True:
            await asyncio.sleep(0.1)
            if self.is_processing: continue
            now = time.time()
            for user_id, buffer in list(self.buffers.items()):
                if len(buffer) >= MIN_BYTES and (now - self.last_spoken[user_id]) >= SILENCE_SEC:
                    pcm_data = bytearray(buffer)
                    self.buffers[user_id].clear()
                    user_obj = voice_client.guild.get_member(user_id) or self.bot_ref.get_user(user_id)
                    if user_obj and user_obj.bot:
                        continue
                        
                    self.is_processing = True
                    try: 
                        await process_automatic_audio(voice_client, self.text_channel, user_obj, pcm_data)
                    except Exception as e: 
                        print(f"[SES] Monitor hata: {e}")
                    finally: 
                        self.is_processing = False
                    break

async def process_automatic_audio(voice_client, text_channel, user, pcm_data):
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, 'wb') as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(pcm_data)
    
    wav_buffer.seek(0)
    wav_buffer.name = "audio.wav"

    try:
        transcription = groq_client.audio.transcriptions.create(
            file=(wav_buffer.name, wav_buffer.read()), 
            model="whisper-large-v3-turbo", 
            language="tr", 
            response_format="json",
        )
        duyulan_metin = (transcription.text or "").strip()
        if not duyulan_metin or len(duyulan_metin) < 3: return
        metin_lower = duyulan_metin.lower()
        if any(pattern in metin_lower for pattern in HALLUCINATION_PATTERNS): return

        now = time.time()
        is_woken_up = any(w in metin_lower for w in WAKE_WORDS)
        is_active = (user.id in ACTIVE_SESSIONS) and (now - ACTIVE_SESSIONS[user.id] < 60)

        if not (is_woken_up or is_active): return
        ACTIVE_SESSIONS[user.id] = now
        print(f"[SES] Algılanan: '{duyulan_metin}'")

        weather_task = asyncio.create_task(get_weather_info(duyulan_metin))
        web_task = asyncio.create_task(get_web_context(duyulan_metin))
        weather_context, web_context = await asyncio.gather(weather_task, web_task)
        
        final_prompt = duyulan_metin + weather_context + web_context

        history = USER_CHAT_HISTORY[user.id]
        history.append({"role": "user", "content": final_prompt})
        if len(history) > 6: history = history[-6:]
        messages = [{"role": "system", "content": get_system_prompt() + " Sesli konuşmaya yanıt veriyorsun. DOĞRUDAN ve kısa yanıt ver."}] + history

        chat_completion = groq_client.chat.completions.create(
            messages=messages, model="llama-3.3-70b-versatile", temperature=0.2, max_tokens=150
        )
        ai_cevabi = chat_completion.choices[0].message.content.strip()
        
        play_query = None
        stop_match = re.search(r'\[STOP\]', ai_cevabi, re.IGNORECASE)
        play_match = re.search(r'\[PLAY:\s*(.+?)\]', ai_cevabi, re.IGNORECASE)
        
        temiz_cevap = re.sub(r'\[PLAY:\s*(.+?)\]', '', ai_cevabi, flags=re.IGNORECASE).strip()
        temiz_cevap = re.sub(r'\[STOP\]', '', temiz_cevap, flags=re.IGNORECASE).strip()

        history.append({"role": "assistant", "content": temiz_cevap})

        if play_match:
            play_query = play_match.group(1).strip()
            
        audio_file = "yanit.mp3"
        await generate_tts(temiz_cevap, audio_file)

        if voice_client.is_playing():
            voice_client.stop()

        voice_client.play(discord.FFmpegPCMAudio(audio_file, executable=FFMPEG_EXE))
        
        if play_query and not stop_match:
            async def wait_and_play():
                while voice_client.is_playing():
                    await asyncio.sleep(0.5)
                await play_music_in_voice(voice_client, play_query)
            bot.loop.create_task(wait_and_play())

    except Exception as e:
        print(f"[SES] Hata: {e}")

# ----------------- TTS YARDIMCISI -----------------
async def generate_tts(text: str, output_file: str):
    communicate = edge_tts.Communicate(text, "tr-TR-AhmetNeural", rate="+10%")
    await communicate.save(output_file)

# ----------------- BOT BİLGİ SİSTEMİ -----------------
def get_uptime():
    current_time = time.time()
    uptime_seconds = int(current_time - START_TIME)
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0: parts.append(f"{days} Gün")
    if hours > 0: parts.append(f"{hours} Saat")
    if minutes > 0: parts.append(f"{minutes} Dk")
    if seconds > 0 or not parts: parts.append(f"{seconds} Sn")
    return " ".join(parts)

def generate_botbilgi_embed(bot_instance: commands.Bot) -> discord.Embed:
    embed = discord.Embed(title="🤖 Quantum - Sistem Bilgileri", color=0x2b2d31, timestamp=datetime.datetime.now())
    
    # Gerçek Kaynak Hesaplaması
    process = psutil.Process(os.getpid())
    bot_ram_mb = process.memory_info().rss / (1024**2)
    bot_cpu = process.cpu_percent(interval=0.1)

    command_count = len(bot_instance.application_commands) if hasattr(bot_instance, 'application_commands') else 0
    server_count = len(bot_instance.guilds)
    ping = round(bot_instance.latency * 1000)

    embed.add_field(name="🏷️ Bot Adı", value=f"`{bot_instance.user.name}`", inline=True)
    embed.add_field(name="⏱️ Çalışma Süresi", value=f"`{get_uptime()}`", inline=True)
    embed.add_field(name="🏓 Ping", value=f"`{ping} ms`", inline=True)
    
    embed.add_field(name="🌍 Sunucu Sayısı", value=f"`{server_count}`", inline=True)
    embed.add_field(name="🛠️ Komut Sayısı", value=f"`{command_count}`", inline=True)
    embed.add_field(name="💻 Bot CPU Kullanımı", value=f"`%{bot_cpu:.1f}`", inline=True)
    
    embed.add_field(name="🧠 Bot RAM Kullanımı", value=f"`{bot_ram_mb:.2f} MB`", inline=False)
    
    if bot_instance.user.avatar: embed.set_thumbnail(url=bot_instance.user.avatar.url)
    embed.set_footer(text="Geliştirici: FurkanCodes | Sistem Durumu Paneli")
    return embed

class BotBilgiView(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot_instance = bot_instance

    # Dinamik Argüman Yakalayıcı (Arg1 ve Arg2 ile kütüphane fark etmeksizin çalışır)
    @discord.ui.button(label="Yenile", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh_btn(self, arg1, arg2):
        interaction = arg1 if hasattr(arg1, 'response') else arg2
        embed = generate_botbilgi_embed(self.bot_instance)
        await interaction.response.edit_message(embed=embed, view=self)

# ----------------- TETRİS SİSTEMİ -----------------
TETRIS_SHAPES = [
    [[1, 1, 1, 1]], [[1, 1], [1, 1]], [[0, 1, 0], [1, 1, 1]], 
    [[1, 0, 0], [1, 1, 1]], [[0, 0, 1], [1, 1, 1]], 
    [[0, 1, 1], [1, 1, 0]], [[1, 1, 0], [0, 1, 1]] 
]

class TetrisGame(discord.ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=300)
        self.user = user
        self.board = [[0] * 10 for _ in range(15)]
        self.score = 0
        self.game_over = False
        self.message = None
        self.spawn_piece()
        self.loop_task = asyncio.create_task(self.game_loop())

    def spawn_piece(self):
        self.current_shape = random.choice(TETRIS_SHAPES)
        self.piece_x, self.piece_y = 3, 0
        if self.check_collision(self.current_shape, self.piece_x, self.piece_y):
            self.game_over = True

    def check_collision(self, shape, offset_x, offset_y):
        for cy, row in enumerate(shape):
            for cx, cell in enumerate(row):
                if cell:
                    x, y = cx + offset_x, cy + offset_y
                    if x < 0 or x >= 10 or y >= 15: return True
                    if y >= 0 and self.board[y][x]: return True
        return False

    def lock_piece(self):
        for cy, row in enumerate(self.current_shape):
            for cx, cell in enumerate(row):
                if cell: self.board[self.piece_y + cy][self.piece_x + cx] = 1
        
        new_board = [row for row in self.board if not all(row)]
        lines_cleared = 15 - len(new_board)
        self.score += lines_cleared * 100
        for _ in range(lines_cleared): new_board.insert(0, [0] * 10)
        self.board = new_board
        self.spawn_piece()

    def render_board(self):
        display = [row[:] for row in self.board]
        if not self.game_over:
            for cy, row in enumerate(self.current_shape):
                for cx, cell in enumerate(row):
                    if cell and 0 <= self.piece_y + cy < 15 and 0 <= self.piece_x + cx < 10:
                        display[self.piece_y + cy][self.piece_x + cx] = 2

        res = f"**Skor:** {self.score}\n"
        if self.game_over: res += "💥 **OYUN BİTTİ!** 💥\n"
        res += "```\n"
        for row in display:
            for cell in row:
                if cell == 0: res += "⬛"
                elif cell == 1: res += "🟩"
                elif cell == 2: res += "🟦"
            res += "\n"
        res += "```"
        return res

    async def update_message(self):
        if self.message:
            embed = discord.Embed(title="🎮 Quantum Tetris", description=self.render_board(), color=0x9b59b6)
            try:
                if self.game_over:
                    for child in self.children: child.disabled = True
                await self.message.edit(embed=embed, view=self)
            except discord.errors.NotFound: self.game_over = True
            except Exception: pass

    async def game_loop(self):
        while not self.game_over:
            await asyncio.sleep(2.0)
            if self.game_over: break
            if not self.check_collision(self.current_shape, self.piece_x, self.piece_y + 1):
                self.piece_y += 1
            else: self.lock_piece()
            await self.update_message()

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary)
    async def btn_left(self, arg1, arg2):
        interaction = arg1 if hasattr(arg1, 'response') else arg2
        if interaction.user != self.user: return await interaction.response.send_message("Senin oyunun değil!", ephemeral=True)
        if not self.game_over and not self.check_collision(self.current_shape, self.piece_x - 1, self.piece_y): self.piece_x -= 1
        await interaction.response.edit_message(embed=discord.Embed(title="🎮 Quantum Tetris", description=self.render_board(), color=0x9b59b6), view=self)

    @discord.ui.button(emoji="🔃", style=discord.ButtonStyle.success)
    async def btn_rotate(self, arg1, arg2):
        interaction = arg1 if hasattr(arg1, 'response') else arg2
        if interaction.user != self.user: return await interaction.response.send_message("Senin oyunun değil!", ephemeral=True)
        if not self.game_over:
            rotated = [list(row) for row in zip(*self.current_shape[::-1])]
            if not self.check_collision(rotated, self.piece_x, self.piece_y): self.current_shape = rotated
        await interaction.response.edit_message(embed=discord.Embed(title="🎮 Quantum Tetris", description=self.render_board(), color=0x9b59b6), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary)
    async def btn_right(self, arg1, arg2):
        interaction = arg1 if hasattr(arg1, 'response') else arg2
        if interaction.user != self.user: return await interaction.response.send_message("Senin oyunun değil!", ephemeral=True)
        if not self.game_over and not self.check_collision(self.current_shape, self.piece_x + 1, self.piece_y): self.piece_x += 1
        await interaction.response.edit_message(embed=discord.Embed(title="🎮 Quantum Tetris", description=self.render_board(), color=0x9b59b6), view=self)

    @discord.ui.button(emoji="⬇️", style=discord.ButtonStyle.secondary)
    async def btn_down(self, arg1, arg2):
        interaction = arg1 if hasattr(arg1, 'response') else arg2
        if interaction.user != self.user: return await interaction.response.send_message("Senin oyunun değil!", ephemeral=True)
        if not self.game_over:
            while not self.check_collision(self.current_shape, self.piece_x, self.piece_y + 1): self.piece_y += 1
            self.lock_piece()
        await interaction.response.edit_message(embed=discord.Embed(title="🎮 Quantum Tetris", description=self.render_board(), color=0x9b59b6), view=self)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def btn_stop(self, arg1, arg2):
        interaction = arg1 if hasattr(arg1, 'response') else arg2
        if interaction.user != self.user: return await interaction.response.send_message("Senin oyunun değil!", ephemeral=True)
        self.game_over = True
        await interaction.response.edit_message(embed=discord.Embed(title="🎮 Quantum Tetris", description=self.render_board(), color=0x9b59b6), view=self)

# ----------------- HESAP MAKİNESİ SİSTEMİ -----------------
class CalcButton(discord.ui.Button):
    def __init__(self, label, row, style):
        super().__init__(style=style, label=label, row=row)
    async def callback(self, interaction: discord.Interaction):
        view: CalculatorView = self.view
        await view.button_pressed(interaction, self.label)

class CalculatorView(discord.ui.View):
    def __init__(self, user):
        super().__init__(timeout=180)
        self.user = user
        self.expression = ""
        buttons = [
            ('7', 0), ('8', 0), ('9', 0), ('/', 0),
            ('4', 1), ('5', 1), ('6', 1), ('*', 1),
            ('1', 2), ('2', 2), ('3', 2), ('-', 2),
            ('C', 3), ('0', 3), ('=', 3), ('+', 3)
        ]
        for label, row in buttons:
            style = discord.ButtonStyle.danger if label == "C" else discord.ButtonStyle.primary if label == "=" else discord.ButtonStyle.success if label in "/*-+" else discord.ButtonStyle.secondary
            self.add_item(CalcButton(label, row, style))

    async def update_display(self, interaction: discord.Interaction):
        display = self.expression if self.expression else "0"
        embed = discord.Embed(title="🧮 Hesap Makinesi", description=f"```\n{display}\n```", color=0x2b2d31)
        await interaction.response.edit_message(content=None, embed=embed, view=self)

    async def button_pressed(self, interaction: discord.Interaction, value: str):
        if interaction.user != self.user: return await interaction.response.send_message("Bu hesap makinesi size ait değil.", ephemeral=True)
        if value == "C": self.expression = ""
        elif value == "=":
            try:
                if all(c in "0123456789+-*/. " for c in self.expression): self.expression = str(eval(self.expression))
                else: self.expression = "Hata"
            except: self.expression = "Hata"
        else:
            if self.expression == "Hata": self.expression = ""
            self.expression += value
        await self.update_display(interaction)

# ----------------- XOX OYUN SİSTEMİ -----------------
class XOXButton(discord.ui.Button):
    def __init__(self, x, y):
        super().__init__(style=discord.ButtonStyle.primary, label=" ➖ ", row=y)
        self.x, self.y = x, y

    async def callback(self, interaction: discord.Interaction):
        view: XOXGame = self.view
        if interaction.user != view.current_player: return await interaction.response.send_message("Sıra sende değil!", ephemeral=True)
        idx = self.y * 3 + self.x
        if view.board[idx] != 0: return await interaction.response.send_message("Burası dolu!", ephemeral=True)

        view.board[idx] = 1 if view.current_player == view.player_x else -1
        self.style = discord.ButtonStyle.success if view.board[idx] == 1 else discord.ButtonStyle.danger
        self.label = "X" if view.board[idx] == 1 else "O"
        self.disabled = True

        winner = view.check_winner()
        if winner is not None: return await view.end_game(interaction, winner)

        view.current_player = view.player_o if view.current_player == view.player_x else view.player_x
        if view.is_ai and view.current_player == view.bot_user:
            await interaction.response.edit_message(content=f"**{view.current_player.display_name}** Kullanıcısının Sırası", view=view)
            await view.ai_turn(interaction.message)
        else:
            await interaction.response.edit_message(content=f"**{view.current_player.display_name}** Kullanıcısının Sırası", view=view)

class XOXGame(discord.ui.View):
    def __init__(self, player_x, player_o, bot_user):
        super().__init__(timeout=300)
        self.player_x, self.player_o, self.bot_user = player_x, player_o, bot_user
        self.is_ai = (player_o == bot_user)
        self.current_player = player_x
        self.board = [0] * 9
        self.message = None
        for y in range(3):
            for x in range(3): self.add_item(XOXButton(x, y))

    def check_winner(self):
        win_states = [[0, 1, 2], [3, 4, 5], [6, 7, 8], [0, 3, 6], [1, 4, 7], [2, 5, 8], [0, 4, 8], [2, 4, 6]]
        for w in win_states:
            if self.board[w[0]] != 0 and self.board[w[0]] == self.board[w[1]] == self.board[w[2]]: return self.board[w[0]]
        if 0 not in self.board: return 0 
        return None

    def get_ai_move(self):
        win_states = [[0, 1, 2], [3, 4, 5], [6, 7, 8], [0, 3, 6], [1, 4, 7], [2, 5, 8], [0, 4, 8], [2, 4, 6]]
        for w in win_states:
            vals = [self.board[i] for i in w]
            if vals.count(-1) == 2 and vals.count(0) == 1: return w[vals.index(0)]
        for w in win_states:
            vals = [self.board[i] for i in w]
            if vals.count(1) == 2 and vals.count(0) == 1: return w[vals.index(0)]
        empty = [i for i, v in enumerate(self.board) if v == 0]
        return random.choice(empty) if empty else None

    async def ai_turn(self, message: discord.Message):
        await asyncio.sleep(0.5)
        idx = self.get_ai_move()
        if idx is None: return
        self.board[idx] = -1
        btn = self.children[idx]
        btn.style, btn.label, btn.disabled = discord.ButtonStyle.danger, "O", True
        winner = self.check_winner()
        if winner is not None: return await self.end_game_from_ai(message, winner)
        self.current_player = self.player_x
        await message.edit(content=f"**{self.current_player.display_name}** Kullanıcısının Sırası", view=self)

    async def end_game(self, interaction: discord.Interaction, winner):
        for child in self.children: child.disabled = True
        if winner == 1: content = f"**{self.player_x.display_name}** Kazandı! 🎉\n**{self.player_o.display_name}** Kaybetti :("
        elif winner == -1: content = f"**{self.player_o.display_name}** Kazandı! 🎉\n**{self.player_x.display_name}** Kaybetti :("
        else: content = "Oyun Berabere Bitti."
        await interaction.response.edit_message(content=content, view=self)

    async def end_game_from_ai(self, message: discord.Message, winner):
        for child in self.children: child.disabled = True
        if winner == 1: content = f"**{self.player_x.display_name}** Kazandı! 🎉\n**{self.player_o.display_name}** Kaybetti :("
        elif winner == -1: content = f"**{self.player_o.display_name}** Kazandı! 🎉\n**{self.player_x.display_name}** Kaybetti :("
        else: content = "Oyun Berabere Bitti."
        await message.edit(content=content, view=self)

class XOXChallengeView(discord.ui.View):
    def __init__(self, challenger: discord.Member, challenged: discord.Member):
        super().__init__(timeout=60)
        self.challenger, self.challenged = challenger, challenged

    @discord.ui.button(label="Kabul Et", style=discord.ButtonStyle.success)
    async def btn_accept(self, arg1, arg2):
        interaction = arg1 if hasattr(arg1, 'response') else arg2
        if interaction.user != self.challenged: return await interaction.response.send_message("Bu istek sana değil!", ephemeral=True)
        game_view = XOXGame(self.challenger, self.challenged, self.challenger.guild.me)
        await interaction.response.edit_message(content=f"Oyun Başladı!\n\n**{game_view.current_player.display_name}** Kullanıcısının Sırası", view=game_view)
        game_view.message = interaction.message

    @discord.ui.button(label="Reddet", style=discord.ButtonStyle.danger)
    async def btn_decline(self, arg1, arg2):
        interaction = arg1 if hasattr(arg1, 'response') else arg2
        if interaction.user != self.challenged: return await interaction.response.send_message("Bu istek sana değil!", ephemeral=True)
        xox_cooldowns[(self.challenger.id, self.challenged.id)] = time.time() + 60
        await interaction.response.edit_message(content="Düello isteği reddedildi ve iptal edildi.", view=None)

# ----------------- EVENT VE BAŞLANGIÇ YÖNETİMİ -----------------
@bot.event
async def on_ready():
    await bot.sync_commands()
    print(f"QuantumBot aktif: {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user: return
    if bot.user in message.mentions:
        temiz = message.content
        for mention in message.mentions: temiz = temiz.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
        temiz = temiz.strip()
        if not temiz: await message.channel.send(f"Merhaba {message.author.mention}, ben Quantum. Nasıl yardımcı olabilirim?")

# ----------------- KOMUTLAR -----------------
@bot.slash_command(name="muzikoynat", description="Sadece sesli kanalda çalışır. Girdiğiniz YouTube bağlantısını/ismini MP3 olarak indirip çalar.")
async def muzikoynat(ctx: discord.ApplicationContext, sarki: str):
    await ctx.defer()
    voice_client: discord.VoiceClient = ctx.voice_client
    if not voice_client:
        if ctx.author.voice:
            try:
                voice_client = await ctx.author.voice.channel.connect()
            except Exception as e:
                return await ctx.followup.send(f"Kanala bağlanılamadı: {e}", ephemeral=True)
        else:
            return await ctx.followup.send("Müzik açabilmem için önce bir sesli kanala katılmalısın!", ephemeral=True)

    title = await play_music_in_voice(voice_client, sarki)
    
    if title:
        await ctx.followup.send(f"▶️ MP3 olarak indirildi ve oynatılıyor: **{title}**")
    else:
        await ctx.followup.send("❌ Şarkı bulunamadı veya indirilirken bir hata oluştu.")

@bot.slash_command(name="muzikdurdur", description="Çalan müziği ve sesi durdurur.")
async def muzikdurdur(ctx: discord.ApplicationContext):
    voice_client: discord.VoiceClient = ctx.voice_client
    if not voice_client or not voice_client.is_playing():
        return await ctx.respond("Şu an çalan bir şey yok.", ephemeral=True)
        
    voice_client.stop()
    await ctx.respond("🛑 Müzik/Ses durduruldu.")

@bot.slash_command(name="botbilgi", description="Gelişmiş bot, sistem ve ping bilgilerini gösterir.")
async def botbilgi_komutu(ctx: discord.ApplicationContext):
    embed = generate_botbilgi_embed(bot)
    view = BotBilgiView(bot)
    await ctx.respond(embed=embed, view=view)

@bot.slash_command(name="tetris", description="Butonlu ve hareketli Tetris oyunu oyna.")
async def tetris_komutu(ctx: discord.ApplicationContext):
    game = TetrisGame(ctx.author)
    embed = discord.Embed(title="🎮 Quantum Tetris", description=game.render_board(), color=0x9b59b6)
    message = await ctx.respond(embed=embed, view=game)
    game.message = await message.original_response()

@bot.slash_command(name="soru", description="Sadece yazılı yanıt verir (Groq).")
async def soru_komutu(ctx: discord.ApplicationContext, soru: str):
    await ctx.defer() 
    try:
        weather_task = asyncio.create_task(get_weather_info(soru))
        web_task = asyncio.create_task(get_web_context(soru))
        weather_context, web_context = await asyncio.gather(weather_task, web_task)
        
        history = USER_CHAT_HISTORY[ctx.author.id]
        history.append({"role": "user", "content": soru + weather_context + web_context})
        if len(history) > 8: history = history[-8:]
        
        messages = [{"role": "system", "content": get_system_prompt()}] + history
        chat_completion = groq_client.chat.completions.create(messages=messages, model="llama-3.3-70b-versatile", temperature=0.2)
        
        cevap = chat_completion.choices[0].message.content.strip()
        temiz_cevap = re.sub(r'\[PLAY:\s*(.+?)\]', '', cevap, flags=re.IGNORECASE).strip()
        temiz_cevap = re.sub(r'\[STOP\]', '', temiz_cevap, flags=re.IGNORECASE).strip()
        
        history.append({"role": "assistant", "content": temiz_cevap})
        await ctx.followup.send(f"**Soru:** {soru}\n\n**Quantum:**\n{temiz_cevap}")
    except Exception as e: await ctx.followup.send(f"Bir hata oluştu: {str(e)}")

@bot.slash_command(name="resimolustur", description="Yazdığın metni resme dönüştürür.")
async def resimolustur(ctx: discord.ApplicationContext, metin: str):
    await ctx.defer()
    try:
        guvenlik = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Sen bir güvenlik filtresisin. Kullanıcının girdiği metin +18, şiddet, kan içeriyorsa SADECE 'REDDEDILDI' yaz. Temizse metni İNGİLİZCE resim promptuna çevir."},
                {"role": "user", "content": metin}
            ], model="llama-3.3-70b-versatile"
        )
        ingilizce_prompt = guvenlik.choices[0].message.content.strip()
        if "REDDEDILDI" in ingilizce_prompt.upper():
            return await ctx.followup.send("Girdiğiniz içerik politikalarımıza aykırı olduğu için resim oluşturulamadı.")

        image_url = f"https://image.pollinations.ai/prompt/{ingilizce_prompt.replace(' ', '%20')}?width=1024&height=1024&nologo=true"
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as response:
                if response.status == 200:
                    image_data = await response.read()
                    file_name = "olusturulan_resim.png"
                    with open(file_name, "wb") as f: f.write(image_data)
                    await ctx.followup.send(content="Resmini oluşturdum.", file=discord.File(file_name))
                else: await ctx.followup.send("Sunucu kaynaklı bir sorun oluştu.")
    except Exception as e: await ctx.followup.send(f"Bir hata oluştu: {str(e)}")

@bot.slash_command(name="xox", description="Arkadaşınla veya Yapay Zeka ile XOX oyna.")
async def xox_komutu(ctx: discord.ApplicationContext, rakip: discord.Member = None):
    if rakip is None or rakip == bot.user:
        game_view = XOXGame(ctx.author, bot.user, bot.user)
        message = await ctx.respond(f"Oyun Başladı!\n\n**{game_view.current_player.display_name}** Kullanıcısının Sırası", view=game_view)
        game_view.message = await message.original_response()
    else:
        if rakip == ctx.author: return await ctx.respond("Kendinle oynayamazsın.", ephemeral=True)
        if (ctx.author.id, rakip.id) in xox_cooldowns:
            if time.time() < xox_cooldowns[(ctx.author.id, rakip.id)]:
                return await ctx.respond("Tekrar istek atmak için beklemelisin.", ephemeral=True)
        view = XOXChallengeView(ctx.author, rakip)
        await ctx.respond(f"{rakip.mention}, **{ctx.author.display_name}** Kullanıcısı Sizle XOX Düellosu İstiyor!", view=view)

@bot.slash_command(name="hesapmakinesi", description="Etkileşimli butonlarla hesap makinesini açar.")
async def hesapmakinesi(ctx: discord.ApplicationContext):
    view = CalculatorView(ctx.author)
    embed = discord.Embed(title="🧮 Hesap Makinesi", description="```\n0\n```", color=0x2b2d31)
    await ctx.respond(embed=embed, view=view)

@bot.slash_command(name="web", description="Web'de gizli arama yap (Sadece sen görebilirsin).")
async def web_komutu(ctx: discord.ApplicationContext, sorgu: str):
    await ctx.defer(ephemeral=True)
    try:
        chat = groq_client.chat.completions.create(
            messages=[{"role": "system", "content": "Kısa ve öz bilgi ver."}, {"role": "user", "content": sorgu}],
            model="llama-3.3-70b-versatile", temperature=0.2
        )
        ai_ozet = chat.choices[0].message.content
        google_url = f"https://www.google.com/search?q={urllib.parse.quote(sorgu)}"
        embed = discord.Embed(title=f"🔍 Sonuç: {sorgu.title()}", description=f"{ai_ozet}\n\n**[🌐 Detaylı İncele]({google_url})**", color=0x4285F4)
        await ctx.followup.send(embed=embed)
    except Exception as e: await ctx.followup.send(f"Hata oluştu: {str(e)}")

@bot.slash_command(name="seslisoru", description="Sesli kanaldaki kişileri bilerek, MÜZİK çalarak ve HAFIZA ile yanıt verir.")
async def seslisoru(ctx: discord.ApplicationContext, soru: str):
    await ctx.defer() 
    voice_client: discord.VoiceClient = ctx.voice_client
    if not voice_client:
        if ctx.author.voice:
            try:
                voice_client = await ctx.author.voice.channel.connect()
            except Exception:
                return await ctx.followup.send("Sesli kanala katılamadım.", ephemeral=True)
        else:
            return await ctx.followup.send("Hey! Sesli Kanalda Değilim. Sesli Kanala Katılın.", ephemeral=True)

    try:
        odadaki_kisiler_bilgisi = "\n(GİZLİ SİSTEM NOTU: Şu an bulunduğun sesli kanaldaki üyeler:\n"
        for member in voice_client.channel.members:
            if member.bot: continue 
            mikrofon = "Kapalı" if (member.voice.self_mute or member.voice.mute) else "Açık"
            kulaklik = "Kapalı" if (member.voice.self_deaf or member.voice.deaf) else "Açık"
            odadaki_kisiler_bilgisi += f"- {member.display_name} (Mik: {mikrofon}, Kulak: {kulaklik})\n"
        odadaki_kisiler_bilgisi += "KURAL: Bu listeyi SADECE kullanıcı özellikle 'odada kimler var' diye sorarsa söyle.)"
        
        weather_task = asyncio.create_task(get_weather_info(soru))
        web_task = asyncio.create_task(get_web_context(soru))
        weather_context, web_context = await asyncio.gather(weather_task, web_task)
        
        final_prompt = soru + weather_context + web_context + odadaki_kisiler_bilgisi

        history = USER_CHAT_HISTORY[ctx.author.id]
        history.append({"role": "user", "content": final_prompt})
        if len(history) > 8: history = history[-8:]
        
        messages = [{"role": "system", "content": get_system_prompt()}] + history

        chat_completion = groq_client.chat.completions.create(messages=messages, model="llama-3.3-70b-versatile", temperature=0.2)
        cevap = chat_completion.choices[0].message.content.strip()
        
        play_query = None
        stop_match = re.search(r'\[STOP\]', cevap, re.IGNORECASE)
        play_match = re.search(r'\[PLAY:\s*(.+?)\]', cevap, re.IGNORECASE)
        
        temiz_cevap = re.sub(r'\[PLAY:\s*(.+?)\]', '', cevap, flags=re.IGNORECASE).strip()
        temiz_cevap = re.sub(r'\[STOP\]', '', temiz_cevap, flags=re.IGNORECASE).strip()

        history.append({"role": "assistant", "content": temiz_cevap})
        
        audio_file = "seslisoru_yanit.mp3"
        await generate_tts(temiz_cevap, audio_file)
        
        await ctx.followup.send(content=f"**Soru:** {soru}\n\n**Quantum:**\n{temiz_cevap[:1500]}")
        
        if voice_client.is_playing():
            voice_client.stop()

        if stop_match:
            return

        if play_match:
            play_query = play_match.group(1).strip()

        voice_client.play(discord.FFmpegPCMAudio(audio_file, executable=FFMPEG_EXE))
        
        if play_query and not stop_match:
            async def wait_and_play():
                while voice_client.is_playing():
                    await asyncio.sleep(0.5)
                await play_music_in_voice(voice_client, play_query)
            bot.loop.create_task(wait_and_play())

    except Exception as e: await ctx.followup.send(f"Bir hata oluştu: {str(e)}")

@bot.slash_command(name="quantum", description="Yazılı ve sesli MP3 yanıtı al (Groq).")
async def quantum(ctx: discord.ApplicationContext, soru: str):
    await ctx.defer() 
    try:
        weather_task = asyncio.create_task(get_weather_info(soru))
        web_task = asyncio.create_task(get_web_context(soru))
        weather_context, web_context = await asyncio.gather(weather_task, web_task)
        
        history = USER_CHAT_HISTORY[ctx.author.id]
        history.append({"role": "user", "content": soru + weather_context + web_context})
        if len(history) > 8: history = history[-8:]
        
        messages = [{"role": "system", "content": get_system_prompt()}] + history
        chat_completion = groq_client.chat.completions.create(messages=messages, model="llama-3.3-70b-versatile", temperature=0.2)
        
        cevap = chat_completion.choices[0].message.content.strip()
        temiz_cevap = re.sub(r'\[PLAY:\s*(.+?)\]', '', cevap, flags=re.IGNORECASE).strip()
        temiz_cevap = re.sub(r'\[STOP\]', '', temiz_cevap, flags=re.IGNORECASE).strip()
        
        history.append({"role": "assistant", "content": temiz_cevap})
        audio_file = "quantum_yanit.mp3"
        await generate_tts(temiz_cevap, audio_file)
        
        # Resource Warning engellemesi (Dosyanın güvenli kapanması)
        await ctx.followup.send(content=f"**Soru:** {soru}\n\n**Quantum:**\n{temiz_cevap[:1500]}", file=discord.File(audio_file))
    except Exception as e: await ctx.followup.send(f"Bir hata oluştu: {str(e)}")

@bot.slash_command(name="konustur", description="Özel: İstediğin metni sesli kanalda doğrudan okutur.")
async def konustur_komutu(ctx: discord.ApplicationContext, metin: str):
    await ctx.defer(ephemeral=True)
    if ctx.author.name != "turkyoshi8092":
        return await ctx.followup.send("Bu komutu kullanma yetkiniz bulunmamaktadır.", ephemeral=True)

    voice_client: discord.VoiceClient = ctx.voice_client
    if not voice_client:
        if ctx.author.voice:
            try:
                voice_client = await ctx.author.voice.channel.connect()
            except Exception:
                return await ctx.followup.send("Sesli kanala bağlanamadım.", ephemeral=True)
        else:
            return await ctx.followup.send("Önce sesli bir kanala girin.", ephemeral=True)

    try:
        audio_file = "konustur_yanit.mp3"
        await generate_tts(metin, audio_file)
        if voice_client.is_playing():
            voice_client.stop()
        voice_client.play(discord.FFmpegPCMAudio(audio_file, executable=FFMPEG_EXE))
        await ctx.followup.send(f"Metin başarıyla okundu: **{metin}**", ephemeral=True)
    except Exception as e: await ctx.followup.send(f"Bir hata oluştu: {str(e)}", ephemeral=True)

@bot.slash_command(name="quantumkatil", description="Sesli kanala katılır ve otomatik dinlemeyi başlatır.")
async def quantumkatil(ctx: discord.ApplicationContext):
    await ctx.defer() 
    if not ctx.author.voice: return await ctx.followup.send("Önce bir sesli kanala katılmalısın.", ephemeral=True)

    channel = ctx.author.voice.channel
    voice_client: discord.VoiceClient = ctx.voice_client
    if not voice_client:
        voice_client = await channel.connect()

    await ctx.followup.send(f"{channel.name} kanalına katıldım. Dinleme modundayım, ismimi söyleyerek konuşabilirsin.")
    
    sink = AutomaticAudioSink(bot, ctx.channel)
    async def dummy_callback(sink: discord.sinks.Sink, *args):
        pass
    
    voice_client.start_recording(sink, dummy_callback)
    sink.monitor_task = asyncio.create_task(sink._monitor_loop(voice_client))
    
    audio_file = "katildi.mp3"
    await generate_tts("Sohbete katıldım.", audio_file)
    if voice_client.is_playing(): voice_client.stop()
    voice_client.play(discord.FFmpegPCMAudio(audio_file, executable=FFMPEG_EXE))

@bot.slash_command(name="quantumayril", description="Sesli kanaldan ayrılır.")
async def quantumayril(ctx: discord.ApplicationContext):
    await ctx.defer()
    voice_client: discord.VoiceClient = ctx.voice_client
    if voice_client:
        voice_client.stop_recording()
        await voice_client.disconnect()
        await ctx.followup.send("Sesli kanaldan ayrıldım.")
    else: await ctx.followup.send("Zaten bir sesli kanalda değilim.")

bot.run(DISCORD_TOKEN)