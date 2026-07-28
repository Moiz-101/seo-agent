"""Telegram entrypoint for the SEO agent.

Flow per website:
  /newsite <url> | <topic1, topic2, ...>
      -> runs research, generates first article, sends .docx on Telegram
  "sent" (reply after receiving a doc)
      -> marks it as handed to the developer, bot now waits
  "go ahead" (once the developer has published the update live)
      -> runs a technical/on-page audit of the live page, sends a report,
         then generates + sends the next article in the queue (loop)
  /status
      -> shows current stage of the active site
  /stop <url>
      -> stops the agent working on that site
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import config
from seo_agent import pipeline
from seo_agent.storage import state_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _authorized(update: Update) -> bool:
    if not config.TELEGRAM_ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(config.TELEGRAM_ALLOWED_USER_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(f"Aapki Telegram User ID: {update.effective_user.id}")
    await update.message.reply_text(
        "SEO Agent ready.\n\n"
        "Naya site shuru karne ke liye:\n"
        "/newsite <url> | <seed topic 1, seed topic 2>\n\n"
        "Doc developer ko de diya? Reply karo: sent\n"
        "Developer ne live update kar diya? Reply karo: go ahead\n"
        "/status - current progress dekhne ke liye\n"
        "/stop <url> - kaam rokne ke liye"
    )


async def newsite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text = " ".join(context.args)
    if "|" not in text:
        await update.message.reply_text("Format: /newsite https://example.com | topic one, topic two")
        return

    url_part, topics_part = text.split("|", 1)
    url = url_part.strip()
    seed_topics = [t.strip() for t in topics_part.split(",") if t.strip()]

    await update.message.reply_text(f"Research shuru kar raha hoon: {url} ...")
    try:
        site = await asyncio.to_thread(pipeline.start_new_site, url, seed_topics)
    except Exception as e:
        logger.exception("Research failed for %s", url)
        await update.message.reply_text(f"Research karte waqt error aaya: {e}")
        return

    await update.message.reply_text(
        f"Research complete.\nKeywords found: {len(site['keywords'])}\n"
        f"Competitors: {', '.join(site['competitors']) or 'none found'}\n"
        f"Content pieces planned: {len(site['content_queue'])}\n\nPehla article likh raha hoon..."
    )

    await _generate_and_send_next(update, context, url)


async def _generate_and_send_next(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    try:
        result = await asyncio.to_thread(pipeline.generate_next_content_doc, url)
    except Exception as e:
        logger.exception("Content generation failed for %s", url)
        await update.message.reply_text(f"Article generate karte waqt error aaya: {e}")
        return

    if result is None:
        state_store.update_site(url, stage="MONITORING")
        await update.message.reply_text("Saara planned content bhej diya gaya hai. Ab sirf ranking monitor karunga.")
        return

    site, docx_path = result
    await update.message.reply_document(
        document=open(docx_path, "rb"),
        caption=f"Article ready: {site['published_topics'][-1]}\n\nDeveloper ko de dena, phir 'sent' likh dena.",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text = update.message.text.strip().lower()
    active_site = state_store.get_active_site_for_user()
    if not active_site:
        await update.message.reply_text("Koi active site nahi mila. Pehle /newsite se shuru karo.")
        return

    url = active_site["url"]

    if text in ("sent", "sent to dev", "developer ko de diya"):
        pipeline.mark_sent_to_dev(url)
        await update.message.reply_text("Theek hai, developer ke update ka wait kar raha hoon. Jab live ho jaye, 'go ahead' likh dena.")
        return

    if "go ahead" in text or text in ("go", "done", "live ho gaya"):
        await update.message.reply_text("Live page check kar raha hoon aur agla step shuru kar raha hoon...")
        try:
            result = await asyncio.to_thread(pipeline.handle_go_ahead, url)
        except Exception as e:
            logger.exception("Technical audit failed for %s", url)
            await update.message.reply_text(f"Technical audit karte waqt error aaya: {e}")
            return

        await update.message.reply_document(
            document=open(result["report_path"], "rb"),
            caption="Technical SEO report (is update ke baad)",
        )

        site = result["site"]
        if site["stage"] == "MONITORING":
            await update.message.reply_text("Content queue khatam. Ab main periodically rankings monitor karunga.")
        else:
            await update.message.reply_text("Agla article likh raha hoon...")
            await _generate_and_send_next(update, context, url)
        return

    await update.message.reply_text("Samajh nahi aaya. 'sent' ya 'go ahead' likho, ya /status check karo.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    sites = state_store.list_sites()
    if not sites:
        await update.message.reply_text("Koi site track nahi ho rahi.")
        return

    lines = [f"{s['url']} -> {s['stage']} (published: {len(s['published_topics'])}, queued: {len(s['content_queue'])})" for s in sites]
    await update.message.reply_text("\n".join(lines))


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Format: /stop https://example.com")
        return
    url = context.args[0]
    state_store.stop_site(url)
    await update.message.reply_text(f"{url} par kaam rok diya gaya.")


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN .env mein set karo")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newsite", newsite))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if config.WEBHOOK_URL:
        logger.info("Bot starting in webhook mode on port %s...", config.PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=config.PORT,
            url_path=config.TELEGRAM_BOT_TOKEN,
            webhook_url=f"{config.WEBHOOK_URL}/{config.TELEGRAM_BOT_TOKEN}",
        )
    else:
        logger.info("Bot starting in polling mode (local dev)...")
        app.run_polling()


if __name__ == "__main__":
    main()
