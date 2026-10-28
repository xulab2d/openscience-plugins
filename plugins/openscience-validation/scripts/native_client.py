"""Transfer through Codex's saved plugin connection; never read or export tokens.

Uses the documented app-server MCP API without starting a model turn. Python
3.9+ and the Codex executable are needed only for this optional local helper.
The ordinary remote plugin has neither dependency. Login stays in Codex's UI.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
try:
    import tomllib
except ImportError:  # Bundled pure Python backport; no client pip setup.
    try:
        from vendor import tomli as tomllib
    except ImportError:
        from runtime.remote_context.vendor import tomli as tomllib

try:
    from desktop_runtime import resolve_codex
except ImportError:
    from runtime.remote_context.desktop_runtime import resolve_codex

try:
    from client import Client
except ImportError:
    from runtime.remote_context.client import Client


RESOURCE = "https://mcp.xu-lab.net/mcp"
PLUGINS = {"openscience-validation@xu-lab-remote": "openscience_validation",
           "openscience@xu-lab-remote": "openscience"}


class NativeClient(Client):
    """Reuse the native OAuth session; fail closed if login or approval is needed."""

    def __init__(self, plugin, *, codex_home=None, executable=None, timeout=600):
        if plugin not in PLUGINS:
            raise ValueError("Choose the installed shared OpenScience plugin")
        home = Path(codex_home or os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve()
        config = tomllib.loads((home / "config.toml").read_text())
        if not config.get("plugins", {}).get(plugin, {}).get("enabled"):
            raise ValueError("Enable and connect the shared OpenScience plugin in Codex first")
        executable = resolve_codex(executable)
        args = [str(executable), "app-server", "--stdio"]
        # These overrides bound optional helper startup to its own plugin; they
        # never change the user's config. Coexistence is tested separately with
        # both the production and validation plugins enabled.
        for name in config.get("plugins", {}):
            if not re.fullmatch(r"[A-Za-z0-9_@/-]+", name):
                raise ValueError("Plugin name cannot be isolated by this Codex version")
            args += ["-c", "plugins." + name + ".enabled=" +
                     ("true" if name == plugin else "false")]
        for name in config.get("mcp_servers", {}):
            if not re.fullmatch(r"[A-Za-z0-9_@/-]+", name):
                raise ValueError("Server name cannot be isolated by this Codex version")
            args += ["-c", "mcp_servers." + name + ".enabled=false"]
        self.url, self.plugin, self.timeout = RESOURCE, plugin, timeout
        self.server = PLUGINS[plugin]
        self.counter, self.thread_id = 0, None
        self.messages = queue.Queue(maxsize=128)
        self.stopped = threading.Event()
        self.process = subprocess.Popen(args, cwd=home,
            env=dict(os.environ, CODEX_HOME=str(home)), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", bufsize=1)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self._request("initialize", {"clientInfo": {"name": "openscience_transfer",
                "version": "0.2"}, "capabilities": {"experimentalApi": True}})
            self._send({"method": "initialized", "params": {}})
            thread = self._request("thread/start", {"ephemeral": True, "cwd": str(home)})
            if not thread["thread"].get("ephemeral"):
                raise RuntimeError("Native runtime did not provide an ephemeral context")
            self.thread_id = thread["thread"]["id"]
            status = self._request("mcpServerStatus/list", {"threadId": self.thread_id,
                "serverName": self.server, "detail": "toolsAndAuthOnly", "limit": 100})
            servers = [s for s in status["data"] if s["name"] == self.server]
            if (len(servers) != 1 or servers[0].get("pluginId") != plugin
                    or servers[0].get("authStatus") != "oAuth"
                    or "sync_status" not in servers[0].get("tools", {})):
                raise RuntimeError("Connect the selected OpenScience plugin in Codex before retrying")
            identity = self.call("sync_status")
            actor = identity.get("actor")
            if not actor:
                raise RuntimeError("OpenScience did not return its authenticated member")
            self.key_id = hashlib.sha256(json.dumps([plugin, self.url, actor]).encode()).hexdigest()
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            while not self.stopped.is_set():
                line = self.process.stdout.readline(24 * 1024 * 1024 + 1)
                if not line or len(line) > 24 * 1024 * 1024:
                    break
                message = json.loads(line)
                # Discard ordinary notifications: they can contain tool arguments.
                if "id" in message:
                    while not self.stopped.is_set():
                        try:
                            self.messages.put(message, timeout=0.2)
                            break
                        except queue.Full:
                            pass
        except (ValueError, OSError):
            pass
        finally:
            self.stopped.set()

    def _send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _request(self, method, params):
        self.counter += 1
        request_id = self.counter
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                result = self.messages.get(timeout=min(0.2, max(0.001, deadline-time.monotonic())))
            except queue.Empty:
                if self.stopped.is_set():
                    raise RuntimeError("Native OpenScience runtime exited; retry from Codex")
                continue
            if "method" in result:
                # A background collector cannot consent to new permissions or
                # approve an elicitation on the member's behalf.
                raise RuntimeError("OpenScience requires interaction in Codex; pending data retained")
            if result.get("id") != request_id:
                raise RuntimeError("Unexpected native response")
            if "error" in result:
                # Never echo a raw response that could contain private evidence.
                raise RuntimeError("Native OpenScience request failed; pending data retained")
            return result["result"]
        raise TimeoutError("Native OpenScience request timed out; retry to resume")

    def call(self, name, args=None):
        result = self._request("mcpServer/tool/call", {"threadId": self.thread_id,
            "server": self.server, "tool": name, "arguments": args or {}})
        if result.get("isError"):
            raise RuntimeError("OpenScience operation failed: " + name)
        return result.get("structuredContent") or json.loads(next(
            c["text"] for c in result["content"] if c["type"] == "text"))

    def close(self):
        self.stopped.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
