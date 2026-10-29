"""Private byte operations for Claude's native MCP mod; JSON over stdin/stdout.

This helper has no network access, credentials, transcript access, or capture
operation. Stdout is consumed by the mod, never returned as a model tool result.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile

CHUNK_BYTES = 196608
STATE = Path.home() / ".openscience-claude-transfers"


def check_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid SHA-256")
    return value


def digest(path, count=None):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = count
        while remaining is None or remaining > 0:
            chunk = stream.read(1048576 if remaining is None else min(1048576, remaining))
            if not chunk:
                if remaining:
                    raise ValueError("Truncated partial file")
                break
            result.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return result.hexdigest()


def save(path, value):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
        draft = Path(stream.name)
    draft.replace(path)


@contextmanager
def locked(path):
    # The shipped macOS/Linux path. No Windows compatibility claim until tested.
    import fcntl
    with path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def summary(state, **extra):
    return {"operation": state["request"]["operation"], "local_path": state["request"]["path"],
            "sha256": state["sha256"], "bytes": state["bytes"], **extra}


def action(state, name, arguments):
    state["action_id"] = secrets.token_hex(16)
    state["call"] = {"name": name, "arguments": arguments}


def prepare(request):
    if not isinstance(request, dict) or set(request) - {"operation", "path", "sha256", "project_id"}:
        raise ValueError("Invalid request")
    operation = request.get("operation")
    if operation not in {"upload", "download"} or not isinstance(request.get("path"), str):
        raise ValueError("Artifact operation and explicit local path required")
    target = Path(request["path"]).expanduser()
    if not target.is_absolute():
        raise ValueError("Use an absolute local path")
    # Refuse symlink destinations; resolving one would hide an overwrite target.
    if target.is_symlink():
        raise ValueError("Use an original file path, not a symlink")
    target = target.resolve()
    project = request.get("project_id", "")
    if not isinstance(project, str) or len(project) > 200:
        raise ValueError("Invalid project")
    clean = {"operation": operation, "path": str(target), "project_id": project}
    if operation == "upload":
        if not target.is_file():
            raise ValueError("Upload requires an original file")
        sha = digest(target)
        size = target.stat().st_size
    else:
        sha = check_hash(request.get("sha256"))
        clean["sha256"] = sha
        size = None
    identity = hashlib.sha256(json.dumps([clean, sha], sort_keys=True).encode()).hexdigest()
    return identity, {"request": clean, "sha256": sha, "bytes": size, "offset": 0}


def verify_original(state):
    path = Path(state["request"]["path"])
    if path.is_symlink() or path.stat().st_size != state["bytes"] or digest(path) != state["sha256"]:
        raise ValueError("Original changed during transfer")


def partial_path(state, identity):
    target = Path(state["request"]["path"])
    return target.parent / (".openscience-claude-" + identity + ".part")


def verify_partial(state, partial):
    if partial.is_symlink():
        raise ValueError("Partial cannot be a symlink")
    offset = 0
    with partial.open("rb") as stream:
        for item in state["chunks"]:
            chunk = stream.read(item["bytes"])
            if len(chunk) != item["bytes"] or hashlib.sha256(chunk).hexdigest() != item["sha256"]:
                raise ValueError("Saved partial checksum failed")
            offset += len(chunk)
    if offset != state["offset"]:
        raise ValueError("Invalid partial checkpoint")


def queue_upload(state):
    offset = state["offset"]
    if type(offset) is not int or not 0 <= offset <= state["bytes"]:
        raise ValueError("Invalid server upload offset")
    if offset == state["bytes"]:
        verify_original(state)
        action(state, "complete_upload", {"upload_id": state["upload_id"]})
    else:
        action(state, "upload_chunk", {"upload_id": state["upload_id"], "offset": offset})


def advance(state, identity, response):
    if not isinstance(response, dict):
        raise ValueError("Invalid server response")
    name = state["call"]["name"]
    if name in {"begin_upload", "upload_status"}:
        if name == "begin_upload":
            upload_id, chunk_bytes = response.get("upload_id"), response.get("chunk_bytes")
            if not isinstance(upload_id, str) or not re.fullmatch(r"[0-9a-f]{48}", upload_id):
                raise ValueError("Invalid upload ID")
            if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= CHUNK_BYTES:
                raise ValueError("Invalid upload chunk size")
            state.update(upload_id=upload_id, chunk_bytes=chunk_bytes)
        state["offset"] = response.get("next_offset")
        queue_upload(state)
    elif name == "upload_chunk":
        expected = min(state["bytes"], state["offset"] + state["chunk_bytes"])
        if response.get("next_offset") != expected:
            raise ValueError("Upload offset did not advance exactly")
        state["offset"] = expected
        queue_upload(state)
    elif name == "complete_upload":
        if response.get("state") == "pending_cloud_sync":
            state["pending"] = True
            return
        receipt = response.get("receipt", {})
        if (receipt.get("exact_readback") is not True or receipt.get("sha256") != state["sha256"]
                or receipt.get("bytes") != state["bytes"]):
            raise ValueError("Remote receipt failed verification")
        verify_original(state)
        state["done"] = True
    elif name == "read_artifact":
        target, partial = Path(state["request"]["path"]), partial_path(state, identity)
        total, offset = response.get("bytes"), state["offset"]
        if (response.get("sha256") != state["sha256"] or type(total) is not int or total < 0
                or state["bytes"] is not None and state["bytes"] != total):
            raise ValueError("Artifact identity or size changed")
        encoded = response.get("base64")
        if not isinstance(encoded, str) or len(encoded) > CHUNK_BYTES * 4 // 3 + 4:
            raise ValueError("Oversized artifact response")
        chunk = base64.b64decode(encoded, validate=True)
        stop = offset + len(chunk)
        if (response.get("offset", 0 if total == 0 else None) != offset or stop > total
                or response.get("next_offset") != (stop if stop < total else None)
                or not chunk and total != 0
                or chunk and hashlib.sha256(chunk).hexdigest() != response.get("chunk_sha256")):
            raise ValueError("Artifact chunk verification failed")
        if partial.is_symlink() or partial.stat().st_size < offset:
            raise ValueError("Saved partial was truncated")
        with partial.open("r+b") as stream:
            stream.truncate(offset)  # discard only a crash tail after the saved prefix
            stream.seek(offset)
            stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        state["chunks"].append({"bytes": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
        state.update(offset=stop, bytes=total)
        if stop == total:
            if digest(partial) != state["sha256"]:
                raise ValueError("Complete artifact checksum failed")
            try:
                os.link(partial, target)  # atomic create, never replace an existing original
            except FileExistsError:
                if target.is_symlink() or not target.is_file() or digest(target) != state["sha256"]:
                    raise ValueError("Destination exists with different bytes") from None
            state["done"] = True
        else:
            action(state, "read_artifact", {"sha256": state["sha256"], "offset": stop})
    else:
        raise ValueError("Unknown transfer phase")


def step(payload, root=STATE):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if "request" in payload:
        identity, initial = prepare(payload["request"])
    else:
        identity, initial = check_hash(payload.get("id")), None
    path = root / (identity + ".json")
    with locked(root / (identity + ".lock")):
        state = json.loads(path.read_text()) if path.exists() else initial
        if state is None:
            raise ValueError("Transfer checkpoint missing")
        state.pop("pending", None)
        target = Path(state["request"]["path"])
        if initial is not None:
            if state["request"] != initial["request"] or state["sha256"] != initial["sha256"]:
                raise ValueError("Transfer identity mismatch")
            if state["request"]["operation"] == "upload":
                # Revalidate account authorization at the server on every retry;
                # saved receipts alone never pretend a new account uploaded data.
                state.pop("done", None)
                if "upload_id" in state:
                    action(state, "upload_status", {"upload_id": state["upload_id"]})
                else:
                    action(state, "begin_upload", {"filename": target.name, "sha256": state["sha256"],
                           "size": state["bytes"], "kind": "artifact", "project_id": state["request"]["project_id"]})
            elif target.exists():
                if not target.is_file() or digest(target) != state["sha256"]:
                    raise ValueError("Destination exists with different bytes")
                state.update(done=True, bytes=target.stat().st_size)
            else:
                state.pop("done", None)
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = partial_path(state, identity)
                if "chunks" not in state:
                    # Persist the empty checkpoint before making the partial, so
                    # interrupted initialization can safely resume in either order.
                    state["chunks"] = []
                    save(path, state)
                if not partial.exists() and state["offset"] == 0:
                    os.close(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                verify_partial(state, partial)
                action(state, "read_artifact", {"sha256": state["sha256"], "offset": state["offset"]})
        else:
            if payload.get("action_id") != state.get("action_id"):
                raise ValueError("Stale or concurrent response; retry the original command")
            advance(state, identity, payload.get("response"))
        save(path, state)
        if state.get("done"):
            partial = partial_path(state, identity)
            if state["request"]["operation"] == "download" and partial.exists():
                partial.unlink()
            return {"done": True, "summary": summary(state, ok=True, verified=True)}
        if state.get("pending"):
            return {"pending": True, "summary": summary(state, ok=False, pending_cloud_sync=True,
                    retry="Run the same command; original and upload checkpoint are retained")}
        call = state["call"]
        if call["name"] == "upload_chunk":
            if target.stat().st_size != state["bytes"]:
                raise ValueError("Original size changed")
            with target.open("rb") as stream:
                stream.seek(state["offset"])
                chunk = stream.read(state["chunk_bytes"])
            call = {"name": "upload_chunk", "arguments": {**call["arguments"],
                    "base64_data": base64.b64encode(chunk).decode()}}
        return {"id": identity, "action_id": state["action_id"], "call": call}


def main():
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Oversized private transfer response")
        print(json.dumps(step(json.loads(raw))))
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "pending_retained": True}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
