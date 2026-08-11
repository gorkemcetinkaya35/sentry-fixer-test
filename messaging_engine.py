"""
Real-Time Messaging Platform Backend
Handles user sessions, message routing, group chats,
file attachments, encryption, and notification delivery.
"""

import asyncio
import json
import hashlib
import hmac
import base64
import secrets
import time
import uuid
import re
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Set, Tuple, Any, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from collections import defaultdict, OrderedDict
from weakref import WeakValueDictionary
from contextlib import asynccontextmanager
from functools import wraps
import struct
import zlib

logger = logging.getLogger(__name__)


# ─── Enums ────────────────────────────────────────────────────────────

class MessageType(Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    AUDIO = "audio"
    VIDEO = "video"
    SYSTEM = "system"
    REACTION = "reaction"
    REPLY = "reply"
    FORWARD = "forward"
    LOCATION = "location"


class UserStatus(Enum):
    ONLINE = "online"
    AWAY = "away"
    DND = "do_not_disturb"
    OFFLINE = "offline"
    INVISIBLE = "invisible"


class ConversationType(Enum):
    DIRECT = "direct"
    GROUP = "group"
    CHANNEL = "channel"
    BROADCAST = "broadcast"


class DeliveryStatus(Enum):
    SENT = auto()
    DELIVERED = auto()
    READ = auto()
    FAILED = auto()


class MemberRole(Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MODERATOR = "moderator"
    MEMBER = "member"
    GUEST = "guest"


# ─── Data Models ──────────────────────────────────────────────────────

@dataclass
class UserProfile:
    user_id: str
    username: str
    display_name: str
    email: str
    avatar_url: Optional[str] = None
    status: UserStatus = UserStatus.OFFLINE
    bio: str = ""
    last_seen: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    settings: Dict[str, Any] = field(default_factory=dict)
    blocked_users: Set[str] = field(default_factory=set)
    muted_conversations: Set[str] = field(default_factory=set)

    def is_blocked(self, other_user_id: str) -> bool:
        return other_user_id in self.blocked_users

    def to_public_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "status": self.status.value if self.status != UserStatus.INVISIBLE else UserStatus.OFFLINE.value,
            "bio": self.bio,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


@dataclass
class Message:
    id: str
    conversation_id: str
    sender_id: str
    content: str
    message_type: MessageType = MessageType.TEXT
    reply_to: Optional[str] = None
    attachments: List[Dict] = field(default_factory=list)
    reactions: Dict[str, List[str]] = field(default_factory=dict)
    mentions: List[str] = field(default_factory=list)
    edited: bool = False
    edited_at: Optional[datetime] = None
    deleted: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    delivery_status: Dict[str, DeliveryStatus] = field(default_factory=dict)

    def to_dict(self, viewer_id: Optional[str] = None) -> dict:
        if self.deleted:
            return {
                "id": self.id,
                "conversation_id": self.conversation_id,
                "deleted": True,
                "created_at": self.created_at.isoformat(),
            }
        result = {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "sender_id": self.sender_id,
            "content": self.content,
            "message_type": self.message_type.value,
            "reply_to": self.reply_to,
            "attachments": self.attachments,
            "reactions": self.reactions,
            "mentions": self.mentions,
            "edited": self.edited,
            "created_at": self.created_at.isoformat(),
        }
        if viewer_id and viewer_id in self.delivery_status:
            result["delivery_status"] = self.delivery_status[viewer_id].name.lower()
        return result


@dataclass
class Conversation:
    id: str
    conv_type: ConversationType
    name: Optional[str] = None
    description: str = ""
    avatar_url: Optional[str] = None
    creator_id: Optional[str] = None
    members: Dict[str, MemberRole] = field(default_factory=dict)
    pinned_messages: List[str] = field(default_factory=list)
    settings: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_message_at: Optional[datetime] = None
    is_archived: bool = False

    def has_member(self, user_id: str) -> bool:
        return user_id in self.members

    def get_role(self, user_id: str) -> Optional[MemberRole]:
        return self.members.get(user_id)

    def can_send_message(self, user_id: str) -> bool:
        if not self.has_member(user_id):
            return False
        if self.conv_type == ConversationType.BROADCAST:
            return self.members[user_id] in (MemberRole.OWNER, MemberRole.ADMIN)
        return True

    def can_manage_members(self, user_id: str) -> bool:
        role = self.members.get(user_id)
        return role in (MemberRole.OWNER, MemberRole.ADMIN, MemberRole.MODERATOR)


@dataclass
class FileAttachment:
    id: str
    filename: str
    file_size: int
    mime_type: str
    url: str
    thumbnail_url: Optional[str] = None
    checksum: Optional[str] = None
    uploaded_by: Optional[str] = None
    uploaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

    def validate(self) -> Tuple[bool, str]:
        if self.file_size > self.MAX_FILE_SIZE:
            return False, f"File exceeds maximum size of {self.MAX_FILE_SIZE} bytes"

        dangerous_extensions = ['.exe', '.bat', '.cmd', '.scr', '.pif', '.js']
        ext = os.path.splitext(self.filename)[1].lower()
        if ext in dangerous_extensions:
            return False, f"File type {ext} is not allowed"

        if not re.match(r'^[\w\-. ()]+$', self.filename):
            return False, "Filename contains invalid characters"

        return True, ""


# ─── Rate Limiter ─────────────────────────────────────────────────────

class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[str, List[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        window_start = now - self.window_seconds

        self._requests[key] = [
            ts for ts in self._requests[key] if ts > window_start
        ]

        if len(self._requests[key]) >= self.max_requests:
            return False

        self._requests[key].append(now)
        return True

    def get_remaining(self, key: str) -> int:
        now = time.time()
        window_start = now - self.window_seconds
        active = [ts for ts in self._requests[key] if ts > window_start]
        return max(0, self.max_requests - len(active))

    def reset(self, key: str) -> None:
        if key in self._requests:
            del self._requests[key]

    def cleanup(self) -> int:
        now = time.time()
        cleaned = 0
        expired_keys = []

        for key, timestamps in self._requests.items():
            self._requests[key] = [
                ts for ts in timestamps if ts > now - self.window_seconds
            ]
            if not self._requests[key]:
                expired_keys.append(key)

        for key in expired_keys:
            del self._requests[key]
            cleaned += 1

        return cleaned


# ─── Message Encryption ──────────────────────────────────────────────

class MessageEncryption:
    def __init__(self, master_key: Optional[bytes] = None):
        self._master_key = master_key or secrets.token_bytes(32)
        self._key_cache: Dict[str, bytes] = {}

    def derive_key(self, conversation_id: str) -> bytes:
        if conversation_id in self._key_cache:
            return self._key_cache[conversation_id]

        key = hashlib.pbkdf2_hmac(
            'sha256',
            self._master_key,
            conversation_id.encode('utf-8'),
            iterations=100000,
        )
        self._key_cache[conversation_id] = key
        return key

    def encrypt_message(self, content: str, conversation_id: str) -> str:
        key = self.derive_key(conversation_id)
        nonce = secrets.token_bytes(12)
        content_bytes = content.encode('utf-8')

        checksum = zlib.crc32(content_bytes) & 0xFFFFFFFF
        payload = struct.pack('>I', checksum) + content_bytes

        xor_stream = self._generate_stream(key, nonce, len(payload))
        encrypted = bytes(a ^ b for a, b in zip(payload, xor_stream))

        result = nonce + encrypted
        return base64.b64encode(result).decode('ascii')

    def decrypt_message(self, encrypted_content: str, conversation_id: str) -> str:
        key = self.derive_key(conversation_id)
        raw = base64.b64decode(encrypted_content)

        nonce = raw[:12]
        ciphertext = raw[12:]

        xor_stream = self._generate_stream(key, nonce, len(ciphertext))
        decrypted = bytes(a ^ b for a, b in zip(ciphertext, xor_stream))

        # Ensure at least 4 bytes are present for the checksum
        if len(decrypted) < 8:
            raise ValueError("Invalid message length")

        stored_checksum = struct.unpack('>I', decrypted[:4])[0]
        content_bytes = decrypted[4:]

        actual_checksum = zlib.crc32(content_bytes) & 0xFFFFFFFF
        if stored_checksum != actual_checksum:
            raise ValueError("Message integrity check failed")

        return content_bytes.decode('utf-8')

    def _generate_stream(self, key: bytes, nonce: bytes, length: int) -> bytes:
        stream = b""
        counter = 0
        while len(stream) < length:
            block_input = key + nonce + struct.pack('>Q', counter)
            block = hashlib.sha256(block_input).digest()
            stream += block
            counter += 1
        return stream[:length]


# ─── LRU Cache for Messages ──────────────────────────────────────────

class MessageCache:
    def __init__(self, max_size: int = 10000):
        self._max_size = max_size
        self._cache: OrderedDict[str, Message] = OrderedDict()
        self._conversation_index: Dict[str, List[str]] = defaultdict(list)
        self._hits = 0
        self._misses = 0

    def get(self, message_id: str) -> Optional[Message]:
        if message_id in self._cache:
            self._cache.move_to_end(message_id)
            self._hits += 1
            return self._cache[message_id]
        self._misses += 1
        return None

    def put(self, message: Message) -> None:
        if message.id in self._cache:
            self._cache.move_to_end(message.id)
            self._cache[message.id] = message
            return

        if len(self._cache) >= self._max_size:
            evicted_id, _ = self._cache.popitem(last=False)
            for conv_id, msg_ids in self._conversation_index.items():
                if evicted_id in msg_ids:
                    msg_ids.remove(evicted_id)
                    break

        self._cache[message.id] = message
        self._conversation_index[message.conversation_id].append(message.id)

    def get_conversation_messages(
        self, conversation_id: str, limit: int = 50
    ) -> List[Message]:
        msg_ids = self._conversation_index.get(conversation_id, [])
        messages = []
        for mid in msg_ids[-limit:]:
            msg = self._cache.get(mid)
            if msg:
                messages.append(msg)
        return messages

    def invalidate(self, message_id: str) -> bool:
        if message_id in self._cache:
            msg = self._cache.pop(message_id)
            conv_msgs = self._conversation_index.get(msg.conversation_id, [])
            if message_id in conv_msgs:
                conv_msgs.remove(message_id)
            return True
        return False

    def invalidate_conversation(self, conversation_id: str) -> int:
        msg_ids = self._conversation_index.pop(conversation_id, [])
        count = 0
        for mid in msg_ids:
            if mid in self._cache:
                del self._cache[mid]
                count += 1
        return count

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total != 0 else 0.0

    @property
    def size(self) -> int:
        return len(self._cache)


# ─── Notification Manager ────────────────────────────────────────────

class NotificationManager:
    def __init__(self):
        self._push_tokens: Dict[str, List[str]] = defaultdict(list)
        self._preferences: Dict[str, Dict[str, bool]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._batch_size = 100
        self._batch_interval = 5.0
        self._running = False

    def register_push_token(self, user_id: str, token: str) -> None:
        if token not in self._push_tokens[user_id]:
            self._push_tokens[user_id].append(token)

    def unregister_push_token(self, user_id: str, token: str) -> None:
        tokens = self._push_tokens.get(user_id, [])
        if token in tokens:
            tokens.remove(token)

    def set_preferences(self, user_id: str, prefs: Dict[str, bool]) -> None:
        self._preferences[user_id] = {**self._preferences.get(user_id, {}), **prefs}

    def should_notify(self, user_id: str, notification_type: str) -> bool:
        prefs = self._preferences.get(user_id, {})
        return prefs.get(notification_type, True)

    async def enqueue_notification(
        self, user_id: str, title: str, body: str,
        data: Optional[Dict] = None, notification_type: str = "message"
    ) -> None:
        if not self.should_notify(user_id, notification_type):
            return

        await self._queue.put({
            "user_id": user_id,
            "title": title,
            "body": body,
            "data": data or {},
            "type": notification_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def start_batch_processor(self) -> None:
        self._running = True
        while self._running:
            batch = []
            try:
                while len(batch) < self._batch_size:
                    try:
                        item = await asyncio.wait_for(
                            self._queue.get(), timeout=self._batch_interval
                        )
                        batch.append(item)
                    except asyncio.TimeoutError:
                        break

                if batch:
                    await self._send_batch(batch)
            except Exception as e:
                logger.error(f"Notification batch processing error: {e}")

    async def _send_batch(self, batch: List[Dict]) -> None:
        grouped = defaultdict(list)
        for notification in batch:
            grouped[notification["user_id"]].append(notification)

        for user_id, notifications in grouped.items():
            tokens = self._push_tokens.get(user_id, [])
            if not tokens:
                continue

            for token in tokens:
                try:
                    await self._send_push(token, notifications)
                except Exception as e:
                    logger.warning(f"Push send failed for {user_id}: {e}")

    async def _send_push(self, token: str, notifications: List[Dict]) -> None:
        await asyncio.sleep(0.01)
        logger.debug(f"Sent {len(notifications)} notifications to token {token[:8]}...")

    def stop(self) -> None:
        self._running = False


# ─── Messaging Engine ────────────────────────────────────────────────

class MessagingEngine:
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._users: Dict[str, UserProfile] = {}
        self._conversations: Dict[str, Conversation] = {}
        self._messages: Dict[str, Dict[str, Message]] = defaultdict(dict)
        self._user_conversations: Dict[str, Set[str]] = defaultdict(set)
        self._active_connections: Dict[str, Set[Any]] = defaultdict(set)
        self._typing_indicators: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._read_receipts: Dict[str, Dict[str, str]] = defaultdict(dict)

        self._rate_limiter = SlidingWindowRateLimiter(
            max_requests=self.config.get("rate_limit", 30),
            window_seconds=self.config.get("rate_window", 60),
        )
        self._encryption = MessageEncryption()
        self._cache = MessageCache(
            max_size=self.config.get("cache_size", 10000)
        )
        self._notifications = NotificationManager()
        self._event_handlers: Dict[str, List[Callable]] = defaultdict(list)

    # ─── User Management ──────────────────────────────────────────

    def register_user(self, profile: UserProfile) -> bool:
        if profile.user_id in self._users:
            return False

        existing = [u for u in self._users.values() if u.username == profile.username]
        if existing:
            raise ValueError(f"Username '{profile.username}' is already taken")

        if not self._validate_username(profile.username):
            raise ValueError("Invalid username format")

        if not self._validate_email(profile.email):
            raise ValueError("Invalid email format")

        self._users[profile.user_id] = profile
        self._emit_event("user_registered", {"user_id": profile.user_id})
        return True

    def update_user_status(self, user_id: str, status: UserStatus) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False

        old_status = user.status
        user.status = status

        if status == UserStatus.OFFLINE:
            user.last_seen = datetime.now(timezone.utc)

        if old_status != status:
            asyncio.ensure_future(self._broadcast_presence(user_id, status))

        return True

    def block_user(self, blocker_id: str, blocked_id: str) -> bool:
        blocker = self._users.get(blocker_id)
        if not blocker:
            return False

        blocker.blocked_users.add(blocked_id)

        direct_convs = [
            conv for conv in self._conversations.values()
            if conv.conv_type == ConversationType.DIRECT
            and blocker_id in conv.members
            and blocked_id in conv.members
        ]

        for conv in direct_convs:
            conv.is_archived = True

        return True

    def get_user_contacts(self, user_id: str) -> List[Dict]:
        user = self._users.get(user_id)
        if not user:
            return []

        contact_ids = set()
        for conv_id in self._user_conversations.get(user_id, set()):
            conv = self._conversations.get(conv_id)
            if conv and conv.conv_type == ConversationType.DIRECT:
                for member_id in conv.members:
                    if member_id != user_id:
                        contact_ids.add(member_id)

        contacts = []
        for cid in contact_ids:
            contact = self._users.get(cid)
            if contact and cid not in user.blocked_users:
                contacts.append(contact.to_public_dict())

        return sorted(contacts, key=lambda c: c["display_name"])

    # ─── Conversation Management ──────────────────────────────────

    def create_conversation(
        self,
        creator_id: str,
        conv_type: ConversationType,
        member_ids: List[str],
        name: Optional[str] = None,
        settings: Optional[Dict] = None,
    ) -> Optional[Conversation]:
        creator = self._users.get(creator_id)
        if not creator:
            return None

        if conv_type == ConversationType.DIRECT:
            if len(member_ids) != 1:
                raise ValueError("Direct conversations require exactly one other member")

            other_id = member_ids[0]
            other_user = self._users.get(other_id)
            if not other_user:
                raise ValueError(f"User {other_id} not found")

            if creator.is_blocked(other_id) or other_user.is_blocked(creator_id):
                raise ValueError("Cannot create conversation with blocked user")

            existing = self._find_direct_conversation(creator_id, other_id)
            if existing:
                return existing

        all_member_ids = [creator_id] + member_ids
        members = {}
        for mid in all_member_ids:
            if mid not in self._users:
                continue
            if mid == creator_id:
                members[mid] = MemberRole.OWNER
            else:
                members[mid] = MemberRole.MEMBER

        conv = Conversation(
            id=str(uuid.uuid4()),
            conv_type=conv_type,
            name=name,
            creator_id=creator_id,
            members=members,
            settings=settings or {},
        )

        self._conversations[conv.id] = conv
        for mid in members:
            self._user_conversations[mid].add(conv.id)

        system_msg = self._create_system_message(
            conv.id,
            f"{creator.display_name} created the conversation"
        )
        self._store_message(system_msg)

        return conv

    def add_member(
        self, conversation_id: str, adder_id: str, new_member_id: str,
        role: MemberRole = MemberRole.MEMBER,
    ) -> bool:
        conv = self._conversations.get(conversation_id)
        if not conv:
            return False

        if conv.conv_type == ConversationType.DIRECT:
            return False

        if not conv.can_manage_members(adder_id):
            return False

        if conv.has_member(new_member_id):
            return False

        new_user = self._users.get(new_member_id)
        if not new_user:
            return False

        conv.members[new_member_id] = role
        self._user_conversations[new_member_id].add(conversation_id)
        conv.updated_at = datetime.now(timezone.utc)

        adder = self._users[adder_id]
        sys_msg = self._create_system_message(
            conversation_id,
            f"{adder.display_name} added {new_user.display_name}"
        )
        self._store_message(sys_msg)

        return True

    def remove_member(
        self, conversation_id: str, remover_id: str, target_id: str
    ) -> bool:
        conv = self._conversations.get(conversation_id)
        if not conv:
            return False

        if not conv.has_member(target_id):
            return False

        if remover_id != target_id and not conv.can_manage_members(remover_id):
            return False

        remover_role = conv.get_role(remover_id)
        target_role = conv.get_role(target_id)

        role_hierarchy = {
            MemberRole.OWNER: 4,
            MemberRole.ADMIN: 3,
            MemberRole.MODERATOR: 2,
            MemberRole.MEMBER: 1,
            MemberRole.GUEST: 0,
        }

        if remover_id != target_id:
            if role_hierarchy[remover_role] < role_hierarchy[target_role]:
                return False

        del conv.members[target_id]
        self._user_conversations[target_id].discard(conversation_id)
        conv.updated_at = datetime.now(timezone.utc)

        return True

    def get_user_conversations(
        self, user_id: str, include_archived: bool = False
    ) -> List[Dict]:
        conv_ids = self._user_conversations.get(user_id, set())
        result = []

        for conv_id in conv_ids:
            conv = self._conversations.get(conv_id)
            if not conv:
                continue
            if conv.is_archived and not include_archived:
                continue

            conv_data = {
                "id": conv.id,
                "type": conv.conv_type.value,
                "name": conv.name or self._get_conversation_display_name(conv, user_id),
                "last_message": self._get_last_message(conv_id),
                "unread_count": self._get_unread_count(conv_id, user_id),
                "members_count": len(conv.members),
                "is_muted": conv_id in self._users[user_id].muted_conversations,
                "updated_at": conv.updated_at.isoformat(),
            }
            result.append(conv_data)

        result.sort(key=lambda c: c["updated_at"], reverse=True)
        return result

    # ─── Messaging ────────────────────────────────────────────────

    async def send_message(
        self,
        sender_id: str,
        conversation_id: str,
        content: str,
        message_type: MessageType = MessageType.TEXT,
        reply_to: Optional[str] = None,
        attachments: Optional[List[Dict]] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[Message]:
        if not self._rate_limiter.is_allowed(sender_id):
            raise PermissionError("Rate limit exceeded. Please slow down.")

        sender = self._users.get(sender_id)
        if not sender:
            return None

        conv = self._conversations.get(conversation_id)
        if not conv:
            return None

        if not conv.can_send_message(sender_id):
            raise PermissionError("You don't have permission to send messages here")

        if message_type == MessageType.TEXT:
            if not content or len(content.strip()) == 0:
                raise ValueError("Message content cannot be empty")
            if len(content) > 10000:
                raise ValueError("Message too long (max 10000 characters)")

        mentions = self._extract_mentions(content)
        content = self._sanitize_content(content)

        encrypted_content = content
        if conv.settings.get("encryption_enabled", False):
            encrypted_content = self._encryption.encrypt_message(content, conversation_id)

        message = Message(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            sender_id=sender_id,
            content=encrypted_content,
            message_type=message_type,
            reply_to=reply_to,
            attachments=attachments or [],
            mentions=mentions,
            metadata=metadata or {},
        )

        self._store_message(message)
        conv.last_message_at = message.created_at
        conv.updated_at = message.created_at

        await self._deliver_message(message, conv)

        for mentioned_id in mentions:
            if mentioned_id != sender_id:
                await self._notifications.enqueue_notification(
                    mentioned_id,
                    f"{sender.display_name} mentioned you",
                    content[:100],
                    {"conversation_id": conversation_id, "message_id": message.id},
                    "mention",
                )

        self._emit_event("message_sent", {
            "message_id": message.id,
            "conversation_id": conversation_id,
            "sender_id": sender_id,
        })

        return message

    async def edit_message(
        self, message_id: str, editor_id: str, new_content: str,
        conversation_id: str,
    ) -> bool:
        messages = self._messages.get(conversation_id, {})
        message = messages.get(message_id)

        if not message:
            return False

        if message.sender_id != editor_id:
            return False

        age = datetime.now(timezone.utc) - message.created_at
        max_edit_window = timedelta(hours=24)
        if age > max_edit_window:
            return False

        message.content = self._sanitize_content(new_content)
        message.edited = True
        message.edited_at = datetime.now(timezone.utc)
        message.mentions = self._extract_mentions(new_content)

        self._cache.invalidate(message_id)

        await self._broadcast_to_conversation(conversation_id, {
            "type": "message_edited",
            "message_id": message_id,
            "new_content": message.content,
            "edited_at": message.edited_at.isoformat(),
        })

        return True

    async def delete_message(
        self, message_id: str, deleter_id: str, conversation_id: str
    ) -> bool:
        messages = self._messages.get(conversation_id, {})
        message = messages.get(message_id)

        if not message:
            return False

        conv = self._conversations.get(conversation_id)
        is_admin = conv and conv.get_role(deleter_id) in (
            MemberRole.OWNER, MemberRole.ADMIN, MemberRole.MODERATOR
        )

        if message.sender_id != deleter_id and not is_admin:
            return False

        message.deleted = True
        message.content = ""
        message.attachments = []

        self._cache.invalidate(message_id)

        await self._broadcast_to_conversation(conversation_id, {
            "type": "message_deleted",
            "message_id": message_id,
        })

        return True

    async def add_reaction(
        self, message_id: str, user_id: str,
        conversation_id: str, emoji: str,
    ) -> bool:
        messages = self._messages.get(conversation_id, {})
        message = messages.get(message_id)

        if not message or message.deleted:
            return False

        if not self._validate_emoji(emoji):
            return False

        if emoji not in message.reactions:
            message.reactions[emoji] = []

        if user_id in message.reactions[emoji]:
            message.reactions[emoji].remove(user_id)
            if not message.reactions[emoji]:
                del message.reactions[emoji]
        else:
            message.reactions[emoji].append(user_id)

        self._cache.invalidate(message_id)

        await self._broadcast_to_conversation(conversation_id, {
            "type": "reaction_updated",
            "message_id": message_id,
            "reactions": message.reactions,
        })

        return True

    def get_conversation_messages(
        self,
        conversation_id: str,
        user_id: str,
        before: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        conv = self._conversations.get(conversation_id)
        if not conv or not conv.has_member(user_id):
            return []

        cached = self._cache.get_conversation_messages(conversation_id, limit)
        if cached and len(cached) >= limit:
            messages = cached
        else:
            all_msgs = list(self._messages.get(conversation_id, {}).values())
            all_msgs.sort(key=lambda m: m.created_at)
            messages = all_msgs

        if before:
            messages = [m for m in messages if m.id != before and m.created_at < self._get_message_time(before, conversation_id)]

        messages = messages[-limit:]

        result = []
        for msg in messages:
            msg_dict = msg.to_dict(viewer_id=user_id)
            if conv.settings.get("encryption_enabled") and not msg.deleted:
                try:
                    msg_dict["content"] = self._encryption.decrypt_message(
                        msg.content, conversation_id
                    )
                except Exception:
                    msg_dict["content"] = "[Decryption failed]"
            result.append(msg_dict)

        return result

    # ─── Typing & Presence ────────────────────────────────────────

    async def set_typing(self, user_id: str, conversation_id: str) -> None:
        self._typing_indicators[conversation_id][user_id] = time.time()

        await self._broadcast_to_conversation(
            conversation_id,
            {"type": "typing", "user_id": user_id},
            exclude_user=user_id,
        )

    def get_typing_users(self, conversation_id: str) -> List[str]:
        now = time.time()
        typing_timeout = 5.0
        typing = self._typing_indicators.get(conversation_id, {})

        active_typers = [
            uid for uid, ts in typing.items()
            if now - ts < typing_timeout
        ]

        expired = [uid for uid, ts in typing.items() if now - ts >= typing_timeout]
        for uid in expired:
            del typing[uid]

        return active_typers

    async def mark_as_read(
        self, user_id: str, conversation_id: str, message_id: str
    ) -> None:
        self._read_receipts[conversation_id][user_id] = message_id

        messages = self._messages.get(conversation_id, {})
        for msg in messages.values():
            if msg.sender_id != user_id and msg.id <= message_id:
                msg.delivery_status[user_id] = DeliveryStatus.READ

        await self._broadcast_to_conversation(
            conversation_id,
            {
                "type": "read_receipt",
                "user_id": user_id,
                "last_read_message_id": message_id,
            },
            exclude_user=user_id,
        )

    # ─── Search ───────────────────────────────────────────────────

    def search_messages(
        self, user_id: str, query: str,
        conversation_id: Optional[str] = None,
        message_type: Optional[MessageType] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 25,
    ) -> List[Dict]:
        if len(query) < 2:
            return []

        query_lower = query.lower()
        user_convs = self._user_conversations.get(user_id, set())
        results = []

        search_convs = [conversation_id] if conversation_id else user_convs

        for conv_id in search_convs:
            if conv_id not in user_convs:
                continue

            messages = self._messages.get(conv_id, {})
            for msg in messages.values():
                if msg.deleted:
                    continue

                if message_type and msg.message_type != message_type:
                    continue

                if date_from and msg.created_at < date_from:
                    continue
                if date_to and msg.created_at > date_to:
                    continue

                if query_lower in msg.content.lower():
                    conv = self._conversations.get(conv_id)
                    results.append({
                        "message": msg.to_dict(viewer_id=user_id),
                        "conversation_id": conv_id,
                        "conversation_name": conv.name if conv else "Unknown",
                        "relevance_score": self._calculate_relevance(
                            msg.content, query_lower
                        ),
                    })

        results.sort(key=lambda r: r["relevance_score"], reverse=True)
        return results[:limit]

    def _calculate_relevance(self, content: str, query: str) -> float:
        content_lower = content.lower()
        exact_matches = content_lower.count(query)
        words = content_lower.split()
        word_matches = sum(1 for w in words if query in w)

        score = exact_matches * 2.0 + word_matches * 0.5

        if content_lower.startswith(query):
            score += 3.0

        length_penalty = min(1.0, 100 / max(len(content), 1))
        score *= length_penalty

        return round(score, 2)

    # ─── Internal Helpers ─────────────────────────────────────────

    def _store_message(self, message: Message) -> None:
        self._messages[message.conversation_id][message.id] = message
        self._cache.put(message)

    async def _deliver_message(self, message: Message, conv: Conversation) -> None:
        payload = {
            "type": "new_message",
            "message": message.to_dict(),
        }

        for member_id in conv.members:
            if member_id == message.sender_id:
                continue

            message.delivery_status[member_id] = DeliveryStatus.SENT

            connections = self._active_connections.get(member_id, set())
            if connections:
                message.delivery_status[member_id] = DeliveryStatus.DELIVERED
                for conn in connections:
                    try:
                        await conn.send_json(payload)
                    except Exception:
                        logger.warning(f"Failed to deliver to {member_id}")

            user = self._users.get(member_id)
            if user and conv.id not in user.muted_conversations:
                sender = self._users.get(message.sender_id)
                sender_name = sender.display_name if sender else "Someone"
                await self._notifications.enqueue_notification(
                    member_id,
                    sender_name,
                    message.content[:150],
                    {"conversation_id": conv.id, "message_id": message.id},
                )

    async def _broadcast_to_conversation(
        self, conversation_id: str, payload: Dict,
        exclude_user: Optional[str] = None,
    ) -> None:
        conv = self._conversations.get(conversation_id)
        if not conv:
            return

        for member_id in conv.members:
            if member_id == exclude_user:
                continue
            connections = self._active_connections.get(member_id, set())
            for conn in connections:
                try:
                    await conn.send_json(payload)
                except Exception:
                    pass

    async def _broadcast_presence(self, user_id: str, status: UserStatus) -> None:
        broadcast_status = status
        if status == UserStatus.INVISIBLE:
            broadcast_status = UserStatus.OFFLINE

        contacts = set()
        for conv_id in self._user_conversations.get(user_id, set()):
            conv = self._conversations.get(conv_id)
            if conv:
                for mid in conv.members:
                    if mid != user_id:
                        contacts.add(mid)

        payload = {
            "type": "presence_update",
            "user_id": user_id,
            "status": broadcast_status.value,
        }

        for contact_id in contacts:
            connections = self._active_connections.get(contact_id, set())
            for conn in connections:
                try:
                    await conn.send_json(payload)
                except Exception:
                    pass

    def _find_direct_conversation(
        self, user_a: str, user_b: str
    ) -> Optional[Conversation]:
        a_convs = self._user_conversations.get(user_a, set())
        for conv_id in a_convs:
            conv = self._conversations.get(conv_id)
            if (conv
                and conv.conv_type == ConversationType.DIRECT
                and user_b in conv.members):
                return conv
        return None

    def _get_conversation_display_name(
        self, conv: Conversation, viewer_id: str
    ) -> str:
        if conv.name:
            return conv.name
        if conv.conv_type == ConversationType.DIRECT:
            for mid in conv.members:
                if mid != viewer_id:
                    user = self._users.get(mid)
                    return user.display_name if user else "Unknown User"
        members = []
        for mid in list(conv.members.keys())[:3]:
            user = self._users.get(mid)
            if user and mid != viewer_id:
                members.append(user.display_name)
        if len(conv.members) > 4:
            return ", ".join(members) + f" +{len(conv.members) - 3} more"
        return ", ".join(members) or "Empty Group"

    def _get_last_message(self, conversation_id: str) -> Optional[Dict]:
        messages = self._messages.get(conversation_id, {})
        if not messages:
            return None
        last = max(messages.values(), key=lambda m: m.created_at)
        return {
            "content": last.content[:100] if not last.deleted else "[deleted]",
            "sender_id": last.sender_id,
            "created_at": last.created_at.isoformat(),
            "type": last.message_type.value,
        }

    def _get_unread_count(self, conversation_id: str, user_id: str) -> int:
        last_read = self._read_receipts.get(conversation_id, {}).get(user_id)
        messages = self._messages.get(conversation_id, {})

        count = 0
        for msg in messages.values():
            if msg.sender_id == user_id:
                continue
            if msg.deleted:
                continue
            if last_read and msg.id <= last_read:
                continue
            count += 1
        return count

    def _get_message_time(
        self, message_id: str, conversation_id: str
    ) -> datetime:
        messages = self._messages.get(conversation_id, {})
        msg = messages.get(message_id)
        return msg.created_at if msg else datetime.min

    def _create_system_message(self, conversation_id: str, content: str) -> Message:
        return Message(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            sender_id="system",
            content=content,
            message_type=MessageType.SYSTEM,
        )

    def _extract_mentions(self, content: str) -> List[str]:
        pattern = r'@(\w+)'
        usernames = re.findall(pattern, content)
        mentioned_ids = []
        for username in usernames:
            for user in self._users.values():
                if user.username.lower() == username.lower():
                    mentioned_ids.append(user.user_id)
                    break
        return mentioned_ids

    def _sanitize_content(self, content: str) -> str:
        content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
        content = content.replace('<', '&lt;').replace('>', '&gt;')
        content = content.strip()
        return content

    def _validate_username(self, username: str) -> bool:
        return bool(re.match(r'^[a-zA-Z][a-zA-Z0-9._]{2,29}$', username))

    def _validate_email(self, email: str) -> bool:
        return bool(re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email))

    def _validate_emoji(self, emoji: str) -> bool:
        if len(emoji) > 10:
            return False
        return True

    def _emit_event(self, event_name: str, data: Any) -> None:
        handlers = self._event_handlers.get(event_name, [])
        for handler in handlers:
            try:
                handler(data)
            except Exception as e:
                logger.error(f"Event handler error for {event_name}: {e}")

    def on_event(self, event_name: str, handler: Callable) -> None:
        self._event_handlers[event_name].append(handler)

    def get_system_stats(self) -> Dict:
        total_messages = sum(
            len(msgs) for msgs in self._messages.values()
        )
        return {
            "total_users": len(self._users),
            "total_conversations": len(self._conversations),
            "total_messages": total_messages,
            "active_connections": sum(
                len(conns) for conns in self._active_connections.values()
            ),
            "cache_size": self._cache.size,
            "cache_hit_rate": f"{self._cache.hit_rate:.1%}",
            "rate_limiter_keys": len(self._rate_limiter._requests),
        }
