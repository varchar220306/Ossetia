import logging
import os
import random
import re
import requests
from urllib.parse import urlparse, urljoin
import feedparser
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from datetime import datetime, timedelta
from time import mktime

# ── Настройки ───────────────────────────────────────────────
BOT_TOKEN = "8197963395:AAFj_KzRxjfoe7CbLh_aRJq_L4zek1S0h_4"
CHANNEL = "@osetia_lenta"

SOURCES = [
    {"name": "15-й РЕГИОН",       "url": "https://region15.ru/rss/",          "allow_media": True},
    {"name": "Алания ТВ",          "url": "https://alaniatv.ru/novosti/feed/", "allow_media": True},
    {"name": "Bezformata Топ",     "url": "https://vladikavkaz.bezformata.com/rsstop.xml", "allow_media": False},
]

DB = "posted.txt"
INTERVAL = 900           # 15 минут
MAX_POSTS_PER_RUN = 1
ACTUALITY_HOURS = 48     # не старше 2 суток

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-5s | %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ── Утилиты ─────────────────────────────────────────────────
def normalize(url):
    try: p = urlparse(url); return f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}".lower()
    except: return url.lower().strip()

def load_posted():
    return set(line.strip() for line in open(DB, encoding='utf-8')) if os.path.exists(DB) else set()

def save_posted(link):
    with open(DB, "a", encoding="utf-8") as f: f.write(link + "\n")

def clean_text(html):
    if not html: return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "iframe"]): tag.decompose()
    for p in soup.find_all("p"): p.replace_with(p.get_text(strip=True) + "\n\n")
    return re.sub(r'\n{3,}', '\n\n', soup.get_text(separator="\n").strip())

def highlight(text):
    for word in ["Владикавказ", "Северная Осетия", "Алания", "Осетия", "ДТП"]:
        text = re.sub(rf"(?i)\b{re.escape(word)}\b", r"<b>\g<0></b>", text)
    return text

def smart_truncate(text, threshold=100):
    if len(text) <= threshold: return text
    pos = text.find(".", threshold)
    return text[:pos + 1] if pos != -1 else text[:threshold]

def extract_text(entry):
    for field in [entry.get("description", ""), entry.get("summary", ""), entry.get("content", [{}])[0].get("value", "")]:
        cleaned = clean_text(field)
        if len(cleaned.strip()) > 30: return cleaned
    return ""

def find_media(entry):
    if hasattr(entry, "media_content") and entry.media_content:
        for m in entry.media_content:
            url = m.get("url")
            if url:
                if re.search(r"\.(mp4|m4v|mov|webm)$", url, re.I): return {"type": "video", "url": url}
                if re.search(r"\.(jpe?g|png|webp|gif)$", url, re.I): return {"type": "photo", "url": url}

    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            url = enc.get("href") or enc.get("url")
            if url:
                if re.search(r"\.(mp4|m4v|mov|webm)$", url, re.I): return {"type": "video", "url": url}
                if re.search(r"\.(jpe?g|png|webp|gif)$", url, re.I): return {"type": "photo", "url": url}

    link = getattr(entry, "link", None)
    if link:
        try:
            r = requests.get(link, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            candidates = soup.select(".entry-content img, .post-thumbnail img, article img, img.size-full, img.wp-post-image")
            for img in candidates:
                src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
                if src and re.search(r"\.(jpe?g|png|webp)$", src, re.I):
                    if "logo" not in src.lower() and "avatar" not in src.lower():
                        return {"type": "photo", "url": urljoin(link, src)}
        except:
            pass

    return None

def prepare_post(entry, source_name):
    title = (entry.title or "Без заголовка").strip()
    text = extract_text(entry)
    text = highlight(text)
    preview = smart_truncate(text)

    emoji = random.choice("📰📢🔥⚡🏔️🚨📍✨🎥")
    if any(w in title.lower() for w in ["дтп", "авария", "происшествие"]):
        emoji = "🚨"

    message = (
        f"{emoji} <b>{title}</b>\n\n"
        f"{preview}\n\n"
        f"<i>Источник: {source_name}</i>\n"
        f"<b>@osetia_lenta</b>\n\n"
        f"#Владикавказ #Осетия #Новости"
    )

    return message, title

def get_entry_date(entry):
    for field in ['published_parsed', 'updated_parsed', 'created_parsed']:
        parsed = getattr(entry, field, None)
        if parsed:
            try: return datetime.fromtimestamp(mktime(parsed))
            except: pass
    return datetime.now()

# ── Основная логика ─────────────────────────────────────────
async def check_feeds(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if now.hour < 7 or now.hour > 23:
        logger.info("Ночное время — пропускаем цикл")
        return

    posted = load_posted()
    all_new_entries = []

    for source in SOURCES:
        try:
            feed = feedparser.parse(requests.get(source["url"], timeout=12).content)

            for entry in feed.entries:
                link = getattr(entry, "link", None)
                if not link: continue

                norm_link = normalize(link)
                if norm_link in posted: continue

                pub_date = get_entry_date(entry)
                if pub_date < now - timedelta(hours=ACTUALITY_HOURS):
                    continue

                all_new_entries.append((pub_date, entry, source))

        except Exception as e:
            logger.error(f"Ошибка источника {source['name']}: {e}")

    all_new_entries.sort(key=lambda x: x[0], reverse=True)

    posted_count = 0
    for pub_date, entry, source in all_new_entries:
        if posted_count >= MAX_POSTS_PER_RUN:
            break

        text, title = prepare_post(entry, source["name"])
        caption = text if len(text) <= 1024 else text[:1010] + "…"

        media = find_media(entry) if source.get("allow_media", False) else None

        try:
            if media and media["type"] == "video":
                with requests.get(media["url"], timeout=30, headers={"User-Agent": "Mozilla/5.0"}, stream=True) as r:
                    r.raise_for_status()
                    await context.bot.send_video(CHANNEL, r.raw, caption=caption, parse_mode="HTML", supports_streaming=True)
            elif media and media["type"] == "photo":
                with requests.get(media["url"], timeout=25, headers={"User-Agent": "Mozilla/5.0"}, stream=True) as r:
                    r.raise_for_status()
                    await context.bot.send_photo(CHANNEL, r.raw, caption=caption, parse_mode="HTML")
            else:
                await context.bot.send_message(CHANNEL, caption, parse_mode="HTML")

            posted.add(normalize(entry.link))
            save_posted(normalize(entry.link))
            posted_count += 1
            logger.info(f"[{source['name']}] Опубликовано: {title[:60]}... ({media['type'] if media else 'text'}) → {pub_date}")

        except Exception as e:
            logger.error(f"Ошибка публикации {source['name']}: {e}")
            await context.bot.send_message(CHANNEL, caption, parse_mode="HTML")

    if posted_count == 0:
        logger.info("Нет свежих постов в цикле")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен • 1 пост / 15 мин")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))

    # Проверяем наличие JobQueue (на случай, если пакет не установлен полностью)
    if app.job_queue is None:
        logger.error("JobQueue не доступен! Убедитесь, что установлен python-telegram-bot[job-queue]")
        # Fallback: простой цикл без job_queue (для теста)
        import asyncio
        async def loop():
            while True:
                await check_feeds(None)
                await asyncio.sleep(INTERVAL)
        asyncio.run(loop())
    else:
        app.job_queue.run_repeating(check_feeds, interval=INTERVAL, first=10)

    logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
