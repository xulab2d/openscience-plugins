# OpenScience Remote

Use Xu Lab's shared context, current skills, original conversations, scientific sources and figures from Codex. The service runs on the lab Mac; members need no AWS credentials, local corpus sync, NAS mount or Google Drive plugin. Lab records and credentials stay out of this public repository.

## Install

Ask Codex:

> Install OpenScience Remote from https://github.com/xulab2d/openscience-plugins.git, marketplace `xu-lab-remote`, then help me connect it.

Or run:

```sh
codex plugin marketplace add https://github.com/xulab2d/openscience-plugins.git
codex plugin add openscience-validation@xu-lab-remote
```

Open **Plugins → OpenScience Remote → Connect**, sign in with your authorized lab email, and approve the connection you initiated. Codex saves and renews it. Access is limited to the lab's configured member allowlist.

The internal package ID `openscience-validation` is retained so existing installations can update in place. The connection is uniquely named `openscience_validation`; it can coexist with the original OpenScience installation. You do not need to rename, disable or remove the original connector.

## Use

Ask normal lab questions in your existing Codex workflow. The plugin assembles current shared context and discovers the latest private lab skills. New skills, references, scripts and binary templates become available through the shared service without a plugin reinstall. Requested skill edits preserve version history and reject conflicting changes.

The service also supports source-linked observations and correction proposals, original-conversation retrieval, verified file transfer, existing figures, fixed host measurement/figure checks, and the lab's existing opt-in update subscriptions. Historical evidence does not authorize instruments or establish live acquisition status. The selected model stays in your conversation.

Installing or connecting does **not** capture conversations. Optional original-conversation capture is bundled but dormant. To enable it, explicitly ask Codex to capture lab conversations in named project folders on this computer and review the native hook permission. It reuses the saved browser connection; no key file or AWS setup is needed. Disable it at any time without affecting other computers. Original records are preserved with credential redaction and explicit coverage gaps. The optional helper supports Python 3.9+ and discovers the installed Codex desktop runtime; normal context access needs neither Python nor hooks.

## Updates

Ordinary OpenScience changes are central: context, existing and new shared skills,
supporting resources, and compatible server improvements are available through
the existing connection. Members do not run a sync job or reinstall the plugin
for those changes. The installed package is a stable loader for the current
server and skill catalog.

Only when the installed package itself changes, ask Codex to update this
marketplace and plugin, or run:

```sh
codex plugin marketplace upgrade xu-lab-remote --json
codex plugin add openscience-validation@xu-lab-remote --json
```

The current release is **0.2.0**. Shared context and skills update independently of the package. Browser grants renew for up to two weeks; reconnect normally when sign-in expires.

Changes to optional local capture hooks remain subject to Codex's native trust
review. Installing or updating the package never opts a computer into capture.

## Storage and migration

Active context and indexes remain on the lab Mac. Original conversations, artifacts and encrypted recovery backups use Google Drive for desktop on that host. Members access them through the authenticated service.

The service currently follows the existing publication stream so old clients and host jobs keep their updates and contributions. AWS retirement is a separate migration step; this release does not claim that old client outboxes or unavailable originals have all been recovered. No legacy data is deleted by installing this plugin.
