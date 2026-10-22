# YiAI Plugins

A Git-backed Codex plugin marketplace containing YiAI-image.

## YiAI-image

- Generate and edit raster images using the bundled Python 3 client.
- Default API base URL: `https://api.aigcrouter.org`.
- Default model: `gpt-image-2.5-sunburst`.
- On first use, Codex asks for an AigcRouter user API key in the conversation,
  saves it locally, and continues the pending image request.
- Automatic skill invocation is enabled. Explicit requests for another provider
  or workflow take precedence.

Python 3 must be available on the user's machine. No third-party Python packages
are required. Image API requests require the user's own credentials and quota.

## Install From GitHub

After publishing, replace `OWNER/REPO` with this repository's GitHub owner/name:

```powershell
codex plugin marketplace add OWNER/REPO --ref main
codex plugin add yiai-image@yiai
```

Local installation for development:

```powershell
codex plugin marketplace add /absolute/path/to/yiai-plugins
codex plugin add yiai-image@yiai
```

If migrating from a standalone `yiai-image` skill, keep a backup outside the
skill discovery directories and remove the duplicate after verifying the plugin.
Credentials are stored outside the plugin directory and can be reused.

## Configure YiAI-image

Version `0.1.4` makes command failures visible on stdout and saves per-request
execution receipts next to generated images. After a conversation reconnect,
Codex can read the existing receipt to display saved images or report the error
without submitting another generation. This cannot repair a disconnected host
model connection or a request routed to a different image tool.

Version `0.1.3` makes Setup report where an existing key comes from. The
plugin package contains no key. Verify the client's actual `status` result before
claiming a key is configured; a real configured result identifies a saved key or
an `AIGCROUTER_API_KEY` environment override.

Version `0.1.2` adds image dimension controls for generation and editing.
Users can ask for exact pixels such as "1536x1024" or ratios such as "16:9"
and "9:16". Ratio requests default to 2k; users can also request 1k or 4k.

Version `0.1.1` adds a conversation-based setup skill, declared through
`extensions.com.openai.onboardingSkill`. Run the plugin's Setup action when
available in your Codex client, or ask Codex to configure YiAI-image. This is a
guided conversation, not a native settings form or a separate web page.

The setup can save or replace your API key, change the Base URL or default model,
and show the current configuration without revealing the key. Changing only the
URL or model preserves an already saved key. Defaults remain
`https://api.aigcrouter.org` and `gpt-image-2.5-sunburst`.

Setup saves settings locally without calling the API or generating a test image.
Existing `AIGCROUTER_*` environment overrides still take precedence; setup reports
them without changing the environment. Plugin updates reuse the existing external
configuration. If a client does not expose Setup, ask: "Configure YiAI-image."

## Default Image Routing

The skill allows implicit invocation. For a user-wide default, merge
`plugins/yiai-image/skills/yiai-image/assets/default-image-routing.md` into the
user's `AGENTS.md`, preserving existing content. Installation does not run an
automatic hook to edit user configuration.

Higher-priority host or gateway instructions can override skill routing. See
the skill's installation notes for native-image conflicts and Sub2API account
settings. Installing this plugin does not change a remote gateway's settings.

## Publish And Refresh

1. Edit the image skill under `plugins/yiai-image/skills/yiai-image/` or the setup
   skill under `plugins/yiai-image/skills/setup/`.
2. Run the local checks below.
3. Increment `plugins/yiai-image/.codex-plugin/plugin.json`'s version.
4. Commit, push, and create a matching Git tag, such as `v0.1.0`.

Users can refresh a configured Git marketplace and inspect available versions:

```powershell
codex plugin marketplace upgrade yiai
codex plugin list --marketplace yiai --available --json
```

Marketplace refresh fetches the catalog snapshot. Confirm the installed plugin
version in the target Codex client and use its plugin installation/update flow;
do not assume refreshing the catalog alone upgraded a loaded plugin.

## Local Checks

From the repository root:

```powershell
python -B -m unittest discover -s plugins/yiai-image/skills/yiai-image/scripts -p test_image_client.py -v
```

These tests use a local HTTP server and fake credentials. They do not call the
real image API or incur generation charges.

## Credentials And Output

API keys are saved outside this repository, in the existing `aigcrouter-image`
user configuration directory. Never commit keys, generated images, local
configuration, or conversation logs. Updating the plugin must preserve the
external user configuration. No real API key is included in this repository.
