# Xu Lab OpenScience plugin

Connect Codex to the lab's shared context, original conversations, source files, and figures. The authenticated service runs on the lab host. This repository contains the plugin package; lab records and credentials are not included.

## Install the validation preview

On a computer with Codex installed, ask Codex:

> Install OpenScience Validation from https://github.com/xulab2d/openscience-plugins.git, marketplace `xu-lab-remote`. Keep the production OpenScience plugin installed, then help me connect the validation plugin.

Or run:

```sh
codex plugin marketplace add https://github.com/xulab2d/openscience-plugins.git
codex plugin add openscience-validation@xu-lab-remote
```

Open **Plugins → OpenScience Validation → Connect**. Sign in with an authorized lab email and the one-time code sent to that inbox. Approve the connection you initiated. Codex saves and renews the connection automatically; you do not transfer a credential file or enter a lab connection key.

Start a new chat and explicitly select OpenScience Validation for testing. Ask it to assemble project context, retrieve an original conversation, and show an existing project figure.

This is an invited validation preview of a historical snapshot, not the production lab corpus. Keep the production OpenScience plugin installed. Canonical context writes and new conversation capture are disabled for the validation account. Installing the plugin never starts conversation capture.

## Distribution

Codex downloads the package from this repository. No manual ZIP transfer, AWS setup, local corpus sync, or Google Drive plugin is needed on a member's computer. A workspace administrator can also import this repository as a plugin marketplace.

The install deep link works only after the marketplace is registered:

[Open OpenScience Validation](codex://plugins/install/openscience-validation?marketplace=xu-lab-remote)

Browser login uses Cloudflare Access. Access tokens last 15 minutes and renew automatically within a two-week sign-in grant. After that, reconnect through the browser. Access is limited to the lab's explicit member allowlist.
