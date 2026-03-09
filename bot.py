import html
import logging
import os
import time
from datetime import datetime, timedelta, timezone
import requests
from openai import OpenAI
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
NEWS_API_KEY = os.environ["NEWS_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

openai_client = OpenAI(api_key=OPENAI_API_KEY)

DOMAINS = ",".join([
    "straitstimes.com",
    "channelnewsasia.com",
    "bbc.com",
    "theguardian.com",
    "nytimes.com",
    "washingtonpost.com",
])

FALLBACK_TOPICS = [
    "Artificial Intelligence", "Bitcoin", "Climate Change", "Ukraine",
    "Space", "Tech", "US Politics", "Health",
]

# Cache: (topics list, timestamp)
_trending_cache: tuple[list[str], float] = ([], 0)
CACHE_TTL = 1800  # 30 minutes


def fetch_trending_topics() -> list[str]:
    global _trending_cache
    topics, ts = _trending_cache
    if topics and time.time() - ts < CACHE_TTL:
        return topics

    try:
        resp = requests.get(
            "https://newsapi.org/v2/top-headlines",
            params={
                "country": "us",
                "pageSize": 20,
                "apiKey": NEWS_API_KEY,
            },
            timeout=10,
        )
        resp.raise_for_status()
        headlines = [
            a["title"] for a in resp.json().get("articles", [])
            if a.get("title") and a["title"] != "[Removed]"
        ]

        if headlines:
            prompt = (
                "Here are today's top news headlines:\n\n"
                + "\n".join(f"- {h}" for h in headlines)
                + "\n\nExtract exactly 8 short topic labels (2–3 words each) that capture "
                "the distinct stories. Return only a JSON array of strings, e.g. "
                '["Iran strikes", "Trump tariffs", "Gaza ceasefire"]'
            )
            result = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            import json
            topics = json.loads(result.choices[0].message.content.strip())[:8]
            if topics:
                _trending_cache = (topics, time.time())
                logger.info("Trending topics refreshed: %s", topics)
                return topics
    except Exception as e:
        logger.warning("Could not fetch trending topics: %s", e)

    return FALLBACK_TOPICS


def topic_keyboard() -> InlineKeyboardMarkup:
    topics = fetch_trending_topics()
    buttons = [
        InlineKeyboardButton(t, callback_data=f"topic:{t}")
        for t in topics
    ]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def fetch_news(topic: str) -> list[dict]:
    from_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    for q in (f'"{topic}"', topic):
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": q,
                "domains": DOMAINS,
                "pageSize": 5,
                "sortBy": "publishedAt",
                "from": from_date,
                "language": "en",
                "apiKey": NEWS_API_KEY,
            },
            timeout=10,
        )
        resp.raise_for_status()
        articles = [
            a for a in resp.json().get("articles", [])
            if a.get("title") != "[Removed]"
        ]
        if articles:
            return articles
    return []


def summarize_articles(topic: str, articles: list[dict]) -> str:
    snippets = []
    for a in articles:
        title = a.get("title") or ""
        desc = a.get("description") or ""
        snippets.append(f"- {title}. {desc}")

    prompt = (
        f'Summarize the latest news on "{topic}" based on these articles:\n\n'
        + "\n".join(snippets)
        + "\n\nIf the articles cover multiple distinct sub-topics, write one bullet point "
        "per sub-topic (use • as the bullet). If they all cover the same story, write "
        "2–4 sentences as a single paragraph. Keep it concise. Plain text only, no markdown."
    )
    result = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return result.choices[0].message.content.strip()


def format_digest(topic: str, articles: list[dict], summary: str) -> str:
    lines = [f"<b>News digest: {html.escape(topic)}</b>\n"]
    lines.append(f"{html.escape(summary)}\n")
    lines.append("<b>Sources:</b>")

    for i, article in enumerate(articles, 1):
        title = html.escape(article.get("title") or "No title")
        source = html.escape(article.get("source", {}).get("name") or "Unknown")
        url = article.get("url") or ""
        lines.append(f'{i}. <a href="{url}">{title}</a> — <i>{source}</i>')

    lines.append("")
    lines.append("─────────────────────")
    lines.append("Pick another topic or type your own:")
    return "\n".join(lines)


async def send_digest(topic: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = update.message.reply_text if update.message else update.callback_query.message.reply_text

    await reply(f"Fetching news for <b>{html.escape(topic)}</b>…", parse_mode="HTML")

    try:
        articles = fetch_news(topic)
        if not articles:
            digest = f"No news found for <b>{html.escape(topic)}</b> in the last 7 days."
        else:
            summary = summarize_articles(topic, articles)
            digest = format_digest(topic, articles, summary)
    except requests.HTTPError as e:
        logger.error("NewsAPI error: %s", e)
        digest = "Failed to fetch news (API error). Check your NEWS_API_KEY."
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        digest = f"Something went wrong: {html.escape(str(e))}"

    await reply(digest, parse_mode="HTML", disable_web_page_preview=True, reply_markup=topic_keyboard())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to <b>TLDRNews</b>!\n\n"
        "Send me any topic and I'll fetch the latest news digest.\n\n"
        "Here's what's trending right now — pick one or type your own:",
        parse_mode="HTML",
        reply_markup=topic_keyboard(),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    topic = update.message.text.strip()
    if topic:
        await send_digest(topic, update, context)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    topic = query.data.removeprefix("topic:")
    await send_digest(topic, update, context)


def main() -> None:
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button, pattern="^topic:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started — polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
