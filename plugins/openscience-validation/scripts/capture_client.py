"""Optional desktop collector. With no explicit enrollment it performs no reads or uploads."""
from __future__ import annotations
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import time
import uuid

try:  # packaged alongside the original collector, no lab workspace installation
    from conversation_archive import ConversationArchive, _under
    from client import Client, save
except ImportError:
    from runtime.conversation_archive import ConversationArchive, _under
    from runtime.remote_context.client import Client, save


def connection(config):
    if config.get("authentication") == "native_oauth":
        try:
            from native_client import NativeClient
        except ImportError:
            from runtime.remote_context.native_client import NativeClient
        return NativeClient(config["plugin"], codex_home=config["codex_home"])
    return Client(config["credential_file"])  # retained legacy enrollment only


def disable(data_dir):
    """Stop locally first. A failed remote revocation remains queued for retry."""
    config_file = Path(data_dir) / "capture.json"
    if not config_file.exists():
        return {"enabled": False}
    config = json.loads(config_file.read_text())
    config.update(enabled=False, revocation_pending=True)
    save(config_file, config)
    try:
        with closing(connection(config)) as client:
            client.call("set_conversation_capture", {"enabled": False, "device_id": config["node_id"]})
    except Exception:
        return {"enabled": False, "revocation_pending": True, "pending_retained": True}
    config["revocation_pending"] = False
    save(config_file, config)
    return {"enabled": False, "revocation_pending": False}


def run(data_dir, hook=None):
    data_dir = Path(data_dir)
    config_file = data_dir / "capture.json"
    if not config_file.exists(): return {"enabled": False}
    config = json.loads(config_file.read_text())
    if not config.get("enabled"):
        return disable(data_dir) if config.get("revocation_pending") else {"enabled": False}
    if not config.get("allowed_roots"):
        raise ValueError("Explicit lab workspace enrollment required")
    if hook and not _under(hook.get("cwd", ""), config["allowed_roots"]):
        return {"enabled": True, "skipped": "outside enrolled lab workspace"}
    def locally_enabled():
        current = json.loads(config_file.read_text())
        return bool(current.get("enabled") and current.get("node_id") == config.get("node_id")
                    and current.get("allowed_roots") == config["allowed_roots"])
    # No model turn, recursive hook, or edits to the original Codex transcripts.
    lock = (data_dir / ".capture.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if lock.tell() == 0: lock.write(b"0"); lock.flush()
            lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return {"enabled": True, "deferred": "another capture is active"}
    try:
        with closing(connection(config)) as client:
            # Authenticate and check current device consent before opening any
            # transcript database. Remote revocation never auto-reenrolls.
            enrollment = client.call("conversation_capture_status", {"device_id": config["node_id"]})
            if not enrollment.get("enabled"):
                config.update(enabled=False, remote_revoked=True)
                save(config_file, config)
                return {"enabled": False, "remote_revoked": True, "pending_retained": True}
            if not locally_enabled():
                return {"enabled": False, "pending_retained": True}
            with ConversationArchive(data_dir / "capture-index.sqlite3", data_dir / "objects") as archive:
                report = archive.ingest_codex(config["codex_home"], config["allowed_roots"], max_threads=40, max_events=5000, chunk_events=100)
                manifest = archive.export_manifest(config["node_id"])
                for obj in manifest["objects"]:
                    if not locally_enabled():
                        return {"enabled": False, "pending_retained": True}
                    path = archive.object_path(obj["sha256"])
                    done = data_dir / (obj["sha256"] + ".uploaded.json")
                    if not done.exists():
                        result = client.upload(path, data_dir / "transfers", kind="conversation",
                                               device_id=config["node_id"], continue_upload=locally_enabled)
                        if (not result.get("receipt", {}).get("exact_readback")
                                or result["receipt"].get("sha256") != obj["sha256"]):
                            raise ValueError("Capture receipt did not verify the pending original")
                        save(done, result)
                    ids = [r[0] for r in archive.db.execute("SELECT event_id FROM events WHERE object_sha=?", (obj["sha256"],))]
                    with archive.db:
                        archive.db.executemany("DELETE FROM events_fts WHERE event_id=?", [(e,) for e in ids])
                        archive.db.execute("UPDATE events SET text='',record_json='' WHERE object_sha=?", (obj["sha256"],))
                    if path.exists(): path.unlink()  # verified replaceable upload copy only
                if not locally_enabled():
                    return {"enabled": False, "pending_retained": True}
                coverage = client.call("record_capture_coverage", {"manifest": manifest, "device_id": config["node_id"]})
        status = {"enabled": True, "last_success": time.time(), "capture": report, "coverage": coverage}
        save(data_dir / "status.json", status)
        return status
    finally:
        lock.close()


def capture_data_dir():
    # Commands run by an agent and plugin hooks must select the same persistent
    # state even when only hooks receive PLUGIN_DATA. Retain a preexisting legacy
    # plugin-data enrollment; new enrollment always uses the stable home folder.
    default = Path("~/.openscience-capture").expanduser()
    if (default / "capture.json").exists():
        return default
    for name in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        configured = os.environ.get(name)
        if configured and (Path(configured).expanduser() / "capture.json").exists():
            return Path(configured).expanduser()
    return default


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=capture_data_dir())
    p.add_argument("--enable", action="store_true", help="Opt in to available past and future visible lab exchanges under the selected workspaces")
    p.add_argument("--disable", action="store_true")
    p.add_argument("--workspace", type=Path, action="append")
    p.add_argument("--credential-file", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--plugin", default="openscience-validation@xu-lab-remote",
                   help="Installed shared plugin; uses its saved browser connection")
    p.add_argument("--codex-home", type=Path, default=Path("~/.codex").expanduser())
    p.add_argument("--hook", action="store_true")
    p.add_argument("--dispatch", action="store_true", help="Start the optional collector without blocking a short SessionEnd hook")
    a = p.parse_args()
    if a.enable and a.disable: p.error("Choose enable or disable")
    config_file = a.data / "capture.json"
    if a.enable:
        if not a.workspace: p.error("Enrollment needs at least one explicit --workspace")
        roots = [str(p.expanduser().resolve()) for p in a.workspace]
        if any(Path(root) == Path.home() or Path(root) in Path.home().parents or not Path(root).is_dir() for root in roots):
            p.error("Choose existing lab project directories; home/root-wide capture is refused")
        a.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        node_id = json.loads(config_file.read_text())["node_id"] if config_file.exists() else uuid.uuid4().hex
        config = {"enabled": True, "allowed_roots": roots,
                  "codex_home": str(a.codex_home.resolve()), "node_id": node_id}
        if a.credential_file:
            config["credential_file"] = str(a.credential_file.resolve())
        else:
            config.update(authentication="native_oauth", plugin=a.plugin)
        with closing(connection(config)) as client:
            client.call("set_conversation_capture", {"enabled": True, "device_id": node_id, "source_label": node_id})
        save(config_file, config)
    elif a.disable:
        print(json.dumps(disable(a.data))); return
    try:
        hook = json.load(sys.stdin) if a.hook else None
        if a.dispatch:
            if not config_file.exists(): return
            config = json.loads(config_file.read_text())
            if config.get("enabled"):
                if not hook or not _under(hook.get("cwd", ""), config.get("allowed_roots", [])): return
            elif not config.get("revocation_pending"):
                return
            import subprocess
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--data", str(a.data)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return
        result = run(a.data, hook)
        if not a.hook: print(json.dumps(result))
    except Exception as exc:
        # No raw provider response, key, transcript or exception message in logs.
        if a.data.exists(): save(a.data / "status.json", {"ok": False, "error_type": type(exc).__name__, "pending_retained": True})
        print("OpenScience capture deferred; local pending evidence retained", file=sys.stderr)
        if not a.hook: raise SystemExit(1)


if __name__ == "__main__": main()
