"""Read the bridge's message store and call the bridge API.

messages.db is opened read-only. Results are compact text meant for a language model: one line per message,
grouped by day, with attachments as one-line summaries. Everything in it was written by other people.
"""
import http.client
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Dict, List, Optional, Tuple

import requests

import audio
import bridge

BRIDGE_DIR = bridge.BRIDGE_DIR
MESSAGES_DB_PATH = os.path.join(bridge.STORE_DIR, 'messages.db')
# whatsmeow's own database: only the contacts and LID tables are read, to name group members
SESSION_DB_PATH = os.path.join(bridge.STORE_DIR, 'whatsapp.db')
# Full-text index of messages, document text and transcripts, built from the two databases above
SEARCH_DB_PATH = os.path.join(bridge.STORE_DIR, 'search.db')
MEDIA_CACHE_DB_PATH = os.path.join(bridge.STORE_DIR, 'media_cache.db')
MEDIA_DIR = os.path.join(bridge.STORE_DIR, 'media')
# Only files in here can be sent; the bridge enforces the same rule
OUTBOX_DIR = os.path.realpath(os.path.expanduser(os.environ.get("WHATSAPP_OUTBOX_DIR", "~/WhatsApp-outbox")))
# Where download_media saves files for the user
DOWNLOAD_DIR = os.path.expanduser(os.environ.get("WHATSAPP_DOWNLOAD_DIR", "~/Downloads/WhatsApp"))

# WhatsApp keeps media on its servers for roughly this long
SERVER_MEDIA_DAYS = 14
# Longest message text shown in a listing
MAX_TEXT = 3000

# stdout is the MCP stdio channel, so diagnostics must go to stderr
log = partial(print, file=sys.stderr)


# --- bridge API -------------------------------------------------------------------------------------

class _Response:
    """Minimal stand-in for requests.Response for bridge API calls."""
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def _post(path: str, payload: dict) -> _Response:
    """POST to the bridge API over its Unix socket, with the shared API token."""
    try:
        return _Response(*bridge.call(path, payload))
    except (OSError, http.client.HTTPException) as e:
        raise requests.RequestException(f"WhatsApp bridge is not reachable ({e})")


def _outbox_path(media_path: str) -> str:
    """Return the real path of media_path, which must be a file inside the outbox."""
    real = os.path.realpath(os.path.expanduser(media_path))
    if real == OUTBOX_DIR or os.path.commonpath([real, OUTBOX_DIR]) != OUTBOX_DIR:
        raise ValueError(f"Media files must be inside {OUTBOX_DIR}: copy the file there first")
    if not os.path.isfile(real):
        raise ValueError(f"Media file not found: {media_path}")
    return real


# --- databases --------------------------------------------------------------------------------------

def _uri(path: str) -> str:
    """Read-only SQLite URI for a path (quoted, so "?" or "#" in a folder name cannot change its meaning)."""
    return "file:" + urllib.parse.quote(path) + "?mode=ro"


def _connect() -> sqlite3.Connection:
    if not os.path.exists(MESSAGES_DB_PATH):
        raise ValueError("No messages yet: the bridge has not been linked to WhatsApp. Pair it once in a terminal "
                         f"(cd {BRIDGE_DIR} && go run .)")
    return sqlite3.connect(_uri(MESSAGES_DB_PATH), uri=True, timeout=10)


def _readonly(path: str) -> Optional[sqlite3.Connection]:
    if not os.path.exists(path):
        return None
    try:
        return sqlite3.connect(_uri(path), uri=True, timeout=5)
    except sqlite3.Error:
        return None


# Columns read for every message. Columns an older bridge has not created yet read as NULL.
MEDIA_COLUMNS = ["filename", "file_length", "mimetype", "media_seconds", "media_width", "media_height",
                 "media_pages", "media_voice", "link_url", "link_title", "link_description"]
_present_columns = None


def _message_select(conn: sqlite3.Connection) -> str:
    global _present_columns
    if _present_columns is None or not _present_columns.issuperset(MEDIA_COLUMNS):
        _present_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    media = [f"m.{c}" if c in _present_columns else "NULL" for c in MEDIA_COLUMNS]
    return ", ".join(["m.timestamp", "m.sender", "c.name", "m.content", "m.is_from_me", "m.chat_jid", "m.id",
                      "m.media_type"] + media)


@dataclass
class Message:
    timestamp: datetime
    sender: str
    chat_name: Optional[str]
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    media_type: Optional[str]
    media: dict


def _row_to_message(row) -> Message:
    return Message(
        timestamp=datetime.fromisoformat(row[0]), sender=row[1] or "", chat_name=row[2], content=row[3] or "",
        is_from_me=bool(row[4]), chat_jid=row[5], id=row[6], media_type=row[7],
        media=dict(zip(MEDIA_COLUMNS, row[8:8 + len(MEDIA_COLUMNS)])),
    )


def _fetch(conn: sqlite3.Connection, where: str, params: tuple, order: str = "m.timestamp DESC",
           limit: int = 50, offset: int = 0) -> List[Message]:
    sql = (f"SELECT {_message_select(conn)} FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid "
           f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?")
    return [_row_to_message(r) for r in conn.execute(sql, params + (limit, offset))]


# --- text helpers -----------------------------------------------------------------------------------

def _size(n: Optional[int]) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _clean(text: Optional[str], limit: int) -> str:
    """Untrusted short text for a one-line summary: no control characters or newlines, capped."""
    text = "".join(" " if ch.isspace() else ch for ch in (text or "") if ch.isprintable() or ch.isspace())
    text = " ".join(text.split())
    return text[:limit] + "…" if len(text) > limit else text


def _fold(text: str) -> str:
    """Lowercase without accents, for matching names the way people type them."""
    text = unicodedata.normalize("NFKD", text or "").casefold()
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def describe_media(media_type: str, media: Optional[dict]) -> str:
    """One-line summary of an attachment, enough to decide whether to open it with inspect_media."""
    m = media or {}
    parts = []
    if media_type == "audio":
        parts.append("voice message" if m.get("media_voice") else "audio")
    else:
        parts.append(media_type)
    if media_type == "document" and m.get("filename"):
        parts.append(f'"{_clean(m["filename"], 80)}"')
    if m.get("media_seconds"):
        s = int(m["media_seconds"])
        parts.append(f"{s // 60}:{s % 60:02d}")
    if m.get("media_width") and m.get("media_height") and media_type in ("image", "video"):
        parts.append(f'{m["media_width"]}x{m["media_height"]}')
    if m.get("media_pages"):
        parts.append(f'{m["media_pages"]} pages')
    if media_type == "document" and m.get("mimetype"):
        parts.append(_clean(m["mimetype"].split(";")[0], 60))
    if m.get("file_length"):
        parts.append(_size(m["file_length"]))
    return " ".join(parts)


def describe_link(media: Optional[dict]) -> str:
    m = media or {}
    if not m.get("link_url") and not m.get("link_title"):
        return ""
    out = f"[link preview: {_clean(m.get('link_url'), 200)}"
    if m.get("link_title"):
        out += f' — "{_clean(m["link_title"], 120)}"'
    if m.get("link_description"):
        out += f": {_clean(m['link_description'], 200)}"
    return out + "]"


# --- time -------------------------------------------------------------------------------------------

_RELATIVE = re.compile(r"\s*(\d+)\s*(m|min|mins|minutes?|h|hours?|d|days?|w|weeks?)\s*(ago)?\s*", re.I)


def parse_time(value: Optional[str], end: bool = False, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse "today", "yesterday", "3d", "12h", "2w", "2026-11-20" or "2026-11-20T15:00" as local time.

    With end=True a bare date means the end of that day, so until="2026-11-20" includes the 20th.
    """
    if value is None or not str(value).strip():
        return None
    now = now or datetime.now()
    v = str(value).strip().lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if v in ("now",):
        return now
    if v in ("today", "oggi"):
        return midnight + timedelta(days=1) if end else midnight
    if v in ("yesterday", "ieri"):
        return midnight if end else midnight - timedelta(days=1)
    m = _RELATIVE.fullmatch(v)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return now - delta
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f'Invalid time "{value}": use today, yesterday, a duration like 3d / 12h / 2w, '
                         'or a date like 2026-11-20 (optionally with a time, 2026-11-20T15:00)')
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value).strip()):
        dt += timedelta(days=1)
    return dt


def _sql_time(dt: datetime) -> str:
    # Stored timestamps look like "2026-11-25 18:20:35+01:00" in local time, so they compare as text
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _time_filter(since: Optional[str], until: Optional[str], column: str = "m.timestamp") -> Tuple[List[str], list]:
    where, params = [], []
    start, stop = parse_time(since), parse_time(until, end=True)
    if start:
        where.append(f"{column} >= ?")
        params.append(_sql_time(start))
    if stop:
        where.append(f"{column} < ?")
        params.append(_sql_time(stop))
    return where, params


# --- names ------------------------------------------------------------------------------------------

class Names:
    """Display names for senders, from chat names and the address book.

    Names saved in the address book are used as-is; names people chose themselves (push or business name)
    are prefixed with "~", like the bridge does, so they never pass for a saved contact.
    """
    _cache: Optional[Tuple[float, "Names"]] = None

    def __init__(self, conn: sqlite3.Connection):
        self.chats: Dict[str, str] = {}
        self.by_user: Dict[str, str] = {}
        self.lid_to_pn: Dict[str, str] = {}
        self.pn_to_lid: Dict[str, str] = {}
        for jid, name in conn.execute("SELECT jid, name FROM chats"):
            self.chats[jid] = name or ""
            if name and not jid.endswith("@g.us") and name != jid.split("@")[0]:
                self.by_user[jid.split("@")[0]] = name
        session = _readonly(SESSION_DB_PATH)
        if session:
            try:
                for lid, pn in session.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
                    self.lid_to_pn[lid.split("@")[0]] = pn.split("@")[0]
                    self.pn_to_lid[pn.split("@")[0]] = lid.split("@")[0]
                for jid, first, full, push, business in session.execute(
                        "SELECT their_jid, first_name, full_name, push_name, business_name FROM whatsmeow_contacts"):
                    user = jid.split("@")[0]
                    if user in self.by_user:
                        continue
                    saved = _clean(full or first, 60)
                    chosen = _clean(business or push, 60)
                    if saved or chosen:
                        self.by_user[user] = saved or "~" + chosen
            except sqlite3.Error as e:
                log(f"Could not read contact names: {e}")
            finally:
                session.close()

    @classmethod
    def get(cls, conn: sqlite3.Connection) -> "Names":
        # Built once a minute: names change rarely and building reads every contact
        if cls._cache and time.time() - cls._cache[0] < 60:
            return cls._cache[1]
        names = cls(conn)
        cls._cache = (time.time(), names)
        return names

    def sender(self, sender: str, is_from_me: bool) -> str:
        if is_from_me:
            return "Me"
        user = (sender or "").split("@")[0]
        for candidate in (user, self.lid_to_pn.get(user), self.pn_to_lid.get(user)):
            if candidate and candidate in self.by_user:
                return self.by_user[candidate]
        # Without a mapping, a long number is probably a LID (an anonymous ID), not a phone number
        pn = self.lid_to_pn.get(user)
        if not pn and user.isdigit() and not sender.endswith("@lid") and (sender.endswith("@s.whatsapp.net") or len(user) <= 13):
            pn = user
        return f"+{pn}" if pn else (f"unknown ({user})" if user else "unknown")

    def chat(self, jid: str) -> str:
        return self.chats.get(jid) or jid.split("@")[0]


def _kind(jid: str) -> str:
    return "group" if jid.endswith("@g.us") else "broadcast" if jid.endswith("@broadcast") else "direct"


def resolve_chat(conn: sqlite3.Connection, chat: str) -> Tuple[str, str]:
    """Find a chat by JID, phone number or (part of) its name. Returns (jid, name)."""
    chat = (chat or "").strip()
    if not chat:
        raise ValueError("Pass a chat: its name, a phone number or its JID")
    rows = conn.execute("SELECT jid, name, last_message_time FROM chats ORDER BY last_message_time DESC").fetchall()
    names = Names.get(conn)

    if "@" in chat:
        for jid, name, _ in rows:
            if jid == chat:
                return jid, name or jid
        raise ValueError(f"No chat with JID {chat}. Use list_chats to find it.")

    digits = re.sub(r"\D", "", chat)
    if re.fullmatch(r"[\d\s+().-]+", chat) and len(digits) >= 6:
        users = {digits, names.pn_to_lid.get(digits, ""), names.lid_to_pn.get(digits, "")} - {""}
        found = [(j, n) for j, n, _ in rows if j.split("@")[0] in users or (len(digits) >= 8 and j.split("@")[0].endswith(digits))]
        if len(found) == 1:
            return found[0][0], found[0][1] or found[0][0]
        if not found:
            raise ValueError(f"No chat with phone number {chat}. Use list_chats to search by name.")
        matches = found
    else:
        q = _fold(chat).lstrip("~")
        exact = [(j, n) for j, n, _ in rows if _fold(n or "").lstrip("~") == q]
        if len(exact) == 1:
            return exact[0][0], exact[0][1]
        words = q.split()
        matches = exact or [(j, n) for j, n, _ in rows if all(w in _fold(n or "") for w in words)]
        if len(matches) == 1:
            return matches[0][0], matches[0][1]
        if not matches:
            raise ValueError(f'No chat matches "{chat}". Use list_chats with a shorter query to find it.')

    options = "; ".join(f"{_clean(n, 60) or '?'} ({j}, {_kind(j)})" for j, n in matches[:8])
    more = f" and {len(matches) - 8} more" if len(matches) > 8 else ""
    raise ValueError(f'Several chats match "{chat}": {options}{more}. Call again with the JID of the right one.')


# --- formatting -------------------------------------------------------------------------------------

def _text(content: str) -> str:
    content = (content or "").strip()
    if len(content) > MAX_TEXT:
        content = content[:MAX_TEXT] + f"… [cut, {len(content)} characters in total]"
    return content.replace("\n", "\n    ")


def format_line(msg: Message, names: Names, with_chat: bool = False, marker: str = "") -> str:
    who = names.sender(msg.sender, msg.is_from_me)
    where = f"[{_clean(names.chat(msg.chat_jid), 40)}] " if with_chat else ""
    parts = []
    if msg.media_type:
        ref = f"id {msg.id}" + (f" chat {msg.chat_jid}" if with_chat else "")
        parts.append(f"[{describe_media(msg.media_type, msg.media)} · {ref}]")
    if msg.content:
        parts.append(_text(msg.content))
    link = describe_link(msg.media)
    if link:
        parts.append(link)
    return f"{marker}{msg.timestamp:%H:%M} {where}{who}: {' '.join(parts)}"


def format_messages(messages: List[Message], names: Names, with_chat: bool = False, mark: Optional[str] = None) -> str:
    """Messages in time order, with a header line for each day."""
    out, day = [], None
    for msg in messages:
        d = msg.timestamp.date()
        if d != day:
            out.append(f"── {msg.timestamp:%a %d %b %Y} ──")
            day = d
        out.append(format_line(msg, names, with_chat, "▶ " if mark and msg.id == mark else ""))
    return "\n".join(out)


# --- chats ------------------------------------------------------------------------------------------

def list_chats(query: Optional[str] = None, kind: str = "all", limit: int = 20, page: int = 0) -> str:
    """Chats sorted by last activity, optionally filtered by name, phone number or JID."""
    if kind not in ("all", "direct", "group"):
        raise ValueError('kind must be "all", "direct" or "group"')
    conn = _connect()
    try:
        names = Names.get(conn)
        rows = conn.execute("SELECT jid, name, last_message_time FROM chats ORDER BY last_message_time DESC").fetchall()
        if kind != "all":
            rows = [r for r in rows if _kind(r[0]) == kind]
        if query:
            q, digits = _fold(query).lstrip("~"), re.sub(r"\D", "", query)
            rows = [r for r in rows if all(w in _fold(r[1] or "") for w in q.split())
                    or query in r[0] or (len(digits) >= 4 and digits in r[0])]
        total = len(rows)
        rows = rows[page * limit:(page + 1) * limit]
        if not rows:
            return "No chats match." if query or page == 0 else "No more chats."

        lines = [f"{total} chat(s){' matching ' + repr(query) if query else ''}; showing {page * limit + 1}-{page * limit + len(rows)}:"]
        for jid, name, last in rows:
            preview = ""
            last_msg = _fetch(conn, "m.chat_jid = ?", (jid,), limit=1)
            if last_msg:
                m = last_msg[0]
                body = describe_media(m.media_type, m.media) if m.media_type and not m.content else m.content
                preview = f' — {names.sender(m.sender, m.is_from_me)}: "{_clean(body, 80)}"'
            when = datetime.fromisoformat(last).strftime("%Y-%m-%d %H:%M") if last else "never"
            lines.append(f"- {_clean(name, 60) or jid} · {_kind(jid)} · {jid} · last {when}{preview}")
        if total > (page + 1) * limit:
            lines.append(f"More: page={page + 1}")
        return "\n".join(lines)
    finally:
        conn.close()


def read_chat(chat: str, since: Optional[str] = None, until: Optional[str] = None, limit: int = 50, page: int = 0) -> str:
    """The most recent messages of one chat in a time range, oldest first; page=1 goes further back."""
    conn = _connect()
    try:
        jid, name = resolve_chat(conn, chat)
        names = Names.get(conn)
        where, params = _time_filter(since, until)
        where = ["m.chat_jid = ?"] + where
        params = [jid] + params
        total = conn.execute(f"SELECT count(*) FROM messages m WHERE {' AND '.join(where)}", params).fetchone()[0]
        messages = _fetch(conn, " AND ".join(where), tuple(params), limit=limit, offset=page * limit)[::-1]
        header = f"Chat: {_clean(name, 60)} ({jid}, {_kind(jid)})"
        if not messages:
            return header + "\nNo messages in this range."
        shown_to = page * limit + len(messages)
        header += f" — messages {page * limit + 1}-{shown_to} of {total} counting back from the newest"
        if since or until:
            header += f" (range: {since or 'start'} → {until or 'now'})"
        more = f"\nOlder messages: page={page + 1}" if total > shown_to else ""
        return f"{header}\n{format_messages(messages, names)}{more}"
    finally:
        conn.close()


def get_message_context(message_id: str, chat: Optional[str] = None, before: int = 5, after: int = 5) -> str:
    """A message with the messages around it; the message itself is marked with ▶."""
    conn = _connect()
    try:
        names = Names.get(conn)
        if chat:
            jid, _ = resolve_chat(conn, chat)
            target = _fetch(conn, "m.id = ? AND m.chat_jid = ?", (message_id, jid), limit=1)
        else:
            target = _fetch(conn, "m.id = ?", (message_id,), limit=1)
        if not target:
            raise ValueError(f"Message {message_id} not found")
        t = target[0]
        ts = t.timestamp.isoformat(sep=" ")
        older = _fetch(conn, "m.chat_jid = ? AND m.timestamp < ?", (t.chat_jid, ts), limit=before)[::-1]
        newer = _fetch(conn, "m.chat_jid = ? AND m.timestamp > ?", (t.chat_jid, ts), order="m.timestamp ASC", limit=after)
        header = f"Chat: {_clean(names.chat(t.chat_jid), 60)} ({t.chat_jid})"
        return f"{header}\n{format_messages(older + [t] + newer, names, mark=t.id)}"
    finally:
        conn.close()


def recent_activity(since: str = "24h", limit: int = 20) -> str:
    """Chats with new messages since a time: how many, from whom, and whether the last one awaits a reply."""
    start = parse_time(since) or datetime.now() - timedelta(days=1)
    conn = _connect()
    try:
        names = Names.get(conn)
        rows = conn.execute("""
            SELECT chat_jid, count(*), sum(CASE WHEN is_from_me THEN 0 ELSE 1 END), max(timestamp)
            FROM messages WHERE timestamp >= ? GROUP BY chat_jid ORDER BY max(timestamp) DESC LIMIT ?
        """, (_sql_time(start), limit)).fetchall()
        if not rows:
            return f"No messages since {start:%Y-%m-%d %H:%M}."
        lines = [f"Chats with messages since {start:%Y-%m-%d %H:%M} (newest first):"]
        for jid, count, incoming, _ in rows:
            last = _fetch(conn, "m.chat_jid = ?", (jid,), limit=1)[0]
            body = describe_media(last.media_type, last.media) if last.media_type and not last.content else last.content
            waiting = " · awaiting your reply" if not last.is_from_me and _kind(jid) == "direct" else ""
            lines.append(f"- {_clean(names.chat(jid), 60)} ({jid}) · {count} message(s), {incoming or 0} from others · "
                         f"last {last.timestamp:%d %b %H:%M} {names.sender(last.sender, last.is_from_me)}: "
                         f'"{_clean(body, 100)}"{waiting}')
        lines.append("Read a chat with read_chat(chat=<JID>, since=...).")
        return "\n".join(lines)
    finally:
        conn.close()


# --- search -----------------------------------------------------------------------------------------

FTS_COLUMNS = "body, msg_id UNINDEXED, chat_jid UNINDEXED, source UNINDEXED"
FTS_TOKENIZER = "tokenize = 'unicode61 remove_diacritics 2'"


def _search_db() -> sqlite3.Connection:
    new = not os.path.exists(SEARCH_DB_PATH)
    # uri=True so the message store can be attached read-only
    conn = sqlite3.connect("file:" + urllib.parse.quote(SEARCH_DB_PATH), uri=True, timeout=10)
    if new:
        os.chmod(SEARCH_DB_PATH, 0o600)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value)")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'fts'").fetchone():
        try:
            # Contentless: the index does not keep a second copy of every message (about half the size)
            conn.execute(f"CREATE VIRTUAL TABLE fts USING fts5({FTS_COLUMNS}, content = '', contentless_unindexed = 1, {FTS_TOKENIZER})")
        except sqlite3.OperationalError:
            # SQLite older than 3.43
            conn.execute(f"CREATE VIRTUAL TABLE fts USING fts5({FTS_COLUMNS}, {FTS_TOKENIZER})")
        conn.execute("DELETE FROM meta")
        conn.commit()
    return conn


def _meta(conn, key, default=0):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _sync_index(conn: sqlite3.Connection) -> None:
    """Add messages, document text and transcripts that arrived since the last search."""
    conn.execute("ATTACH DATABASE ? AS msgs", (_uri(MESSAGES_DB_PATH),))
    try:
        last = _meta(conn, "messages_rowid")
        while True:
            rows = conn.execute("""
                SELECT rowid, id, chat_jid, trim(coalesce(content, '') || ' ' ||
                    CASE WHEN media_type = 'document' THEN coalesce(filename, '') ELSE '' END || ' ' ||
                    coalesce(link_title, ''))
                FROM msgs.messages WHERE rowid > ? ORDER BY rowid LIMIT 5000""", (last,)).fetchall()
            if not rows:
                break
            conn.executemany("INSERT INTO fts (body, msg_id, chat_jid, source) VALUES (?, ?, ?, 'message')",
                             [(r[3], r[1], r[2]) for r in rows if r[3]])
            last = rows[-1][0]
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('messages_rowid', ?)", (last,))
            conn.commit()
    finally:
        conn.execute("DETACH DATABASE msgs")

    if os.path.exists(MEDIA_CACHE_DB_PATH):
        cache = _readonly(MEDIA_CACHE_DB_PATH)
        if cache:
            try:
                since = _meta(conn, "cache_time", 0.0)
                rows = cache.execute("SELECT message_id, chat_jid, kind, substr(text, 1, 200000), created_at FROM analysis "
                                     "WHERE created_at > ? ORDER BY created_at", (since,)).fetchall()
            except sqlite3.Error:
                rows = []
            finally:
                cache.close()
            if rows:
                conn.executemany("INSERT INTO fts (body, msg_id, chat_jid, source) VALUES (?, ?, ?, ?)",
                                 [(r[3], r[0], r[1], r[2]) for r in rows])
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('cache_time', ?)", (rows[-1][4],))
                conn.commit()


def _fts_query(query: str) -> str:
    """Every word must appear, as a word or the start of one; quoted parts must appear as a phrase."""
    parts = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', query):
        tokens = re.findall(r"\w+", phrase or word)
        if not tokens:
            continue
        if phrase:
            parts.append('"' + " ".join(tokens) + '"')
        else:
            parts.extend(f'"{t}"*' for t in tokens)
    return " ".join(parts)


SOURCE_LABEL = {"text": "match in the document's text", "transcript": "match in the voice/video transcript"}


def search_messages(query: str, chat: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
                    limit: int = 20, page: int = 0, context: int = 0) -> str:
    """Full-text search, newest first, ignoring case and accents. Also searches the text of documents and
    transcripts of voice notes that were opened before with inspect_media."""
    fts = _fts_query(query or "")
    if not fts:
        raise ValueError("Pass at least one word to search for")
    conn = _connect()
    try:
        names = Names.get(conn)
        jid = resolve_chat(conn, chat)[0] if chat else None
    finally:
        conn.close()

    search = _search_db()
    try:
        _sync_index(search)
        search.execute("ATTACH DATABASE ? AS msgs", (_uri(MESSAGES_DB_PATH),))
        where, params = _time_filter(since, until)
        if jid:
            where.append("m.chat_jid = ?")
            params.append(jid)
        extra = (" AND " + " AND ".join(where)) if where else ""
        rows = search.execute(f"""
            SELECT m.id, m.chat_jid, group_concat(DISTINCT fts.source) FROM fts
            JOIN msgs.messages m ON m.id = fts.msg_id AND m.chat_jid = fts.chat_jid
            WHERE fts MATCH ?{extra}
            GROUP BY m.id, m.chat_jid ORDER BY max(m.timestamp) DESC LIMIT ? OFFSET ?
        """, [fts] + params + [limit + 1, page * limit]).fetchall()
    finally:
        search.close()

    more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return f'No messages match "{query}".' + ("" if page == 0 else " (no more results)")

    conn = _connect()
    try:
        out = [f'Messages matching: {query}' + (f" in {names.chat(jid)}" if jid else "") +
               f", newest first ({page * limit + 1}-{page * limit + len(rows)}):"]
        for msg_id, chat_jid, sources in rows:
            hit = _fetch(conn, "m.id = ? AND m.chat_jid = ?", (msg_id, chat_jid), limit=1)
            if not hit:
                continue
            h = hit[0]
            notes = [SOURCE_LABEL[s] for s in (sources or "").split(",") if s in SOURCE_LABEL]
            note = f"  ({'; '.join(notes)} — open it with inspect_media)" if notes else ""
            if context > 0:
                ts = h.timestamp.isoformat(sep=" ")
                older = _fetch(conn, "m.chat_jid = ? AND m.timestamp < ?", (chat_jid, ts), limit=context)[::-1]
                newer = _fetch(conn, "m.chat_jid = ? AND m.timestamp > ?", (chat_jid, ts), order="m.timestamp ASC", limit=context)
                out.append(f"\n[{_clean(names.chat(chat_jid), 60)} · {chat_jid}]{note}")
                out.append(format_messages(older + [h] + newer, names, mark=h.id))
            else:
                out.append(f"{h.timestamp:%Y-%m-%d} " + format_line(h, names, with_chat=True) + note)
        if more:
            out.append(f"More: page={page + 1}")
        return "\n".join(out)
    finally:
        conn.close()


# --- attachments ------------------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]")


def safe_filename(name: str, fallback: str) -> str:
    """Same as safeFilename in the bridge (security.go): one safe path component."""
    name = (name or "").replace("\\", "/")
    stripped = name.rstrip("/")
    name = os.path.basename(stripped) if stripped else ("/" if name else "")
    name = _UNSAFE.sub("_", name).lstrip(". ")
    if len(name) > 120:
        name = name[-120:]
    return fallback if name in ("", ".", "..") else name


def local_media_path(chat_jid: str, message_id: str, filename: Optional[str], media_type: str) -> str:
    """Where the bridge stores a message's media (localMediaPath in mediastore.go)."""
    chat_dir = safe_filename(chat_jid.replace(":", "_"), "unknown_chat")
    return os.path.join(MEDIA_DIR, chat_dir, safe_filename(message_id, "message") + "_" + safe_filename(filename or "", media_type))


def _cached_kinds() -> Dict[Tuple[str, str], str]:
    cache = _readonly(MEDIA_CACHE_DB_PATH)
    if not cache:
        return {}
    try:
        return {(r[0], r[1]): r[2] for r in cache.execute("SELECT message_id, chat_jid, kind FROM analysis")}
    except sqlite3.Error:
        return {}
    finally:
        cache.close()


def media_status(msg: Message, cached: Dict[Tuple[str, str], str]) -> str:
    """Whether an attachment can be opened right away, and if not, what opening it will do."""
    kind = cached.get((msg.id, msg.chat_jid))
    if kind and msg.media_type in ("document", "audio"):
        return "text ready" if kind == "text" else "transcript ready"
    if os.path.exists(local_media_path(msg.chat_jid, msg.id, msg.media.get("filename"), msg.media_type)):
        return "saved locally"
    age = datetime.now() - msg.timestamp.replace(tzinfo=None)
    if age < timedelta(days=SERVER_MEDIA_DAYS):
        return "on WhatsApp's servers"
    return "expired on WhatsApp's servers (opening it asks the sender's phone to upload it again)"


ATTACHMENT_KINDS = ("document", "image", "video", "audio", "voice", "sticker")


def list_attachments(chat: Optional[str] = None, kind: Optional[str] = None, query: Optional[str] = None,
                     since: Optional[str] = None, until: Optional[str] = None, limit: int = 30, page: int = 0) -> str:
    """Attachments, newest first, with whether each one can be opened right away."""
    if kind and kind not in ATTACHMENT_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(ATTACHMENT_KINDS)}")
    conn = _connect()
    try:
        names = Names.get(conn)
        where, params = _time_filter(since, until)
        where.append("m.media_type IS NOT NULL AND m.media_type != ''")
        jid = None
        if chat:
            jid, _ = resolve_chat(conn, chat)
            where.append("m.chat_jid = ?")
            params.append(jid)
        if kind == "voice":
            where.append("m.media_type = 'audio' AND m.media_voice")
        elif kind:
            where.append("m.media_type = ?")
            params.append(kind)
        if query:
            where.append("(lower(m.filename) LIKE ? OR lower(m.content) LIKE ?)")
            params += [f"%{query.lower()}%"] * 2
        messages = _fetch(conn, " AND ".join(where), tuple(params), limit=limit + 1, offset=page * limit)
        more = len(messages) > limit
        messages = messages[:limit]
        if not messages:
            return "No attachments match."
        cached = _cached_kinds()
        title = f"Attachments in {_clean(names.chat(jid), 60)} ({jid})" if jid else "Attachments"
        out = [f"{title}, newest first ({page * limit + 1}-{page * limit + len(messages)}):"]
        for m in messages:
            who = names.sender(m.sender, m.is_from_me)
            chat_name = _clean(names.chat(m.chat_jid), 40)
            where_ = "" if jid else (f" · {chat_name}" if chat_name != who else "") + f" · chat {m.chat_jid}"
            caption = f' · caption: "{_clean(m.content, 100)}"' if m.content else ""
            out.append(f"- {m.timestamp:%Y-%m-%d %H:%M} · {who}{where_} · "
                       f"{describe_media(m.media_type, m.media)} · {media_status(m, cached)} · id {m.id}{caption}")
        if more:
            out.append(f"More: page={page + 1}")
        return "\n".join(out)
    finally:
        conn.close()


def get_media_info(message_id: str, chat_jid: str) -> dict:
    """Metadata of the media in a message, or raise ValueError if there is none."""
    conn = _connect()
    try:
        rows = _fetch(conn, "m.id = ? AND m.chat_jid = ?", (message_id, chat_jid), limit=1)
    finally:
        conn.close()
    if not rows:
        raise ValueError(f"Message {message_id} not found in chat {chat_jid}")
    message = rows[0]
    if not message.media_type:
        raise ValueError(f"Message {message_id} has no media")
    m = message.media
    return {
        "media_type": message.media_type,
        "filename": m.get("filename"),
        "mimetype": m.get("mimetype"),
        "file_length": m.get("file_length"),
        "voice": bool(m.get("media_voice")),
        "summary": describe_media(message.media_type, m),
    }


class DownloadError(ValueError):
    """A download failed; the message says why and what the user can do."""


# What each bridge error code (download.go) means for the user
DOWNLOAD_HELP = {
    "expired": "WhatsApp has deleted this file from its servers (it keeps media for about 2-3 weeks) and it could "
               "not be requested again. Ask the sender to send it again.",
    "not_on_phone": "WhatsApp has deleted this file from its servers and the sender's phone no longer has it either. "
                    "Ask the sender to send it again.",
    "retry_timeout": "This file had expired on WhatsApp's servers. The sender's phone was asked to upload it again "
                     "but did not answer within 30 seconds (it may be offline). Try again later.",
    "retry_limited": "This file had expired on WhatsApp's servers, and many expired files were already requested "
                     "in the last hour (the limit avoids looking like a bot). Try again later.",
    "incomplete": "WhatsApp did not include download information for this attachment, so it cannot be downloaded. "
                  "Ask the sender to send it again.",
    "not_found": "This message is not in the local message store.",
    "not_media": "This message has no attachment.",
}


def fetch_media(message_id: str, chat_jid: str) -> str:
    """Have the bridge download (or reuse) the media of a message. Returns the local path or raises DownloadError."""
    try:
        response = _post("download", {"message_id": message_id, "chat_jid": chat_jid})
    except requests.RequestException as e:
        raise DownloadError(str(e))
    try:
        result = response.json()
    except json.JSONDecodeError:
        raise DownloadError(f"Download failed: HTTP {response.status_code} - {response.text[:300]}")
    if response.status_code == 200 and result.get("success") and result.get("path"):
        return result["path"]
    detail = result.get("message") or f"HTTP {response.status_code}"
    help_text = DOWNLOAD_HELP.get(result.get("error_code") or "")
    raise DownloadError(f"{help_text} (Details: {detail})" if help_text else f"Download failed: {detail}")


def save_media(message_id: str, chat_jid: str) -> str:
    """Download an attachment and put a copy in the download folder. Returns the new file's path."""
    info = get_media_info(message_id, chat_jid)
    source = fetch_media(message_id, chat_jid)
    real, media_root = os.path.realpath(source), os.path.realpath(MEDIA_DIR)
    if os.path.commonpath([real, media_root]) != media_root or not os.path.isfile(real):
        raise DownloadError("The bridge returned an unexpected file path; refusing to copy it")

    os.makedirs(DOWNLOAD_DIR, mode=0o700, exist_ok=True)
    # The name comes from the sender: keep only safe characters, and never overwrite an existing file
    name = safe_filename(info.get("filename") or os.path.basename(real), info["media_type"])
    stem, ext = os.path.splitext(name)
    dest = os.path.join(DOWNLOAD_DIR, name)
    n = 2
    while os.path.lexists(dest):
        dest = os.path.join(DOWNLOAD_DIR, f"{stem} ({n}){ext}")
        n += 1
    try:
        os.link(real, dest)  # no extra disk space; survives the cleanup of the bridge's copy
    except OSError:
        shutil.copyfile(real, dest)
        os.chmod(dest, 0o600)
    return dest


# --- sending ----------------------------------------------------------------------------------------

def _send(payload: dict) -> Tuple[bool, str]:
    try:
        response = _post("send", payload)
    except requests.RequestException as e:
        return False, f"Request error: {e}"
    try:
        result = response.json()
    except json.JSONDecodeError:
        return False, f"Error: HTTP {response.status_code} - {response.text[:300]}"
    if response.status_code == 200:
        return result.get("success", False), result.get("message", "Unknown response")
    return False, f"Error: HTTP {response.status_code} - {result.get('message', response.text[:300])}"


def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    if not recipient:
        return False, "Recipient must be provided"
    try:
        return _send({"recipient": recipient, "message": message})
    except (ValueError, RuntimeError, bridge.BridgeUnavailable) as e:
        return False, str(e)


def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    if not recipient:
        return False, "Recipient must be provided"
    if not media_path:
        return False, "Media path must be provided"
    try:
        return _send({"recipient": recipient, "media_path": _outbox_path(media_path)})
    except (ValueError, RuntimeError, bridge.BridgeUnavailable) as e:
        return False, str(e)


def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    if not recipient:
        return False, "Recipient must be provided"
    if not media_path:
        return False, "Media path must be provided"
    try:
        media_path = _outbox_path(media_path)
        converted_path = None
        if not media_path.endswith(".ogg"):
            try:
                # Converted file goes in the outbox so the bridge accepts it
                converted_path = audio.convert_to_opus_ogg_temp(media_path, dir=OUTBOX_DIR)
                media_path = converted_path
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {e}"
        try:
            return _send({"recipient": recipient, "media_path": media_path})
        finally:
            if converted_path and os.path.exists(converted_path):
                os.unlink(converted_path)
    except (ValueError, RuntimeError, bridge.BridgeUnavailable) as e:
        return False, str(e)
