"""
Database and Media Merger
==========================
Synchronizes and merges crawled novel data, chapters, images, and media assets
downloaded from GitHub Actions (or another machine) into the local database and filesystem.
"""

import os
import shutil
import sqlite3
import tempfile
import zipfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from ..utils.logger import get_logger, console
from rich.table import Table

log = get_logger("merger")


@dataclass
class MergeStats:
    source_db_path: str = ""
    target_db_path: str = ""
    source_type: str = ""  # zip, directory, db_file
    
    # Novels
    novels_checked: int = 0
    novels_added: int = 0
    novels_updated: int = 0
    novels_unchanged: int = 0

    # Volumes
    volumes_checked: int = 0
    volumes_added: int = 0
    volumes_updated: int = 0

    # Chapters
    chapters_checked: int = 0
    chapters_added: int = 0
    chapters_updated: int = 0
    chapters_skipped: int = 0

    # Images
    images_checked: int = 0
    images_added: int = 0
    images_updated: int = 0
    images_skipped: int = 0

    # Files / Media copied
    covers_copied: int = 0
    chapter_images_copied: int = 0
    novels_exported_copied: int = 0

    # Retries and logs
    retries_merged: int = 0
    logs_merged: int = 0

    # Metadata
    duration_seconds: float = 0.0
    backup_path: Optional[str] = None
    dry_run: bool = False
    errors: List[str] = field(default_factory=list)


class DataMerger:
    """
    Merges SQLite database and output media files from a source (zip archive, folder, or db)
    into the local target environment.
    """

    def __init__(
        self,
        target_db_path: Path,
        target_output_dir: Path,
        create_backup: bool = True,
        dry_run: bool = False,
    ):
        self.target_db_path = Path(target_db_path).resolve()
        self.target_output_dir = Path(target_output_dir).resolve()
        self.create_backup = create_backup
        self.dry_run = dry_run

    def _create_database_backup(self) -> Optional[Path]:
        """Creates a consistent online SQLite backup before applying changes."""
        if not self.target_db_path.exists() or self.target_db_path.stat().st_size == 0:
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = self.target_db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = backup_dir / f"hako_backup_{timestamp}.db"

        log.info(f"[Merger] Creating safety backup of local DB at {backup_file}...")
        src_conn = sqlite3.connect(self.target_db_path)
        dst_conn = sqlite3.connect(backup_file)
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
            log.info(f"[Merger] Backup completed successfully: {backup_file}")
            return backup_file
        finally:
            src_conn.close()
            dst_conn.close()

    def _find_source_components(self, root_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
        """Finds db file and output directory within extracted folder."""
        # 1. Search for database file
        candidate_dbs = [
            root_dir / "data" / "hako.db",
            root_dir / "hako.db",
        ]
        db_path = None
        for cand in candidate_dbs:
            if cand.is_file():
                db_path = cand
                break

        if not db_path:
            all_dbs = list(root_dir.glob("**/*.db"))
            # Exclude test dbs if possible
            all_dbs = [d for d in all_dbs if "test" not in d.name.lower()]
            if all_dbs:
                db_path = all_dbs[0]

        # 2. Search for output directory
        candidate_outputs = [
            root_dir / "output",
            root_dir / "media",
        ]
        output_path = None
        for cand in candidate_outputs:
            if cand.is_dir():
                output_path = cand
                break

        return db_path, output_path

    def _copy_media_files(self, source_output_dir: Path, stats: MergeStats):
        """Copies cover and chapter illustration files from source to target output directory."""
        if not source_output_dir or not source_output_dir.exists():
            return

        target_media_covers = self.target_output_dir / "media" / "covers"
        target_media_chapters = self.target_output_dir / "media" / "chapters"
        target_novels = self.target_output_dir / "novels"

        # 1. Copy covers
        src_covers = source_output_dir / "media" / "covers"
        if not src_covers.exists():
            src_covers = source_output_dir / "covers"

        if src_covers.is_dir():
            target_media_covers.mkdir(parents=True, exist_ok=True)
            for cover_file in src_covers.glob("*.*"):
                if cover_file.is_file():
                    dest_file = target_media_covers / cover_file.name
                    if not dest_file.exists() or dest_file.stat().st_size == 0:
                        if not self.dry_run:
                            shutil.copy2(cover_file, dest_file)
                        stats.covers_copied += 1

        # 2. Copy chapter illustrations
        src_chap_imgs = source_output_dir / "media" / "chapters"
        if not src_chap_imgs.exists():
            src_chap_imgs = source_output_dir / "chapters"

        if src_chap_imgs.is_dir():
            target_media_chapters.mkdir(parents=True, exist_ok=True)
            for novel_folder in src_chap_imgs.iterdir():
                if novel_folder.is_dir():
                    dest_novel_folder = target_media_chapters / novel_folder.name
                    dest_novel_folder.mkdir(parents=True, exist_ok=True)
                    for img_file in novel_folder.glob("*.*"):
                        if img_file.is_file():
                            dest_file = dest_novel_folder / img_file.name
                            if not dest_file.exists() or dest_file.stat().st_size == 0:
                                if not self.dry_run:
                                    shutil.copy2(img_file, dest_file)
                                stats.chapter_images_copied += 1

        # 3. Copy exported novel files (.txt, .epub)
        src_novels = source_output_dir / "novels"
        if src_novels.is_dir():
            target_novels.mkdir(parents=True, exist_ok=True)
            for novel_file in src_novels.glob("*.*"):
                if novel_file.is_file() and novel_file.suffix in (".txt", ".epub", ".md"):
                    dest_file = target_novels / novel_file.name
                    if not dest_file.exists() or novel_file.stat().st_size > dest_file.stat().st_size:
                        if not self.dry_run:
                            shutil.copy2(novel_file, dest_file)
                        stats.novels_exported_copied += 1

    def merge_from_source(self, source_path: Path) -> MergeStats:
        """
        Main entry point for merging.
        Accepts a path to a .zip file, a directory, or a .db file.
        """
        start_time = time.monotonic()
        source_path = Path(source_path).resolve()

        stats = MergeStats(
            target_db_path=str(self.target_db_path),
            dry_run=self.dry_run,
        )

        if not source_path.exists():
            stats.errors.append(f"Source path does not exist: {source_path}")
            return stats

        # Ensure target database parent directory exists
        self.target_db_path.parent.mkdir(parents=True, exist_ok=True)

        temp_dir = None
        source_db_path: Optional[Path] = None
        source_output_dir: Optional[Path] = None

        try:
            # 1. Determine source type and extract if zip
            if source_path.is_file() and source_path.suffix.lower() == ".zip":
                stats.source_type = "zip"
                log.info(f"[Merger] Extracting source zip archive: {source_path}")
                temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                extract_root = Path(temp_dir.name)
                with zipfile.ZipFile(source_path, "r") as zf:
                    zf.extractall(extract_root)
                source_db_path, source_output_dir = self._find_source_components(extract_root)

            elif source_path.is_dir():
                stats.source_type = "directory"
                source_db_path, source_output_dir = self._find_source_components(source_path)

            elif source_path.is_file() and source_path.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
                stats.source_type = "db_file"
                source_db_path = source_path
                # Check for output dir in same folder or parent
                cand_out = source_path.parent / "output"
                if cand_out.is_dir():
                    source_output_dir = cand_out

            else:
                stats.errors.append(f"Unsupported source format: {source_path}")
                return stats

            if not source_db_path or not source_db_path.is_file():
                stats.errors.append(f"Could not find SQLite database in source: {source_path}")
                return stats

            stats.source_db_path = str(source_db_path)
            log.info(f"[Merger] Source DB: {source_db_path}")
            if source_output_dir:
                log.info(f"[Merger] Source Media/Output: {source_output_dir}")

            # 2. Backup target DB before any operation (if target exists)
            if self.create_backup and not self.dry_run and self.target_db_path.exists():
                backup = self._create_database_backup()
                if backup:
                    stats.backup_path = str(backup)

            # 3. Copy media assets first
            if source_output_dir:
                log.info("[Merger] Merging media files (covers, chapter images, novels)...")
                self._copy_media_files(source_output_dir, stats)

            # 4. Merge SQLite databases
            log.info("[Merger] Merging database records...")
            self._merge_sqlite(source_db_path, self.target_db_path, stats)

        except Exception as e:
            log.error(f"[Merger] Exception during merge: {e}", exc_info=True)
            stats.errors.append(str(e))
        finally:
            if temp_dir:
                import gc
                gc.collect()
                try:
                    temp_dir.cleanup()
                except Exception:
                    pass

        stats.duration_seconds = time.monotonic() - start_time
        return stats

    def _merge_sqlite(self, source_db: Path, target_db: Path, stats: MergeStats):
        """
        Merges SQLite database records with full primary key remapping:
        novels -> volumes -> chapters -> images -> retry_queue -> crawl_logs.
        """
        # Ensure target database is initialized with schema if it doesn't exist
        from .connection import DatabaseManager
        DatabaseManager(target_db)

        # Open source in read-only mode with POSIX path for Windows compatibility
        source_posix = source_db.resolve().as_posix()
        s_conn = sqlite3.connect(f"file:{source_posix}?mode=ro", uri=True)
        s_conn.row_factory = sqlite3.Row
        s_cur = s_conn.cursor()

        t_conn = sqlite3.connect(target_db)
        t_conn.row_factory = sqlite3.Row
        t_cur = t_conn.cursor()

        try:
            # Disable FK checks during merge to avoid constraint timing issues, re-enable before commit
            t_cur.execute("PRAGMA foreign_keys = OFF;")
            t_cur.execute("BEGIN TRANSACTION;")

            # ─────────────────────────────────────────────────────────
            # 1. NOVELS
            # ─────────────────────────────────────────────────────────
            novel_id_map: Dict[int, int] = {}  # source_novel_id -> target_novel_id

            s_cur.execute("SELECT * FROM novels")
            source_novels = s_cur.fetchall()
            stats.novels_checked = len(source_novels)

            for s_nov in source_novels:
                t_cur.execute("SELECT id, crawl_status, total_words, site_last_updated, cover_local_path FROM novels WHERE url = ?", (s_nov["url"],))
                t_nov = t_cur.fetchone()

                if t_nov:
                    t_id = t_nov["id"]
                    novel_id_map[s_nov["id"]] = t_id

                    # Determine if we should update target
                    should_update = False
                    if t_nov["crawl_status"] != "completed" and s_nov["crawl_status"] == "completed":
                        should_update = True
                    elif (s_nov["total_words"] or 0) > (t_nov["total_words"] or 0):
                        should_update = True
                    elif (s_nov["site_last_updated"] or "") > (t_nov["site_last_updated"] or ""):
                        should_update = True
                    elif not t_nov["cover_local_path"] and s_nov["cover_local_path"]:
                        should_update = True

                    if should_update and not self.dry_run:
                        cover_local = t_nov["cover_local_path"] or s_nov["cover_local_path"]
                        # Normalize path
                        if cover_local:
                            cover_local = os.path.normpath(cover_local)

                        t_cur.execute("""
                            UPDATE novels SET
                                title = COALESCE(NULLIF(?, ''), title),
                                alternative_titles = COALESCE(NULLIF(?, ''), alternative_titles),
                                author = COALESCE(NULLIF(?, ''), author),
                                artist = COALESCE(NULLIF(?, ''), artist),
                                status = ?,
                                novel_type = ?,
                                cover_url = COALESCE(NULLIF(?, ''), cover_url),
                                cover_local_path = ?,
                                summary = COALESCE(NULLIF(?, ''), summary),
                                genres = ?,
                                total_words = MAX(total_words, ?),
                                views = MAX(views, ?),
                                likes = MAX(likes, ?),
                                bookmarks = MAX(bookmarks, ?),
                                rating = CASE WHEN ? > 0 THEN ? ELSE rating END,
                                rating_count = MAX(rating_count, ?),
                                site_last_updated = COALESCE(NULLIF(?, ''), site_last_updated),
                                crawl_status = CASE WHEN ? = 'completed' THEN 'completed' ELSE crawl_status END,
                                error_message = CASE WHEN ? = 'completed' THEN NULL ELSE error_message END,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (
                            s_nov["title"], s_nov["alternative_titles"], s_nov["author"], s_nov["artist"],
                            s_nov["status"], s_nov["novel_type"], s_nov["cover_url"], cover_local,
                            s_nov["summary"], s_nov["genres"], s_nov["total_words"] or 0,
                            s_nov["views"] or 0, s_nov["likes"] or 0, s_nov["bookmarks"] or 0,
                            s_nov["rating"] or 0.0, s_nov["rating"] or 0.0, s_nov["rating_count"] or 0,
                            s_nov["site_last_updated"], s_nov["crawl_status"], s_nov["crawl_status"],
                            t_id
                        ))
                        stats.novels_updated += 1
                    elif should_update and self.dry_run:
                        stats.novels_updated += 1
                    else:
                        stats.novels_unchanged += 1

                else:
                    # Novel does not exist -> Insert
                    cover_local = s_nov["cover_local_path"]
                    if cover_local:
                        cover_local = os.path.normpath(cover_local)

                    if not self.dry_run:
                        t_cur.execute("""
                            INSERT INTO novels (
                                url, slug, title, alternative_titles, author, artist,
                                status, novel_type, cover_url, cover_local_path,
                                summary, genres, total_words, views, likes, bookmarks,
                                rating, rating_count, site_last_updated, crawl_status,
                                error_message, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            s_nov["url"], s_nov["slug"], s_nov["title"], s_nov["alternative_titles"],
                            s_nov["author"], s_nov["artist"], s_nov["status"], s_nov["novel_type"],
                            s_nov["cover_url"], cover_local, s_nov["summary"],
                            s_nov["genres"], s_nov["total_words"], s_nov["views"], s_nov["likes"],
                            s_nov["bookmarks"], s_nov["rating"], s_nov["rating_count"],
                            s_nov["site_last_updated"], s_nov["crawl_status"], s_nov["error_message"],
                            s_nov["created_at"] or datetime.now().isoformat(),
                            s_nov["updated_at"] or datetime.now().isoformat(),
                        ))
                        t_id = t_cur.lastrowid
                        novel_id_map[s_nov["id"]] = t_id
                    else:
                        novel_id_map[s_nov["id"]] = 999999
                    stats.novels_added += 1

            # ─────────────────────────────────────────────────────────
            # 2. VOLUMES
            # ─────────────────────────────────────────────────────────
            vol_id_map: Dict[int, int] = {}  # source_vol_id -> target_vol_id

            s_cur.execute("SELECT * FROM volumes")
            source_volumes = s_cur.fetchall()
            stats.volumes_checked = len(source_volumes)

            for s_vol in source_volumes:
                t_novel_id = novel_id_map.get(s_vol["novel_id"])
                if not t_novel_id:
                    continue

                t_cur.execute(
                    "SELECT id FROM volumes WHERE novel_id = ? AND title = ?",
                    (t_novel_id, s_vol["title"]),
                )
                t_vol = t_cur.fetchone()

                if t_vol:
                    vol_id_map[s_vol["id"]] = t_vol["id"]
                    stats.volumes_updated += 1
                else:
                    if not self.dry_run:
                        t_cur.execute("""
                            INSERT INTO volumes (novel_id, vol_index, title, url, created_at)
                            VALUES (?, ?, ?, ?, ?)
                        """, (
                            t_novel_id, s_vol["vol_index"], s_vol["title"],
                            s_vol["url"], s_vol["created_at"] or datetime.now().isoformat(),
                        ))
                        vol_id_map[s_vol["id"]] = t_cur.lastrowid
                    else:
                        vol_id_map[s_vol["id"]] = 999999
                    stats.volumes_added += 1

            # ─────────────────────────────────────────────────────────
            # 3. CHAPTERS
            # ─────────────────────────────────────────────────────────
            chap_id_map: Dict[int, int] = {}  # source_chap_id -> target_chap_id

            s_cur.execute("SELECT * FROM chapters")
            source_chapters = s_cur.fetchall()
            stats.chapters_checked = len(source_chapters)

            for s_chap in source_chapters:
                t_novel_id = novel_id_map.get(s_chap["novel_id"])
                if not t_novel_id:
                    continue

                t_vol_id = vol_id_map.get(s_chap["volume_id"]) if s_chap["volume_id"] else None

                t_cur.execute(
                    "SELECT id, crawl_status, text_content, word_count FROM chapters WHERE url = ?",
                    (s_chap["url"],),
                )
                t_chap = t_cur.fetchone()

                if t_chap:
                    chap_id_map[s_chap["id"]] = t_chap["id"]

                    t_has_content = bool(t_chap["text_content"] and len(t_chap["text_content"].strip()) > 0)
                    s_has_content = bool(s_chap["text_content"] and len(s_chap["text_content"].strip()) > 0)
                    t_is_completed = (t_chap["crawl_status"] == "completed")
                    s_is_completed = (s_chap["crawl_status"] == "completed")

                    should_update = False
                    if not t_has_content and s_has_content:
                        should_update = True
                    elif not t_is_completed and s_is_completed:
                        should_update = True
                    elif s_has_content and (s_chap["word_count"] or 0) > (t_chap["word_count"] or 0):
                        should_update = True

                    if should_update and not self.dry_run:
                        t_cur.execute("""
                            UPDATE chapters SET
                                novel_id = ?,
                                volume_id = COALESCE(?, volume_id),
                                chapter_index = ?,
                                title = ?,
                                word_count = ?,
                                publish_date = COALESCE(NULLIF(?, ''), publish_date),
                                text_content = ?,
                                html_content = ?,
                                images_json = ?,
                                crawl_status = ?,
                                error_message = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (
                            t_novel_id, t_vol_id, s_chap["chapter_index"], s_chap["title"],
                            s_chap["word_count"], s_chap["publish_date"], s_chap["text_content"],
                            s_chap["html_content"], s_chap["images_json"], s_chap["crawl_status"],
                            s_chap["error_message"], t_chap["id"]
                        ))
                        stats.chapters_updated += 1
                    elif should_update and self.dry_run:
                        stats.chapters_updated += 1
                    else:
                        stats.chapters_skipped += 1

                else:
                    # New chapter -> Insert
                    if not self.dry_run:
                        t_cur.execute("""
                            INSERT INTO chapters (
                                novel_id, volume_id, chapter_index, title, url,
                                word_count, publish_date, text_content, html_content,
                                images_json, crawl_status, error_message, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            t_novel_id, t_vol_id, s_chap["chapter_index"], s_chap["title"], s_chap["url"],
                            s_chap["word_count"], s_chap["publish_date"], s_chap["text_content"],
                            s_chap["html_content"], s_chap["images_json"], s_chap["crawl_status"],
                            s_chap["error_message"],
                            s_chap["created_at"] or datetime.now().isoformat(),
                            s_chap["updated_at"] or datetime.now().isoformat(),
                        ))
                        chap_id_map[s_chap["id"]] = t_cur.lastrowid
                    else:
                        chap_id_map[s_chap["id"]] = 999999
                    stats.chapters_added += 1

            # ─────────────────────────────────────────────────────────
            # 4. IMAGES
            # ─────────────────────────────────────────────────────────
            s_cur.execute("SELECT * FROM images")
            source_images = s_cur.fetchall()
            stats.images_checked = len(source_images)

            for s_img in source_images:
                t_novel_id = novel_id_map.get(s_img["novel_id"]) if s_img["novel_id"] else None
                t_chap_id = chap_id_map.get(s_img["chapter_id"]) if s_img["chapter_id"] else None

                t_cur.execute(
                    "SELECT id, status, local_path FROM images WHERE original_url = ?",
                    (s_img["original_url"],),
                )
                t_img = t_cur.fetchone()

                local_path_norm = os.path.normpath(s_img["local_path"]) if s_img["local_path"] else None

                if t_img:
                    if t_img["status"] != "downloaded" and s_img["status"] == "downloaded":
                        if not self.dry_run:
                            t_cur.execute("""
                                UPDATE images SET
                                    local_path = ?,
                                    file_size = ?,
                                    status = ?,
                                    error_message = NULL
                                WHERE id = ?
                            """, (local_path_norm, s_img["file_size"], s_img["status"], t_img["id"]))
                        stats.images_updated += 1
                    else:
                        stats.images_skipped += 1
                else:
                    if not self.dry_run:
                        t_cur.execute("""
                            INSERT INTO images (
                                novel_id, chapter_id, image_type, original_url,
                                local_path, file_size, status, error_message, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            t_novel_id, t_chap_id, s_img["image_type"], s_img["original_url"],
                            local_path_norm, s_img["file_size"], s_img["status"],
                            s_img["error_message"],
                            s_img["created_at"] or datetime.now().isoformat(),
                        ))
                    stats.images_added += 1

            # ─────────────────────────────────────────────────────────
            # 5. RETRY QUEUE
            # ─────────────────────────────────────────────────────────
            try:
                s_cur.execute("SELECT * FROM retry_queue")
                for s_ret in s_cur.fetchall():
                    t_cur.execute(
                        "SELECT id, status FROM retry_queue WHERE item_type = ? AND target_url = ?",
                        (s_ret["item_type"], s_ret["target_url"]),
                    )
                    t_ret = t_cur.fetchone()
                    if t_ret:
                        if s_ret["status"] == "resolved" and t_ret["status"] != "resolved" and not self.dry_run:
                            t_cur.execute(
                                "UPDATE retry_queue SET status = 'resolved', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (t_ret["id"],),
                            )
                            stats.retries_merged += 1
                    else:
                        if s_ret["status"] != "resolved" and not self.dry_run:
                            t_cur.execute("""
                                INSERT INTO retry_queue (
                                    item_type, target_id, target_url, extra_data,
                                    attempts, max_attempts, status, last_error, next_retry_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                s_ret["item_type"], s_ret["target_id"], s_ret["target_url"],
                                s_ret["extra_data"], s_ret["attempts"], s_ret["max_attempts"],
                                s_ret["status"], s_ret["last_error"], s_ret["next_retry_at"],
                            ))
                            stats.retries_merged += 1
            except sqlite3.OperationalError:
                pass

            # ─────────────────────────────────────────────────────────
            # 6. CRAWL LOGS
            # ─────────────────────────────────────────────────────────
            try:
                s_cur.execute("SELECT * FROM crawl_logs ORDER BY id DESC LIMIT 20")
                for s_log in s_cur.fetchall():
                    details_str = s_log["details"] or "{}"
                    if not self.dry_run:
                        t_cur.execute("""
                            INSERT INTO crawl_logs (
                                crawl_type, status, items_checked, items_updated,
                                new_chapters, errors_count, duration_seconds, details, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            f"{s_log['crawl_type']} (merged)", s_log["status"],
                            s_log["items_checked"], s_log["items_updated"],
                            s_log["new_chapters"], s_log["errors_count"],
                            s_log["duration_seconds"], details_str,
                            s_log["created_at"] or datetime.now().isoformat(),
                        ))
                        stats.logs_merged += 1
            except sqlite3.OperationalError:
                pass

            # ─────────────────────────────────────────────────────────
            # 7. COMMIT & PRAGMA
            # ─────────────────────────────────────────────────────────
            if not self.dry_run:
                t_cur.execute("PRAGMA foreign_keys = ON;")
                fk_violations = t_cur.execute("PRAGMA foreign_key_check;").fetchall()
                if fk_violations:
                    log.warning(f"[Merger] Warning: Foreign key check found {len(fk_violations)} violations: {fk_violations}")
                t_conn.commit()
                log.info("[Merger] SQLite transaction committed successfully.")
            else:
                t_conn.rollback()
                log.info("[Merger] Dry-run enabled: SQLite transaction rolled back.")

        except Exception as e:
            t_conn.rollback()
            raise e
        finally:
            try:
                s_cur.close()
            except Exception:
                pass
            try:
                t_cur.close()
            except Exception:
                pass
            try:
                s_conn.close()
            except Exception:
                pass
            try:
                t_conn.close()
            except Exception:
                pass


def print_merge_report(stats: MergeStats):
    """Prints a beautiful Rich table summarizing the merge results."""
    title = "[bold green]Data Merge Report[/bold green]"
    if stats.dry_run:
        title += " [bold yellow](DRY-RUN - NO CHANGES SAVED)[/bold yellow]"

    table = Table(title=title, border_style="cyan", show_header=True)
    table.add_column("Category", style="bold cyan", min_width=20)
    table.add_column("Metric", style="white", min_width=25)
    table.add_column("Count / Detail", style="bold green", justify="right")

    table.add_row("Source", "Type", stats.source_type.upper())
    table.add_row("Source", "Source DB Path", stats.source_db_path or "N/A")
    table.add_row("Target", "Target DB Path", stats.target_db_path)
    if stats.backup_path:
        table.add_row("Safety", "Safety Backup File", f"[yellow]{stats.backup_path}[/yellow]")

    table.add_section()
    table.add_row("Novels", "Total Checked", str(stats.novels_checked))
    table.add_row("Novels", "Newly Added", f"[green]{stats.novels_added}[/green]")
    table.add_row("Novels", "Updated / Refreshed", f"[yellow]{stats.novels_updated}[/yellow]")
    table.add_row("Novels", "Unchanged", str(stats.novels_unchanged))

    table.add_section()
    table.add_row("Volumes", "Total Checked", str(stats.volumes_checked))
    table.add_row("Volumes", "Newly Added", f"[green]{stats.volumes_added}[/green]")
    table.add_row("Volumes", "Linked / Updated", str(stats.volumes_updated))

    table.add_section()
    table.add_row("Chapters", "Total Checked", str(stats.chapters_checked))
    table.add_row("Chapters", "Newly Added", f"[bold green]{stats.chapters_added}[/bold green]")
    table.add_row("Chapters", "Content Updated", f"[bold yellow]{stats.chapters_updated}[/bold yellow]")
    table.add_row("Chapters", "Skipped (Already Up-to-date)", str(stats.chapters_skipped))

    table.add_section()
    table.add_row("Images", "Total Checked", str(stats.images_checked))
    table.add_row("Images", "Newly Added", f"[green]{stats.images_added}[/green]")
    table.add_row("Images", "Marked Downloaded", f"[yellow]{stats.images_updated}[/yellow]")

    table.add_section()
    table.add_row("Media Files", "Cover Images Copied", str(stats.covers_copied))
    table.add_row("Media Files", "Chapter Illustrations Copied", str(stats.chapter_images_copied))
    table.add_row("Media Files", "Exported Novel Files Copied", str(stats.novels_exported_copied))

    table.add_section()
    table.add_row("Diagnostics", "Retry Queue Items Synced", str(stats.retries_merged))
    table.add_row("Diagnostics", "Crawl Logs Imported", str(stats.logs_merged))
    table.add_row("Performance", "Merge Duration", f"{stats.duration_seconds:.2f}s")

    if stats.errors:
        table.add_section()
        for err in stats.errors:
            table.add_row("Error", "Encountered Error", f"[bold red]{err}[/bold red]")

    console.print(table)
