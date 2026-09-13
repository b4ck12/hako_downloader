"""
Database Skeleton / Seed Generator
==================================
Creates a lightweight, index-only SQLite database (~65MB) from the full local database.
It preserves all novel metadata, volumes, and chapter URLs with crawl_status='completed'
while excluding heavy chapter text/HTML bodies. This allows GitHub Actions to recognize
existing chapters and only crawl newly published daily updates.
"""

import os
import sqlite3
import time
from pathlib import Path
from typing import Tuple
from ..utils.logger import get_logger, console

log = get_logger("skeleton")


def generate_skeleton_db(src_db_path: Path, dst_db_path: Path) -> Tuple[int, int, float]:
    """
    Extracts catalog skeleton from src_db_path and writes compact database to dst_db_path.
    Returns: (src_size_bytes, dst_size_bytes, elapsed_seconds)
    """
    src_path = Path(src_db_path).resolve()
    dst_path = Path(dst_db_path).resolve()

    if not src_path.exists():
        raise FileNotFoundError(f"Source database not found: {src_path}")

    start_time = time.monotonic()
    src_size = src_path.stat().st_size

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        try:
            dst_path.unlink()
        except Exception:
            pass

    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)

    try:
        # 1. Recreate schema (skip internal sqlite_% tables)
        cur = src.cursor()
        cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")
        tables = cur.fetchall()
        for name, sql in tables:
            dst.execute(sql)

        cur.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")
        indices = cur.fetchall()
        for name, sql in indices:
            # Skip idx_chapters_url: chapters.url already has UNIQUE constraint which SQLite automatically indexes
            if name == "idx_chapters_url":
                continue
            try:
                dst.execute(sql)
            except Exception:
                pass

        # 2. Copy all novels
        novels = src.execute(
            """
            SELECT id, url, slug, title, alternative_titles, author, artist,
                   status, novel_type, cover_url, cover_local_path, summary,
                   genres, total_words, views, likes, bookmarks, rating,
                   rating_count, site_last_updated, crawl_status, error_message,
                   created_at, updated_at
            FROM novels
            """
        ).fetchall()
        dst.executemany(
            "INSERT INTO novels VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            novels,
        )

        # 3. Copy all volumes
        volumes = src.execute(
            "SELECT id, novel_id, vol_index, title, url, created_at FROM volumes"
        ).fetchall()
        dst.executemany("INSERT INTO volumes VALUES (?,?,?,?,?,?)", volumes)

        # 4. Copy chapters metadata (slim: omit heavy text_content & html_content)
        chapters = src.execute(
            """
            SELECT id, novel_id, volume_id, chapter_index, title, url,
                   0, "", NULL, NULL, "[]", "completed", NULL, "", ""
            FROM chapters
            """
        ).fetchall()
        dst.executemany(
            "INSERT INTO chapters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            chapters,
        )

        dst.commit()
        dst.execute("VACUUM")

    finally:
        dst.close()
        src.close()

    dst_size = dst_path.stat().st_size
    elapsed = time.monotonic() - start_time
    return src_size, dst_size, elapsed
