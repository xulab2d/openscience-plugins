"""Dependency-free private-client transfer helper; credentials never enter arguments.

Ordinary context access uses the remote plugin directly. This helper is only for
optional capture and uploading/downloading original files on a desktop client.
"""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import urllib.request
import time
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Refusing to forward connection credentials to a redirect")


class Client:
    def __init__(self, credential_file):
        path = Path(credential_file).expanduser()
        import os
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ValueError("Credential file must be readable only by its owner")
        data = json.loads(path.read_text())
        self.url, self.key = data["resource"], data["token"]
        self.key_id = data.get("key_id", hashlib.sha256(self.key.encode()).hexdigest())
        url = urlsplit(self.url)
        if url.scheme != "https" or url.path != "/mcp" or url.username or url.password or url.query or url.fragment:
            raise ValueError("A canonical HTTPS /mcp resource is required")
        self.opener = urllib.request.build_opener(NoRedirect())
        self.counter = 0
        self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                               "clientInfo": {"name": "openscience-transfer", "version": "0.2.0"}})
        self.rpc("notifications/initialized", {}, notification=True)

    def rpc(self, method, params, notification=False):
        self.counter += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self.counter
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST", headers={
            "Authorization": "Bearer " + self.key, "Content-Type": "application/json",
            "User-Agent": "OpenScience-Transfer/0.2.0",
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"})
        with self.opener.open(request, timeout=600) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if notification:
            return None
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Oversized server response")
        result = json.loads(raw)
        if "error" in result:
            raise RuntimeError("Remote protocol request failed")
        return result["result"]

    def call(self, name, args=None):
        result = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        if result.get("isError"):
            raise RuntimeError("OpenScience operation failed: " + name)
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def close(self):
        """Compatibility with native clients that own a short-lived process."""

    def upload(self, path, state_dir, *, kind="artifact", project_id="", device_id="", continue_upload=None):
        def check_active():
            if continue_upload is not None and not continue_upload():
                raise RuntimeError("Capture stopped locally; pending evidence is retained")
        check_active()
        path, state_dir = Path(path), Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        sha = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""): sha.update(chunk)
        digest = sha.hexdigest()
        fingerprint = hashlib.sha256(json.dumps([self.url, self.key_id, digest, kind, project_id, path.name] + ([device_id] if kind == "conversation" else [])).encode()).hexdigest()
        receipt = state_dir / (fingerprint + ".receipt.json")
        if receipt.exists(): return json.loads(receipt.read_text())
        pending = state_dir / (fingerprint + ".pending.json")
        if pending.exists():
            upload = json.loads(pending.read_text())
            status = self.call("upload_status", {"upload_id": upload["upload_id"]})
            offset = status["next_offset"]
        else:
            check_active()
            arguments = {"filename": path.name, "sha256": digest,
                "size": path.stat().st_size, "kind": kind, "project_id": project_id}
            if kind == "conversation":
                arguments["device_id"] = device_id
            upload = self.call("begin_upload", arguments)
            save(pending, upload)
            offset = 0
        with path.open("rb") as f:
            f.seek(offset)
            while chunk := f.read(upload["chunk_bytes"]):
                check_active()
                result = self.call("upload_chunk", {"upload_id": upload["upload_id"], "offset": offset,
                                                    "base64_data": base64.b64encode(chunk).decode()})
                offset = result["next_offset"]
        deadline = time.monotonic() + 120
        while True:
            check_active()
            result = self.call("complete_upload", {"upload_id": upload["upload_id"]})
            if result.get("state") != "pending_cloud_sync":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Drive sync is still pending; original and upload state are retained. Retry to resume.")
            time.sleep(min(5, max(1, result.get("retry_after_seconds", 5))))
        if not result["receipt"].get("exact_readback") or result["receipt"]["sha256"] != digest:
            raise ValueError("Remote receipt does not match the original")
        save(receipt, result)
        return result


def save(path, value):
    import os, tempfile
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2)
        f.flush(); os.fsync(f.fileno())
        draft = Path(f.name)
    draft.replace(path)


def main():
    import argparse
    from contextlib import closing
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--credential-file", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--plugin", default="openscience-validation@xu-lab-remote",
                   help="Installed shared plugin; uses its saved browser connection")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("file", type=Path)
    p.add_argument("--project", default="")
    a = p.parse_args()
    if a.credential_file:
        client = Client(a.credential_file)
    else:
        try:
            from native_client import NativeClient
        except ImportError:
            from runtime.remote_context.native_client import NativeClient
        client = NativeClient(a.plugin)
    with closing(client):
        result = client.upload(a.file, a.state, project_id=a.project)
    print(json.dumps(result, indent=2))


if __name__ == "__main__": main()
