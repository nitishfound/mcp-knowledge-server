"""Notes stored in SQLite with FTS5 full-text search (BM25 ranking, snippets).

All SQL uses ? parameters (never string formatting), and user search text is
turned into a safe FTS5 query, so neither SQL nor FTS syntax can be injected.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_TITLE = 200
MAX_CONTENT = 20_000
MAX_TAGS = 10
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '[]',   -- JSON array
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title, content, tags, content='notes', content_rowid='id', tokenize='porter unicode61'
);
-- Triggers keep the search index in sync with the notes table.
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO notes_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;
"""


class NoteError(ValueError):
    """Bad input or missing note. The message is safe to show to the user/model."""


@dataclass
class Note:
    id: int
    title: str
    content: str
    tags: list[str]
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_tags(tags: list[str] | None) -> list[str]:
    clean = sorted({t.strip().lower() for t in (tags or []) if t.strip()})
    if len(clean) > MAX_TAGS:
        raise NoteError(f"at most {MAX_TAGS} tags")
    bad = [t for t in clean if not TAG_RE.match(t)]
    if bad:
        raise NoteError(f"invalid tag(s) {bad}: use lowercase letters, digits and dashes (max 32 chars)")
    return clean


def to_fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: every word becomes a quoted term, joined with OR.

    Raw user text could contain FTS operators (NEAR, *, column filters, unbalanced
    quotes) that cause errors or unexpected matches. Quoting each word avoids that.
    BM25 ranking still puts notes that match more words first.
    """
    words = re.findall(r"\w+", text, flags=re.UNICODE)[:20]
    if not words:
        raise NoteError("search query must contain at least one word")
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in words)


class NoteStore:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Note:
        return Note(row["id"], row["title"], row["content"], json.loads(row["tags"]), row["created_at"], row["updated_at"])

    @staticmethod
    def _check(title: str | None, content: str | None) -> None:
        if title is not None and not (1 <= len(title.strip()) <= MAX_TITLE):
            raise NoteError(f"title must be 1-{MAX_TITLE} characters")
        if content is not None and not (1 <= len(content.strip()) <= MAX_CONTENT):
            raise NoteError(f"content must be 1-{MAX_CONTENT} characters")

    def add(self, title: str, content: str, tags: list[str] | None = None) -> Note:
        self._check(title, content)
        tags = normalize_tags(tags)
        now = _now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO notes(title, content, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (title.strip(), content.strip(), json.dumps(tags), now, now),
            )
        return self.get(cur.lastrowid)

    def get(self, note_id: int) -> Note:
        row = self._conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise NoteError(f"note {note_id} not found")
        return self._row(row)

    def update(self, note_id: int, title: str | None = None, content: str | None = None,
               tags: list[str] | None = None) -> Note:
        self._check(title, content)
        note = self.get(note_id)
        new_tags = normalize_tags(tags) if tags is not None else note.tags
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE notes SET title = ?, content = ?, tags = ?, updated_at = ? WHERE id = ?",
                (
                    title.strip() if title is not None else note.title,
                    content.strip() if content is not None else note.content,
                    json.dumps(new_tags),
                    _now(),
                    note_id,
                ),
            )
        return self.get(note_id)

    def delete(self, note_id: int) -> None:
        with self._lock, self._conn:
            if self._conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount == 0:
                raise NoteError(f"note {note_id} not found")

    def search(self, query: str, tag: str | None = None, limit: int = 5) -> list[dict]:
        limit = max(1, min(limit, 20))
        sql = (
            "SELECT n.id, n.title, n.tags, "
            "snippet(notes_fts, 1, '**', '**', '...', 16) AS snippet, "
            "bm25(notes_fts, 5.0, 1.0, 2.0) AS rank "  # title matches count 5x, tags 2x
            "FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid WHERE notes_fts MATCH ?"
        )
        params: list = [to_fts_query(query)]
        if tag:
            sql += " AND EXISTS (SELECT 1 FROM json_each(n.tags) WHERE value = ?)"
            params.append(tag.strip().lower())
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        # SQLite's bm25() is "lower is better" (negative); flip it so higher = better.
        return [
            {"id": r["id"], "title": r["title"], "tags": json.loads(r["tags"]), "snippet": r["snippet"],
             "score": round(-r["rank"], 3)}
            for r in rows
        ]

    def recent(self, limit: int = 10) -> list[Note]:
        rows = self._conn.execute("SELECT * FROM notes ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows]

    def by_tag(self, tag: str, limit: int = 50) -> list[Note]:
        rows = self._conn.execute(
            "SELECT * FROM notes n WHERE EXISTS (SELECT 1 FROM json_each(n.tags) WHERE value = ?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (tag.strip().lower(), limit),
        )
        return [self._row(r) for r in rows]

    def tags(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT value AS tag, COUNT(*) AS n FROM notes, json_each(notes.tags) GROUP BY value ORDER BY n DESC, tag"
        )
        return {r["tag"]: r["n"] for r in rows}

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
