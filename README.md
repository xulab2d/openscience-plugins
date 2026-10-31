# OpenScience Remote

Connects Claude Code, Codex, Claude and ChatGPT to the Xu Lab's shared OpenScience context. The service runs on the lab Mac at `https://mcp.xu-lab.net/mcp`. You sign in with your lab email; no AWS credentials, local corpus, NAS mount or Google Drive plugin is needed. Access is limited to the lab's member allowlist, so ask the OpenScience admin to add you first.

Lab records and credentials are never stored in this public repository.

## Claude Code

Requires a current Claude Code (validated on 2.1.312 and later).

1. Install the plugin. This also adds the `xu-lab-claude` marketplace:

   ```sh
   claude plugin install openscience-remote --marketplace xulab2d/openscience-plugins
   ```

   From inside a session, the equivalent is `/plugin marketplace add xulab2d/openscience-plugins`, then `/plugin install openscience-remote@xu-lab-claude`.

2. Restart Claude Code.
3. Run `/mcp`, select **plugin:openscience-remote:openscience**, choose **Authenticate**, and sign in with your lab email in the browser.
4. Check the connection by asking: *"Run OpenScience sync_status."* A healthy result shows `authentication.kind: cloudflare_access`, `last_sync.ok: true`, and a generation and record count.

**Optional: use OpenScience automatically.** The plugin's skill loads for lab questions. To make it unconditional, add this to `~/.claude/CLAUDE.md`:

> For Xu Lab research, materials, experiments, measurements, methods, equipment or lab operations, call OpenScience `sync_status` and `assemble_context` before substantive work.

**File transfers.** Uploading or downloading original files needs Python 3.9+ and was verified on macOS only. Use `/openscience-file {"operation":"upload","path":"/absolute/path/figure.png","project_id":"known-project"}`. For a download, use `"operation":"download"`, a new local `path`, and the original's `sha256`. If a transfer reports a missing permission, allow only the tools it names under `/permissions`, then retry. See [the package README](plugins/openscience-remote/README.md).

**Updating.** Shared context and skills update on the server, so you don't need to reinstall for them. To update the plugin itself:

```sh
claude plugin marketplace update xu-lab-claude
claude plugin update openscience-remote@xu-lab-claude
```

## Codex

Requires a current Codex (validated on CLI 0.164). If `codex` isn't on your PATH, the ChatGPT desktop app includes it at `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`.

1. Add the marketplace (its name is `xu-lab-remote`) and install the plugin:

   ```sh
   codex plugin marketplace add xulab2d/openscience-plugins
   codex plugin add openscience-validation@xu-lab-remote
   ```

   Or ask Codex: *"Install OpenScience Remote from https://github.com/xulab2d/openscience-plugins.git, marketplace `xu-lab-remote`, then help me connect it."*

2. Sign in. In the app, open **Plugins → OpenScience Remote → Connect**. From a terminal, run:

   ```sh
   codex mcp login openscience_validation
   ```

   Then sign in with your lab email.

3. Check the connection: `codex mcp list` should show `openscience_validation` as `enabled` with `OAuth` auth.

The package ID `openscience-validation` and the server name `openscience_validation` are kept so that existing installs update in place. They can run alongside an older local OpenScience connector; you don't need to remove it.

**Updating:**

```sh
codex plugin marketplace upgrade xu-lab-remote
codex plugin add openscience-validation@xu-lab-remote
```

## Claude (web, desktop, phone)

Go to **Customize → Connectors → Add → Add custom connector**:

- Name: **OpenScience Remote**
- URL: `https://mcp.xu-lab.net/mcp`
- Authentication: **Sign in now**, with OAuth client set to **Register automatically**

Sign in with your lab email. On your phone, use the same Claude account and enable the connector from **+ → Connectors** in a conversation. On Team and Enterprise plans, an owner has to add the connector for the organization first.

**Stanford-managed Claude accounts** need an administrator to approve the connector. Request **OpenScience Remote** (`https://mcp.xu-lab.net/mcp`, OAuth → Register automatically) through [Stanford's connector request form](https://stanford.service-now.com/it_services?id=sc_cat_item&sys_id=4a36a9363b148bd08b33f9ac24e45abe). Approval doesn't grant access to lab data; each member still signs in. Claude Code connects separately, as described above.

## ChatGPT (web, phone)

In [ChatGPT Plugins](https://chatgpt.com/plugins), choose **Add → Add custom MCP server**. Name it **OpenScience Remote**, use `https://mcp.xu-lab.net/mcp`, and keep **OAuth**. Sign in with your lab email. Do this in your own ChatGPT account (the one on your phone), not the shared lab account. Installing the plugin in Codex doesn't add it to ChatGPT.

## What to expect

- **Context and skills are live.** New context, shared skills and server improvements arrive through the existing connection, with no sync job or reinstall. Packages: Claude Code 0.2.1, Codex 0.2.0.
- **Contributions are reviewed before they become canonical.** Observations and proposed corrections are saved under your name as unreviewed records.
- **There's no instrument authority.** Historical evidence never authorizes operating hardware or establishes live acquisition status.
- **Conversation capture is off by default.** Installing never captures conversations. Codex includes an optional capture hook that stays inactive until you ask Codex to enable it for named project folders. The Claude Code package has no capture collector.
- **Sign-in renews.** Browser grants last up to two weeks; reconnect normally when yours expires.
- **Data stays on the lab host.** Active context lives on the lab Mac. Originals and encrypted backups live in Google Drive on that host. The AWS retirement is a separate migration, and installing this plugin deletes no legacy data.
