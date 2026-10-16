"""Mô hình dữ liệu, SQLite/JSONL và trạng thái của phiên thu thập."""

import asyncio
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Any


class Post(BaseModel):
    post_id: str
    author: str = ""
    created_at: str | None = None
    time_label: str | None = None
    like_count: int = 0
    comment_count: int = 0
    text: str = ""
    image_urls: list[str] = Field(default_factory=list)
    permalink: str | None = None
    id_unstable: bool = False
    raw: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude={"raw"})

class Comment(BaseModel):

    comment_id: str
    post_id: str = ""
    parent_id: str | None = None
    depth: int = 0
    author: str = ""
    text: str = ""
    created_at: str = ""
    like_count: int = 0

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump()

def content_hash(post: Post) -> str:
    payload = {
        "text": post.text,
        "like_count": post.like_count,
        "comment_count": post.comment_count,
        "image_urls": sorted(post.image_urls),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class Store:
    def __init__(self, db_path: Path, jsonl_path: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = Path(jsonl_path) if jsonl_path else self.db_path.parent / "posts.jsonl"
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS collections (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT UNIQUE NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  page_url TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  finished_at TEXT,
                  status TEXT NOT NULL,
                  stop_reason TEXT,
                  new_count INTEGER NOT NULL DEFAULT 0,
                  skip_count INTEGER NOT NULL DEFAULT 0,
                  updated_count INTEGER NOT NULL DEFAULT 0,
                  rounds INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS posts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  post_id TEXT UNIQUE NOT NULL,
                  page_url TEXT NOT NULL,
                  author TEXT NOT NULL,
                  created_at TEXT,
                  time_label TEXT,
                  like_count INTEGER NOT NULL,
                  comment_count INTEGER NOT NULL,
                  text TEXT NOT NULL,
                  image_urls_json TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  permalink TEXT,
                  id_unstable INTEGER NOT NULL DEFAULT 0,
                  raw_json TEXT,
                  first_seen_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_posts_page ON posts(page_url);
            """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO collections (id, name, created_at) VALUES (1, ?, ?)",
                ("mặc định", _now()),
            )
            post_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(posts)")}
            if "collection_id" not in post_columns:
                self._conn.execute("ALTER TABLE posts ADD COLUMN collection_id INTEGER NOT NULL DEFAULT 1")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_collection ON posts(collection_id)")
            self._migrate_posts_composite_key()
            self._drop_feedback_column()
            self._ensure_comments_schema()
            self._conn.commit()

    def _rebuild_posts(self, table: str) -> None:
        """Giữ nguyên bản ghi khi chuyển cấu trúc bảng bài viết."""
        self._conn.executescript(f"""
            CREATE TABLE {table} (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              post_id TEXT NOT NULL,
              page_url TEXT NOT NULL,
              author TEXT NOT NULL,
              created_at TEXT,
              time_label TEXT,
              like_count INTEGER NOT NULL,
              comment_count INTEGER NOT NULL,
              text TEXT NOT NULL,
              image_urls_json TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              permalink TEXT,
              id_unstable INTEGER NOT NULL DEFAULT 0,
              raw_json TEXT,
              first_seen_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              collection_id INTEGER NOT NULL DEFAULT 1,
              UNIQUE(post_id, collection_id)
            );
            INSERT OR IGNORE INTO {table} SELECT id, post_id, page_url, author, created_at,
              time_label, like_count, comment_count, text, image_urls_json, content_hash,
              permalink, id_unstable, raw_json, first_seen_at, updated_at, collection_id
              FROM posts;
            DROP TABLE posts;
            ALTER TABLE {table} RENAME TO posts;
            CREATE INDEX IF NOT EXISTS idx_posts_page ON posts(page_url);
            CREATE INDEX IF NOT EXISTS idx_posts_collection ON posts(collection_id);
                    """)

    def _drop_feedback_column(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(posts)")}
        if "feedback_id" not in columns:
            return
        self._rebuild_posts("posts_clean")

    def _ensure_comments_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS comments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              collection_id INTEGER NOT NULL DEFAULT 1,
              post_id TEXT NOT NULL,
              comment_id TEXT NOT NULL,
              parent_id TEXT,
              depth INTEGER NOT NULL DEFAULT 0,
              author TEXT NOT NULL DEFAULT '',
              text TEXT NOT NULL DEFAULT '',
              created_at TEXT,
              like_count INTEGER NOT NULL DEFAULT 0,
              saved_at TEXT NOT NULL,
              UNIQUE(collection_id, comment_id)
            );
            CREATE INDEX IF NOT EXISTS idx_comments_post
              ON comments(collection_id, post_id, depth);
            CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);
            """
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(posts)")}

        for name, decl in (
            ("comments_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("comments_saved", "INTEGER NOT NULL DEFAULT 0"),
            ("comments_done_at", "TEXT"),
            ("comments_error", "TEXT"),
        ):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE posts ADD COLUMN {name} {decl}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_comments_status ON posts(collection_id, comments_status)"
        )

    def _migrate_posts_composite_key(self) -> None:
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='posts'"
        ).fetchone()
        if row is None:
            return
        create_sql = row[0] or ""
        if "UNIQUE(post_id, collection_id)" in create_sql:
            return
        if "post_id TEXT UNIQUE" not in create_sql and "post_id TEXT NOT NULL UNIQUE" not in create_sql:
            return
        self._rebuild_posts("posts_new")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_run(self, page_url: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO runs (page_url, started_at, status) VALUES (?, ?, ?)",
                (page_url, _now(), "starting"),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "status",
            "stop_reason",
            "new_count",
            "skip_count",
            "updated_count",
            "rounds",
            "finished_at",
        }
        keys = [key for key in fields if key in allowed]
        if not keys:
            return
        assignments = ", ".join(f"{key} = ?" for key in keys)
        values = [fields[key] for key in keys]
        with self._lock:
            self._conn.execute(f'UPDATE runs SET {assignments} WHERE id = ?', (*values, run_id))
            self._conn.commit()

    def finish_run(
        self,
        run_id: int,
        status: str,
        stop_reason: str,
        new_count: int,
        skip_count: int,
        updated_count: int,
        rounds: int,
    ) -> None:
        self.update_run(
            run_id,
            status=status,
            stop_reason=stop_reason,
            new_count=new_count,
            skip_count=skip_count,
            updated_count=updated_count,
            rounds=rounds,
            finished_at=_now(),
        )

    def create_collection(self, name: str) -> dict[str, Any]:
        name = name.strip()
        with self._lock:
            row = self._conn.execute("SELECT id, name, created_at FROM collections WHERE name = ?", (name,)).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    "INSERT INTO collections (name, created_at) VALUES (?, ?)",
                    (name, _now()),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT id, name, created_at FROM collections WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
        return {"id": int(row["id"]), "name": row["name"], "created_at": row["created_at"]}

    def get_collection(self, collection_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, created_at FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
        if row is None:
            return None
        return {"id": int(row["id"]), "name": row["name"], "created_at": row["created_at"]}

    def get_collection_by_name(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, created_at FROM collections WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        return {"id": int(row["id"]), "name": row["name"], "created_at": row["created_at"]}

    def list_collections(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT c.id, c.name, c.created_at, COUNT(p.id) AS post_count
                FROM collections c
                LEFT JOIN posts p ON p.collection_id = c.id
                GROUP BY c.id
                ORDER BY c.id
                """
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "created_at": row["created_at"],
                "post_count": int(row["post_count"]),
            }
            for row in rows
        ]

    def delete_collection(self, collection_id: int) -> tuple[int, int]:
        with self._lock:
            self._conn.execute("DELETE FROM comments WHERE collection_id = ?", (collection_id,))
            posts_del = int(self._conn.execute("DELETE FROM posts WHERE collection_id = ?", (collection_id,)).rowcount)
            cols_del = int(self._conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,)).rowcount)
            self._conn.commit()
        return cols_del, posts_del

    def get_post(self, post_id: str, collection_id: int | None = None) -> dict[str, Any] | None:
        with self._lock:
            if collection_id is not None:
                row = self._conn.execute(
                    "SELECT * FROM posts WHERE post_id = ? AND collection_id = ?",
                    (post_id, collection_id),
                ).fetchone()
            else:
                row = self._conn.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if row is None:
            return None
        return _row_to_post(row)

    def upsert(self, page_url: str, post: Post, *, collection_id: int = 1) -> dict[str, Any]:
        """Cập nhật tại chỗ để giữ ID, thời điểm gặp đầu tiên và trạng thái bình luận."""
        now = _now()
        fields = {
            "page_url": page_url, "author": post.author,
            "created_at": post.created_at, "time_label": post.time_label,
            "like_count": post.like_count, "comment_count": post.comment_count,
            "text": post.text, "image_urls_json": json.dumps(post.image_urls, ensure_ascii=False),
            "content_hash": content_hash(post), "permalink": post.permalink,
            "id_unstable": int(post.id_unstable),
            "raw_json": json.dumps(post.raw, ensure_ascii=False) if post.raw is not None else None,
            "updated_at": now,
        }
        with self._lock:
            exists = self._conn.execute(
                "SELECT id FROM posts WHERE post_id = ? AND collection_id = ?",
                (post.post_id, collection_id),
            ).fetchone()
            if exists:
                assignments = ", ".join(f"{key} = ?" for key in fields)
                self._conn.execute(
                    f"UPDATE posts SET {assignments} WHERE post_id = ? AND collection_id = ?",
                    (*fields.values(), post.post_id, collection_id),
                )
            else:
                fields.update(post_id=post.post_id, collection_id=collection_id, first_seen_at=now)
                columns = ", ".join(fields)
                placeholders = ", ".join("?" for _ in fields)
                self._conn.execute(
                    f"INSERT INTO posts ({columns}) VALUES ({placeholders})", tuple(fields.values())
                )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM posts WHERE post_id = ? AND collection_id = ?",
                (post.post_id, collection_id),
            ).fetchone()
        saved = _row_to_post(row)
        self._append_jsonl(saved)
        return saved

    def list_posts(
        self,
        page_url: str = "",
        query: str = "",
        offset: int = 0,
        limit: int = 50,
        collection_id: int | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if page_url:
            clauses.append("page_url = ?")
            params.append(page_url)
        if collection_id is not None:
            clauses.append("collection_id = ?")
            params.append(collection_id)
        if query:
            clauses.append("(author LIKE ? OR text LIKE ?)")
            like = f"%{query.replace('%', '').replace('_', '')}%"
            params.extend([like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) FROM posts {where}", params).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT * FROM posts {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {"total": int(total), "posts": [_row_to_post(row) for row in rows]}

    def count_for_url(self, page_url: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM posts WHERE page_url = ?", (page_url,)).fetchone()
        return int(row[0])

    def list_posts_needing_comments(
        self,
        collection_id: int,
        min_comments: int = 2,
        limit: int | None = None,
        retry_errors: bool = True,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM posts WHERE collection_id = ? AND comment_count >= ? "
            "AND permalink IS NOT NULL AND permalink != '' "
            "AND comments_status != 'done' "
        )
        params: list[Any] = [collection_id, min_comments]
        if not retry_errors:
            sql += "AND comments_status != 'error' "
        sql += "ORDER BY id DESC"
        if limit and limit > 0:

            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_post(row) for row in rows]

    def save_comments(
        self,
        collection_id: int,
        post_id: str,
        comments: list[Comment],
    ) -> int:
        now = _now()
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO comments
                  (collection_id, post_id, comment_id, parent_id, depth, author,
                   text, created_at, like_count, saved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(collection_id, comment_id) DO UPDATE SET
                  parent_id = excluded.parent_id,
                  depth = excluded.depth,
                  author = excluded.author,
                  text = excluded.text,
                  created_at = excluded.created_at,
                  like_count = excluded.like_count,
                  saved_at = excluded.saved_at
                """,
                [
                    (
                        collection_id,
                        post_id,
                        item.comment_id,
                        item.parent_id,
                        item.depth,
                        item.author,
                        item.text,
                        item.created_at or None,
                        item.like_count,
                        now,
                    )
                    for item in comments
                ],
            )
            total = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM comments WHERE collection_id = ? AND post_id = ?",
                    (collection_id, post_id),
                ).fetchone()[0]
            )
            self._conn.execute(
                "UPDATE posts SET comments_status = 'done', comments_saved = ?, "
                "comments_done_at = ?, comments_error = NULL "
                "WHERE post_id = ? AND collection_id = ?",
                (total, now, post_id, collection_id),
            )
            self._conn.commit()
        return total

    def mark_comments_error(self, collection_id: int, post_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE posts SET comments_status = 'error', comments_error = ? "
                "WHERE post_id = ? AND collection_id = ?",
                (error[:500], post_id, collection_id),
            )
            self._conn.commit()

    def reset_comments(self, collection_id: int, post_id: str | None = None) -> int:
        now = _now()
        with self._lock:
            if post_id:
                self._conn.execute(
                    "DELETE FROM comments WHERE collection_id = ? AND post_id = ?",
                    (collection_id, post_id),
                )
                n = self._conn.execute(
                    "UPDATE posts SET comments_status = 'pending', comments_saved = 0, "
                    "comments_done_at = NULL, comments_error = NULL "
                    "WHERE post_id = ? AND collection_id = ?",
                    (post_id, collection_id),
                ).rowcount
            else:
                self._conn.execute("DELETE FROM comments WHERE collection_id = ?", (collection_id,))
                n = self._conn.execute(
                    "UPDATE posts SET comments_status = 'pending', comments_saved = 0, "
                    "comments_done_at = NULL, comments_error = NULL WHERE collection_id = ?",
                    (collection_id,),
                ).rowcount
            self._conn.execute('UPDATE runs SET finished_at = ? WHERE finished_at IS NULL', (now,))
            self._conn.commit()
        return int(n)

    def list_comments(
        self,
        collection_id: int,
        post_id: str,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            total = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM comments WHERE collection_id = ? AND post_id = ?",
                    (collection_id, post_id),
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                "SELECT * FROM comments WHERE collection_id = ? AND post_id = ? "
                "ORDER BY depth ASC, id ASC LIMIT ? OFFSET ?",
                (collection_id, post_id, limit, offset),
            ).fetchall()
        return {"total": total, "comments": [_row_to_comment(row) for row in rows]}

    def comment_progress(self, collection_id: int, min_comments: int = 2) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                  COUNT(*) AS tong_bai,
                  SUM(CASE WHEN comment_count >= ? AND permalink IS NOT NULL AND permalink != ''
                           THEN 1 ELSE 0 END) AS du_dieu_lien_ket,
                  SUM(CASE WHEN comments_status = 'done' THEN 1 ELSE 0 END) AS xong,
                  SUM(CASE WHEN comments_status = 'error' THEN 1 ELSE 0 END) AS loi,
                  SUM(CASE WHEN comments_status != 'done' AND comment_count >= ?
                                AND permalink IS NOT NULL AND permalink != ''
                           THEN 1 ELSE 0 END) AS cho
                FROM posts WHERE collection_id = ?
                """,
                (min_comments, min_comments, collection_id),
            ).fetchone()
            saved = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM comments WHERE collection_id = ?", (collection_id,)
                ).fetchone()[0]
            )
        return {
            "posts": int(row["tong_bai"] or 0),
            "eligible": int(row["du_dieu_lien_ket"] or 0),
            "done": int(row["xong"] or 0),
            "error": int(row["loi"] or 0),
            "pending": int(row["cho"] or 0),
            "saved": saved,
        }

    def _append_jsonl(self, saved: dict[str, Any]) -> None:
        keys = ("page_url", "post_id", "author", "created_at", "time_label", "like_count",
                "comment_count", "text", "image_urls", "permalink")
        line = {key: saved[key] for key in keys}
        with self._lock, self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

def _row_to_post(row: sqlite3.Row) -> dict[str, Any]:
    keys = ("post_id", "page_url", "author", "created_at", "time_label", "like_count",
            "comment_count", "text", "image_urls", "content_hash", "permalink", "id_unstable",
            "collection_id", "comments_status", "comments_saved", "comments_done_at",
            "comments_error", "first_seen_at", "updated_at")
    values = dict(row)
    values["image_urls"] = json.loads(values.pop("image_urls_json"))
    values["id_unstable"] = bool(values["id_unstable"])
    for key in ("collection_id", "comments_saved"):
        values[key] = int(values[key])
    return {key: values[key] for key in keys}

def _row_to_comment(row: sqlite3.Row) -> dict[str, Any]:
    keys = ("comment_id", "post_id", "parent_id", "depth", "author", "text",
            "created_at", "like_count", "saved_at")
    return {key: int(row[key]) if key in {"depth", "like_count"} else row[key] for key in keys}


class AppState:
    def __init__(self) -> None:
        self.page_url = ""
        self.refresh = False
        self.idle_timeout = 8.0
        self.headless = False
        self.controls = False
        self.collection_id = 1
        self.collection_name = "mặc định"
        self.run_id: int | None = None
        self.status = "starting"
        self.stop_reason = ""
        self.new_count = 0
        self.skip_count = 0
        self.updated_count = 0
        self.rounds = 0
        self.session_ready = False
        self.logs: list[str] = []

        self.comment_running = False
        self.comment_total = 0
        self.comment_done = 0
        self.comment_error = 0
        self.comment_saved = 0
        self.comment_current = ""
        self.page: Any = None
        self.job: asyncio.Task | None = None
        self.comment_job: asyncio.Task | None = None
        self.start_event: asyncio.Event | None = None
        self.stop_event: asyncio.Event | None = None
        self.comment_stop_event: asyncio.Event | None = None
        self.shutdown_event: asyncio.Event | None = None
        self.subscribers: list[asyncio.Queue] = []

    def attach(self) -> None:
        if self.start_event is None:
            self.start_event = asyncio.Event()
            self.stop_event = asyncio.Event()
            self.shutdown_event = asyncio.Event()
            self.comment_stop_event = asyncio.Event()

    def should_stop_comments(self) -> bool:
        return self.comment_stop_event is not None and self.comment_stop_event.is_set()

    def feed_busy(self) -> bool:
        return self.status in {"starting", "opening", "waiting_login", "running", "stalled"} or (
            self.job is not None and not self.job.done()
        )

    def log(self, line: str) -> None:
        self.logs.append(line)
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        self.emit({"type": "log", "line": line})

    def set_status(self, status: str) -> None:
        self.status = status
        self.emit_status()

    def snapshot(self) -> dict[str, Any]:
        keys = ("status", "page_url", "refresh", "idle_timeout", "headless", "controls",
                "collection_id", "collection_name", "stop_reason", "new_count", "skip_count",
                "updated_count", "rounds", "session_ready", "comment_running", "comment_total",
                "comment_done", "comment_error", "comment_saved", "comment_current")
        return {"type": "status", **{key: getattr(self, key) for key in keys}, "logs": self.logs[-50:]}

    def emit_status(self) -> None:
        self.emit(self.snapshot())

    def emit(self, event: dict[str, Any]) -> None:
        dead: list[asyncio.Queue] = []
        for queue in self.subscribers:
            try:
                queue.put_nowait(event)
            except Exception:
                dead.append(queue)
        for queue in dead:
            if queue in self.subscribers:
                self.subscribers.remove(queue)

    def reserve_job(self, url: str, refresh: bool, idle_timeout: float, headless: bool = False) -> bool:
        if self.feed_busy() or self.comment_running:
            return False
        self.attach()
        self.page_url = url
        self.refresh = refresh
        self.idle_timeout = idle_timeout
        self.headless = headless
        self.run_id = None
        self.stop_reason = ""
        self.new_count = 0
        self.skip_count = 0
        self.updated_count = 0
        self.rounds = 0
        self.session_ready = False
        self.page = None
        self.logs = []
        if self.start_event is not None:
            self.start_event.clear()
        if self.stop_event is not None:
            self.stop_event.clear()
        self.set_status("opening")
        return True

    def touch_run(self, store: Store) -> None:
        if self.run_id is None:
            return
        store.update_run(
            self.run_id,
            status=self.status,
            stop_reason=self.stop_reason,
            new_count=self.new_count,
            skip_count=self.skip_count,
            updated_count=self.updated_count,
            rounds=self.rounds,
        )
