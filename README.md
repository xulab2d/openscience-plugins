# Xu Lab OpenScience plugin

This repository distributes the plugin package. Lab data and credentials are not included. The authenticated service runs on the lab host.

## Validation preview

The validation plugin connects to a historical snapshot. Keep the production OpenScience plugin while testing. Conversation capture and canonical context writes are disabled.

Once this repository is published, add its marketplace in Codex:

```sh
codex plugin marketplace add https://github.com/xulab2d/openscience-plugins.git
codex plugin add openscience-validation@xu-lab-remote
```

Then select Connect in the plugin details and sign in through the browser. Browser identity sign-in is currently being completed; the preview is not ready for general onboarding.

Codex downloads and installs this package directly; no manual file transfer is required. For workspace-managed distribution, an administrator can import this repository as a plugin marketplace. An install deep link works only after the marketplace is registered.
