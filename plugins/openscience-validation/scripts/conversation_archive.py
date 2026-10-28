"""Read-only Codex transcript capture, immutable redacted events, and local FTS.

The archive is evidence of what was said, not verified scientific fact. Source files
are never modified. Runtime instructions, hidden reasoning, and binary attachments
are deliberately excluded and coverage records make those boundaries explicit.
"""
from __future__ import annotations

import base64
import collections
import contextlib
import gzip
import hashlib
import io
import json
import re
import sqlite3
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCHEMA_VERSION = 1
_HIDDEN_KEYS = {"encrypted_content", "reasoning_content", "chain_of_thought", "internal_analysis", "raw_content", "replacement_history", "retained_context", "base_instructions"}
_SECRET_KEYS = re.compile(r"^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|session[ _-]?key|(?:oauth[ _-]?)?session[ _-]?nonce|client[_-]?secret|password|passwd|aws[_-]?(?:secret[_-]?access[_-]?key|session[_-]?token)|secret[_-]?access[_-]?key|slack[ _-]?(?:app[ _-]?token|bot[ _-]?token|signing[ _-]?secret)|authorization|cookie|set-cookie|private[_-]?key)$", re.I)
_URL_SECRET_KEYS = {"code", "state", "access_token", "refresh_token", "id_token", "token", "key", "api_key", "signature", "sig", "x-amz-signature", "x-amz-credential", "x-amz-security-token", "se", "sp", "sv", "oauth_session_nonce", "oauth-session-nonce", "oauthsessionnonce", "session_nonce", "session-nonce", "sessionnonce"}
_DATA_URL = re.compile(r"data:([\w.+/-]+)(?:;[^,\s]*)?;base64,([A-Za-z0-9+/=_-]+)")
_CLI_SECRET = re.compile(r'''(?i)(--(?:token|api[-_]key|access[-_]token|refresh[-_]token|(?:oauth[-_]?)?session[-_]?nonce|client[-_]secret|password)(?:=|\s+))(?:"[^"\n]*"|'[^'\n]*'|[^\s"'`,;]+)''')
_URL = re.compile(r"https?://[^\s<>\"'`]+")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----"
    r".*?(?:(?P<end>-----END (?P=label)-----)|\Z)",
    re.S | re.I,
)
_LITERAL_SECRET = re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|session[ _-]?key|client[_-]?secret|password|passwd|aws[_-]?(?:secret[_-]?access[_-]?key|session[_-]?token)|secret[_-]?access[_-]?key|slack[ _-]?(?:app[ _-]?token|bot[ _-]?token|signing[ _-]?secret)|authorization)\b[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)")
_UNQUOTED_SECRET = re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|session[ _-]?key|client[_-]?secret|password|passwd|aws[_-]?(?:secret[_-]?access[_-]?key|session[_-]?token)|secret[_-]?access[_-]?key|slack[ _-]?(?:app[ _-]?token|bot[ _-]?token|signing[ _-]?secret))\b\s*[:=]\s*)((?:(?!\\[rn])[^\s\"',;}])+)")


# Compact signed JWTs need no field name or Bearer prefix to be credentials.
# Validate decoded JSON shape below so dotted scientific identifiers survive.
_JWT = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{8,}={0,2})\.([A-Za-z0-9_-]{2,}={0,2})\.([A-Za-z0-9_-]+={0,2})(?![A-Za-z0-9_-])")
_NONCE_NAME = r"\b(?:oauth[ _-]?)?session[ _-]?nonce\b"
_NONCE_LITERAL = re.compile(
    r"(?i)(" + _NONCE_NAME + r"(?:\\*[\"'])?\s*[:=]\s*)(?P<quote>\\*[\"'])(.*?)(?P=quote)")
_NONCE_UNQUOTED = re.compile(
    r"(?i)(" + _NONCE_NAME + r"(?:\\*[\"'])?\s*[:=]\s*)(?!\[REDACTED:)([^\s\\\"',;}&\]#]+)")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _under(path: str, roots: Iterable[str | Path]) -> bool:
    try:
        p = Path(path).expanduser().resolve()
        return any(p == (r := Path(root).expanduser().resolve()) or r in p.parents for root in roots)
    except (OSError, ValueError, TypeError):
        return False


class Redactor:
    """Deterministic obvious-secret redaction; never emits raw matched values."""

    def __init__(self):
        self.counts = collections.Counter()
        self.attachments: list[dict] = []

    def marker(self, label: str) -> str:
        self.counts[label] += 1
        return f"[REDACTED:{label}]"

    def binary(self, value: str, media_type: str) -> str:
        try:
            data = base64.b64decode(value, validate=True)
            digest_kind = "decoded_bytes"
        except (ValueError, TypeError):
            data = value.encode()
            digest_kind = "encoded_reference"
        ref = {"sha256": _sha(data), "media_type": media_type, "binary_captured": False, "hash_of": digest_kind}
        self.attachments.append(ref)
        self.counts["binary_attachment_not_captured"] += 1
        return f"[ATTACHMENT:sha256:{ref['sha256']};type={media_type};binary_not_captured]"

    def text(self, text: str) -> str:
        text = _DATA_URL.sub(lambda m: self.binary(m[2], m[1]), text)
        text = _CLI_SECRET.sub(lambda m: m[1] + self.marker("credential_argument"), text)
        def jwt(match):
            try:
                header = json.loads(base64.urlsafe_b64decode(match[1] + "=" * (-len(match[1]) % 4)))
                claims = json.loads(base64.urlsafe_b64decode(match[2] + "=" * (-len(match[2]) % 4)))
                if (isinstance(header, dict) and isinstance(header.get("alg"), str)
                        and isinstance(claims, dict)):
                    return self.marker("jwt")
            except (ValueError, UnicodeError):
                pass
            return match[0]
        text = _JWT.sub(jwt, text)
        # Handle nested escaped JSON as well as ordinary config literals. Keep
        # unrelated fields and URL parameters byte-for-byte outside the value.
        text = _NONCE_LITERAL.sub(lambda m: m[1] + m[2] + self.marker("session_nonce") + m[2], text)
        text = _NONCE_UNQUOTED.sub(lambda m: m[1] + self.marker("session_nonce"), text)
        def tunnel_token(match):
            # Cloudflare tunnel credentials can appear as a standalone base64
            # JSON value in argv/plist output, without an adjacent --token flag.
            raw = match[0]
            try:
                value = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
                if (isinstance(value, dict) and {"a", "t", "s"} <= value.keys()
                        and re.fullmatch(r"[0-9a-fA-F]{32}", str(value["a"]))
                        and re.fullmatch(r"[0-9a-fA-F-]{36}", str(value["t"]))
                        and isinstance(value["s"], str)):
                    return self.marker("tunnel_credential")
            except (ValueError, UnicodeError):
                pass
            return raw
        text = re.sub(r"\beyJ[A-Za-z0-9_+/-]{60,}={0,2}", tunnel_token, text)
        def private_key(match):
            # Tool output may end partway through a PEM/PGP block. Once a private
            # key header appears, an absent matching footer cannot make its body
            # safe. Redact to EOF, including literal backslash-n encoded output.
            if match.group("end") is None:
                self.counts["private_key_incomplete"] += 1
            return self.marker("private_key")
        text = _PRIVATE_KEY_BLOCK.sub(private_key, text)
        text = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", lambda m: self.marker("aws_access_key"), text)
        text = re.sub(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", lambda m: self.marker("api_token"), text)
        text = re.sub(r"\b(?:xapp-\d-[A-Za-z0-9-]{10,}|xox[abprso]-[A-Za-z0-9-]{10,})\b", lambda m: self.marker("slack_token"), text)
        text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*", lambda m: "Bearer " + self.marker("bearer_token"), text)
        text = _LITERAL_SECRET.sub(lambda m: m[1] + m[2] + self.marker("credential_literal") + m[4], text)
        text = _UNQUOTED_SECRET.sub(lambda m: m[1] + self.marker("credential_literal"), text)
        def url(m):
            original = m[0]
            try:
                u = urlsplit(original)
                pairs = parse_qsl(u.query, keep_blank_values=True)
                altered = [(k, self.marker("url_credential") if k.lower() in _URL_SECRET_KEYS else v) for k, v in pairs]
                netloc = u.netloc
                if u.username is not None:
                    netloc = self.marker("url_userinfo") + "@" + (u.hostname or "") + (f":{u.port}" if u.port else "")
                fragment = u.fragment
                if "=" in fragment:
                    fragment_pairs = parse_qsl(fragment.lstrip("?"), keep_blank_values=True)
                    fragment_altered = [(k, self.marker("url_credential") if k.lower() in _URL_SECRET_KEYS else v) for k, v in fragment_pairs]
                    if fragment_pairs != fragment_altered:
                        fragment = urlencode(fragment_altered)
                if pairs != altered or netloc != u.netloc or fragment != u.fragment:
                    return urlunsplit((u.scheme, netloc, u.path, urlencode(altered), fragment))
            except (ValueError, TypeError):
                pass
            return original
        return _URL.sub(url, text)

    def clean(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, dict):
            if value.get("role") in {"system", "developer"} or value.get("type") in {"reasoning", "encrypted_content"}:
                self.counts["hidden_content_excluded"] += 1
                return {"excluded": "runtime_instructions_or_hidden_reasoning"}
            out = {}
            media = value.get("type") in {"image", "input_image", "output_image", "audio", "input_audio", "video", "input_file", "file"}
            for key, item in value.items():
                if key in _HIDDEN_KEYS:
                    self.counts["hidden_content_excluded"] += 1
                    continue
                if _SECRET_KEYS.match(key):
                    out[key] = self.marker("credential_field")
                elif media and key in {"data", "base64", "file_data", "audio_data"} and isinstance(item, str):
                    out[key] = self.binary(item, value.get("mimeType") or value.get("mime_type") or value.get("type"))
                else:
                    out[key] = self.clean(item)
            if media:
                out["binary_captured"] = False
                if not any(k in value for k in ("data", "base64", "file_data", "audio_data")):
                    self.attachments.append({"media_type": value.get("type"), "reference": {k: out[k] for k in ("image_url", "file_id", "file_url", "path", "url") if k in out}, "binary_captured": False})
                    self.counts["binary_attachment_not_captured"] += 1
            return out
        return value


def _visible_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_visible_text(x) for x in value)))
    if isinstance(value, dict):
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
        return _json(value)
    return "" if value is None else str(value)


def _normalize(record: dict, metadata: dict, line: int, raw_digest: str, authoritative: bool) -> tuple[list[dict], collections.Counter]:
    counts = collections.Counter()
    rtype, p = record.get("type"), record.get("payload", {})
    if not isinstance(p, dict):
        counts["unsupported_record"] += 1
        return [], counts
    kind, role, body, safe_meta = None, None, None, {}
    typ = p.get("type")
    if rtype == "response_item":
        if typ in {"reasoning", "compaction", "additional_tools"}:
            counts["hidden_or_runtime_excluded"] += 1
            return [], counts
        if typ == "message":
            role = p.get("role")
            if role not in {"user", "assistant"} or p.get("channel") in {"analysis", "summary"} or p.get("phase") in {"analysis", "reasoning", "summary"}:
                counts["hidden_or_runtime_excluded"] += 1
                return [], counts
            kind, body = "message", p.get("content", [])
            safe_meta = {k: p[k] for k in ("id", "phase", "status") if k in p}
        elif typ == "agent_message":
            kind, role, body = "inter_agent_message", "agent", p.get("content", [])
            safe_meta = {k: p[k] for k in ("id", "author", "recipient") if k in p}
        elif typ in {"function_call", "custom_tool_call", "web_search_call", "local_shell_call"}:
            kind, role = "tool_call", "assistant"
            body = {k: p[k] for k in ("name", "namespace", "arguments", "input", "action") if k in p}
            safe_meta = {k: p[k] for k in ("id", "call_id", "status", "name") if k in p}
        elif typ in {"function_call_output", "custom_tool_call_output", "tool_result"}:
            kind, role, body = "tool_result", "tool", p.get("output", p.get("result"))
            safe_meta = {k: p[k] for k in ("id", "call_id") if k in p}
        else:
            counts["unsupported_response_item"] += 1
            return [], counts
    elif rtype == "event_msg":
        if typ in {"item_completed", "user_message", "agent_message"}:
            if authoritative:
                counts["mirror_excluded"] += 1
                return [], counts
            item = p.get("item", {}) if typ == "item_completed" else p
            itype = item.get("type")
            if itype in {"UserMessage", "user_message"}:
                role = "user"
            elif itype in {"AgentMessage", "agent_message"} and item.get("phase") not in {"analysis", "summary"}:
                role = "assistant"
            else:
                counts["unsupported_fallback_item"] += 1
                return [], counts
            kind, body = "message", item.get("content", item.get("message", item.get("text", "")))
            safe_meta = {"event_msg_fallback": True, **{k: item[k] for k in ("id", "phase") if k in item}}
        elif typ in {"task_started", "task_complete", "turn_aborted", "undo", "thread_rolled_back"}:
            kind, role = "turn_metadata", "metadata"
            body = {k: p[k] for k in ("type", "turn_id", "root_turn_id", "started_at", "completed_at", "num_turns") if k in p}
        else:
            counts["runtime_metadata_excluded"] += 1
            return [], counts
    elif rtype == "session_meta":
        kind, role = "session_metadata", "metadata"
        body = {k: p[k] for k in ("id", "session_id", "parent_thread_id", "forked_from_id", "timestamp", "cwd", "originator", "cli_version", "agent_path", "history_mode", "creator_user_id", "creator_account_id", "subagent_history_start_ordinal") if k in p}
    elif rtype == "compacted":
        kind, role = "compaction_metadata", "metadata"
        body = {k: p[k] for k in ("window_number", "first_window_id", "previous_window_id", "window_id", "compaction_response_id") if k in p}
        body["replacement_history_captured"] = False
        counts["compaction_hidden_history_excluded"] += 1
    elif rtype == "turn_context":
        kind, role = "turn_metadata", "metadata"
        body = {k: p[k] for k in ("turn_id", "root_turn_id", "cwd", "model") if k in p}
    elif rtype == "inter_agent_communication_metadata":
        kind, role = "inter_agent_metadata", "metadata"
        body = {k: p[k] for k in ("trigger_turn", "sender_thread_id", "recipient_thread_id") if k in p}
    else:
        counts["runtime_metadata_excluded"] += 1
        return [], counts
    redactor = Redactor()
    clean = redactor.clean(body)
    safe_meta = redactor.clean(safe_meta)
    text = _visible_text(clean)
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": _sha(f"{metadata['session_id']}:{line}:{record.get('ordinal', 0)}:{raw_digest}".encode()),
        "session_id": metadata["session_id"],
        "source": {"path": metadata["path"], "line": line, "ordinal": record.get("ordinal", 0), "record_sha256": raw_digest, "version": metadata.get("version", 1)},
        "timestamp": record.get("timestamp") or p.get("timestamp"),
        "kind": kind, "role": role, "text": text, "content": clean,
        "metadata": safe_meta,
        "creator_user_id": metadata.get("creator_user_id"),
        "creator_account_id": metadata.get("creator_account_id"),
        "attachments": redactor.attachments,
        "redactions": dict(redactor.counts),
        "epistemic_status": "transcript_of_statements_not_verified_fact",
    }
    counts.update(redactor.counts)
    counts["captured_events"] += 1
    return [event], counts


def _open_source(path: Path):
    if path.suffix == ".zst":
        try:
            from compression import zstd
        except ImportError:
            try:
                from desktop_runtime import open_zstd
            except ImportError:
                from runtime.remote_context.desktop_runtime import open_zstd
            return open_zstd(path)
        return zstd.open(path, "rb")
    return path.open("rb")


class ConversationArchive:
    def __init__(self, db_path: str | Path, object_dir: str | Path):
        self.db_path, self.object_dir = Path(db_path), Path(object_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.object_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=10000;
        CREATE TABLE IF NOT EXISTS events (
          event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, line INTEGER NOT NULL,
          ordinal INTEGER NOT NULL, source_version INTEGER NOT NULL, timestamp TEXT,
          kind TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, record_json TEXT NOT NULL,
          origin_node TEXT, local_event INTEGER NOT NULL, object_sha TEXT);
        CREATE INDEX IF NOT EXISTS events_context ON events(session_id,line,ordinal);
        CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(event_id UNINDEXED,text,tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS sources (
          session_id TEXT PRIMARY KEY,path TEXT NOT NULL,cwd TEXT,version INTEGER NOT NULL,
          line_count INTEGER NOT NULL,byte_count INTEGER NOT NULL,prefix_sha256 TEXT NOT NULL,
          stat_size INTEGER,stat_mtime_ns INTEGER,complete INTEGER NOT NULL,
          metadata_json TEXT NOT NULL,counts_json TEXT NOT NULL,error_json TEXT,last_attempt INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS objects (
          sha256 TEXT PRIMARY KEY,bytes INTEGER NOT NULL,event_count INTEGER NOT NULL,
          local_object INTEGER NOT NULL,origin_node TEXT);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def object_path(self, sha256: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("invalid_object_sha256")
        return self.object_dir / f"{sha256}.jsonl.gz"

    def _put_events(self, events: list[dict], origin_node=None, local=True) -> int:
        added = 0
        for event in events:
            cur = self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                event["event_id"], event["session_id"], event["source"]["line"], event["source"].get("ordinal", 0), event["source"].get("version", 1), event.get("timestamp"), event["kind"], event["role"], event["text"], _json(event), origin_node, int(local), None))
            if cur.rowcount:
                self.db.execute("INSERT INTO events_fts(event_id,text) VALUES(?,?)", (event["event_id"], event["text"]))
                added += 1
            elif local:
                # A source later discovered locally remains locally exportable.
                self.db.execute("UPDATE events SET local_event=1 WHERE event_id=?", (event["event_id"],))
        return added

    def _seal(self, chunk_events=500):
        while True:
            rows, raw_bytes = [], 0
            cursor = self.db.execute("SELECT event_id,record_json FROM events WHERE local_event=1 AND object_sha IS NULL ORDER BY session_id,line,ordinal,event_id LIMIT ?", (chunk_events,))
            for candidate in cursor:
                size = len(candidate["record_json"].encode()) + 1
                if rows and raw_bytes + size > 8 * 1024 * 1024:
                    break
                rows.append(candidate); raw_bytes += size
            if not rows:
                break
            raw = ("\n".join(r["record_json"] for r in rows) + "\n").encode()
            packed = gzip.compress(raw, mtime=0)
            if len(packed) >= 64 * 1024 * 1024:
                raise ValueError("archive_event_exceeds_64MiB_compressed_object_limit")
            digest = _sha(packed)
            path = self.object_path(digest)
            if not path.exists():
                temp = path.with_suffix(".tmp")
                temp.write_bytes(packed)
                temp.replace(path)
            self.db.execute("INSERT OR IGNORE INTO objects VALUES(?,?,?,?,?)", (digest, len(packed), len(rows), 1, None))
            self.db.executemany("UPDATE events SET object_sha=? WHERE event_id=?", [(digest, r["event_id"]) for r in rows])
        self.db.commit()

    def ingest_codex(self, codex_home: str | Path, allowed_roots: Iterable[str | Path], max_threads=50, max_events=5000, chunk_events=500) -> dict:
        if max_threads < 1 or max_events < 1 or chunk_events < 1:
            raise ValueError("capture_bounds_must_be_positive")
        roots = list(allowed_roots)
        if not roots:
            raise ValueError("explicit_allowed_roots_required")
        home = Path(codex_home).expanduser()
        source_db = home / "state_5.sqlite"
        report = {"schema_version": 1, "threads_considered": 0, "threads_processed": 0, "threads_unchanged": 0, "threads_out_of_scope": 0, "threads_in_scope": 0, "events_added": 0, "errors": [], "bounded": False}
        attempt_row = self.db.execute("SELECT value FROM state WHERE key='attempt'").fetchone()
        attempt = int(attempt_row[0]) + 1 if attempt_row else 1
        with contextlib.closing(sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)) as src:
            src.row_factory = sqlite3.Row
            columns = {r[1] for r in src.execute("PRAGMA table_info(threads)")}
            required = {"id", "rollout_path", "cwd"}
            if not required <= columns:
                raise RuntimeError("unsupported_codex_threads_schema")
            optional = [c for c in ("creator_user_id", "creator_account_id", "history_mode", "updated_at") if c in columns]
            rows = [dict(r) for r in src.execute("SELECT id,rollout_path,cwd" + ("," + ",".join(optional) if optional else "") + " FROM threads")]
        pending = []
        relocated_sources = None
        for row in rows:
            report["threads_considered"] += 1
            if not _under(row.get("cwd", ""), roots):
                report["threads_out_of_scope"] += 1
                continue
            report["threads_in_scope"] += 1
            old = self.db.execute("SELECT * FROM sources WHERE session_id=?", (row["id"],)).fetchone()
            path = Path(row["rollout_path"])
            if not path.exists() and path.suffix == ".jsonl" and Path(str(path) + ".zst").exists():
                path = Path(str(path) + ".zst")
            if not path.exists():
                if relocated_sources is None:
                    relocated_sources = {}
                    for source_root in (home / "sessions", home / "archived_sessions"):
                        for existing in source_root.rglob("*.jsonl*"):
                            if existing.is_file():
                                relocated_sources.setdefault(existing.name.removesuffix(".zst"), []).append(existing)
                candidates = relocated_sources.get(path.name.removesuffix(".zst"), [])
                if len(candidates) == 1:
                    path = candidates[0]
            if not _under(str(path), [home / "sessions", home / "archived_sessions"]):
                report["errors"].append({"session_id": row["id"], "error": "source_path_outside_codex_sessions"})
                continue
            try:
                st = path.stat()
            except OSError as exc:
                report["errors"].append({"session_id": row["id"], "error": type(exc).__name__})
                continue
            if old and old["path"] == str(path) and old["complete"] and old["stat_size"] == st.st_size and old["stat_mtime_ns"] == st.st_mtime_ns:
                report["threads_unchanged"] += 1
                continue
            pending.append((old["last_attempt"] if old else -1, row, path, st, old))
        # Round-robin age prevents one constantly growing transcript starving others.
        pending.sort(key=lambda x: (x[0], -x[1].get("updated_at", 0), x[1]["id"]))
        # Alternate newly changed known sessions with the oldest unattempted or
        # incomplete work. This keeps live conversations fresh during backfill.
        live = sorted((p for p in pending if p[4] and (p[4]["stat_size"] != p[3].st_size or p[4]["stat_mtime_ns"] != p[3].st_mtime_ns or p[4]["path"] != str(p[2]))), key=lambda x: (-x[1].get("updated_at", 0), x[0]))
        queues = [list(live), list(pending)]
        if min(max_threads, max_events) == 1 and attempt % 2 == 0:
            queues.reverse()
        scheduled, seen = [], set()
        while any(queues) and len(scheduled) < max_threads:
            for queue in queues:
                while queue and queue[0][1]["id"] in seen:
                    queue.pop(0)
                if queue and len(scheduled) < max_threads:
                    item = queue.pop(0)
                    scheduled.append(item); seen.add(item[1]["id"])
        per_thread_cap = min(1000, max(1, max_events // min(2, max(1, len(pending)))))
        budget = max_events
        for _, row, path, st, old in scheduled:
            if budget <= 0:
                break
            report["threads_processed"] += 1
            try:
                result = self._ingest_source(row, path, st, old, min(budget, per_thread_cap), attempt)
            except (OSError, RuntimeError, EOFError) as exc:
                report["errors"].append({"session_id": row["id"], "error": type(exc).__name__})
                continue
            budget -= result["captured"]
            report["events_added"] += result["added"]
            report["errors"].extend(result["errors"])
            report["bounded"] |= result["bounded"]
        report["bounded"] |= len(pending) > report["threads_processed"]
        report["pending_threads_not_processed"] = len(pending) - report["threads_processed"]
        inventory = {k: report[k] for k in ("threads_considered", "threads_in_scope", "threads_out_of_scope", "pending_threads_not_processed", "errors")}
        self.db.execute("INSERT OR REPLACE INTO state VALUES('inventory',?)", (_json(inventory),))
        self.db.execute("INSERT OR REPLACE INTO state VALUES('attempt',?)", (str(attempt),))
        self._seal(chunk_events)
        report["status"] = self.status()
        return report

    def _ingest_source(self, row, path, st, old, budget, attempt):
        # First pass verifies the previous checkpoint and identifies authoritative
        # response records, so event_msg mirrors never become duplicate knowledge.
        prefix = hashlib.sha256()
        old_count = old["line_count"] if old else 0
        prefix_matches = old_count == 0
        authoritative = False
        complete_lines = 0
        with _open_source(path) as stream:
            for n, raw in enumerate(stream, 1):
                if not raw.endswith(b"\n"):
                    break
                complete_lines = n
                if n <= old_count:
                    prefix.update(raw)
                    if n == old_count:
                        prefix_matches = prefix.hexdigest() == old["prefix_sha256"]
                try:
                    d = json.loads(raw)
                    authoritative |= isinstance(d, dict) and d.get("type") == "response_item" and isinstance(d.get("payload"), dict) and d["payload"].get("type") in {"message", "function_call", "custom_tool_call", "agent_message"}
                except (ValueError, UnicodeDecodeError):
                    pass
                if authoritative and (not old_count or n >= old_count):
                    break
        resume = bool(old and complete_lines >= old_count and prefix_matches)
        start = old_count if resume else 0
        version = old["version"] if resume else ((old["version"] + 1) if old else 1)
        counts = collections.Counter(json.loads(old["counts_json"])) if resume else collections.Counter()
        metadata = {"session_id": row["id"], "path": str(path), "version": version, **{k: row.get(k) for k in ("creator_user_id", "creator_account_id", "history_mode")}}
        line_count, byte_count, hasher = 0, 0, hashlib.sha256()
        events, errors, captured, added, buffered_bytes = [], [], 0, 0, 0
        complete, bounded = True, False
        with _open_source(path) as stream:
            for n, raw in enumerate(stream, 1):
                if not raw.endswith(b"\n"):
                    complete = False
                    errors.append({"session_id": row["id"], "line": n, "error": "partial_final_line_deferred"})
                    break
                if n <= start:
                    hasher.update(raw); line_count = n; byte_count += len(raw)
                    continue
                if captured >= budget:
                    complete = False; bounded = True
                    break
                try:
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        raise ValueError("non_object_record")
                except (ValueError, UnicodeDecodeError):
                    complete = False
                    errors.append({"session_id": row["id"], "line": n, "error": "malformed_json_record", "record_sha256": _sha(raw)})
                    break
                found, excluded = _normalize(record, metadata, n, _sha(raw), authoritative)
                counts.update(excluded)
                events.extend(found); captured += len(found)
                buffered_bytes += sum(len(_json(e)) for e in found)
                if len(events) >= 100 or buffered_bytes >= 4 * 1024 * 1024:
                    added += self._put_events(events)
                    events.clear(); buffered_bytes = 0
                hasher.update(raw); line_count = n; byte_count += len(raw)
        added += self._put_events(events)
        self.db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            row["id"], str(path), row.get("cwd"), version, line_count, byte_count, hasher.hexdigest(), st.st_size, st.st_mtime_ns, int(complete), _json(metadata), _json(dict(counts)), _json(errors) if errors else None, attempt))
        # Value count intentionally checked by SQLite; transaction persists only
        # with successfully sealed objects at the end of the bounded run.
        self.db.commit()
        return {"captured": captured, "added": added, "errors": errors, "bounded": bounded}

    def status(self) -> dict:
        return {
            "schema_version": 1,
            "events": self.db.execute("SELECT count(*) FROM events").fetchone()[0],
            "local_events": self.db.execute("SELECT count(*) FROM events WHERE local_event=1").fetchone()[0],
            "remote_events": self.db.execute("SELECT count(*) FROM events WHERE local_event=0").fetchone()[0],
            "sources": self.db.execute("SELECT count(*) FROM sources").fetchone()[0],
            "sources_complete": self.db.execute("SELECT count(*) FROM sources WHERE complete=1").fetchone()[0],
            "sources_with_errors": self.db.execute("SELECT count(*) FROM sources WHERE error_json IS NOT NULL").fetchone()[0],
            "objects": self.db.execute("SELECT count(*) FROM objects").fetchone()[0],
        }

    def export_manifest(self, node_id: str) -> dict:
        self._seal()
        objects = [dict(r) for r in self.db.execute("SELECT sha256,bytes,event_count FROM objects WHERE local_object=1 ORDER BY sha256")]
        sessions = []
        for row in self.db.execute("SELECT * FROM sources ORDER BY session_id"):
            sessions.append({"session_id": row["session_id"], "source_path": row["path"], "cwd": row["cwd"], "version": row["version"], "complete_lines": row["line_count"], "complete_uncompressed_bytes": row["byte_count"], "prefix_sha256": row["prefix_sha256"], "complete_at_observed_stat": bool(row["complete"]), "counts": json.loads(row["counts_json"]), "errors": json.loads(row["error_json"]) if row["error_json"] else [], "history_mode": json.loads(row["metadata_json"]).get("history_mode")})
        inventory_row = self.db.execute("SELECT value FROM state WHERE key='inventory'").fetchone()
        return {"schema_version": 1, "node_id": node_id, "objects": objects, "sessions": sessions, "inventory": json.loads(inventory_row[0]) if inventory_row else {},
                "limitations": ["Visible user/assistant exchanges, tool activity and inter-agent messages only; no system/developer instructions or hidden reasoning.", "Obvious credentials are redacted; binary attachments are referenced but not captured.", "Local Codex records are not proof that all historical conversations, remote computers or deleted files have been captured.", "Transcripts record statements, not independently established scientific facts."]}

    def import_manifest(self, manifest: dict, fetch_object: Callable[[str], bytes]) -> dict:
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("node_id"), str):
            raise ValueError("unsupported_manifest")
        added, imported = 0, 0
        for obj in manifest.get("objects", []):
            digest = obj["sha256"]
            path = self.object_path(digest)
            if self.db.execute("SELECT 1 FROM objects WHERE sha256=?", (digest,)).fetchone():
                continue
            packed = fetch_object(digest)
            if _sha(packed) != digest or len(packed) != obj["bytes"]:
                raise ValueError("archive_object_integrity_mismatch")
            events = [json.loads(line) for line in gzip.decompress(packed).splitlines() if line]
            if len(events) != obj["event_count"]:
                raise ValueError("archive_object_count_mismatch")
            for event in events:
                if event.get("schema_version") != 1 or event.get("role") not in {"user", "assistant", "agent", "tool", "metadata"}:
                    raise ValueError("invalid_archive_event")
            if not path.exists():
                temp = path.with_suffix(".tmp"); temp.write_bytes(packed); temp.replace(path)
            added += self._put_events(events, origin_node=manifest["node_id"], local=False)
            self.db.execute("INSERT INTO objects VALUES(?,?,?,?,?)", (digest, len(packed), len(events), 0, manifest["node_id"]))
            self.db.commit(); imported += 1
        return {"objects_imported": imported, "events_added": added, "node_id": manifest["node_id"]}

    def search(self, query: str, limit=8, session_id=None, role=None) -> list[dict]:
        if not query.strip():
            return []
        limit = max(1, min(int(limit), 100))
        params = [query]
        condition = ""
        if session_id:
            condition += " AND e.session_id=?"; params.append(session_id)
        if role:
            if role not in {"user", "assistant", "agent", "tool", "metadata"}:
                raise ValueError("invalid_role_filter")
            condition += " AND e.role=?"; params.append(role)
        params.append(limit)
        sql = "SELECT e.record_json,e.origin_node,bm25(events_fts) AS rank,snippet(events_fts,1,'','',' … ',192) AS excerpt FROM events_fts JOIN events e ON e.event_id=events_fts.event_id WHERE events_fts MATCH ?" + condition + " ORDER BY rank LIMIT ?"
        try:
            rows = self.db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            tokens = re.findall(r"\w+", query, flags=re.UNICODE)
            if not tokens:
                return []
            params[0] = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            rows = self.db.execute(sql, params).fetchall()
        results = []
        for row in rows:
            event = json.loads(row["record_json"])
            event.pop("content", None)
            event["total_chars"] = len(event["text"])
            event["text"] = row["excerpt"][:1600]
            event["excerpt_only"] = True
            event["origin_node"] = row["origin_node"]
            event["rank"] = row["rank"]
            results.append(event)
        return results

    def read_context(self, event_id: str, before=2, after=2) -> dict:
        row = self.db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            raise KeyError("event_not_found")
        before, after = max(0, min(int(before), 50)), max(0, min(int(after), 50))
        # For each line use its newest observation no newer than the requested
        # source version. Earlier immutable versions remain explicitly reachable.
        scope = "WITH eligible AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY line,ordinal ORDER BY source_version DESC,event_id) AS choice FROM events WHERE session_id=? AND source_version<=?) "
        earlier = self.db.execute(scope + "SELECT record_json FROM eligible WHERE choice=1 AND line<? ORDER BY line DESC,ordinal DESC LIMIT ?", (row["session_id"], row["source_version"], row["line"], before)).fetchall()
        later = self.db.execute(scope + "SELECT record_json FROM eligible WHERE choice=1 AND line>? ORDER BY line,ordinal LIMIT ?", (row["session_id"], row["source_version"], row["line"], after)).fetchall()
        same_line = self.db.execute("SELECT record_json FROM events WHERE session_id=? AND line=? AND event_id!=? ORDER BY source_version,event_id", (row["session_id"], row["line"], event_id)).fetchall()
        return {"event": json.loads(row["record_json"]), "before": [json.loads(r[0]) for r in reversed(earlier)], "after": [json.loads(r[0]) for r in later], "other_source_versions_at_line": [json.loads(r[0]) for r in same_line], "origin_node": row["origin_node"], "context_source_version_ceiling": row["source_version"]}
