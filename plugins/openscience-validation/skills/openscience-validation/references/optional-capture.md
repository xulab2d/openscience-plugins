# Optional original-conversation capture

Capture is off until the user explicitly opts this computer and selected lab project folders in. Installing the plugin, connecting, reading shared context, or granting capture capability does not enroll any computer. Never capture unrelated personal folders or enable collection merely to troubleshoot the connection.

When the user asks to enable capture, establish the exact lab project folders covered by that request. Explain that it includes available past and future visible exchanges in those folders, redacts recognizable credentials, excludes system/developer instructions and hidden reasoning, and records attachment references rather than attachment binaries. Then perform enrollment using the scripts bundled with this installed plugin:

```sh
python3 "<installed-plugin-directory>/scripts/capture_client.py" --enable --workspace "<requested-lab-project-folder>"
```

Resolve the real plugin directory from the installed skill location; do not ask the user to find files, exchange keys, configure AWS, or set environment variables. Add one `--workspace` argument for each requested folder. The default plugin connection is `openscience-validation@xu-lab-remote`. The helper uses the existing Codex browser connection, finds the installed desktop runtime, and never reads or exports credentials or starts a model turn. Its persistent state is `~/.openscience-capture`; plugin updates retain it. Python 3.9+ is supported. The bundle includes the TOML compatibility dependency; compressed transcripts use Python 3.14 or the installed desktop Node runtime.

Codex requires the user to review and trust plugin hooks before automatic capture can run. Honor that native permission flow. Do not mark hooks trusted yourself or bypass it. Once enrolled and trusted, Stop and SessionEnd hooks run bounded, resumable collection. An interrupted upload keeps its original and pending state until exact Drive readback is verified.

To stop this computer's capture at the user's request:

```sh
python3 "<installed-plugin-directory>/scripts/capture_client.py" --disable
```

This disables local capture immediately, queues remote revocation if offline, preserves existing shared evidence, and does not revoke other enrolled computers. `conversation_capture_status` checks the stable device ID from this computer's capture configuration; `conversation_coverage` reports stored coverage and gaps. Never infer that missing or unreported computers were captured. A remote revocation is honored before opening any transcript database and does not silently reenroll itself.

For an expired native login, use the normal Codex connection interface. Pending evidence remains local until the connection is restored. Ordinary shared context and figure access work without enabling capture.
