import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import os
import os.path
import sys
from functools import partial
import requests
import json
import http.client
import audio
import bridge

BRIDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge')
MESSAGES_DB_PATH = os.path.join(BRIDGE_DIR, 'store', 'messages.db')
# Only files in here can be sent; the bridge enforces the same rule
OUTBOX_DIR = os.path.realpath(os.path.expanduser(os.environ.get("WHATSAPP_OUTBOX_DIR", "~/WhatsApp-outbox")))

# stdout is the MCP stdio channel, so diagnostics must go to stderr
log = partial(print, file=sys.stderr)

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

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None
    media: Optional[dict] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

# Media metadata and link preview columns, added by newer bridges (media.go)
MEDIA_COLUMNS = ["filename", "file_length", "mimetype", "media_seconds", "media_width", "media_height",
                 "media_pages", "media_voice", "link_url", "link_title", "link_description"]
_present_columns = None

def _message_select(cursor) -> str:
    """Columns for _row_to_message. Columns an older bridge has not created yet read as NULL."""
    global _present_columns
    if _present_columns is None or not _present_columns.issuperset(MEDIA_COLUMNS):
        _present_columns = {row[1] for row in cursor.execute("PRAGMA table_info(messages)")}
    media = [f"messages.{c}" if c in _present_columns else "NULL" for c in MEDIA_COLUMNS]
    return ", ".join(["messages.timestamp", "messages.sender", "chats.name", "messages.content", "messages.is_from_me",
                      "chats.jid", "messages.id", "messages.media_type"] + media)

def _row_to_message(row) -> Message:
    media = dict(zip(MEDIA_COLUMNS, row[8:8 + len(MEDIA_COLUMNS)]))
    return Message(
        timestamp=datetime.fromisoformat(row[0]),
        sender=row[1],
        chat_name=row[2],
        content=row[3],
        is_from_me=row[4],
        chat_jid=row[5],
        id=row[6],
        media_type=row[7],
        media=media,
    )

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

def get_media_info(message_id: str, chat_jid: str) -> dict:
    """Metadata of the media in a message, or raise ValueError if there is none."""
    conn = sqlite3.connect(MESSAGES_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT {_message_select(cursor)}
            FROM messages JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ? AND messages.chat_jid = ?
        """, (message_id, chat_jid))
        row = cursor.fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(f"Message {message_id} not found in chat {chat_jid}")
    message = _row_to_message(row)
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

def get_sender_name(sender_jid: str) -> str:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # First try matching by exact JID
        cursor.execute("""
            SELECT name
            FROM chats
            WHERE jid = ?
            LIMIT 1
        """, (sender_jid,))
        
        result = cursor.fetchone()
        
        # If no result, try looking for the number within JIDs
        if not result:
            # Extract the phone number part if it's a JID
            if '@' in sender_jid:
                phone_part = sender_jid.split('@')[0]
            else:
                phone_part = sender_jid
                
            cursor.execute("""
                SELECT name
                FROM chats
                WHERE jid LIKE ?
                LIMIT 1
            """, (f"%{phone_part}%",))
            
            result = cursor.fetchone()
        
        if result and result[0]:
            return result[0]
        else:
            return sender_jid
        
    except sqlite3.Error as e:
        log(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if message.media_type:
        content_prefix = f"[{describe_media(message.media_type, message.media)} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    link = describe_link(message.media)

    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}{' ' + link if link else ''}\n"
    except Exception as e:
        log(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = [f"SELECT {_message_select(cursor)} FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        
        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            where_clauses.append("messages.sender = ?")
            params.append(sender_phone_number)
            
        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = [_row_to_message(msg) for msg in messages]
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        log(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Get the target message first
        columns = _message_select(cursor)
        cursor.execute(f"""
            SELECT {columns}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = _row_to_message(msg_data)
        
        # Get messages before
        cursor.execute(f"""
            SELECT {columns}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (target_message.chat_jid, msg_data[0], before))
        before_messages = [_row_to_message(msg) for msg in cursor.fetchall()]
        
        # Get messages after
        cursor.execute(f"""
            SELECT {columns}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (target_message.chat_jid, msg_data[0], after))
        after_messages = [_row_to_message(msg) for msg in cursor.fetchall()]
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        log(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["""
            SELECT 
                chats.jid,
                chats.name,
                chats.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats
        """]
        
        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid 
                AND chats.last_message_time = messages.timestamp
            """)
            
        where_clauses = []
        params = []
        
        if query:
            where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        log(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Split query into characters to support partial matching
        search_pattern = '%' +query + '%'
        
        cursor.execute("""
            SELECT DISTINCT 
                jid,
                name
            FROM chats
            WHERE 
                (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
                AND jid NOT LIKE '%@g.us'
            ORDER BY name, jid
            LIMIT 50
        """, (search_pattern, search_pattern))
        
        contacts = cursor.fetchall()
        
        result = []
        for contact_data in contacts:
            contact = Contact(
                phone_number=contact_data[0].split('@')[0],
                name=contact_data[1],
                jid=contact_data[0]
            )
            result.append(contact)
            
        return result
        
    except sqlite3.Error as e:
        log(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender = ? OR c.jid = ?
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (jid, jid, limit, page * limit))
        
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        log(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(f"""
            SELECT {_message_select(cursor)}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.sender = ? OR chats.jid = ?
            ORDER BY messages.timestamp DESC
            LIMIT 1
        """, (jid, jid))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        return format_message(_row_to_message(msg_data))
        
    except sqlite3.Error as e:
        log(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            """
            
        query += " WHERE c.jid = ?"
        
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        log(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
            LIMIT 1
        """, (f"%{sender_phone_number}%",))
        
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        log(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        url = "send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = _post(url, payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except (ValueError, RuntimeError, bridge.BridgeUnavailable) as e:
        return False, str(e)
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        media_path = _outbox_path(media_path)
        
        url = "send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = _post(url, payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except (ValueError, RuntimeError, bridge.BridgeUnavailable) as e:
        return False, str(e)
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        media_path = _outbox_path(media_path)
        converted_path = None

        if not media_path.endswith(".ogg"):
            try:
                # Converted file goes in the outbox so the bridge accepts it
                converted_path = audio.convert_to_opus_ogg_temp(media_path, dir=OUTBOX_DIR)
                media_path = converted_path
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"

        url = "send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }

        try:
            response = _post(url, payload)
        finally:
            if converted_path and os.path.exists(converted_path):
                os.unlink(converted_path)

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except (ValueError, RuntimeError, bridge.BridgeUnavailable) as e:
        return False, str(e)
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def fetch_media(message_id: str, chat_jid: str) -> str:
    """Have the bridge download (or reuse) the media of a message. Returns the local path or raises ValueError."""
    try:
        response = _post("download", {"message_id": message_id, "chat_jid": chat_jid})
    except requests.RequestException as e:
        raise ValueError(str(e))
    try:
        result = response.json()
    except json.JSONDecodeError:
        raise ValueError(f"Download failed: HTTP {response.status_code} - {response.text[:300]}")
    if response.status_code != 200 or not result.get("success") or not result.get("path"):
        raise ValueError(result.get("message") or f"Download failed: HTTP {response.status_code}")
    return result["path"]

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path, or None on failure."""
    try:
        path = fetch_media(message_id, chat_jid)
        log(f"Media downloaded successfully: {path}")
        return path
    except (ValueError, bridge.BridgeUnavailable) as e:
        log(f"Download failed: {e}")
        return None
