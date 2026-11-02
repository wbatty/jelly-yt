"""Component C – Telegram ingest bot (python-telegram-bot v20+, long polling).

Only messages from ALLOWED_TELEGRAM_USER_ID reach any handler; everything else
is dropped silently by the handler filters.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .ingest import Pipeline, SyncReport, build_pipeline
from .urls import Link, extract_links
from .ytdlp import AuthRequired, VideoUnavailable, YtDlpError

log = logging.getLogger("jelly_yt.bot")

HELP = (
    "Send me YouTube links:\n"
    "• a video (watch?v=…, youtu.be/…, /shorts/…) → added to its channel folder\n"
    "• a playlist (…list=PL…, list=WL for Watch Later) → mirrored and kept in sync\n"
    "• a channel (@handle, /channel/UC…) → tracked, newest uploads backfilled\n\n"
    "Commands: /list, /remove <id>, /sync"
)


def handle_link(pipeline: Pipeline, link: Link) -> tuple[str, list[SyncReport]]:
    """Blocking: process one link and return (reply text, reports for Jellyfin notification)."""
    try:
        if link.kind == "video":
            result = pipeline.ingest_channel_video(link.value)
            report = SyncReport(link.value, name=result.show)
            report.record(result)
            if result.status == "added":
                return f"✅ Added “{result.title}” to {result.show}", [report]
            if result.status == "exists":
                return "ℹ️ That video is already in the library", []
            if result.status == "unavailable":
                return f"❌ Video unavailable: {result.message}", []
            return f"⏳ Couldn't add {link.value} yet ({result.message}); will retry on next sync", []

        if link.kind == "playlist":
            target, report = pipeline.track_playlist(link.value)
            text = f"📃 Playlist “{target.display_name}” synced: {len(report.added)} added"
            if report.removed:
                text += f", {len(report.removed)} removed"
            if report.existing:
                text += f", {report.existing} already present"
            if report.deferred:
                text += f", {len(report.deferred)} skipped"
            return text, [report]

        if link.kind == "channel":
            target, report = pipeline.track_channel(link.value)
            text = f"📺 Tracking “{target.display_name}”: {len(report.added)} recent videos added"
            if report.deferred:
                text += f", {len(report.deferred)} skipped"
            if report.error:
                text += f" (backfill error: {report.error})"
            return text, [report]
    except AuthRequired as exc:
        return f"🔒 YouTube refused the request – cookies expired or rate limited.\n{exc}", []
    except VideoUnavailable as exc:
        return f"❌ Not available: {exc}", []
    except YtDlpError as exc:
        return f"⚠️ yt-dlp error: {exc}", []
    return "🤷 Unsupported link", []


class Bot:
    def __init__(self, settings: Settings, pipeline: Pipeline):
        self.settings = settings
        self.pipeline = pipeline
        # Serialise pipeline work: yt-dlp + SQLite + filesystem writes for one request at a time.
        self._lock = asyncio.Lock()

    async def _run(self, func, *args):
        async with self._lock:
            return await asyncio.to_thread(func, *args)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if update.effective_user is None or update.effective_user.id != self.settings.allowed_telegram_user_id:
            return  # defence in depth; filters already drop these
        links = extract_links(message.text or "")
        if not links:
            await message.reply_text("I couldn't find a YouTube video, playlist or channel link in that.")
            return
        for link in links:
            status = await message.reply_text(f"⏳ Working on {link.kind} {link.value}…")
            text, reports = await self._run(handle_link, self.pipeline, link)
            if reports and any(r.changed for r in reports):
                refreshed = await asyncio.to_thread(self.pipeline.notify, reports)
                text += "\n🔄 Jellyfin scan triggered" if refreshed else "\n(Jellyfin refresh not configured/failed)"
            await status.edit_text(text)

    async def list_targets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        targets = self.pipeline.db.list_targets()
        if not targets:
            await update.effective_message.reply_text("Nothing tracked yet.")
            return
        lines = [f"{'📺' if t.target_type == 'channel' else '📃'} {t.display_name} — {t.target_id}" for t in targets]
        await update.effective_message.reply_text("\n".join(lines))

    async def remove_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.effective_message.reply_text("Usage: /remove <channel or playlist id>")
            return
        removed = self.pipeline.db.remove_target(context.args[0])
        await update.effective_message.reply_text(
            "Stopped tracking (existing files kept)." if removed else "No such tracked target."
        )

    async def sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        status = await update.effective_message.reply_text("⏳ Syncing all tracked targets…")
        reports = await self._run(self.pipeline.poll_all)
        changed = [r.summary() for r in reports if r.changed or r.error]
        await status.edit_text("✅ Sync complete\n" + ("\n".join(changed) if changed else "No changes."))

    def build(self) -> Application:
        if not self.settings.telegram_bot_token or self.settings.allowed_telegram_user_id is None:
            raise SystemExit("TELEGRAM_BOT_TOKEN and ALLOWED_TELEGRAM_USER_ID must be set")
        app = ApplicationBuilder().token(self.settings.telegram_bot_token).build()
        only_owner = filters.User(user_id=self.settings.allowed_telegram_user_id)
        app.add_handler(CommandHandler(["start", "help"], self.start, filters=only_owner))
        app.add_handler(CommandHandler("list", self.list_targets, filters=only_owner))
        app.add_handler(CommandHandler("remove", self.remove_target, filters=only_owner))
        app.add_handler(CommandHandler("sync", self.sync, filters=only_owner))
        app.add_handler(MessageHandler(only_owner & filters.TEXT & ~filters.COMMAND, self.on_message))
        return app


def main() -> None:
    from .logging_setup import configure_logging

    configure_logging()
    settings = Settings.from_env()
    bot = Bot(settings, build_pipeline(settings))
    bot.build().run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
