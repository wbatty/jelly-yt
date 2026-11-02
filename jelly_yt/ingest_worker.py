"""Component B – feed ingestion worker.

    python -m jelly_yt.ingest_worker run            # loop every POLL_INTERVAL_MINUTES
    python -m jelly_yt.ingest_worker run --once     # single pass (for cron)
    python -m jelly_yt.ingest_worker add <url|@handle|UC…|PL…> [--folder NAME]
    python -m jelly_yt.ingest_worker list
    python -m jelly_yt.ingest_worker remove <target_id>
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from .config import Settings
from .ingest import Pipeline, SyncReport, build_pipeline
from .urls import classify
from .ytdlp import YtDlpError

log = logging.getLogger("jelly_yt.worker")


def run_once(pipeline: Pipeline) -> list[SyncReport]:
    started = time.perf_counter()
    calls_before = pipeline.ytdlp.calls
    reports = pipeline.poll_all()
    elapsed = time.perf_counter() - started
    for report in reports:
        if report.changed or report.error:
            log.info(report.summary())
    log.info(
        "Poll complete: %d targets, %d added, %d removed, %d errors, %d yt-dlp calls, %.1fs",
        len(reports),
        sum(len(r.added) for r in reports),
        sum(len(r.removed) for r in reports),
        sum(1 for r in reports if r.error),
        pipeline.ytdlp.calls - calls_before,
        elapsed,
    )
    return reports


def cmd_run(pipeline: Pipeline, settings: Settings, once: bool) -> int:
    if once:
        run_once(pipeline)
        return 0
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    interval = max(1, settings.poll_interval_minutes) * 60
    while not stop.is_set():
        try:
            run_once(pipeline)
        except Exception:
            log.exception("Poll run failed")
        stop.wait(interval)
    return 0


def cmd_add(pipeline: Pipeline, value: str, folder: str | None) -> int:
    link = classify(value)
    if link is None:
        print(f"Unrecognised YouTube link: {value}", file=sys.stderr)
        return 2
    try:
        if link.kind == "channel":
            target, report = pipeline.track_channel(link.value, custom_folder=folder)
        elif link.kind == "playlist":
            target, report = pipeline.track_playlist(link.value, custom_folder=folder)
        else:
            result = pipeline.ingest_channel_video(link.value)
            report = SyncReport(link.value, name=result.show or result.title)
            report.record(result)
            print(f"{result.status}: {result.title or link.value} {result.message}".strip())
            pipeline.notify(report)
            return 0 if result.ok else 1
    except YtDlpError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    print(f"Tracking {target.target_type} {target.display_name} ({target.target_id}) – {report.summary()}")
    pipeline.notify(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    from .logging_setup import configure_logging

    parser = argparse.ArgumentParser(prog="jelly-yt-worker", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="poll all tracked targets")
    run.add_argument("--once", action="store_true", help="single pass then exit (cron mode)")
    add = sub.add_parser("add", help="track a channel/playlist or ingest a single video")
    add.add_argument("value")
    add.add_argument("--folder", help="custom show folder name")
    sub.add_parser("list", help="list tracked targets")
    rm = sub.add_parser("remove", help="stop tracking a target (files are kept)")
    rm.add_argument("target_id")
    args = parser.parse_args(argv)

    configure_logging()
    settings = Settings.from_env()
    pipeline = build_pipeline(settings)
    command = args.command or "run"
    try:
        if command == "run":
            return cmd_run(pipeline, settings, getattr(args, "once", False))
        if command == "add":
            return cmd_add(pipeline, args.value, args.folder)
        if command == "list":
            for t in pipeline.db.list_targets():
                print(f"{t.target_type:8} {t.target_id:34} {t.display_name}"
                      + (f"  -> {t.custom_folder}" if t.custom_folder else ""))
            return 0
        if command == "remove":
            ok = pipeline.db.remove_target(args.target_id)
            print("removed" if ok else "not tracked")
            return 0 if ok else 1
    finally:
        pipeline.ytdlp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
