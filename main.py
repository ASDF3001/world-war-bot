import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import pandas as pd
import numpy as np
from PIL import Image
import io
import random
import ast
import os
import datetime
import traceback
import asyncio
import logging
import colorsys
from dotenv import load_dotenv
import aiohttp

# ==============================================================================
# 初期設定と環境変数
# ==============================================================================
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
DB_FILE = os.getenv("DB_FILE", "war_game_worlds.db")
BACKUP_DIR = os.getenv("BACKUP_DIR", "db_backups")
PROMO_LINK = "https://discord.gg/dsGhNNJfzc"
TIPS_FILE = "tips.txt"

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

SAY_LOG_DIR = os.path.expanduser("./world-war-bot/say_log")
os.makedirs(SAY_LOG_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# ==============================================================================
# 定数・辞書データ
# ==============================================================================
def load_country_map():
    mapping = {"中国": "CHN", "CHINA": "CHN", "PRC": "CHN"}
    if os.path.exists("country_map.txt"):
        with open("country_map.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                parts = line.split(",")
                if len(parts) == 2: mapping[parts[0].strip().upper()] = parts[1].strip().upper()
    return mapping

def get_paypay_link():
    if os.path.exists("paypay.txt"):
        with open("paypay.txt", "r", encoding="utf-8") as f:
            link = f.read().strip()
            if link: return link
    return "現在、支援リンクは設定されていません。"

COUNTRY_MAP = load_country_map()
TERRITORY_YIELD = {"USA": 300, "RUS": 300, "CHN": 300, "JPN": 200, "DEU": 200, "FRA": 200, "GBR": 200, "IND": 200, "BRA": 150, "AUS": 150, "CAN": 150}
BASE_INCOME = 1000
DEFAULT_DEFENSE = 200 

RAW_ADJACENCY = {
    "JPN": ["KOR", "PRK", "RUS", "TWN", "CHN", "PHL"],
    "USA": ["CAN", "MEX", "CUB", "RUS", "BHS"],
    "RUS": ["CHN", "PRK", "MNG", "KAZ", "JPN", "FIN", "BLR", "UKR", "EST", "LVA", "LTU", "POL", "NOR", "GEO", "AZE", "USA"],
    "CHN": ["RUS", "MNG", "PRK", "KAZ", "KGZ", "TJK", "AFG", "PAK", "IND", "NPL", "BTN", "MMR", "LAO", "VNM", "TWN", "JPN", "MAC", "HKG"],
    "DEU": ["DNK", "POL", "CZE", "AUT", "CHE", "FRA", "LUX", "BEL", "NLD"],
    "FRA": ["BEL", "LUX", "DEU", "CHE", "ITA", "MCO", "ESP", "AND", "GBR"],
    "GBR": ["IRL", "FRA"],
    "IND": ["PAK", "CHN", "NPL", "BTN", "BGD", "MMR", "LKA", "MDV"],
    "BRA": ["URY", "ARG", "PRY", "BOL", "PER", "COL", "VEN", "GUY", "SUR", "GUF"],
    "AUS": ["IDN", "PNG", "NZL", "TLS"],
    "CAN": ["USA"],
    "KOR": ["PRK", "JPN", "CHN"],
    "PRK": ["KOR", "CHN", "RUS", "JPN"],
    "ITA": ["FRA", "CHE", "AUT", "SVN", "SMR", "VAT"],
    "ESP": ["FRA", "PRT", "AND", "MAR"],
    "TUR": ["GRC", "BGR", "GEO", "ARM", "AZE", "IRN", "IRQ", "SYR"],
    "SAU": ["JOR", "IRQ", "KWT", "BHR", "QAT", "ARE", "OMN", "YEM"],
    "ZAF": ["NAM", "BWA", "ZWE", "MOZ", "SWZ", "LSO"]
}
ADJACENCY_GRAPH = {}
for k, v_list in RAW_ADJACENCY.items():
    if k not in ADJACENCY_GRAPH: ADJACENCY_GRAPH[k] = set()
    for v in v_list:
        ADJACENCY_GRAPH[k].add(v)
        if v not in ADJACENCY_GRAPH: ADJACENCY_GRAPH[v] = set()
        ADJACENCY_GRAPH[v].add(k)

try:
    df_countries = pd.read_csv("countries_data.csv")
    country_to_mask = {row['iso_alpha'].upper().strip(): ast.literal_eval(row['rgb_str']) for _, row in df_countries.iterrows()}
    VALID_CODES = set(country_to_mask.keys())
except Exception as e:
    logger.error(f"countries_data.csv 読み込み失敗: {e}")
    VALID_CODES = set()

# ==============================================================================
# ユーティリティ・DB関数群
# ==============================================================================
def get_promo_and_tip():
    res = ""
    if random.random() < 0.1: res += f"\n\n-# 公式サポートサーバー: {PROMO_LINK}"
    if random.random() < 0.3:
        try:
            if os.path.exists(TIPS_FILE):
                with open(TIPS_FILE, "r", encoding="utf-8") as f:
                    tips = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                if tips:
                    if not res: res += "\n\n"
                    else: res += "\n"
                    res += f"-# Tip: {random.choice(tips)}"
        except Exception: pass
    return res

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=60.0)
    conn.execute("PRAGMA busy_timeout = 60000;")
    return conn

def init_db():
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute('''CREATE TABLE IF NOT EXISTS server_channels (guild_id TEXT PRIMARY KEY, world1_ch TEXT, world2_ch TEXT, world3_ch TEXT, notify_ch TEXT, notify_enabled INTEGER DEFAULT 1)''')
            c.execute('''CREATE TABLE IF NOT EXISTS user_settings (guild_id TEXT, user_id TEXT, active_world INTEGER DEFAULT 1, PRIMARY KEY(guild_id, user_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS players (guild_id TEXT, world_id INTEGER, user_id TEXT, user_name TEXT, short_name TEXT DEFAULT '無名', r INTEGER, g INTEGER, b INTEGER, gold INTEGER DEFAULT 1000, main_country TEXT DEFAULT '未設定', PRIMARY KEY(guild_id, world_id, user_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS territories (guild_id TEXT, world_id INTEGER, iso_alpha TEXT, owner_id TEXT, defense INTEGER DEFAULT 100, PRIMARY KEY(guild_id, world_id, iso_alpha))''')
            c.execute('''CREATE TABLE IF NOT EXISTS alliances (guild_id TEXT, world_id INTEGER, user_a TEXT, user_b TEXT, PRIMARY KEY(guild_id, world_id, user_a, user_b))''')
            c.execute('''CREATE TABLE IF NOT EXISTS wars (guild_id TEXT, world_id INTEGER, attacker_id TEXT, defender_id TEXT, PRIMARY KEY(guild_id, world_id, attacker_id, defender_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS un_members (guild_id TEXT, world_id INTEGER, user_id TEXT, PRIMARY KEY(guild_id, world_id, user_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS un_invites (guild_id TEXT, world_id INTEGER, user_id TEXT, PRIMARY KEY(guild_id, world_id, user_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS camps (guild_id TEXT, world_id INTEGER, camp_name TEXT, founder_id TEXT, PRIMARY KEY(guild_id, world_id, camp_name))''')
            c.execute('''CREATE TABLE IF NOT EXISTS camp_members (guild_id TEXT, world_id INTEGER, user_id TEXT, camp_name TEXT, PRIMARY KEY(guild_id, world_id, user_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS camp_invites (guild_id TEXT, world_id INTEGER, user_id TEXT, camp_name TEXT, PRIMARY KEY(guild_id, world_id, user_id, camp_name))''')
            c.execute('''CREATE TABLE IF NOT EXISTS server_ops (guild_id TEXT, user_id TEXT, PRIMARY KEY(guild_id, user_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS world_logs (guild_id TEXT, world_id INTEGER, timestamp TEXT, event_text TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS unlocked_trophies (guild_id TEXT, world_id INTEGER, user_id TEXT, trophy_id TEXT, PRIMARY KEY(guild_id, world_id, user_id, trophy_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS peace_treaties (guild_id TEXT, world_id INTEGER, user_a TEXT, user_b TEXT, expires_at TEXT, PRIMARY KEY(guild_id, world_id, user_a, user_b))''')

            def add_column(table, column, data_type):
                c.execute(f"PRAGMA table_info({table})")
                if column not in [row[1] for row in c.fetchall()]: c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {data_type}")
                    
            add_column("players", "oil", "INTEGER DEFAULT 8000")
            add_column("players", "debt", "INTEGER DEFAULT 0")
            add_column("players", "invest", "INTEGER DEFAULT 0")
            add_column("players", "tech_level", "INTEGER DEFAULT 1")
            add_column("players", "title", "TEXT DEFAULT '未設定'")
            add_column("players", "wins", "INTEGER DEFAULT 0")
            add_column("players", "losses", "INTEGER DEFAULT 0")
            add_column("players", "trophy_count", "INTEGER DEFAULT 0")
            add_column("server_channels", "oil_enabled_w1", "INTEGER DEFAULT 1")
            add_column("server_channels", "oil_enabled_w2", "INTEGER DEFAULT 1")
            add_column("server_channels", "oil_enabled_w3", "INTEGER DEFAULT 1")
            add_column("server_channels", "adjacency_penalty", "INTEGER DEFAULT 0")
            add_column("server_channels", "reset_interval", "INTEGER DEFAULT 7")
            add_column("server_channels", "last_reset_date", f"TEXT DEFAULT '{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}'")
            add_column("user_settings", "confirm_attack", "INTEGER DEFAULT 1")
            conn.commit()
    except Exception as e: logger.error(f"DB初期化エラー: {e}")

async def safe_defer(interaction: discord.Interaction, ephemeral: bool = False):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)
    except (discord.NotFound, discord.HTTPException):
        pass

async def send_dm_fallback(member: discord.Member, channel: discord.TextChannel, content: str, view: discord.ui.View = None, embed: discord.Embed = None):
    try:
        await member.send(content, view=view, embed=embed)
        return True
    except Exception:
        try:
            msg = f"{member.mention} [通知] DM送信がオフのため、こちらに通知します。\n{content}"
            await channel.send(msg, view=view, embed=embed)
        except Exception: pass
        return False

def is_slash_op_or_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator: return True
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM server_ops WHERE guild_id = ? AND user_id = ?", (str(interaction.guild_id), str(interaction.user.id)))
            if c.fetchone(): return True
        return False
    return app_commands.check(predicate)

async def ensure_world_context(interaction: discord.Interaction) -> int:
    guild_id, channel_id, user_id = str(interaction.guild_id), str(interaction.channel.id), str(interaction.user.id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT world1_ch, world2_ch, world3_ch FROM server_channels WHERE guild_id = ?", (guild_id,))
        row = c.fetchone()
        if row and any(row): 
            if channel_id == row[0]: return 1
            if channel_id == row[1]: return 2
            if channel_id == row[2]: return 3
            msg = "[エラー] このチャンネルでは実行できません。\n各Worldの専用チャンネルを使用してください。"
            if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
            else: await interaction.response.send_message(msg, ephemeral=True)
            return 0
        c.execute("SELECT active_world FROM user_settings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        urow = c.fetchone()
        return urow[0] if urow else 1

def is_oil_enabled(guild_id: str, world_id: int) -> bool:
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT oil_enabled_w1, oil_enabled_w2, oil_enabled_w3 FROM server_channels WHERE guild_id = ?", (guild_id,))
        row = c.fetchone()
        if not row: return True
        return bool(row[world_id - 1]) if 1 <= world_id <= 3 else True

def resolve_country_code(target: str) -> str: return COUNTRY_MAP.get(target.replace(" ", "").strip().upper(), target.replace(" ", "").strip().upper())

def is_allied(guild_id: str, world_id: int, user_a: str, user_b: str) -> bool:
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM alliances WHERE guild_id=? AND world_id=? AND ((user_a=? AND user_b=?) OR (user_a=? AND user_b=?))", (guild_id, world_id, user_a, user_b, user_b, user_a))
        return c.fetchone() is not None

def is_at_war(guild_id: str, world_id: int, attacker: str, defender: str) -> bool:
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM wars WHERE guild_id=? AND world_id=? AND attacker_id=? AND defender_id=?", (guild_id, world_id, attacker, defender))
        return c.fetchone() is not None

def is_peace_treaty_active(guild_id: str, world_id: int, user_a: str, user_b: str) -> bool:
    with get_db_connection() as conn:
        c = conn.cursor()
        now_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        c.execute("SELECT 1 FROM peace_treaties WHERE guild_id=? AND world_id=? AND ((user_a=? AND user_b=?) OR (user_a=? AND user_b=?)) AND expires_at > ?", (guild_id, world_id, user_a, user_b, user_b, user_a, now_str))
        return c.fetchone() is not None

def add_world_log(guild_id: str, world_id: int, text: str):
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO world_logs (guild_id, world_id, timestamp, event_text) VALUES (?, ?, ?, ?)", (guild_id, world_id, now_str, text))
            conn.commit()
    except Exception as e:
        logger.error(f"世界ログ記録エラー: {e}")

def add_trophy(guild_id: str, world_id: int, user_id: str, trophy_id: str):
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO unlocked_trophies (guild_id, world_id, user_id, trophy_id) VALUES (?, ?, ?, ?)", (guild_id, world_id, user_id, trophy_id))
            if c.rowcount > 0:
                c.execute("UPDATE players SET trophy_count = trophy_count + 1 WHERE guild_id=? AND world_id=? AND user_id=?", (guild_id, world_id, user_id))
            conn.commit()
    except Exception as e:
        logger.error(f"実績追加エラー: {e}")

def check_and_create_user(cursor, guild_id, world_id, user_id, user_name):
    cursor.execute("SELECT gold FROM players WHERE guild_id = ? AND world_id = ? AND user_id = ?", (guild_id, world_id, user_id))
    if not cursor.fetchone():
        h, s, v = random.random(), random.uniform(0.6, 1.0), random.uniform(0.8, 1.0)
        r, g, b = [int(x * 255) for x in colorsys.hsv_to_rgb(h, s, v)]
        cursor.execute("INSERT INTO players (guild_id, world_id, user_id, user_name, short_name, r, g, b, oil, debt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 8000, 0)", (guild_id, world_id, user_id, user_name, user_name[:3], r, g, b))

def _generate_current_map_sync(guild_id: str, world_id: int):
    try:
        base_np, mask_np = np.array(Image.open("base_map.png").convert("RGBA")), np.array(Image.open("mask_map.png").convert("RGB"))
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT t.iso_alpha, p.r, p.g, p.b, p.user_name FROM territories t JOIN players p ON t.owner_id = p.user_id AND t.world_id = p.world_id AND t.guild_id = p.guild_id WHERE t.guild_id = ? AND t.world_id = ?", (guild_id, world_id))
            occupied_lands = c.fetchall()
        for iso_alpha, r, g, b, _ in occupied_lands:
            if iso_alpha in country_to_mask:
                mask_color = country_to_mask[iso_alpha]
                match = ((mask_np[:, :, 0] == mask_color[0]) & (mask_np[:, :, 1] == mask_color[1]) & (mask_np[:, :, 2] == mask_color[2]))
                base_np[match] = [r, g, b, 160]
        img_bin = io.BytesIO()
        Image.fromarray(base_np).save(img_bin, format="PNG")
        img_bin.seek(0)
        return discord.File(fp=img_bin, filename="war_map.png"), occupied_lands
    except Exception as e:
        logger.error(f"マップ生成エラー: \n{traceback.format_exc()}")
        return None, []


async def generate_and_send_news(guild, channel):
    if not GEMINI_API_KEY: return
    guild_id = str(guild.id)
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT world_id, event_text FROM world_logs WHERE guild_id=?", (guild_id,))
            logs = c.fetchall()
            
            if not logs: return
            
            world_events = {}
            for w_id, text in logs:
                if w_id not in world_events: world_events[w_id] = []
                world_events[w_id].append(text)
            
            c.execute("DELETE FROM world_logs WHERE guild_id=?", (guild_id,))
            conn.commit()

        for w_id, events in world_events.items():
            if not events: continue
            events_str = "\n".join(events[:50])
            prompt = f"""
あなたは「World War Bot」の世界情勢を報道する、架空のデイリー新聞「World Times」の凄腕AI記者です。
以下の直近の出来事ログを元に、面白くて臨場感のある新聞記事テキスト（マークダウン形式、最大400文字程度）を作成してください。

【出来事ログ（世界#{w_id}）】
{events_str}

【出力要件】
- キャッチーな見出し（大見出し）を含めること
- 記者の視点から、世界の戦況や外交の動きをドラマチックに要約すること
"""
            url = f"{GEMINI_BASE_URL}/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            headers = {"Content-Type": "application/json"}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            news_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                        except (KeyError, IndexError):
                            news_text = "特派員からの通信が途絶えました。(生成エラー)"
                    else:
                        error_text = await resp.text()
                        logger.error(f"Gemini APIエラー: {resp.status} - {error_text}")
                        continue
            
            embed = discord.Embed(title=f"📰 World Times - 世界 #{w_id} 最新情勢", description=news_text, color=0x3498db)
            embed.set_footer(text="AI Reporter")
            await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"新聞生成エラー: {e}")

import math
from discord.ext import tasks

# ==============================================================================
# Botクラスの定義とバックグラウンドタスク
# ==============================================================================
class WarBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        intents.presences = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.command_count = 0
        self.status_index = 0

    async def setup_hook(self):
        init_db()
        await self.load_extension("cogs.commands")
        await self.load_extension("cogs.admin")  # ここでコマンド群を読み込み
        await self.tree.sync()
        logger.info("スラッシュコマンドを同期しました。")
        if not scheduled_tasks.is_running(): 
            scheduled_tasks.start()
        if not change_status_task.is_running():
            change_status_task.start()
        if not sync_supabase_task.is_running():
            sync_supabase_task.start()

bot = WarBot()

from supabase import create_client, Client
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


@tasks.loop(minutes=1)
async def sync_supabase_task():
    if not supabase_client or not bot.is_ready(): return
    try:
        # 1. DBからデータを一瞬でメモリに吸い出して即クローズ（ロック防止）
        worlds_payload = []
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT DISTINCT guild_id, world_id FROM territories")
            active_worlds = c.fetchall()
            
            for guild_id, w_id in active_worlds:
                # 領土ランキング
                c.execute("SELECT owner_id, COUNT(*) as c FROM territories WHERE guild_id=? AND world_id=? GROUP BY owner_id ORDER BY c DESC LIMIT 10", (guild_id, w_id))
                t_ranks = c.fetchall()
                territory_leaderboard = []
                for uid, count in t_ranks:
                    c.execute("SELECT user_name, title, main_country FROM players WHERE user_id=? AND guild_id=? AND world_id=?", (uid, guild_id, w_id))
                    p = c.fetchone()
                    if p:
                        territory_leaderboard.append({"name": "Secret Player", "title": p[1], "country": p[2], "territories": count})
                
                # 資金ランキング
                c.execute("SELECT user_name, gold FROM players WHERE guild_id=? AND world_id=? ORDER BY gold DESC LIMIT 10", (guild_id, w_id))
                gold_leaderboard = [{"name": "Secret Player", "gold": r[1]} for r in c.fetchall()]
                
                # 陣営(Camp)の勢力(領土数)
                c.execute("SELECT camp_name, founder_id FROM camps WHERE guild_id=? AND world_id=?", (guild_id, w_id))
                camps = c.fetchall()
                camp_shares = []
                for c_name, _ in camps:
                    c.execute("SELECT user_id FROM camp_members WHERE camp_name=? AND guild_id=? AND world_id=?", (c_name, guild_id, w_id))
                    members = [m[0] for m in c.fetchall()]
                    if members:
                        placeholders = ','.join('?' * len(members))
                        c.execute(f"SELECT COUNT(*) FROM territories WHERE guild_id=? AND world_id=? AND owner_id IN ({placeholders})", [guild_id, w_id] + members)
                        camp_territories = c.fetchone()[0]
                        camp_shares.append({"camp_name": c_name, "territories": camp_territories})
                camp_shares.sort(key=lambda x: x["territories"], reverse=True)

                data = {
                    "territory_ranking": territory_leaderboard,
                    "gold_ranking": gold_leaderboard,
                    "camp_shares": camp_shares
                }
                worlds_payload.append((guild_id, w_id, data))

        # 2. DBクローズ後に、画像生成とSupabaseアップロードを実行
        for guild_id, w_id, data in worlds_payload:
            try:
                # マップ画像生成（別スレッド）
                file_obj, _ = await asyncio.to_thread(_generate_current_map_sync, guild_id, w_id)
                if file_obj:
                    file_bytes = file_obj.fp.read()
                    def _upload_img():
                        supabase_client.storage.from_("maps").upload(f"map_{guild_id}_{w_id}.png", file_bytes, file_options={"upsert": "true", "contentType": "image/png"})
                    await asyncio.to_thread(_upload_img)
            except Exception as img_err:
                logger.error(f"マップ画像アップロードエラー ({guild_id}_{w_id}): {img_err}")

            # DBレコードの更新
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            def _upload_db():
                record_id = f"{guild_id}_{w_id}"
                supabase_client.table("world_status").upsert({
                    "id": record_id, 
                    "world_id": w_id, 
                    "updated_at": now_iso, 
                    "data": data,
                    "map_url": f"{SUPABASE_URL}/storage/v1/object/public/maps/map_{guild_id}_{w_id}.png"
                }).execute()
            await asyncio.to_thread(_upload_db)
            await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"Supabase同期エラー: {e}")

@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command):
    bot.command_count += 1

@tasks.loop(seconds=10)
async def change_status_task():
    try:
        if not bot.is_ready(): return
        statuses = []
        
        # 1. 〇〇人|〇〇鯖
        total_members = sum(g.member_count for g in bot.guilds if g.member_count)
        total_guilds = len(bot.guilds)
        statuses.append(discord.Activity(type=discord.ActivityType.watching, name=f"{total_members}人 | {total_guilds}鯖"))
        
        # 2. Ping 〇〇ms
        ping = bot.latency * 1000
        if math.isinf(ping) or math.isnan(ping) or ping < 0 or ping > 10000:
            ping_str = "Error ms"
        else:
            ping_str = f"{int(ping)}ms"
        statuses.append(discord.Activity(type=discord.ActivityType.watching, name=f"Ping {ping_str}"))
        
        # 3. Powered by rds9
        statuses.append(discord.Activity(type=discord.ActivityType.watching, name="Powered by rds9"))
        
        # 4. 〇〇回のコマンド
        statuses.append(discord.Activity(type=discord.ActivityType.watching, name=f"{bot.command_count}回のコマンド"))
        
        bot.status_index = (bot.status_index + 1) % len(statuses)
        await bot.change_presence(activity=statuses[bot.status_index])
    except Exception as e:
        logger.error(f"ステータス更新エラー: {e}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, (discord.NotFound, discord.errors.NotFound)):
        logger.warning(f"コマンド ({interaction.command.name if interaction.command else 'unknown'}) が3秒以内に応答できずタイムアウトしました(10062)。")
        return
    if isinstance(error, app_commands.CommandOnCooldown):
        minutes, seconds = divmod(int(error.retry_after), 60)
        msg = f"まだ準備中です。あと {minutes}分 {seconds}秒 お待ちください。"
        try:
            if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
            else: await interaction.response.send_message(msg, ephemeral=True)
        except: pass
    elif isinstance(error, app_commands.CheckFailure) or isinstance(error, app_commands.MissingPermissions):
        msg = "[エラー] あなたにはこのコマンド（またはOP権限）を実行する権限がありません。"
        try:
            if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
            else: await interaction.response.send_message(msg, ephemeral=True)
        except: pass
    else:
        logger.error(f"コマンドエラー ({interaction.command.name}): \n{traceback.format_exc()}")
        try:
            msg = "[エラー] 予期せぬエラーが発生しました。\n※権限エラー(403 Forbidden)の場合は、Botのチャンネル権限を見直してください。"
            if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
            else: await interaction.response.send_message(msg, ephemeral=True)
        except: pass

@tasks.loop(time=[datetime.time(hour=7, tzinfo=datetime.timezone.utc), datetime.time(hour=19, tzinfo=datetime.timezone.utc)])
async def scheduled_tasks():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    today_str = now_utc.strftime('%Y-%m-%d')
    reset_guilds = []
    if now_utc.hour == 19:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT guild_id, reset_interval, last_reset_date FROM server_channels")
            for g_id, interval, last_date_str in c.fetchall():
                if interval > 0:
                    try:
                        last_date = datetime.datetime.strptime(last_date_str, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc)
                        if (now_utc - last_date).days >= interval: reset_guilds.append(g_id)
                    except: reset_guilds.append(g_id)
            for g_id in reset_guilds:
                for table in ['players', 'territories', 'alliances', 'wars', 'un_members', 'un_invites', 'camps', 'camp_members', 'camp_invites', 'world_logs', 'unlocked_trophies', 'peace_treaties']: 
                    c.execute(f"DELETE FROM {table} WHERE guild_id=?", (g_id,))
                c.execute("UPDATE server_channels SET last_reset_date=? WHERE guild_id=?", (today_str, g_id))
            conn.commit()
        for g_id in reset_guilds:
            guild = bot.get_guild(int(g_id))
            if guild:
                with get_db_connection() as conn:
                    c = conn.cursor()
                    c.execute("SELECT notify_ch, notify_enabled FROM server_channels WHERE guild_id=?", (str(guild.id),))
                    row = c.fetchone()
                if row and row[1] == 1 and row[0]:
                    channel = guild.get_channel(int(row[0]))
                    if channel:
                        try: await channel.send("[定期リセット完了] データがワイプされ、新たな歴史が始まりました。")
                        except: pass
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT owner_id, iso_alpha, guild_id, world_id FROM territories")
            income_map, territory_counts = {}, {}
            for o_id, iso, g_id, w_id in c.fetchall():
                key = (g_id, w_id, o_id)
                income_map[key] = income_map.get(key, 0) + TERRITORY_YIELD.get(iso, 50)
                territory_counts[key] = territory_counts.get(key, 0) + 1
            c.execute("SELECT guild_id, oil_enabled_w1, oil_enabled_w2, oil_enabled_w3 FROM server_channels")
            oil_configs = {r[0]: (r[1], r[2], r[3]) for r in c.fetchall()}
            
            c.execute("SELECT guild_id, world_id, user_id, gold, oil, debt FROM players")
            for g_id, w_id, u_id, gold, oil, debt in c.fetchall():
                key = (g_id, w_id, u_id)
                new_gold = gold + BASE_INCOME + income_map.get(key, 0)
                
                if debt and debt > 0:
                    repay = min(debt, int(new_gold * 0.5))
                    if repay > 0:
                        debt -= repay
                        new_gold -= repay
                        c.execute("UPDATE players SET debt=? WHERE guild_id=? AND world_id=? AND user_id=?", (debt, g_id, w_id, u_id))

                oil_enabled = bool(oil_configs.get(g_id, (1,1,1))[w_id-1]) if 1 <= w_id <= 3 else True
                if oil_enabled:
                    oil_drain = territory_counts.get(key, 0) * 25
                    c.execute("UPDATE players SET gold=?, oil=? WHERE guild_id=? AND world_id=? AND user_id=?", (new_gold, max(0, oil + 2000 - oil_drain), g_id, w_id, u_id))
                else:
                    c.execute("UPDATE players SET gold=? WHERE guild_id=? AND world_id=? AND user_id=?", (new_gold, g_id, w_id, u_id))
            conn.commit()
            
        for guild in bot.guilds:
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT notify_ch, notify_enabled FROM server_channels WHERE guild_id=?", (str(guild.id),))
                row = c.fetchone()
            if row and row[1] == 1 and row[0]:
                channel = guild.get_channel(int(row[0]))
                if channel:
                    try: 
                        await channel.send("[定時給付] 基本給与・税収・配給石油が振り込まれました。(※戦債がある場合は一部天引きされます)")
                        await generate_and_send_news(guild, channel)
                    except: pass
    except Exception as e: logger.error(f"定時給付エラー: {e}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    channel = guild.system_channel
    if not channel:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages: channel = ch; break
    if channel: 
        embed = discord.Embed(
            title="全世界戦争Botへようこそ", 
            description="導入ありがとうございます！\n\n**【管理者向け】**\n`/op setup` を実行して各設定コマンドを確認してください。\n\n**【全プレイヤー向け】**\nルール確認は `/help`、かんたん操作は `/command` を確認してください！", 
            color=0x2ecc71
        )
        try: await channel.send(embed=embed)
        except: pass

@tasks.loop(minutes=10)
async def update_presence():
    try:
        total_members = sum(guild.member_count for guild in bot.guilds if guild.member_count)
        activity = discord.Activity(
            type=discord.ActivityType.watching, 
            name=f"{total_members}人のプレイヤー"
        )
        await bot.change_presence(status=discord.Status.online, activity=activity)
    except Exception as e:
        logger.error(f"ステータス更新エラー: {e}")

@bot.event
async def on_ready():
    logger.info(f"稼働開始: {bot.user}")
    # Botのログインが完了してからステータス更新タスクを開始する
    if not update_presence.is_running():
        update_presence.start()

if __name__ == "__main__":
    if not BOT_TOKEN: print("[エラー] .envファイルに DISCORD_BOT_TOKEN が設定されていません。")
    else: bot.run(BOT_TOKEN)
# botup
