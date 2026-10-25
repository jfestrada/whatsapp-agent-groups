import os
import re
import sqlite3
import threading
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple
import requests
import json
import audio
from whisper_client import WhisperError, request_transcript

MESSAGES_DB_PATH = os.environ.get(
    "MESSAGES_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whatsapp-bridge", "store", "messages.db"),
)
WHATSAPP_API_BASE_URL = os.environ.get("WHATSAPP_API_BASE_URL", "http://localhost:8080/api")
GROUP_ENV_PATTERN = re.compile(r"^ALLOWED_GROUP_([1-9][0-9]*)$")
GROUP_JID_PATTERN = re.compile(r"^[0-9A-Za-z._-]+@g\.us$")
# None means the allowlist has not been loaded. Tools then return no chat data.
ALLOWED_CHAT_JIDS: Optional[Tuple[str, ...]] = None
_allowlist_lock = threading.Lock()
_allowlist_source: Optional[str] = None
_allowlist_stamp: Optional[tuple] = None


class AllowlistError(Exception):
    """The group allowlist is missing or does not match the database."""


def connect_db() -> sqlite3.Connection:
    """Open the message database. WAL readers need a writable directory for the shared-memory files."""
    conn = sqlite3.connect(MESSAGES_DB_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def selection_path() -> str:
    override = os.environ.get("ALLOWED_GROUPS_PATH", "").strip()
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(MESSAGES_DB_PATH)), "allowed-groups.json")


def reset_allowlist_state() -> None:
    """Drop the cached allowlist. Tests use this between cases."""
    global ALLOWED_CHAT_JIDS, _allowlist_source, _allowlist_stamp
    ALLOWED_CHAT_JIDS = None
    _allowlist_source = None
    _allowlist_stamp = None


def _parse_selection(path: str) -> Tuple[str, ...]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AllowlistError("Saved group selection could not be read") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise AllowlistError("Saved group selection could not be read")
    groups = data.get("groups")
    if not isinstance(groups, list):
        raise AllowlistError("Saved group selection could not be read")
    jids = []
    for item in groups:
        jid = item.get("jid") if isinstance(item, dict) else None
        if not isinstance(jid, str) or not GROUP_JID_PATTERN.fullmatch(jid):
            raise AllowlistError("Saved group selection could not be read")
        if jid not in jids:
            jids.append(jid)
    return tuple(jids)


def _apply_selection_file(path: str, stamp: tuple) -> None:
    global ALLOWED_CHAT_JIDS, _allowlist_source, _allowlist_stamp
    jids = _parse_selection(path)
    if jids != ALLOWED_CHAT_JIDS:
        if len(jids) == 1:
            print("Allowing 1 saved group", flush=True)
        elif jids:
            print(f"Allowing {len(jids)} saved groups", flush=True)
        else:
            print("Saved group selection is empty", flush=True)
    ALLOWED_CHAT_JIDS = jids
    _allowlist_source = "file"
    _allowlist_stamp = stamp


def _refresh_allowlist() -> None:
    """Prefer the selection file written by the bridge page, then ALLOWED_GROUP_*."""
    path = selection_path()
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        if _allowlist_source != "env":
            load_allowlist()
        return
    stamp = (stat.st_mtime_ns, stat.st_size)
    if _allowlist_source == "file" and _allowlist_stamp == stamp:
        return
    _apply_selection_file(path, stamp)


def allowed_jids() -> Tuple[str, ...]:
    with _allowlist_lock:
        _refresh_allowlist()
        if not ALLOWED_CHAT_JIDS:
            raise AllowlistError("Group allowlist is not configured")
        return ALLOWED_CHAT_JIDS


def in_clause(column: str) -> Tuple[str, Tuple[str, ...]]:
    jids = allowed_jids()
    marks = ",".join("?" for _ in jids)
    return f"{column} IN ({marks})", jids


def is_allowed_chat(jid: Optional[str]) -> bool:
    return bool(jid) and jid in allowed_jids()


def configured_groups() -> List[Tuple[str, str]]:
    """Return non-empty ALLOWED_GROUP_<n> values in numeric order."""
    found = []
    for name, value in os.environ.items():
        match = GROUP_ENV_PATTERN.match(name)
        if not match:
            continue
        text = value.strip()
        if text:
            found.append((int(match.group(1)), name, text))
    found.sort(key=lambda item: item[0])
    return [(name, text) for _, name, text in found]


def load_allowlist() -> Tuple[str, ...]:
    """Resolve the configured ALLOWED_GROUP_<n> values to group JIDs.

    Any positive number may be set, and blank values are ignored. Each name
    must match exactly one group.
    """
    global ALLOWED_CHAT_JIDS, _allowlist_source
    provided = configured_groups()
    if not provided:
        raise AllowlistError("At least one ALLOWED_GROUP_<number> value is required")

    resolved = []
    conn = connect_db()
    try:
        cursor = conn.cursor()
        for name, value in provided:
            if value.endswith("@g.us"):
                cursor.execute(
                    "SELECT jid FROM chats WHERE jid = ? AND jid LIKE '%@g.us'",
                    (value,),
                )
            else:
                cursor.execute(
                    "SELECT jid FROM chats WHERE name = ? AND jid LIKE '%@g.us'",
                    (value,),
                )
            rows = cursor.fetchall()
            if not rows:
                raise AllowlistError(f"{name} does not match a group")
            if len(rows) > 1:
                matched = ", ".join(row[0] for row in rows)
                raise AllowlistError(f"{name} matches more than one group: {matched}")
            resolved.append(rows[0][0])
    except sqlite3.Error as exc:
        raise AllowlistError(f"Could not read chats: {exc}") from exc
    finally:
        conn.close()

    if len(set(resolved)) != len(resolved):
        raise AllowlistError("ALLOWED_GROUP_* values must be different groups")

    ALLOWED_CHAT_JIDS = tuple(resolved)
    _allowlist_source = "env"
    for (name, value), jid in zip(provided, ALLOWED_CHAT_JIDS):
        print(f"Allowing {name}={value} ({jid})", flush=True)
    return ALLOWED_CHAT_JIDS


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

def get_sender_name(sender_jid: str) -> str:
    try:
        conn = connect_db()
        cursor = conn.cursor()
        
        # First try matching by exact JID
        cursor.execute("""
            SELECT name
            FROM chats
            WHERE jid = ?
            LIMIT 1
        """, (sender_jid,))
        
        result = cursor.fetchone()
        
        # Fall back to the contact's own direct-chat JID. A LIKE across every
        # chat can pick up another group's name and leak it into a digest.
        if not result:
            if "@" in sender_jid:
                phone_part = sender_jid.split("@")[0]
            else:
                phone_part = sender_jid
            cursor.execute("""
                SELECT name
                FROM chats
                WHERE jid = ? AND jid NOT LIKE '%@g.us'
                LIMIT 1
            """, (f"{phone_part}@s.whatsapp.net",))
            result = cursor.fetchone()
        
        if result and result[0]:
            return result[0]
        else:
            return sender_jid
        
    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

def media_placeholder(message: Message) -> str:
    return f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "


def transcript_for(message: Message) -> Optional[str]:
    """Return the voice-note text, or None so the caller keeps the media placeholder."""
    if getattr(message, "media_type", None) != "audio":
        return None
    if not os.environ.get("WHISPER_MCP_URL", "").strip():
        return None
    try:
        path = download_media(message.id, message.chat_jid)
        if not path:
            return None
        text = request_transcript(path)
    except (WhisperError, OSError, ValueError) as exc:
        print(f"Transcription failed: {exc}")
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    if message.content:
        return f"{text.strip()} {message.content}".strip()
    return text.strip()


def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    body = message.content
    if hasattr(message, "media_type") and message.media_type:
        transcript = transcript_for(message)
        if transcript:
            body = transcript
        else:
            content_prefix = media_placeholder(message)
    
    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{body}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
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
        conn = connect_db()
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        try:
            allow_clause, allow_params = in_clause("messages.chat_jid")
        except AllowlistError as exc:
            print(f"Allowlist error: {exc}")
            return format_messages_list([])
        if chat_jid and chat_jid not in allow_params:
            return format_messages_list([])
        
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

        where_clauses.append(allow_clause)
        params.extend(allow_params)
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            )
            result.append(message)
            
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
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def count_messages(chat_jid: str, after: Optional[str] = None) -> int:
    """Count messages in one allowed chat. This does not read or transcribe content."""
    try:
        allow_clause, allow_params = in_clause("messages.chat_jid")
    except AllowlistError as exc:
        print(f"Allowlist error: {exc}")
        return 0
    if not chat_jid or chat_jid not in allow_params:
        return 0
    where_clauses = ["messages.chat_jid = ?", allow_clause]
    params: list = [chat_jid, *allow_params]
    if after:
        try:
            after_value = datetime.fromisoformat(after)
        except ValueError:
            raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
        where_clauses.append("messages.timestamp > ?")
        params.append(after_value)
    try:
        conn = connect_db()
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE " + " AND ".join(where_clauses),
            tuple(params),
        ).fetchone()
        return int(row[0] if row else 0)
    except sqlite3.Error as exc:
        print(f"Database error: {exc}")
        return 0
    finally:
        if "conn" in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = connect_db()
        cursor = conn.cursor()

        try:
            allowed = allowed_jids()
        except AllowlistError as exc:
            print(f"Allowlist error: {exc}")
            raise ValueError(f"Message with ID {message_id} not found")
        cursor.execute("SELECT chat_jid FROM messages WHERE id = ?", (message_id,))
        located = cursor.fetchone()
        if not located or located[0] not in allowed:
            raise ValueError(f"Message with ID {message_id} not found")
        
        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )
        
        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))
        
        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))
        
        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
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
        conn = connect_db()
        cursor = conn.cursor()
        
        # Build base query. The message columns require the join; without it SQLite
        # reports "no such column: messages.content".
        if include_last_message:
            query_parts = ["""
                SELECT
                    chats.jid,
                    chats.name,
                    chats.last_message_time,
                    messages.content as last_message,
                    messages.sender as last_sender,
                    messages.is_from_me as last_is_from_me
                FROM chats
                LEFT JOIN messages ON chats.jid = messages.chat_jid
                    AND chats.last_message_time = messages.timestamp
            """]
        else:
            query_parts = ["""
                SELECT
                    chats.jid,
                    chats.name,
                    chats.last_message_time,
                    NULL as last_message,
                    NULL as last_sender,
                    NULL as last_is_from_me
                FROM chats
            """]
            
        where_clauses = []
        params = []
        try:
            allow_clause, allow_params = in_clause("chats.jid")
        except AllowlistError as exc:
            print(f"Allowlist error: {exc}")
            return []
        where_clauses.append(allow_clause)
        params.extend(allow_params)
        
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
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    try:
        conn = connect_db()
        cursor = conn.cursor()
        
        # Split query into characters to support partial matching
        search_pattern = '%' +query + '%'
        try:
            allow_clause, allow_params = in_clause("chat_jid")
        except AllowlistError as exc:
            print(f"Allowlist error: {exc}")
            return []
        
        cursor.execute(f"""
            SELECT DISTINCT 
                jid,
                name
            FROM chats
            WHERE 
                (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
                AND jid NOT LIKE '%@g.us'
                AND jid IN (
                    SELECT sender FROM messages WHERE {allow_clause}
                )
            ORDER BY name, jid
            LIMIT 50
        """, (search_pattern, search_pattern, *allow_params))
        
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
        print(f"Database error: {e}")
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
        conn = connect_db()
        cursor = conn.cursor()
        try:
            allow_clause, allow_params = in_clause("c.jid")
        except AllowlistError as exc:
            print(f"Allowlist error: {exc}")
            return []
        
        cursor.execute(f"""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE (m.sender = ? OR c.jid = ?)
                AND {allow_clause}
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (jid, jid, *allow_params, limit, page * limit))
        
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
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = connect_db()
        cursor = conn.cursor()
        try:
            allow_clause, allow_params = in_clause("c.jid")
        except AllowlistError as exc:
            print(f"Allowlist error: {exc}")
            return None
        
        cursor.execute(f"""
            SELECT 
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE (m.sender = ? OR c.jid = ?)
                AND {allow_clause}
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, (jid, jid, *allow_params))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        if not is_allowed_chat(chat_jid):
            return None
    except AllowlistError as exc:
        print(f"Allowlist error: {exc}")
        return None
    try:
        conn = connect_db()
        cursor = conn.cursor()
        
        if include_last_message:
            query = """
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
                WHERE c.jid = ?
            """
        else:
            query = """
                SELECT
                    c.jid,
                    c.name,
                    c.last_message_time,
                    NULL as last_message,
                    NULL as last_sender,
                    NULL as last_is_from_me
                FROM chats c
                WHERE c.jid = ?
            """
        
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
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Direct chats are outside the allowed groups."""
    return None


def recipient_refusal(recipient: str) -> Optional[str]:
    """Return an error when recipient is not one of the allowed groups."""
    if not recipient:
        return "Recipient must be provided"
    try:
        if is_allowed_chat(recipient):
            return None
    except AllowlistError as exc:
        print(f"Allowlist error: {exc}")
    return "Recipient is not an allowed group"


def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    refusal = recipient_refusal(recipient)
    if refusal:
        return False, refusal
    try:
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    refusal = recipient_refusal(recipient)
    if refusal:
        return False, refusal
    try:
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    refusal = recipient_refusal(recipient)
    if refusal:
        return False, refusal
    try:
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        if not is_allowed_chat(chat_jid):
            return None
    except AllowlistError as exc:
        print(f"Allowlist error: {exc}")
        return None
    try:
        conn = connect_db()
        row = conn.execute(
            "SELECT 1 FROM messages WHERE id = ? AND chat_jid = ?",
            (message_id, chat_jid),
        ).fetchone()
    except sqlite3.Error as exc:
        print(f"Database error: {exc}")
        return None
    finally:
        if "conn" in locals():
            conn.close()
    if row is None:
        return None
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None
