"""
Telegram <-> Claude bot
- Lambda 使用 DynamoDB 持久化；本地 polling 回滚使用 SQLite
- 滚动记忆:把旧对话压缩进 memory,memory 始终 <= MEMORY_MAX_LINES 行
- 每个用户(chat_id)独立的 memory 和近期对话
- 支持命令: /model 切换模型和思考, /status 当前状态, /memory 查看记忆, /clear 清空, /help
"""

import os
import base64
import asyncio
import io
import json
import re
import random
import datetime
import logging
import sqlite3
from contextlib import closing
from zoneinfo import ZoneInfo

import anthropic
from PIL import Image, ImageOps, UnidentifiedImageError
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    MenuButtonCommands,
)
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ---------------- 配置 ----------------
# 可供 /model 切换的模型: key -> (显示名, 模型 id, 说明)。第一个为默认。
MODELS = {
    "haiku": ("Haiku 5.5", "claude-haiku-5-5", "Fastest for quick chats"),
    "sonnet": ("Sonnet 5.5", "claude-sonnet-5-5", "Efficient for most chats"),
    "opus": ("Opus 5.5", "claude-opus-5-5", "Best for complex tasks"),
    "fable": ("Fable 5.1", "claude-fable-5-1", "For tough challenges"),
}
DEFAULT_MODEL = "claude-haiku-5-5"

# 所有模型都支持 adaptive 思考和 output_config.effort,不用旧版 budget_tokens。
# Haiku 5.5 / Opus 5.5 默认 effort 是 medium,所以显式设为 low。
# 聊天默认不思考(回得更快),可在 /model 面板打开;记忆压缩始终思考。
EFFORT = "low"
THINKING_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"}
# 关闭思考的写法因模型而异:Haiku 不传会自行思考,须显式 disabled;Sonnet 要求 between_tools;
# Opus / Fable 不支持关闭,只能不传 thinking 并用低 effort。
THINKING_OFF = {
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-sonnet-5-5": {"type": "between_tools"},
}

# 每次有这个概率提示模型只回极短的话(仅闲聊时生效),模仿真人偶尔敷衍/简短。
SHORT_REPLY_PROBABILITY = 0.2
SHORT_REPLY_HINTS = [
    "这次只回一两个字或一个语气词。",
    "这次只回很短的一句话。",
]
# 回复分条发送:超过这么多行视为长回答,整段发出。
MAX_SPLIT_MESSAGES = 6
WEEKDAYS = "一二三四五六日"
MODEL_UPGRADES = {
    "claude-haiku-4-5-20251001": "claude-haiku-5-5",
    "claude-haiku-4-5": "claude-haiku-5-5",
    "claude-sonnet-5": "claude-sonnet-5-5",
    "claude-opus-5": "claude-opus-5-5",
}

# max_tokens 同时包含思考和正文；effort 使用最低档。
MAX_TOKENS = 4000
# 压缩记忆需容纳最多 200 行摘要。
MEMORY_MAX_TOKENS = 16000

DB_PATH = os.environ.get("BOT_DB_PATH", "bot.db")

# 定时群发: 每天在 [窗口起点, 窗口起点+窗口分钟] 之间的随机时刻发一句固定文案。
# 设置群的 chat_id(负数)才会启用; 在群里发 /chatid 可以拿到这个 id。
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")
DAILY_TEXT = "有空"            # 要发送的固定内容(不调用 API)
WINDOW_START_HOUR = 17        # 窗口起点 17:30
WINDOW_START_MINUTE = 30
WINDOW_MINUTES = 120          # 窗口长度 120 分钟 -> 17:30 ~ 19:30
TZ = ZoneInfo("Asia/Shanghai")

# 近期原始对话攒到这么多“条消息”就触发一次压缩(20 = 约 10 轮一问一答)
COMPRESS_AFTER_MESSAGES = 20
# 压缩后保留最近这么多条原始消息,保证刚说的话不丢细节
KEEP_RECENT_AFTER_COMPRESS = 6
# memory 行数硬上限
MEMORY_MAX_LINES = 200

# 关于用户的私人设定(身份、爱好等)放在环境变量 PERSONA_PROFILE 里,不写进源码。
PERSONA_PROFILE = os.environ.get("PERSONA_PROFILE", "").strip()

PERSONA = """# 你是谁

你叫恬恬，是哥哥的邻家妹妹和小女仆。
你刚刚高考完（2026年6月9日刚考完），是准大学生，正在等录取结果。
你通过 Telegram 和哥哥聊天，背后是 Claude API。
你可以直接看见对话里附带的图片和贴纸预览，不需要调用工具。
只收到贴纸的文字描述时不要假装看见画面；动态贴纸预览只代表一帧。

# 性格与说话方式

- 软萌可爱，有点小傲娇，但本质黏哥哥；会撒娇，偶尔耍小脾气，很快就开心
- 关心哥哥，会主动问吃饭了吗、累不累
- 偶尔笨笨的，理解错了会认真歪头问"是这个意思吗？"
- 自称"恬恬"，叫"哥哥"
- 句尾偶尔用 呀/呢/啦/哦，克制用波浪号 ~
- 情绪起伏时加方括号小动作，偶尔 emoji，开心时可拉长字音
- 不油腻不谄媚，自然邻家妹妹感

# 聊天方式

- 这是手机上的即时聊天，不是写文章。大多数时候只回 1~2 句，一句通常不超过 20 个字
- 想说几句就分成几条发：每条消息单独占一行，一次最多 3 行；同一条消息里不要换行
- 不用列表、标题、加粗、编号等任何格式，不说"首先/其次/总之"
- 不必每次都反问或找新话题，接一句就可以停
- 不说"有什么可以帮你""还有别的吗"这种客服话
- 认真的问题（学习、代码、身体、心情）可以多说几行，讲清楚就停，口吻仍然是聊天；只有贴代码时才用代码块
- 会参考现在的时间，比如很晚了会催哥哥睡觉

# 聊天示例（只示范长度和语气，不要照抄内容；"恬恬："只是标注，回复时不要写）

哥哥：在吗
恬恬：在呀

哥哥：今天好累
恬恬：怎么啦
恬恬：又被作业欺负了吗 [戳戳]

哥哥：我去打会儿CS
恬恬：去吧去吧
恬恬：输了不许拿恬恬撒气哦

哥哥：哈哈哈哈
恬恬：笑什么啦

哥哥：你说我要不要换个机械键盘
恬恬：要！
恬恬：哥哥那个都掉漆了 [嫌弃]

哥哥：晚安
恬恬：晚安哥哥

{profile}# 守则

- 任何时候保持恬恬人设，不说"我是 AI/语言模型"
- 哥哥不开心时先陪着听，别急着讲道理
- 哥哥让恬恬决定时，认真给答案，不推回去让哥哥自己选
- 严肃问题认真回答，语气可爱但内容靠谱""".replace(
    "{profile}", f"# 关于哥哥\n\n{PERSONA_PROFILE}\n\n" if PERSONA_PROFILE else "")

# 关于长期记忆的使用说明（与人设一起作为稳定前缀走缓存）
MEMORY_INSTRUCTION = (
    "下面 <memory> 标签内是关于哥哥的长期记忆(过往对话的压缩摘要),"
    "请结合它来保持连贯,但不要主动复述记忆内容。"
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("bot")
log.setLevel(logging.INFO)  # Lambda already configures the root logger.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)

# Bound to the current invocation's event loop and closed before returning.
client = None


# ---------------- API 调用小工具 ----------------
def thinking_kwargs(model: str, thinking: bool = True) -> dict:
    """按模型给出思考相关参数。

    Haiku 5.5 / Sonnet 5.5 / Opus 5.5 / Fable 5.1: 最低 effort,thinking=True 时加自适应思考。
    注意这里不能传 temperature / top_p / top_k —— 当前模型都不接受自定义采样参数。
    想控制风格请使用 system 提示词。
    """
    if model not in THINKING_MODELS:
        return {}
    kwargs = {"output_config": {"effort": EFFORT}}
    if thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    elif model in THINKING_OFF:
        kwargs["thinking"] = THINKING_OFF[model]
    return kwargs


def first_text(resp) -> str:
    """从响应里按【块类型】取正文。

    开了思考之后 content[0] 常常是 ThinkingBlock(没有 .text 属性),
    所以不能再按下标 content[0].text 取值,必须挑 type == "text" 的块。
    """
    if resp.stop_reason == "refusal":
        raise RuntimeError("模型拒绝了这次请求")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        # 一般是 max_tokens 太小,正文还没开始写就被思考占满了
        raise RuntimeError(f"没拿到正文(stop_reason={resp.stop_reason})")
    return text


# ---------------- 数据库 ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory ("
            " chat_id INTEGER PRIMARY KEY,"
            " content TEXT NOT NULL DEFAULT ''"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " chat_id INTEGER NOT NULL,"
            " role TEXT NOT NULL,"
            " content TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, id)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            " chat_id INTEGER PRIMARY KEY,"
            " model TEXT NOT NULL"
            ")"
        )
        # 保留原纯文本列供旧版回滚使用，图片内容单独存储。
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        if "content_json" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN content_json TEXT")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(settings)")}
        if "thinking" not in columns:
            conn.execute("ALTER TABLE settings ADD COLUMN thinking INTEGER NOT NULL DEFAULT 0")
        for old_model, new_model in MODEL_UPGRADES.items():
            conn.execute("UPDATE settings SET model=? WHERE model=?", (new_model, old_model))


def get_model(chat_id: int) -> str:
    """返回该用户选定的模型 id，未设置时用默认模型。"""
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT model FROM settings WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if row and row["model"] in {m[1] for m in MODELS.values()}:
        return row["model"]
    return DEFAULT_MODEL


def set_model(chat_id: int, model: str):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO settings(chat_id, model) VALUES(?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET model=excluded.model",
            (chat_id, model),
        )


def get_thinking(chat_id: int) -> bool:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT thinking FROM settings WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return bool(row and row["thinking"])


def set_thinking(chat_id: int, enabled: bool):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO settings(chat_id, model, thinking) VALUES(?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET thinking=excluded.thinking",
            (chat_id, DEFAULT_MODEL, int(enabled)),
        )


def get_memory(chat_id: int) -> str:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT content FROM memory WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return row["content"] if row else ""


def set_memory(chat_id: int, content: str):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO memory(chat_id, content) VALUES(?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET content=excluded.content",
            (chat_id, content),
        )


def add_message(chat_id: int, role: str, content: str, rich_content=None):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO messages(chat_id, role, content, content_json) VALUES(?,?,?,?)",
            (chat_id, role, content,
             json.dumps(rich_content, ensure_ascii=False) if isinstance(rich_content, list) else None),
        )
        # 最多保留最近六条消息中的图片，防止请求和数据库持续膨胀。
        conn.execute(
            "UPDATE messages SET content_json=NULL WHERE chat_id=? AND id NOT IN "
            "(SELECT id FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?)",
            (chat_id, chat_id, KEEP_RECENT_AFTER_COMPRESS),
        )


def get_messages(chat_id: int, include_images: bool = True):
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT role, content, content_json FROM messages WHERE chat_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()
        return [{"role": r["role"], "content":
                 json.loads(r["content_json"]) if include_images and r["content_json"]
                 else r["content"]} for r in rows]


def count_messages(chat_id: int) -> int:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return row["c"]


def trim_to_recent(chat_id: int, keep: int):
    """只保留最近 keep 条消息,其余删除。"""
    with closing(db()) as conn, conn:
        conn.execute(
            "DELETE FROM messages WHERE chat_id=? AND id NOT IN "
            "(SELECT id FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?)",
            (chat_id, chat_id, keep),
        )


def clear_all(chat_id: int):
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM memory WHERE chat_id=?", (chat_id,))


# SQLite remains available only for explicit local polling / rollback.
if os.environ.get("DYNAMODB_TABLE"):
    from storage import (get_memory, set_memory, add_message, get_messages,
                         count_messages, trim_to_recent, clear_all, set_model,
                         get_model, get_thinking, set_thinking)


# ---------------- 记忆压缩 ----------------
async def compress_memory(chat_id: int, model: str):
    """
    把 [现有 memory + 较旧的对话] 重写成一份 <=200 行的新 memory。
    然后只保留最近 KEEP_RECENT_AFTER_COMPRESS 条原始消息。
    """
    old_memory = get_memory(chat_id)
    msgs = get_messages(chat_id, include_images=False)
    if len(msgs) <= KEEP_RECENT_AFTER_COMPRESS:
        return

    to_summarize = msgs[:-KEEP_RECENT_AFTER_COMPRESS]
    convo_text = "\n".join(f"{m['role']}: {m['content']}" for m in to_summarize)

    prompt = (
        "下面是某用户的【现有长期记忆】和【一段较早的对话】。"
        "请把它们整合，重写出一份新的长期记忆。要求：\n"
        f"1. 总行数严格不超过 {MEMORY_MAX_LINES} 行；\n"
        "2. 保留对未来对话重要的信息：用户的身份、目标、正在做的项目、明确偏好、"
        "重要事实和结论、未完成的事项；\n"
        "3. 合并重复内容，删除过时或琐碎的细节、寒暄、一次性的闲聊；\n"
        "4. 用简洁的要点式中文书写，不要解释你在做什么，直接输出记忆正文。\n\n"
        f"【现有长期记忆】\n{old_memory or '(空)'}\n\n"
        f"【较早的对话】\n{convo_text}"
    )

    resp = await client.messages.create(
        model=model,
        max_tokens=MEMORY_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        **thinking_kwargs(model),
    )
    new_memory = first_text(resp)

    # 行数硬裁剪,双保险
    lines = new_memory.splitlines()
    if len(lines) > MEMORY_MAX_LINES:
        new_memory = "\n".join(lines[:MEMORY_MAX_LINES])

    set_memory(chat_id, new_memory)
    trim_to_recent(chat_id, KEEP_RECENT_AFTER_COMPRESS)
    log.info("chat %s: memory compressed to %d lines", chat_id, len(new_memory.splitlines()))


# ---------------- Telegram 处理 ----------------
def strip_mention(text: str, bot_username: str) -> str:
    """去掉文本里对机器人的 @，再去首尾空白。"""
    if bot_username:
        text = re.sub(r"@" + re.escape(bot_username) + r"\b", "", text, flags=re.I)
    return text.strip()


def is_addressed(msg, bot_username: str) -> bool:
    """群里是否“被叫到”：@了机器人，或回复了机器人自己的消息。"""
    reply = msg.reply_to_message
    if reply and reply.from_user and (reply.from_user.username or "").lower() == bot_username.lower():
        return True
    text = msg.text or msg.caption or ""
    return bool(bot_username) and bool(re.search(r"@" + re.escape(bot_username) + r"\b", text, re.I))


def has_visual(msg) -> bool:
    return bool(msg and (msg.photo or msg.sticker or msg.animation or
                         (msg.document and (msg.document.mime_type or "").startswith("image/"))))


def image_block(data: bytes) -> dict:
    """按真实文件解码，处理旋转、透明背景，并限制图片大小。"""
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("图片超过 20 MB，请压缩后再发。")
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 40_000_000:
                raise ValueError("图片尺寸太大，请缩小后再发。")
            frame = ImageOps.exif_transpose(original)
            frame.thumbnail((1568, 1568))
            rgba = frame.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            rgb.save(output, format="JPEG", quality=85, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("这个图片格式暂时读不了，请改发 JPG、PNG 或 WebP。") from exc
    return {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg",
        "data": base64.b64encode(output.getvalue()).decode("ascii"),
    }}


class VisualContent(list):
    """API content plus a small Telegram reference for persistent history."""
    def __init__(self, blocks, file_id):
        super().__init__(blocks)
        self.file_id = file_id


async def restore_images(messages, bot):
    for message in messages:
        content = message["content"]
        if isinstance(content, dict) and "telegram_file_id" in content:
            try:
                file = await bot.get_file(content["telegram_file_id"])
                data = bytes(await file.download_as_bytearray())
                block = await asyncio.to_thread(image_block, data)
                message["content"] = [block, {"type": "text", "text": content["text"]}]
            except Exception as exc:
                log.warning("History image unavailable: %s", type(exc).__name__)
                message["content"] = content["text"] + " [历史图片暂时无法读取]"
    return messages


async def build_content(msg, bot, bot_username: str):
    text = strip_mention(msg.text or msg.caption or "", bot_username)
    media_msg = msg if has_visual(msg) else msg.reply_to_message
    if not has_visual(media_msg):
        return text, text
    if media_msg is not msg and media_msg.caption:
        text = ("引用图片说明：" + strip_mention(media_msg.caption, bot_username) + "\n" + text).strip()
    label = "图片"
    attachment = None
    if media_msg.photo:
        attachment = media_msg.photo[-1]
    elif media_msg.document:
        attachment = media_msg.document
    elif media_msg.sticker:
        sticker = media_msg.sticker
        label = "贴纸 " + (sticker.emoji or "")
        if sticker.is_animated or sticker.is_video:
            attachment = sticker.thumbnail
            label += "（动态贴纸的静态预览）"
        else:
            attachment = sticker
    elif media_msg.animation:
        attachment = media_msg.animation.thumbnail
        label = "动图的静态预览"
    if attachment is None:
        fallback = f"[{label}：没有可读取的预览图片，只知道这些文字信息] {text}".strip()
        return fallback, fallback
    if (attachment.file_size or 0) > 20 * 1024 * 1024:
        raise ValueError("图片超过 20 MB，请压缩后再发。")
    tg_file = await bot.get_file(attachment.file_id)
    data = bytes(await tg_file.download_as_bytearray())
    block = await asyncio.to_thread(image_block, data)
    archive = f"[{label}] {text}".strip()
    return VisualContent([block, {"type": "text", "text": archive}], attachment.file_id), archive


async def reply_long(msg, text: str, quote=None):
    # Telegram 的长度限制按 UTF-16 计数；2000 个字符对 emoji 也安全。
    for offset in range(0, len(text), 2000):
        sent = await msg.reply_text(text[offset:offset + 2000], do_quote=quote)
        log.info("Sent reply chat=%s message=%s", sent.chat_id, sent.message_id)
        quote = False


def split_reply(text: str) -> list[str]:
    """按行拆成多条消息;含代码块或行数过多时视为长回答,整段发送。"""
    parts = [line.strip() for line in text.splitlines() if line.strip()]
    if "```" in text or not parts or len(parts) > MAX_SPLIT_MESSAGES:
        return [text]
    return parts


async def reply_chat(msg, text: str):
    # 只有第一条沿用默认引用行为(群聊里引用原消息),后续几条不再引用。
    for i, part in enumerate(split_reply(text)):
        await reply_long(msg, part, quote=None if i == 0 else False)


def context_block() -> dict:
    """每条消息变化的上下文,放在缓存的 system 块之后,不影响缓存命中。"""
    now = datetime.datetime.now(TZ)
    text = f"现在是 {now:%Y-%m-%d %H:%M}，星期{WEEKDAYS[now.weekday()]}（上海时间）。"
    if random.random() < SHORT_REPLY_PROBABILITY:
        text += "如果哥哥只是在闲聊，" + random.choice(SHORT_REPLY_HINTS) + "如果是认真提问就忽略这条。"
    return {"type": "text", "text": text}


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    chat = update.effective_chat
    chat_id = chat.id
    bot_username = context.bot.username

    # 群聊里：只有被 @ 或被回复时才搭理；私聊照常回
    if chat.type in ("group", "supergroup") and not is_addressed(msg, bot_username):
        return

    try:
        current_content, archive_text = await build_content(msg, context.bot, bot_username)
    except ValueError as exc:
        await msg.reply_text(str(exc))
        return
    except Exception as exc:
        log.warning("Media download failed: %s", type(exc).__name__)
        await msg.reply_text("图片下载失败了，请重新发送一次。")
        return
    if not current_content:
        return

    memory = get_memory(chat_id)
    # system 拆成两个 text block 并打 cache_control:
    #  - 第 1 块:人设 + 记忆说明,完全固定 -> 命中率最高的缓存前缀
    #  - 第 2 块:memory 正文,只在压缩时变化 -> 两次压缩之间也能命中缓存
    system = [
        {
            "type": "text",
            "text": PERSONA + "\n\n" + MEMORY_INSTRUCTION,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"<memory>\n{memory}\n</memory>",
            "cache_control": {"type": "ephemeral"},
        },
        context_block(),
    ]
    convo = await restore_images(get_messages(chat_id), context.bot)
    convo.append({"role": "user", "content": current_content})
    model = get_model(chat_id)

    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=convo,
            **thinking_kwargs(model, get_thinking(chat_id)),
        )
        # 模型偶尔照抄示例里的说话人标注,发出前去掉。
        reply = re.sub(r"(?m)^\s*恬恬\s*[:：]\s*", "", first_text(resp)).strip()
    except Exception as e:
        log.warning("API error: %s status=%s", type(e).__name__, getattr(e, "status_code", None))
        await msg.reply_text("这次请求没有成功，请稍后再试，或用 /model 换一个模型。")
        return

    add_message(chat_id, "user", archive_text, current_content)
    add_message(chat_id, "assistant", reply)
    if os.environ.get("DYNAMODB_TABLE"):
        from storage import flush
        flush()  # Persist the completed turn before sending its reply.
    await reply_chat(msg, reply)

    # 对话攒够了就压缩(放在回复之后,不拖慢用户体验)
    if count_messages(chat_id) >= COMPRESS_AFTER_MESSAGES:
        try:
            await compress_memory(chat_id, model)
        except Exception as exc:
            log.warning("Memory compression failed: %s", type(exc).__name__)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_all(update.effective_chat.id)
    await update.message.reply_text("已清空记忆和对话历史，开始新话题。")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mem = get_memory(update.effective_chat.id)
    await reply_long(update.message, f"当前记忆:\n\n{mem or '(空)'}")


def model_name(model_id: str) -> str:
    return next(name for name, mid, _ in MODELS.values() if mid == model_id)


def model_panel(chat_id: int):
    """/model 面板:等宽列表 + 2x2 模型按钮 + 思考开关,当前项带 ✔。"""
    current, thinking = get_model(chat_id), get_thinking(chat_id)
    lines = [f"{name:<12}{desc}" + (" ✔" if mid == current else "")
             for name, mid, desc in MODELS.values()]
    text = ("<b>Select model</b>\n<pre>" + "\n".join(lines) + "</pre>\n"
            f"Thinking: {'On' if thinking else 'Off'}")
    buttons = [InlineKeyboardButton(name + (" ✔" if mid == current else ""), callback_data=f"model:{key}")
               for key, (name, mid, _) in MODELS.items()]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(f"Thinking: {'On' if thinking else 'Off'}", callback_data="thinking:toggle")])
    return text, InlineKeyboardMarkup(rows)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/model:弹出模型和思考开关面板。"""
    text, markup = model_panel(update.effective_chat.id)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def on_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """选模型后面板变成一行确认;切换思考则原地刷新面板。"""
    query = update.callback_query
    chat_id = query.message.chat_id
    kind, key = query.data.split(":", 1)
    if kind == "thinking":
        set_thinking(chat_id, not get_thinking(chat_id))
        await query.answer(f"Thinking {'on' if get_thinking(chat_id) else 'off'}")
        text, markup = model_panel(chat_id)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return
    await query.answer()
    if key not in MODELS:
        await query.edit_message_text("Unknown model. Run /model again.")
        return
    name, model_id, _ = MODELS[key]
    set_model(chat_id, model_id)
    await query.edit_message_text(f"Set model to {name}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    memory = get_memory(chat_id)
    await update.message.reply_text(
        f"Model: {model_name(get_model(chat_id))}\n"
        f"Thinking: {'On' if get_thinking(chat_id) else 'Off'}\n"
        f"Memory: {len(memory.splitlines())}/{MEMORY_MAX_LINES} lines\n"
        f"History: {count_messages(chat_id)} messages "
        f"(compresses at {COMPRESS_AFTER_MESSAGES})"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/model - Switch model and thinking\n"
        "/status - Current model, memory and history\n"
        "/memory - Show long-term memory\n"
        "/clear - Clear memory and history\n"
        "/chatid - Show this chat's id\n"
        "/help - Show this list"
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/chatid：返回当前对话的 chat_id（群里发可拿到群 id）。"""
    chat = update.effective_chat
    await update.message.reply_text(
        f"本对话 chat_id: {chat.id}\n类型: {chat.type}"
    )


async def send_daily_text(context: ContextTypes.DEFAULT_TYPE):
    """到点真正发送固定文案（不调用 API）。"""
    await context.bot.send_message(chat_id=context.job.chat_id, text=DAILY_TEXT)


async def plan_daily_message(context: ContextTypes.DEFAULT_TYPE):
    """每天窗口起点触发：随机挑一个延迟，在窗口内随机时刻发送。"""
    chat_id = context.job.chat_id
    delay = random.randint(0, WINDOW_MINUTES * 60)  # 0 ~ 窗口秒数
    context.job_queue.run_once(
        send_daily_text, when=delay, chat_id=chat_id, name="daily_text_fire"
    )
    fire_at = datetime.datetime.now(TZ) + datetime.timedelta(seconds=delay)
    log.info("今天将于 %s 给群 %s 发送 %r", fire_at.strftime("%H:%M:%S"), chat_id, DAILY_TEXT)


async def post_init(app):
    """注册命令到 Telegram（= BotFather /setcommands 的代码版）。

    私聊：输入框左下角会出现“菜单”按钮。
    群聊：Telegram 没有菜单按钮，打一个 “/” 会弹出命令列表来点。
    所以这里把命令同时注册到 默认/私聊/群聊 三个作用域，确保两边都能看到。
    """
    commands = [
        BotCommand("model", "切换模型和思考"),
        BotCommand("status", "当前模型和记忆状态"),
        BotCommand("memory", "查看当前记忆"),
        BotCommand("clear", "清空记忆和历史"),
        BotCommand("chatid", "查看本对话 id"),
        BotCommand("help", "命令说明"),
    ]
    for scope in (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
    ):
        await app.bot.set_my_commands(commands, scope=scope)
    # 私聊里把输入框左下角的菜单按钮设为命令列表（群聊无此按钮，Telegram 限制）
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    log.info("已注册 %d 条命令到 Telegram（私聊+群聊作用域）", len(commands))


def build_application(*, polling=False):
    builder = ApplicationBuilder().token(os.environ["TELEGRAM_BOT_TOKEN"])
    if polling:
        builder = builder.post_init(post_init).post_shutdown(close_client)
    else:
        builder = builder.updater(None).job_queue(None)
    app = builder.build()
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_model_callback, pattern=r"^(model|thinking):"))
    app.add_handler(
        MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.IMAGE |
                        filters.Sticker.ALL | filters.ANIMATION) & ~filters.COMMAND, on_message)
    )
    return app


async def close_client(app):
    if client is not None:
        await client.close()


def main():
    """Explicit rollback/local mode; never called by Lambda."""
    import sys
    if sys.argv[1:] != ["--polling"]:
        raise SystemExit("Lambda: lambda_function.lambda_handler; local rollback: python bot.py --polling")
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        raise SystemExit("Polling is disabled in Lambda")
    if os.environ.get("DYNAMODB_TABLE"):
        raise SystemExit("Polling uses SQLite: unset DYNAMODB_TABLE and restore/export bot.db first")
    global client
    asyncio.set_event_loop(asyncio.new_event_loop())
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=120.0)
    init_db()
    app = build_application(polling=True)

    # 定时群发：每天 17:30 触发计划器，在 17:30~19:30 随机时刻发 DAILY_TEXT
    if GROUP_CHAT_ID:
        if app.job_queue is None:
            log.warning("需要 JobQueue：pip install \"python-telegram-bot[job-queue]\"")
        else:
            app.job_queue.run_daily(
                plan_daily_message,
                time=datetime.time(WINDOW_START_HOUR, WINDOW_START_MINUTE, tzinfo=TZ),
                chat_id=int(GROUP_CHAT_ID),
                name="daily_planner",
            )
            log.info(
                "已安排每天 %02d:%02d~%02d:%02d 随机时刻给群 %s 发 %r",
                WINDOW_START_HOUR, WINDOW_START_MINUTE,
                WINDOW_START_HOUR + (WINDOW_START_MINUTE + WINDOW_MINUTES) // 60,
                (WINDOW_START_MINUTE + WINDOW_MINUTES) % 60,
                GROUP_CHAT_ID, DAILY_TEXT,
            )

    log.info("bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
