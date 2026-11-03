"""Telegram bot: paste YouTube links to add them to Jellyfin.

* video    → written to the curated library as a single episode
* playlist → the whole playlist is mirrored and kept in sync
* channel  → appended to ytdlp2STRM's channel_list.json

Only ALLOWED_TELEGRAM_USER_IDS reach any handler; everyone else is dropped silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from .channel_list import ChannelListError, read_channels
from .config import Settings
from .ingest import Pipeline, SyncReport, build_pipeline
from .urls import Link, classify, extract_links
from .ytdlp import AuthRequired, VideoUnavailable, YtDlpError

log = logging.getLogger("jelly_yt.bot")

HELP = (
    "Send me YouTube links:\n"
    "• a video (watch?v=…, youtu.be/…, /shorts/…) → added on its own\n"
    "• a playlist (…list=PL…) → every video added, kept in sync\n"
    "• a channel (@handle, /channel/UC…) → handed to ytdlp2STRM\n\n"
    "Commands: /list, /remove <playlist link or id>, /sync"
)


def handle_link(pipeline: Pipeline, link: Link) -> tuple[str, list[SyncReport]]:
    """Blocking: process one link and return (reply text, reports for Jellyfin notification)."""
    try:
        if link.kind == "video":
            result = pipeline.add_video(link.value)
            report = SyncReport(link.value, name=result.show)
            report.record(result)
            if result.status == "added":
                return f"✅ Added “{result.title}” ({result.show})", [report]
            if result.status == "exists":
                return "ℹ️ That video is already in the library", []
            if result.status == "unavailable":
                return f"❌ Video unavailable: {result.message}", []
            return f"⏳ Can't add it yet ({result.message}); I'll keep trying", []

        if link.kind == "playlist":
            target, report = pipeline.track_playlist(link.value)
            text = f"📃 Playlist “{target.display_name}”: {len(report.added)} videos added"
            if report.existing:
                text += f", {report.existing} already there"
            if report.removed:
                text += f", {len(report.removed)} removed"
            if report.deferred:
                text += f", {len(report.deferred)} unavailable for now"
            return text + ". I'll keep it in sync.", [report]

        if link.kind == "channel":
            added = pipeline.add_channel(link.value)
            if not added:
                return "ℹ️ That channel is already in ytdlp2STRM's channel list", []
            return (
                f"📺 Added {link.value} to ytdlp2STRM's channel list.\n"
                "ytdlp2STRM reads that list at startup, so restart it to pick the channel up.",
                [],
            )
    except AuthRequired as exc:
        return f"🔒 YouTube refused the request (cookies expired or rate limited).\n{exc}", []
    except VideoUnavailable as exc:
        return f"❌ Not available: {exc}", []
    except YtDlpError as exc:
        return f"⚠️ yt-dlp error: {exc}", []
    except (ChannelListError, RuntimeError, OSError) as exc:
        return f"⚠️ Couldn't update ytdlp2STRM's channel list: {exc}", []
    return "🤷 Unsupported link", []


class Bot:
    def __init__(self, settings: Settings, pipeline: Pipeline):
        self.settings = settings
        self.pipeline = pipeline
        # Serialise pipeline work: yt-dlp + SQLite + filesystem writes for one request at a time.
        self._lock = asyncio.Lock()
        self._sync_task: asyncio.Task | None = None

    async def _run(self, func, *args):
        async with self._lock:
            return await asyncio.to_thread(func, *args)

    def _allowed(self, update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id in self.settings.allowed_telegram_user_ids

    # ------------------------------------------------------------------ handlers
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return  # defence in depth; handler filters already drop these
        message = update.effective_message
        links = extract_links(message.text or "")
        if not links:
            await message.reply_text("I couldn't find a YouTube video, playlist or channel link in that.")
            return
        for link in links:
            status = await message.reply_text(f"⏳ Working on {link.kind} {link.value}…")
            text, reports = await self._run(handle_link, self.pipeline, link)
            if reports and any(r.changed for r in reports):
                refreshed = await asyncio.to_thread(self.pipeline.notify, reports)
                text += "\n🔄 Jellyfin scan started" if refreshed else "\n(Jellyfin refresh not configured or failed)"
            await status.edit_text(text)

    async def list_targets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lines = [f"📃 {t.display_name} — {t.target_id}" for t in self.pipeline.db.list_targets("playlist")]
        if self.settings.channel_list_path:
            try:
                lines += [f"📺 {c}" for c in read_channels(self.settings.channel_list_path)]
            except ChannelListError as exc:
                lines.append(f"⚠️ {exc}")
        await update.effective_message.reply_text("\n".join(lines) if lines else "Nothing added yet.")

    async def remove_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.effective_message.reply_text("Usage: /remove <playlist link or id>")
            return
        link = classify(context.args[0])
        playlist_id = link.value if link and link.kind == "playlist" else context.args[0]
        removed = self.pipeline.db.remove_target(playlist_id)
        await update.effective_message.reply_text(
            "Stopped syncing that playlist. Its files are still in the library; delete the folder to remove it."
            if removed else "That isn't a playlist I'm syncing. Channels are removed in ytdlp2STRM."
        )

    async def sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        status = await update.effective_message.reply_text("⏳ Syncing playlists…")
        reports = await self._sync_and_notify()
        lines = [r.summary() for r in reports if r.changed or r.error]
        await status.edit_text("✅ Sync complete\n" + ("\n".join(lines) if lines else "No changes."))

    # ------------------------------------------------------------------ periodic sync
    async def _sync_and_notify(self) -> list[SyncReport]:
        reports = await self._run(self.pipeline.sync_all)
        if any(r.changed for r in reports):
            await asyncio.to_thread(self.pipeline.notify, reports)
        return reports

    async def _sync_loop(self) -> None:
        interval = max(5, self.settings.playlist_sync_interval_minutes) * 60
        while True:
            await asyncio.sleep(interval)
            try:
                reports = await self._sync_and_notify()
                for r in reports:
                    if r.changed or r.error:
                        log.info(r.summary())
            except Exception:
                log.exception("Periodic sync failed")

    async def _post_init(self, app: Application) -> None:
        self._sync_task = asyncio.create_task(self._sync_loop())

    async def _post_shutdown(self, app: Application) -> None:
        if self._sync_task:
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sync_task

    def build(self) -> Application:
        if not self.settings.telegram_bot_token or not self.settings.allowed_telegram_user_ids:
            raise SystemExit("TELEGRAM_BOT_TOKEN and ALLOWED_TELEGRAM_USER_IDS must be set")
        app = (
            ApplicationBuilder()
            .token(self.settings.telegram_bot_token)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        only_allowed = filters.User(user_id=self.settings.allowed_telegram_user_ids)
        app.add_handler(CommandHandler(["start", "help"], self.start, filters=only_allowed))
        app.add_handler(CommandHandler("list", self.list_targets, filters=only_allowed))
        app.add_handler(CommandHandler("remove", self.remove_target, filters=only_allowed))
        app.add_handler(CommandHandler("sync", self.sync, filters=only_allowed))
        app.add_handler(MessageHandler(only_allowed & filters.TEXT & ~filters.COMMAND, self.on_message))
        return app


def main() -> None:
    from .logging_setup import configure_logging

    configure_logging()
    settings = Settings.from_env()
    bot = Bot(settings, build_pipeline(settings))
    bot.build().run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
