import asyncio
import html
import io
import logging
import os
import random
import re
import string
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import aiosqlite
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InputSticker,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ───────────────────────── Настройки ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = "lumenstickers.db"

FONT_CANDIDATES = [
    os.getenv("FONT_PATH", ""),
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]

try:
    from rembg import remove as rembg_remove  # pip install rembg
    HAS_REMBG = True
except Exception:
    HAS_REMBG = False

LIMITS = {"sticker": 120, "emoji": 200}
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u2764]\ufe0f?")

logging.basicConfig(level=logging.INFO)
router = Router()
user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class UserError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


class S(StatesGroup):
    title = State()
    adding = State()
    renaming = State()
    deleting = State()


# ───────────────────────── База данных ─────────────────────────
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, created INTEGER,
                border INTEGER DEFAULT 0, crop INTEGER DEFAULT 0,
                nobg INTEGER DEFAULT 0, emoji TEXT DEFAULT '😀'
            );
            CREATE TABLE IF NOT EXISTS packs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
                user_id INTEGER, title TEXT, kind TEXT, fmt TEXT, created INTEGER
            );
            CREATE INDEX IF NOT EXISTS i_packs_user ON packs(user_id);
            """
        )
        await db.commit()


async def get_user(uid: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("INSERT OR IGNORE INTO users(id, created) VALUES(?,?)", (uid, int(time.time())))
        await db.commit()
        cur = await db.execute("SELECT * FROM users WHERE id=?", (uid,))
        return dict(await cur.fetchone())


async def set_user(uid: int, field: str, value):
    if field not in {"border", "crop", "nobg", "emoji"}:
        raise ValueError(field)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))
        await db.commit()


async def add_pack(name, uid, title, kind, fmt) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO packs(name,user_id,title,kind,fmt,created) VALUES(?,?,?,?,?,?)",
            (name, uid, title, kind, fmt, int(time.time())),
        )
        await db.commit()
        return cur.lastrowid


async def list_packs(uid: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM packs WHERE user_id=? ORDER BY id DESC LIMIT 40", (uid,))
        return [dict(r) for r in await cur.fetchall()]


async def get_pack(pid: int, uid: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM packs WHERE id=? AND user_id=?", (pid, uid))
        row = await cur.fetchone()
        return dict(row) if row else None


async def del_pack(pid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM packs WHERE id=?", (pid,))
        await db.commit()


async def rename_pack(pid: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE packs SET title=? WHERE id=?", (title, pid))
        await db.commit()


# ───────────────────────── Обработка медиа ─────────────────────────
def _alpha_border(img: Image.Image, px: int) -> Image.Image:
    """Белая обводка вокруг объекта (по альфа-каналу)."""
    grown = img.getchannel("A").filter(ImageFilter.MaxFilter(px * 2 + 1))
    out = Image.new("RGBA", img.size, (255, 255, 255, 255))
    out.putalpha(grown)
    out.alpha_composite(img)
    return out


def process_static(data: bytes, emoji: bool, border: bool, crop: bool, nobg: bool) -> bytes:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGBA")
    if nobg and HAS_REMBG:
        img = rembg_remove(img).convert("RGBA")

    if emoji:  # кастомные эмодзи — строго 100x100
        side = 100
        if crop:
            img = ImageOps.fit(img, (side, side), Image.LANCZOS)
        else:
            img.thumbnail((side, side), Image.LANCZOS)
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
            img = canvas
    else:  # стикеры — одна сторона ровно 512
        side = 512
        pad = 8 if border else 0
        inner = side - 2 * pad
        if crop:
            img = ImageOps.fit(img, (inner, inner), Image.LANCZOS)
        else:
            k = inner / max(img.size)
            img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)
        if border:
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
            img = _alpha_border(canvas, pad - 1)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    if buf.tell() > 500_000:
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=85)
    return buf.getvalue()


def process_video(data: bytes, emoji: bool) -> bytes:
    """Видео -> WEBM VP9 с альфой, до 3 сек, до 256 КБ."""
    side = 100 if emoji else 512
    if emoji:
        vf = f"scale={side}:{side}:force_original_aspect_ratio=increase,crop={side}:{side}"
    else:
        vf = f"scale={side}:{side}:force_original_aspect_ratio=decrease"
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.mp4", Path(tmp) / "out.webm"
        src.write_bytes(data)
        for bitrate in (700, 450, 280, 170, 100):
            cmd = [
                "ffmpeg", "-y", "-i", str(src), "-t", "3", "-an",
                "-vf", f"{vf},fps=30", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-b:v", f"{bitrate}K", "-auto-alt-ref", "0", str(dst),
            ]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=180)
            except FileNotFoundError:
                raise UserError("На сервере не установлен ffmpeg — видео пока не поддерживается.")
            if r.returncode != 0:
                raise UserError("Не удалось обработать видео. Попробуй другой файл.")
            if dst.stat().st_size <= 256 * 1024:
                return dst.read_bytes()
    raise UserError("Видео получилось слишком тяжёлым. Пришли покороче или попроще.")


def _find_font(size: int):
    path = next((p for p in FONT_CANDIDATES if p and Path(p).exists()), None)
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def render_text(text: str) -> bytes:
    """Текстовый стикер: крупный белый текст с фиолетовой обводкой."""
    size = 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    text = text.strip()[:200]
    for fs in range(180, 20, -6):
        font = _find_font(fs)
        lines, cur = [], ""
        for word in text.split():
            test = f"{cur} {word}".strip()
            if d.textlength(test, font=font) <= size - 50:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = word
        lines.append(cur)
        joined = "\n".join(lines)
        stroke = max(3, fs // 12)
        box = d.multiline_textbbox((size // 2, size // 2), joined, font=font, anchor="mm",
                                   align="center", stroke_width=stroke)
        if box[2] - box[0] <= size - 20 and box[3] - box[1] <= size - 20:
            break
    d.multiline_text((size // 2, size // 2), joined, font=font, fill="white", anchor="mm",
                     align="center", stroke_width=stroke, stroke_fill=(88, 28, 135, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


async def download_media(bot: Bot, m: Message) -> tuple[bytes, str]:
    """Возвращает (байты, 'image'|'video')."""
    if m.photo:
        obj, kind = m.photo[-1], "image"
    elif m.sticker:
        if m.sticker.is_animated:
            raise UserError("Анимированные TGS-стикеры нельзя использовать в новых паках. Пришли картинку или видео.")
        obj, kind = m.sticker, ("video" if m.sticker.is_video else "image")
    elif m.video:
        obj, kind = m.video, "video"
    elif m.animation:
        obj, kind = m.animation, "video"
    elif m.video_note:
        obj, kind = m.video_note, "video"
    elif m.document:
        mt = (m.document.mime_type or "").lower()
        if mt == "image/gif" or mt.startswith("video/"):
            kind = "video"
        elif mt.startswith("image/"):
            kind = "image"
        else:
            raise UserError("Пришли картинку, видео или GIF.")
        obj = m.document
    else:
        raise UserError("Не понял, что это. Пришли картинку, видео или GIF.")
    if (obj.file_size or 0) > 20 * 1024 * 1024:
        raise UserError("Файл больше 20 МБ — Telegram не даёт ботам такие скачивать.")
    buf = io.BytesIO()
    await bot.download(obj, destination=buf)
    return buf.getvalue(), kind


def pick_emoji(*sources) -> list[str]:
    for s in sources:
        if s:
            found = EMOJI_RE.findall(s)
            if found:
                return found[:5]
    return ["😀"]


def make_short_name(title: str, bot_username: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    if not base:
        base = "pack"
    elif not base[0].isalpha():
        base = "p_" + base
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    tail = f"_by_{bot_username.lower()}"
    base = base[: 64 - len(tail) - 7]
    return f"{base}_{suffix}{tail}"


async def tg_retry(factory, tries: int = 4):
    for _ in range(tries):
        try:
            return await factory()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    raise UserError("Telegram просит подождать. Попробуй через минуту.")


def pack_url(name: str, kind: str) -> str:
    return f"https://t.me/{'addemoji' if kind == 'emoji' else 'addstickers'}/{name}"


# ───────────────────────── Клавиатуры ─────────────────────────
def kb(*rows):
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[InlineKeyboardButton(**x) for x in row])
    return b.as_markup()


def btn(text, data=None, url=None):
    return {"text": text, **({"callback_data": data} if data else {"url": url})}


MAIN_KB = kb(
    [btn("➕ Создать пак", "new")],
    [btn("📂 Мои паки", "packs"), btn("⚙️ Настройки", "settings")],
    [btn("❓ Помощь", "help")],
)
BACK_KB = kb([btn("◀️ В меню", "menu")])

START_TEXT = (
    "💜 <b>LumenStickers</b>\n\n"
    "Делаю стикерпаки и эмодзи-паки прямо в чате:\n"
    "🟣 фото, картинки, GIF и видео → готовые стикеры\n"
    "🟣 кастомные эмодзи (100×100) и обычные стикеры (512×512)\n"
    "🟣 обводка, удаление фона, текстовые стикеры\n"
    "🟣 добавляй, удаляй и переименовывай — всё из бота\n\n"
    "Жми «Создать пак» 👇"
)

HELP_TEXT = (
    "❓ <b>Как пользоваться</b>\n\n"
    "1. «Создать пак» → выбери тип (стикеры / эмодзи) → придумай название.\n"
    "2. Присылай картинки, фото, GIF, видео или статичные стикеры — можно пачкой. "
    "Первый файл создаёт пак, остальные добавляются в него.\n"
    "3. Эмодзи для стикера: напиши его в подписи к файлу или задай по умолчанию: "
    "<code>/emoji 😎</code>\n"
    "4. Просто текстом — получишь текстовый стикер (в паках с картинками).\n"
    "5. «Мои паки» — добавить ещё, удалить стикер, переименовать или удалить пак.\n\n"
    "⚙️ В настройках: обводка, обрезка под квадрат, удаление фона.\n"
    "Видео — до 3 секунд, бот сам сожмёт и конвертирует.\n"
    "/cancel — выйти из текущего действия."
)


def settings_kb(u: dict):
    on = lambda v: "✅" if v else "▫️"
    rows = [
        [btn(f"{on(u['border'])} Белая обводка", "set:border")],
        [btn(f"{on(u['crop'])} Обрезать под квадрат", "set:crop")],
    ]
    if HAS_REMBG:
        rows.append([btn(f"{on(u['nobg'])} Удалять фон (ИИ)", "set:nobg")])
    rows.append([btn("◀️ В меню", "menu")])
    return kb(*rows)


def settings_text(u: dict) -> str:
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"Эмодзи по умолчанию: {u['emoji']} (сменить: <code>/emoji 😎</code>)\n"
        "Настройки применяются к новым стикерам."
    )


async def edit(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


# ───────────────────────── Команды ─────────────────────────
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await get_user(m.from_user.id)
    await m.answer(START_TEXT, reply_markup=MAIN_KB)


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Ок, отменено.", reply_markup=MAIN_KB)


@router.message(Command("new"))
async def cmd_new(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Что создаём?", reply_markup=kind_kb())


@router.message(Command("emoji"))
async def cmd_emoji(m: Message):
    arg = m.text.partition(" ")[2]
    found = EMOJI_RE.findall(arg)
    if not found:
        return await m.answer("Напиши так: <code>/emoji 😎</code>")
    await set_user(m.from_user.id, "emoji", "".join(found[:5]))
    await m.answer(f"Эмодзи по умолчанию: {''.join(found[:5])}")


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        u = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        p = (await (await db.execute("SELECT COUNT(*) FROM packs")).fetchone())[0]
    await m.answer(f"👥 Пользователей: {u}\n📦 Паков: {p}")


@router.message(Command("broadcast"))
async def cmd_broadcast(m: Message, bot: Bot):
    if m.from_user.id != ADMIN_ID:
        return
    text = m.text.partition(" ")[2].strip()
    if not text:
        return await m.answer("Использование: /broadcast текст")
    async with aiosqlite.connect(DB_PATH) as db:
        ids = [r[0] for r in await (await db.execute("SELECT id FROM users")).fetchall()]
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"Отправлено: {ok}/{len(ids)}")


# ───────────────────────── Меню / настройки ─────────────────────────
def kind_kb():
    return kb(
        [btn("🖼 Стикеры (512×512)", "newk:sticker")],
        [btn("😀 Эмодзи-пак (100×100)", "newk:emoji")],
        [btn("◀️ В меню", "menu")],
    )


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, START_TEXT, MAIN_KB)


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await edit(cb, HELP_TEXT, BACK_KB)


@router.callback_query(F.data == "settings")
async def cb_settings(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    await edit(cb, settings_text(u), settings_kb(u))


@router.callback_query(F.data.startswith("set:"))
async def cb_set(cb: CallbackQuery):
    field = cb.data.split(":")[1]
    if field not in {"border", "crop", "nobg"}:
        return await cb.answer()
    u = await get_user(cb.from_user.id)
    await set_user(cb.from_user.id, field, 0 if u[field] else 1)
    u = await get_user(cb.from_user.id)
    await edit(cb, settings_text(u), settings_kb(u))


# ───────────────────────── Создание пака ─────────────────────────
@router.callback_query(F.data == "new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, "Что создаём?", kind_kb())


@router.callback_query(F.data.startswith("newk:"))
async def cb_new_kind(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[1]
    await state.set_state(S.title)
    await state.update_data(kind=kind)
    await edit(cb, "✏️ Придумай название пака (до 64 символов):",
               kb([btn("◀️ Отмена", "menu")]))


@router.message(S.title, F.text)
async def got_title(m: Message, state: FSMContext, bot: Bot):
    title = m.text.strip()
    if not 1 <= len(title) <= 64:
        return await m.answer("Название должно быть от 1 до 64 символов.")
    d = await state.get_data()
    uname = (await bot.me()).username
    await state.set_state(S.adding)
    await state.update_data(
        title=title, name=make_short_name(title, uname), created=False,
        fmt=None, count=0, pid=None,
    )
    hint = "эмодзи" if d["kind"] == "emoji" else "стикеры"
    await m.answer(
        f"Отлично! Присылай картинки, фото, GIF или видео — сделаю {hint}.\n"
        "Можно сразу много. Эмодзи — в подписи к файлу.",
        reply_markup=kb([btn("✅ Готово", "done")]),
    )


@router.callback_query(F.data == "done")
async def cb_done(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    if d.get("created"):
        await edit(cb, f"🎉 Пак «{html.escape(d['title'])}» готов — {d['count']} шт.",
                   kb([btn("🔗 Открыть пак", url=pack_url(d["name"], d["kind"]))],
                      [btn("📂 Мои паки", "packs"), btn("◀️ В меню", "menu")]))
    else:
        await edit(cb, "Пока ничего не добавлено.", MAIN_KB)


async def add_to_pack(m: Message, state: FSMContext, bot: Bot, text_mode: bool = False):
    uid = m.from_user.id
    d = await state.get_data()
    user = await get_user(uid)
    emoji_pack = d["kind"] == "emoji"
    if d["count"] >= LIMITS[d["kind"]]:
        return await m.answer(f"В паке уже максимум ({LIMITS[d['kind']]}). Создай новый.")

    async with user_locks[uid]:
        d = await state.get_data()  # актуально после ожидания блокировки
        status = await m.answer("⏳ Обрабатываю…")
        try:
            if text_mode:
                if d["fmt"] == "video":
                    raise UserError("Это видео-пак, текстовые стикеры тут не подойдут.")
                raw, kind = await asyncio.to_thread(render_text, m.text), "image"
            else:
                raw, kind = await download_media(bot, m)
            fmt = "static" if kind == "image" else "video"
            if d["fmt"] and d["fmt"] != fmt:
                raise UserError(
                    "В этом паке " + ("только картинки" if d["fmt"] == "static" else "только видео")
                    + " — Telegram не разрешает их смешивать. Для другого формата создай отдельный пак."
                )

            if kind == "image":
                data = await asyncio.to_thread(
                    process_static, raw, emoji_pack,
                    False if text_mode else bool(user["border"]),
                    False if text_mode else bool(user["crop"]),
                    False if text_mode else bool(user["nobg"]),
                )
                fname = "sticker.png" if data[:4] == b"\x89PNG" else "sticker.webp"
            else:
                data = await asyncio.to_thread(process_video, raw, emoji_pack)
                fname = "sticker.webm"

            sticker_emoji = m.sticker.emoji if m.sticker else None
            emojis = pick_emoji(m.caption, sticker_emoji, user["emoji"])
            st = InputSticker(sticker=BufferedInputFile(data, fname), format=fmt, emoji_list=emojis)
            uname = (await bot.me()).username
            first = not d["created"]

            if first:
                for _ in range(3):
                    try:
                        await tg_retry(lambda: bot.create_new_sticker_set(
                            user_id=uid, name=d["name"], title=d["title"], stickers=[st],
                            sticker_type="custom_emoji" if emoji_pack else "regular"))
                        break
                    except TelegramBadRequest as e:
                        if "occupied" in str(e).lower():
                            d["name"] = make_short_name(d["title"], uname)
                            continue
                        raise
                else:
                    raise UserError("Не получилось подобрать имя пака, попробуй ещё раз.")
                d["pid"] = await add_pack(d["name"], uid, d["title"], d["kind"], fmt)
                d["created"], d["fmt"] = True, fmt
            else:
                await tg_retry(lambda: bot.add_sticker_to_set(user_id=uid, name=d["name"], sticker=st))

            d["count"] += 1
            await state.update_data(**d)
            rows = [[btn("🔗 Открыть пак", url=pack_url(d["name"], d["kind"]))],
                    [btn("✅ Готово", "done")]]
            head = "🎉 Пак создан!" if first else "✅ Добавлено"
            await status.edit_text(f"{head} · в паке: {d['count']} · {''.join(emojis)}",
                                   reply_markup=kb(*rows))

        except UserError as e:
            await status.edit_text(f"❌ {e}")
        except TelegramBadRequest as e:
            logging.warning("TG error: %s", e)
            msg = str(e).lower()
            if "stickers_too_much" in msg:
                hint = "в паке максимум стикеров."
            elif "invalid" in msg or "wrong" in msg:
                hint = "Telegram не принял файл. Попробуй другой."
            else:
                hint = e.message
            await status.edit_text(f"❌ {hint}")
        except Exception:
            logging.exception("add_to_pack failed")
            await status.edit_text("❌ Не получилось обработать файл. Попробуй другой.")


MEDIA = F.photo | F.document | F.sticker | F.video | F.animation | F.video_note


@router.message(S.adding, MEDIA)
async def adding_media(m: Message, state: FSMContext, bot: Bot):
    await add_to_pack(m, state, bot)


@router.message(S.adding, F.text, ~F.text.startswith("/"))
async def adding_text(m: Message, state: FSMContext, bot: Bot):
    await add_to_pack(m, state, bot, text_mode=True)


# ───────────────────────── Мои паки ─────────────────────────
@router.callback_query(F.data == "packs")
async def cb_packs(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    packs = await list_packs(cb.from_user.id)
    if not packs:
        return await edit(cb, "У тебя пока нет паков.", kb([btn("➕ Создать пак", "new")], [btn("◀️ В меню", "menu")]))
    rows = [[btn(f"{'😀' if p['kind'] == 'emoji' else '🖼'} {p['title'][:32]}", f"p:{p['id']}")] for p in packs]
    rows.append([btn("◀️ В меню", "menu")])
    await edit(cb, "📂 <b>Твои паки:</b>", kb(*rows))


async def _pack_or_gone(cb: CallbackQuery, pid: int):
    p = await get_pack(pid, cb.from_user.id)
    if not p:
        await cb.answer("Пак не найден", show_alert=True)
    return p


@router.callback_query(F.data.startswith("p:"))
async def cb_pack(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    p = await _pack_or_gone(cb, int(cb.data.split(":")[1]))
    if not p:
        return
    i = p["id"]
    await edit(
        cb, f"📦 <b>{html.escape(p['title'])}</b>\n{'Эмодзи-пак' if p['kind'] == 'emoji' else 'Стикерпак'}",
        kb(
            [btn("➕ Добавить", f"padd:{i}"), btn("🔗 Открыть", url=pack_url(p["name"], p["kind"]))],
            [btn("✏️ Название", f"pren:{i}"), btn("🗑 Удалить стикеры", f"pdel:{i}")],
            [btn("❌ Удалить пак", f"pkill:{i}")],
            [btn("◀️ К списку", "packs")],
        ),
    )


@router.callback_query(F.data.startswith("padd:"))
async def cb_padd(cb: CallbackQuery, state: FSMContext, bot: Bot):
    p = await _pack_or_gone(cb, int(cb.data.split(":")[1]))
    if not p:
        return
    try:
        ss = await bot.get_sticker_set(p["name"])
    except TelegramBadRequest:
        await del_pack(p["id"])
        return await edit(cb, "Этот пак уже удалён в Telegram.", BACK_KB)
    await state.set_state(S.adding)
    await state.update_data(title=p["title"], name=p["name"], kind=p["kind"], created=True,
                            fmt=p["fmt"], count=len(ss.stickers), pid=p["id"])
    await edit(cb, f"Присылай файлы для «{html.escape(p['title'])}» (сейчас {len(ss.stickers)} шт.).",
               kb([btn("✅ Готово", "done")]))


@router.callback_query(F.data.startswith("pren:"))
async def cb_pren(cb: CallbackQuery, state: FSMContext):
    p = await _pack_or_gone(cb, int(cb.data.split(":")[1]))
    if not p:
        return
    await state.set_state(S.renaming)
    await state.update_data(pid=p["id"], name=p["name"])
    await edit(cb, "Пришли новое название (до 64 символов):", kb([btn("◀️ Отмена", f"p:{p['id']}")]))


@router.message(S.renaming, F.text, ~F.text.startswith("/"))
async def got_rename(m: Message, state: FSMContext, bot: Bot):
    title = m.text.strip()
    if not 1 <= len(title) <= 64:
        return await m.answer("Название должно быть от 1 до 64 символов.")
    d = await state.get_data()
    try:
        await tg_retry(lambda: bot.set_sticker_set_title(name=d["name"], title=title))
    except (TelegramBadRequest, UserError) as e:
        return await m.answer(f"❌ Не вышло: {e}")
    await rename_pack(d["pid"], title)
    await state.clear()
    await m.answer("✅ Название обновлено.", reply_markup=kb([btn("📂 Мои паки", "packs")]))


@router.callback_query(F.data.startswith("pdel:"))
async def cb_pdel(cb: CallbackQuery, state: FSMContext):
    p = await _pack_or_gone(cb, int(cb.data.split(":")[1]))
    if not p:
        return
    await state.set_state(S.deleting)
    await state.update_data(pid=p["id"], name=p["name"])
    await edit(cb, "Пришли стикер из этого пака, который надо удалить. Можно несколько подряд.",
               kb([btn("✅ Готово", f"p:{p['id']}")]))


@router.message(S.deleting, F.sticker)
async def got_delete(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    if m.sticker.set_name != d["name"]:
        return await m.answer("Этот стикер не из выбранного пака.")
    try:
        await tg_retry(lambda: bot.delete_sticker_from_set(sticker=m.sticker.file_id))
        await m.answer("🗑 Удалён.")
    except (TelegramBadRequest, UserError) as e:
        await m.answer(f"❌ Не вышло: {e}")


@router.callback_query(F.data.startswith("pkill:"))
async def cb_pkill(cb: CallbackQuery):
    p = await _pack_or_gone(cb, int(cb.data.split(":")[1]))
    if not p:
        return
    await edit(cb, f"Точно удалить пак «{html.escape(p['title'])}»? Это необратимо.",
               kb([btn("🗑 Да, удалить", f"pkilly:{p['id']}")], [btn("◀️ Нет", f"p:{p['id']}")]))


@router.callback_query(F.data.startswith("pkilly:"))
async def cb_pkilly(cb: CallbackQuery, bot: Bot):
    p = await _pack_or_gone(cb, int(cb.data.split(":")[1]))
    if not p:
        return
    try:
        await bot.delete_sticker_set(p["name"])
    except TelegramBadRequest:
        pass  # уже удалён
    await del_pack(p["id"])
    await edit(cb, "🗑 Пак удалён.", kb([btn("📂 Мои паки", "packs")]))


# ───────────────────────── Быстрый конвертер (вне диалога) ─────────────────────────
@router.message(StateFilter(None), F.sticker)
async def quick_sticker(m: Message, bot: Bot):
    s = m.sticker
    if s.is_animated:
        return await m.answer("Это анимированный TGS-стикер — его я не конвертирую.")
    buf = io.BytesIO()
    await bot.download(s, destination=buf)
    if s.is_video:
        return await m.answer_document(BufferedInputFile(buf.getvalue(), "sticker.webm"))
    out = io.BytesIO()
    Image.open(buf).convert("RGBA").save(out, "PNG")
    await m.answer_document(BufferedInputFile(out.getvalue(), "sticker.png"),
                            caption="🖼 Твой стикер в PNG\n\nСоздать пак — /new")


@router.message(StateFilter(None), F.photo)
async def quick_photo(m: Message, bot: Bot):
    user = await get_user(m.from_user.id)
    raw, _ = await download_media(bot, m)
    png = await asyncio.to_thread(process_static, raw, False, bool(user["border"]),
                                  bool(user["crop"]), bool(user["nobg"]))
    await m.answer_document(BufferedInputFile(png, "sticker.png"),
                            caption="✨ Готовый стикер 512×512\n\nСделать из таких пак — /new")


@router.message(StateFilter(None))
async def fallback(m: Message):
    await m.answer("Нажми кнопку ниже 👇", reply_markup=MAIN_KB)


# ───────────────────────── Запуск ─────────────────────────
async def main():
    await db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.me()
    logging.info("LumenStickers запущен: @%s", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
