"""Find installed desktop runtimes without installing software or reading tokens."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess


def bundled_candidates(relative_path, application_roots=None):
    # This layout is verified in the installed desktop bundle. Test existence;
    # do not assume a particular app display name or user installation folder.
    roots = application_roots or [Path("/Applications"), Path.home() / "Applications"]
    for root in roots:
        for app in sorted(Path(root).glob("*.app")):
            candidate = app / "Contents" / "Resources" / relative_path
            if candidate.is_file() and os.access(candidate, os.X_OK):
                yield candidate


def resolve_codex(executable=None, *, application_roots=None):
    if executable:
        path = Path(executable).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise ValueError("The selected Codex runtime is unavailable")
    bundled = next(bundled_candidates("codex-cli/bin/codex", application_roots), None)
    found = bundled or shutil.which("codex")
    if not found:
        raise ValueError("The optional collector needs the installed Codex desktop runtime or CLI")
    return str(found)


def resolve_zstd_command(path, *, application_roots=None):
    # Python 3.14 has its own decoder. Older desktop Python uses the already
    # bundled Node decoder, or an existing zstd executable. No network/install.
    candidates = list(bundled_candidates("cua_node/bin/node", application_roots))
    installed = shutil.which("node")
    if installed:
        candidates.append(Path(installed))
    for candidate in candidates:
        try:
            check = subprocess.run([str(candidate), "-e",
                "process.exit(typeof require('node:zlib').createZstdDecompress==='function'?0:1)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if check.returncode == 0:
            code = ("require('node:stream').pipeline(require('node:fs').createReadStream(process.argv[1]),"
                    "require('node:zlib').createZstdDecompress(),process.stdout,"
                    "error=>{if(error)process.exitCode=1})")
            return [str(candidate), "-e", code, str(Path(path).resolve())]
    executable = shutil.which("zstd")
    if executable:
        return [executable, "-d", "-c", "--", str(Path(path).resolve())]
    raise RuntimeError("Compressed originals need Python 3.14 or the installed desktop Node/zstd runtime; originals retained")


class _DecodedStream:
    def __init__(self, process):
        self.process, self.eof = process, False

    def readline(self, size=-1):
        data = self.process.stdout.readline(size)
        if not data:
            self.eof = True
        return data

    def read(self, size=-1):
        data = self.process.stdout.read(size)
        if size < 0 or not data:
            self.eof = True
        return data

    def __iter__(self):
        return self

    def __next__(self):
        data = self.readline()
        if not data:
            raise StopIteration
        return data


@contextmanager
def open_zstd(path):
    process = subprocess.Popen(resolve_zstd_command(path), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    stream = _DecodedStream(process)
    try:
        yield stream
        if stream.eof and process.wait(timeout=10) != 0:
            raise RuntimeError("Compressed transcript decoding failed; original retained")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
