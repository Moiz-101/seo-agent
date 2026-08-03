"""Telegram entrypoint for the SEO agent.

Flow per website:
  /newsite <url>
      -> crawls the whole site, runs a technical/on-page/speed/authority
         audit, and sends an agency-style PDF report with approval buttons.
         Does NOT create any content or make changes - it only looks.
  Approve Audit / Request Revision / Scratch Start / Pause (buttons, or the
  same phrases typed as text)
      -> Scratch Start queues the site's *existing* pages (worst issues
         first) for content updates - never invents new pages/topics.
      -> Request Revision asks for free-text notes on what to redo.
      -> Pause freezes the site until you type "resume".
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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import config
from seo_agent import pipeline
from seo_agent.storage import state_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _authorized(update: Update) -> bool:
    if not config.TELEGRAM_ALLOWED_USER_ID:
        return True
    user = update.effective_user
    return bool(user) and str(user.id) == str(config.TELEGRAM_ALLOWED_USER_ID)


def _audit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Approve Audit", callback_data="approve_audit"), InlineKeyboardButton("Request Revision", callback_data="request_revision")],
            [InlineKeyboardButton("Scratch Start", callback_data="scratch_start"), InlineKeyboardButton("Pause", callback_data="pause")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(f"Aapki Telegram User ID: {update.effective_user.id}")
    await update.message.reply_text(
        "SEO Agent ready.\n\n"
        "Naya site audit karne ke liye:\n"
        "/newsite <url>\n\n"
        "Audit PDF ke saath buttons milenge: Approve Audit, Request Revision, Scratch Start, Pause "
        "(ya wahi text mein bhi likh sakte ho).\n"
        "Doc developer ko de diya? Reply karo: sent\n"
        "Developer ne live update kar diya? Reply karo: go ahead\n"
        "Pause kiya hua site wapas shuru karne ke liye: resume\n"
        "/status - current progress dekhne ke liye\n"
        "/stop <url> - kaam rokna (permanently)"
    )


async def newsite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Format: /newsite https://example.com")
        return
    url = " ".join(context.args).strip()

    await update.message.reply_text(f"Poori site audit kar raha hoon: {url} ...\n(bade sites mein kuch minute lag sakte hain)")
    try:
        result = await asyncio.to_thread(pipeline.start_new_site, url)
    except Exception as e:
        logger.exception("Audit failed for %s", url)
        await update.message.reply_text(f"Audit karte waqt error aaya: {e}")
        return

    site, pdf_path = result["site"], result["pdf_path"]
    stats = site.get("crawl_stats", {})
    await update.message.reply_document(
        document=open(pdf_path, "rb"),
        caption=(
            f"Audit complete: {stats.get('total_pages_crawled', 0)} pages crawled.\n\n"
            "Report review karo aur neeche button dabao (ya text likho)."
        ),
        reply_markup=_audit_keyboard(),
    )


async def _generate_and_send_next(reply_target, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    try:
        result = await asyncio.to_thread(pipeline.generate_next_content_doc, url)
    except Exception as e:
        logger.exception("Content generation failed for %s", url)
        await reply_target.reply_text(f"Article generate karte waqt error aaya: {e}")
        return

    if result is None:
        state_store.update_site(url, stage="MONITORING")
        await reply_target.reply_text("Saara planned content bhej diya gaya hai. Ab sirf ranking monitor karunga.")
        return

    site, docx_path = result
    await reply_target.reply_document(
        document=open(docx_path, "rb"),
        caption=f"Content ready for: {site['published_topics'][-1]}\n\nDeveloper ko de dena, phir 'sent' likh dena.",
    )


async def _start_scratch(reply_target, context: ContextTypes.DEFAULT_TYPE, url: str, current_stage: str) -> None:
    if current_stage != "AUDIT_READY":
        await reply_target.reply_text(f"'{url}' abhi '{current_stage}' stage mein hai, audit poora hone ka wait karo pehle.")
        return

    await reply_target.reply_text("Existing pages ka kaam shuru kar raha hoon (site dobara crawl kar raha hoon)...")
    try:
        site = await asyncio.to_thread(pipeline.start_scratch, url)
    except Exception as e:
        logger.exception("Scratch start failed for %s", url)
        await reply_target.reply_text(f"Scratch start karte waqt error aaya: {e}")
        return

    await reply_target.reply_text(
        f"{len(site['content_queue'])} pages queue mein hain (jo sabse zyada issues wale hain unhe pehle liya hai).\n"
        "Pehle page ka content bana raha hoon..."
    )
    await _generate_and_send_next(reply_target, context, url)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _authorized(update):
        await query.answer()
        return
    await query.answer()

    active_site = state_store.get_active_site_for_user()
    if not active_site:
        await query.message.reply_text("Koi active site nahi mila.")
        return
    url = active_site["url"]

    if query.data == "approve_audit":
        state_store.update_site(url, audit_approved=True)
        await query.message.reply_text(f"Audit approve ho gaya: {url}. Shuru karne ke liye 'Scratch Start' bolo.")
        return

    if query.data == "request_revision":
        state_store.update_site(url, awaiting_revision_input=True)
        await query.message.reply_text("Kya revise karna hai, likh kar bhej do.")
        return

    if query.data == "scratch_start":
        await _start_scratch(query.message, context, url, active_site["stage"])
        return

    if query.data == "pause":
        if active_site["stage"] == "PAUSED":
            await query.message.reply_text(f"'{url}' pehle se paused hai.")
            return
        state_store.update_site(url, stage="PAUSED", paused_from_stage=active_site["stage"])
        await query.message.reply_text(f"'{url}' pause kar diya. Resume karne ke liye 'resume' likho.")
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text = update.message.text.strip()
    text_lower = text.lower()
    active_site = state_store.get_active_site_for_user()
    if not active_site:
        await update.message.reply_text("Koi active site nahi mila. Pehle /newsite se shuru karo.")
        return

    url = active_site["url"]

    if active_site.get("awaiting_revision_input"):
        state_store.update_site(url, awaiting_revision_input=False, revision_notes=text)
        await update.message.reply_text(
            "Revision note save ho gaya. Jab dobara audit chalana ho, /newsite se re-run kar dena "
            "(ya 'Scratch Start' se aage badho agar ye chhoti si baat thi)."
        )
        return

    if text_lower == "resume":
        if active_site["stage"] != "PAUSED":
            await update.message.reply_text(f"'{url}' paused nahi hai.")
            return
        restored_stage = active_site.get("paused_from_stage", "AUDIT_READY")
        state_store.update_site(url, stage=restored_stage)
        await update.message.reply_text(f"'{url}' resume ho gaya, stage: {restored_stage}")
        return

    if text_lower == "pause":
        if active_site["stage"] == "PAUSED":
            await update.message.reply_text(f"'{url}' pehle se paused hai.")
            return
        state_store.update_site(url, stage="PAUSED", paused_from_stage=active_site["stage"])
        await update.message.reply_text(f"'{url}' pause kar diya. Resume karne ke liye 'resume' likho.")
        return

    if text_lower == "approve audit":
        state_store.update_site(url, audit_approved=True)
        await update.message.reply_text(f"Audit approve ho gaya: {url}. Shuru karne ke liye 'Scratch Start' bolo.")
        return

    if text_lower == "request revision":
        state_store.update_site(url, awaiting_revision_input=True)
        await update.message.reply_text("Kya revise karna hai, likh kar bhej do.")
        return

    if text_lower == "scratch start":
        await _start_scratch(update.message, context, url, active_site["stage"])
        return

    if text_lower in ("sent", "sent to dev", "developer ko de diya"):
        pipeline.mark_sent_to_dev(url)
        await update.message.reply_text("Theek hai, developer ke update ka wait kar raha hoon. Jab live ho jaye, 'go ahead' likh dena.")
        return

    if "go ahead" in text_lower or text_lower in ("go", "done", "live ho gaya"):
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
            await _generate_and_send_next(update.message, context, url)
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
    app.add_handler(CallbackQueryHandler(handle_callback))
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
