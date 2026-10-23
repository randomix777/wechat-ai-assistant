from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from .reliability import MessageEvent, MessageKind, UiOperationQueue


_UI_OPERATION_LOCK = threading.RLock()


@dataclass(frozen=True)
class AgentConfig:
    group: str
    db_path: str = "agent.db"
    account_id: str = "default"
    agent_id: str = "default"
    history_limit: int = 50
    bootstrap_history: bool = True
    listen_duration: str = "30s"
    poll_interval: float = 5.0
    group_members: tuple[str, ...] = ()
    background: bool = False
    window_generation: str | None = None
    allowed_targets: tuple[str, ...] = ()
    cloud_url: str = ""
    device_token: str = ""

    def __post_init__(self):
        if not self.group:
            raise ValueError("group is required")
        if self.history_limit < 0:
            raise ValueError("history_limit cannot be negative")
        if self.poll_interval < 0:
            raise ValueError("poll_interval cannot be negative")
        object.__setattr__(self, "group_members", tuple(self.group_members or ()))
        object.__setattr__(self, "allowed_targets", tuple(self.allowed_targets or (self.group,)))


class AgentStore:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    dedup_key TEXT UNIQUE,
                    chat_id TEXT NOT NULL,
                    sender TEXT,
                    content TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_chat_time ON events(chat_id, observed_at);
                CREATE TABLE IF NOT EXISTS event_uploads (
                    event_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 10,
                    next_attempt_at REAL,
                    lease_until REAL,
                    lease_token TEXT,
                    last_error TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS event_uploads_ready ON event_uploads(status, next_attempt_at, updated_at);
                CREATE TABLE IF NOT EXISTS business_uploads (
                    event_id TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL, last_error TEXT, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS business_uploads_ready ON business_uploads(status,next_attempt_at,updated_at);
                """
            )
            for statement in (
                "ALTER TABLE event_uploads ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 10",
                "ALTER TABLE event_uploads ADD COLUMN lease_until REAL",
                "ALTER TABLE event_uploads ADD COLUMN lease_token TEXT",
            ):
                try:
                    self._connection.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            self._connection.execute("INSERT OR IGNORE INTO business_uploads(event_id,status,attempts,next_attempt_at,updated_at) SELECT event_id,'pending',0,?,? FROM events",(self._clock(),self._clock()))

    def set_state(self, state: str, error: str | None = None) -> None:
        self.set_meta("state", state)
        self.set_meta("state_error", error or "")

    def get_state(self) -> str:
        return self.get_meta("state", "stopped")

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO agent_meta(key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM agent_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else default

    def record_event(self, event: MessageEvent) -> bool:
        key = event.dedup_key()
        dedup_key = None
        if key is not None:
            dedup_key = hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        now = self._clock()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    event_id, dedup_key, chat_id, sender, content,
                    direction, kind, observed_at, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    dedup_key,
                    event.chat_id,
                    event.sender,
                    event.content,
                    event.direction.value,
                    event.kind.value,
                    event.observed_at,
                    json.dumps(event.to_dict(), ensure_ascii=False, default=str),
                    now,
                ),
            )
            if cursor.rowcount:
                self._connection.execute(
                    "INSERT OR IGNORE INTO event_uploads(event_id, status, attempts, next_attempt_at, last_error, updated_at) VALUES (?, 'pending', 0, ?, NULL, ?)",
                    (event.event_id, now, now),
                )
                self._connection.execute("INSERT OR IGNORE INTO business_uploads(event_id,status,attempts,next_attempt_at,updated_at) VALUES(?,'pending',0,?,?)",(event.event_id,now,now))
            return cursor.rowcount == 1

    def claim_business_uploads(self,limit:int=100)->list[tuple[str,dict[str,Any]]]:
        now=self._clock()
        with self._lock,self._connection:
            self._connection.execute("UPDATE business_uploads SET status='pending' WHERE status='processing'")
            rows=self._connection.execute("SELECT e.event_id,e.payload FROM business_uploads b JOIN events e ON e.event_id=b.event_id WHERE b.status='pending' AND (b.next_attempt_at IS NULL OR b.next_attempt_at<=?) ORDER BY b.updated_at LIMIT ?",(now,max(1,limit))).fetchall()
            for row in rows:self._connection.execute("UPDATE business_uploads SET status='processing',updated_at=? WHERE event_id=? AND status='pending'",(now,row['event_id']))
        return [(str(r['event_id']),json.loads(r['payload'])) for r in rows]

    def finish_business_upload(self,event_ids:list[str])->None:
        now=self._clock()
        with self._lock,self._connection:self._connection.executemany("UPDATE business_uploads SET status='uploaded',next_attempt_at=NULL,last_error=NULL,updated_at=? WHERE event_id=?",[(now,i) for i in event_ids])

    def fail_business_upload(self,event_ids:list[str],error:str,retry_delay:float=5)->None:
        now=self._clock()
        with self._lock,self._connection:self._connection.executemany("UPDATE business_uploads SET status='pending',attempts=attempts+1,next_attempt_at=?,last_error=?,updated_at=? WHERE event_id=?",[(now+retry_delay,str(error)[:1000],now,i) for i in event_ids])

    def recent_events(self, limit: int = 100, chat_id: str | None = None) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        with self._lock:
            if chat_id is None:
                rows = self._connection.execute(
                    "SELECT payload FROM events ORDER BY observed_at DESC, created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload FROM events WHERE chat_id = ? ORDER BY observed_at DESC, created_at DESC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def pending_event_uploads(self, limit: int = 100) -> list[tuple[str, dict[str, Any]]]:
        if limit < 1:
            return []
        now = self._clock()
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT events.event_id, events.payload
                FROM event_uploads
                JOIN events ON events.event_id = event_uploads.event_id
                WHERE event_uploads.status = 'pending'
                  AND (event_uploads.next_attempt_at IS NULL OR event_uploads.next_attempt_at <= ?)
                ORDER BY event_uploads.updated_at, events.event_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [(str(row["event_id"]), json.loads(row["payload"])) for row in rows]

    def claim_event_uploads(self, limit: int = 100, lease_seconds: float = 60) -> list[tuple[str, dict[str, Any], str]]:
        if limit < 1 or lease_seconds <= 0:
            return []
        now = self._clock()
        claimed: list[tuple[str, dict[str, Any], str]] = []
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE event_uploads
                SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
                    next_attempt_at = CASE WHEN attempts >= max_attempts THEN NULL ELSE next_attempt_at END,
                    lease_until = NULL, lease_token = NULL, updated_at = ?
                WHERE status = 'processing' AND (lease_until IS NULL OR lease_until < ?)
                """,
                (now, now),
            )
            self._connection.execute(
                """
                UPDATE event_uploads
                SET status = 'failed', next_attempt_at = NULL, updated_at = ?
                WHERE status = 'pending' AND attempts >= max_attempts
                """,
                (now,),
            )
            rows = self._connection.execute(
                """
                SELECT events.event_id, events.payload
                FROM event_uploads
                JOIN events ON events.event_id = event_uploads.event_id
                WHERE event_uploads.status = 'pending'
                  AND (event_uploads.next_attempt_at IS NULL OR event_uploads.next_attempt_at <= ?)
                ORDER BY event_uploads.updated_at, events.event_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            for row in rows:
                token = uuid4().hex
                cursor = self._connection.execute(
                    """
                    UPDATE event_uploads
                    SET status = 'processing', attempts = attempts + 1,
                        lease_until = ?, lease_token = ?, updated_at = ?
                    WHERE event_id = ? AND status = 'pending'
                    """,
                    (now + lease_seconds, token, now, row["event_id"]),
                )
                if cursor.rowcount:
                    claimed.append((str(row["event_id"]), json.loads(row["payload"]), token))
        return claimed

    def mark_event_uploaded(self, event_id: str, lease_token: str | None = None) -> bool:
        with self._lock, self._connection:
            if lease_token is None:
                cursor = self._connection.execute(
                    "UPDATE event_uploads SET status = 'uploaded', next_attempt_at = NULL, lease_until = NULL, lease_token = NULL, last_error = NULL, updated_at = ? WHERE event_id = ? AND status IN ('pending', 'processing')",
                    (self._clock(), event_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE event_uploads SET status = 'uploaded', next_attempt_at = NULL, lease_until = NULL, lease_token = NULL, last_error = NULL, updated_at = ? WHERE event_id = ? AND status = 'processing' AND lease_token = ?",
                    (self._clock(), event_id, lease_token),
                )
            return cursor.rowcount == 1

    def mark_event_upload_failed(
        self,
        event_id: str,
        error: str,
        retry_delay: float = 5.0,
        lease_token: str | None = None,
    ) -> bool:
        now = self._clock()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT attempts, max_attempts FROM event_uploads WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return False
            exhausted = int(row["attempts"]) >= int(row["max_attempts"])
            status = "failed" if exhausted else "pending"
            next_attempt = None if exhausted else now + max(0.0, retry_delay)
            if lease_token is None:
                cursor = self._connection.execute(
                    "UPDATE event_uploads SET status = ?, next_attempt_at = ?, lease_until = NULL, lease_token = NULL, last_error = ?, updated_at = ? WHERE event_id = ? AND status IN ('pending', 'processing')",
                    (status, next_attempt, str(error)[:1000], now, event_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE event_uploads SET status = ?, next_attempt_at = ?, lease_until = NULL, lease_token = NULL, last_error = ?, updated_at = ? WHERE event_id = ? AND status = 'processing' AND lease_token = ?",
                    (status, next_attempt, str(error)[:1000], now, event_id, lease_token),
                )
            return cursor.rowcount == 1

    def pending_upload_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM event_uploads WHERE status IN ('pending', 'processing')"
            ).fetchone()
        return int(row["count"])

    def pending_business_upload_count(self) -> int:
        with self._lock:
            row=self._connection.execute("SELECT COUNT(*) AS count FROM business_uploads WHERE status IN ('pending','processing')").fetchone()
        return int(row["count"])

    def failed_upload_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM event_uploads WHERE status = 'failed'"
            ).fetchone()
        return int(row["count"])

    def event_count(self, chat_id: str | None = None) -> int:
        with self._lock:
            if chat_id is None:
                row = self._connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()
            else:
                row = self._connection.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
        return int(row["count"])

    def health(self) -> dict[str, Any]:
        return {
            "state": self.get_state(),
            "state_error": self.get_meta("state_error", ""),
            "event_count": self.event_count(),
            "pending_uploads": self.pending_upload_count(),
            "pending_business_uploads": self.pending_business_upload_count(),
            "failed_uploads": self.failed_upload_count(),
            "database": self.path,
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class LocalAgent:
    def __init__(
        self,
        config: AgentConfig,
        store: AgentStore | None = None,
        history_reader: Callable[..., list[dict[str, Any]]] | None = None,
        dialog_opener: Callable[..., Any] | None = None,
        listener: Callable[..., dict[str, Any]] | None = None,
        cloud: Any | None = None,
    ):
        self.config = config
        self.store = store or AgentStore(config.db_path)
        self.history_reader = history_reader
        self.dialog_opener = dialog_opener
        self.listener = listener
        self.cloud = cloud
        self._cloud_error = ""
        self.window_generation = config.window_generation or self.store.get_meta("window_generation") or uuid4().hex
        self._message_sequence = int(self.store.get_meta("message_sequence", "0") or 0)
        if config.window_generation is None:
            self.store.set_meta("window_generation", self.window_generation)
        self._ui_queue = UiOperationQueue(thread_name=f"{config.agent_id}-ui")
        self._owns_store = store is None

    def _read_history(self) -> list[dict[str, Any]]:
        if not self.config.bootstrap_history or self.config.history_limit < 1:
            return []
        reader = self.history_reader
        if reader is None:
            # 界面链已移除：没有注入读取器就不读。以前这里回退到
            # 直接翻微信窗口读历史，已删除。
            raise RuntimeError("界面读取已移除：请注入 history_reader（解密库）")
        with _UI_OPERATION_LOCK:
            return list(
                reader(
                    friend=self.config.group,
                    number=self.config.history_limit,
                    groupMembers=list(self.config.group_members),
                    is_json=False,
                    save_detail=False,
                    close_weixin=False,
                    search_pages=5,
                    is_maximize=False,
                )
                or []
            )

    def _open_dialog(self) -> Any:
        opener = self.dialog_opener
        if opener is None:
            # 界面链已移除：不会再打开微信聊天窗口。
            raise RuntimeError("界面操作已移除：请注入 dialog_opener，否则无法监听")
        from .Errors import NoSuchFriendError

        last_error = None
        for attempt in range(3):
            try:
                with _UI_OPERATION_LOCK:
                    return opener(
                        friend=self.config.group,
                        select=False,
                        is_maximize=False,
                        window_minimize=self.config.background,
                        close_weixin=False,
                    )
            except NoSuchFriendError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5)
        raise last_error

    def _listen(
        self,
        dialog: Any,
        stop_event: threading.Event | None = None,
        sequence_start: int = 0,
    ) -> dict[str, Any]:
        listener = self.listener
        if listener is None:
            # 界面链已移除：不再开窗监听。注入 listener 才能用（测试替身）。
            raise RuntimeError("持续监听已移除：请注入 listener，否则无法监听")
        return listener(
            dialog_window=dialog,
            duration=self.config.listen_duration,
            groupMembers=list(self.config.group_members),
            close_dialog_window=False,
            account_id=self.config.account_id,
            return_events=True,
            window_generation=self.window_generation,
            stop_event=stop_event,
            sequence_start=sequence_start,
        )

    def _record_event(self, event: MessageEvent) -> bool:
        inserted = self.store.record_event(event)
        if self.cloud is None:
            self.store.mark_event_uploaded(event.event_id)
        return inserted

    def _history_event(self, item: Mapping[str, Any], ordinal: int = 0) -> MessageEvent:
        content = str(item.get("消息内容", ""))
        sender = item.get("消息发送人")
        raw_kind=str(item.get("消息类型","")).lower()
        kind=MessageKind.TEXT
        if any(x in raw_kind for x in ("图片","image")):kind=MessageKind.IMAGE
        elif any(x in raw_kind for x in ("语音","voice","audio")):kind=MessageKind.VOICE
        elif any(x in raw_kind for x in ("视频","video")):kind=MessageKind.VIDEO
        elif any(x in raw_kind for x in ("文件","file")):kind=MessageKind.FILE
        elif any(x in raw_kind for x in ("链接","link")):kind=MessageKind.LINK
        elif any(x in raw_kind for x in ("引用","quote")):kind=MessageKind.QUOTE
        elif raw_kind and not any(x in raw_kind for x in ("文本","text")):kind=MessageKind.OTHER
        raw = json.dumps(dict(item), ensure_ascii=False, sort_keys=True, default=str)
        stable_id = "history:" + hashlib.sha256(raw.encode("utf-8")).hexdigest() + f":{ordinal}"
        return MessageEvent(
            event_id=stable_id,
            stable_id=stable_id,
            chat_id=self.config.group,
            account_id=self.config.account_id,
            sender=None if sender is None else str(sender),
            content=content,
            kind=kind,
            metadata={"source": "history", "raw": dict(item)},
        )

    def _publish(self, events: list[MessageEvent]) -> None:
        del events
        if self.cloud is None:
            return
        pending = self.store.claim_event_uploads(lease_seconds=60)
        if pending:
            try:
                self.cloud.send_events([payload for _, payload, _ in pending])
                for event_id, _, token in pending:
                    self.store.mark_event_uploaded(event_id, lease_token=token)
            except Exception as exc:
                self._cloud_error = str(exc)
                for event_id, _, token in pending:
                    self.store.mark_event_upload_failed(event_id, str(exc), retry_delay=5, lease_token=token)
                self.store.set_state("degraded", str(exc))
        if hasattr(self.cloud, "send_business_events"):
            business_pending=self.store.claim_business_uploads()
            if business_pending:
                try:
                    self.cloud.send_business_events([payload for _, payload in business_pending])
                    self.store.finish_business_upload([event_id for event_id,_ in business_pending])
                except Exception as exc:
                    self._cloud_error = str(exc)
                    self.store.fail_business_upload([event_id for event_id,_ in business_pending],str(exc),retry_delay=5)
                    self.store.set_state("degraded", str(exc))

    def _live_event(self, item: Mapping[str, Any]) -> MessageEvent:
        event = MessageEvent.from_dict(item)
        if event.stable_id is not None:
            return replace(event, window_generation=event.window_generation or self.window_generation)
        if event.sequence is not None:
            return replace(event, window_generation=event.window_generation or self.window_generation)
        if event.runtime_id is None:
            return replace(event, window_generation=event.window_generation or self.window_generation)
        identity = json.dumps(
            [
                self.config.account_id,
                event.chat_id,
                self.window_generation,
                repr(event.runtime_id),
                event.sender or "",
                event.kind.value,
                event.normalized_content,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        runtime_event_id = "runtime:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return replace(event, event_id=runtime_event_id, stable_id=None, window_generation=self.window_generation)

    def bootstrap_history(self, force: bool = False) -> int:
        if not force and self.store.get_meta("history_bootstrapped") == "1":
            return 0
        events = [self._history_event(item, ordinal) for ordinal, item in enumerate(self._read_history())]
        new_events = [event for event in events if self._record_event(event)]
        self._publish(new_events)
        if self.config.bootstrap_history:
            self.store.set_meta("history_bootstrapped", "1")
        return len(new_events)

    def listen_once(self, stop_event: threading.Event | None = None) -> int:
        if self.config.listen_duration in {"", "0", "0s", "0min", "0h"}:
            return 0
        if stop_event is not None and stop_event.is_set():
            return 0
        dialog = None
        try:
            dialog = self._open_dialog()
            result = self._listen(dialog, stop_event=stop_event, sequence_start=self._message_sequence)
        finally:
            if dialog is not None:
                try:
                    dialog.close()
                except Exception:
                    pass
        raw_events = result.get("消息事件", [])
        events = [self._live_event(item) for item in raw_events]
        self._message_sequence += len(raw_events)
        self.store.set_meta("message_sequence", str(self._message_sequence))
        new_events = [event for event in events if self._record_event(event)]
        self._publish(new_events)
        return len(new_events)

    def _summary(
        self,
        history_events: int = 0,
        live_events: int = 0,
        synced_commands: int = 0,
        processed_commands: int = 0,
    ) -> dict[str, Any]:
        return {
            "history_events": history_events,
            "live_events": live_events,
            "synced_commands": synced_commands,
            "processed_commands": processed_commands,
            "health": self.store.health(),
        }

    def run_once(self, stop_event: threading.Event | None = None) -> dict[str, Any]:
        if stop_event is not None and stop_event.is_set():
            return self._summary()
        self._cloud_error = ""
        self.store.set_state("starting")
        try:
            history_count = self.bootstrap_history()
            if stop_event is not None and stop_event.is_set():
                return self._summary(history_events=history_count)
            self.store.set_state("listening")
            live_count = self.listen_once(stop_event=stop_event)
            if stop_event is not None and stop_event.is_set():
                return self._summary(history_events=history_count, live_events=live_count)
            if self.store.pending_upload_count():
                self.store.set_state("degraded", self.store.get_meta("state_error") or "cloud event upload pending")
            elif self._cloud_error:
                self.store.set_state("degraded", self._cloud_error)
            else:
                self.store.set_state("idle")
            if self.cloud is not None:
                try:
                    self.cloud.heartbeat(self.store.health())
                except Exception as exc:
                    self._cloud_error = str(exc)
                    self.store.set_state("degraded", str(exc))
            return self._summary(history_count, live_count)
        except Exception as exc:
            self.store.set_state("degraded", str(exc))
            if self.cloud is not None:
                try:
                    self.cloud.heartbeat(self.store.health())
                except Exception:
                    pass
            raise

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.run_once(stop_event=stop_event)
            except Exception:
                pass
            stop_event.wait(self.config.poll_interval)

    def close(self) -> None:
        self._ui_queue.close()
        if self.store.get_state() != "degraded":
            self.store.set_state("stopped")
        if self._owns_store:
            self.store.close()
