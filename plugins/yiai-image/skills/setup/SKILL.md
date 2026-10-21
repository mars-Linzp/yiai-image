---
name: setup
description: Configure YiAI-image API credentials, Base URL, and default image model, or inspect its saved settings. Use for YiAI plugin setup, replacing a key, or changing image provider settings. Does not generate or edit images.
---

# YiAI-image Setup

Configure the installed YiAI-image plugin in the user's language. This is a
conversation-based setup workflow, not a native settings form. All commands
below are for Codex to run internally; never ask the user to run a script, open
a terminal, or set environment variables.

Resolve `../yiai-image/scripts/image_client.py` relative to this skill directory.
Use the available Python 3 interpreter. Reuse this client and its external user
configuration; do not create another credential store or edit installed code.

## Inspect And Collect Settings

Run the client's `status` command. Display only whether a key is configured,
where the effective key comes from, the effective Base URL, and model.

The plugin package never contains an API key. A configured key on a new plugin
installation comes from this computer and Windows/user account: either a saved
local configuration from an earlier YiAI-image or AigcRouter image installation
(`configuration_sources.api_key: saved`, stored outside the plugin at
`config_path`) or the `AIGCROUTER_API_KEY` environment variable
(`configuration_sources.api_key: environment`). State this source plainly so the
user is not led to believe the plugin supplied a key. Show `config_path` for a
saved key, but never its contents. Never read the configuration file into the
conversation or display a key, even partially. If the user only requested the
current status, show it and stop without saving anything.

For setup with no supplied choices and an existing key, say the existing key
was found on this computer and ask whether to keep it or enter the user's own
key. Do not describe it as configured by the plugin. Otherwise ask which settings
to configure in one concise conversation question. Offer keeping an existing saved key or replacing
it, and keeping the current URL/model or supplying new values. A missing key
must be requested directly in the conversation as the user's AigcRouter user API
key. Explain that it will be saved in the local user configuration. Preserve
settings the user did not ask to change; do not ask again for values already
provided for this request.

Defaults for a fresh installation are `https://api.aigcrouter.org` and
`gpt-image-2.5-sunburst`. Restore those preferences only if requested. Never
silently change the model or provider. Wait for required answers before saving.

`configuration_sources` identifies effective environment overrides. A local save
does not override `AIGCROUTER_API_KEY`, `AIGCROUTER_BASE_URL`, or
`AIGCROUTER_IMAGE_MODEL`. Report an active override when relevant, without reading
or exposing its secret value. Do not change the user's environment as part of
setup. `saved_api_key_configured` indicates whether `--keep-key` is available;
an environment-only key is not automatically copied into local storage.

## Save Internally

For a new or replacement key, follow the credential procedure in
`../yiai-image/SKILL.md`: start `configure --key-stdin` in an interactive command
session with hidden input, pass the supplied key through the session's stdin,
and wait for completion. A direct process-stdin channel is also supported. Never
put the key in shell commands, arguments, prompt files, logs, or source files.

For URL/model changes with a saved key, use `configure --keep-key`. It does not
read stdin or reveal the saved key. Include `--base-url` and/or `--model` only for
preferences the user requested to change. Quote nonsecret arguments appropriately.

```text
python <client> configure --keep-key --model "gpt-image-2.5-sunburst"
python <client> configure --keep-key --base-url "https://api.aigcrouter.org"
```

If no saved key exists, ask for it rather than attempting to reveal an
environment credential. If the user only wants to inspect settings, no key is
needed. Report a concrete saving or validation error and preserve the existing
configuration on invalid input.

## Verify And Finish

After a successful save, run `status` again and show the effective URL, model,
and configured/missing key state. Mention any environment override that prevents
the saved preference from taking effect. Say the configuration was saved locally;
do not claim that the key or model was verified against the service. Setup makes
no API request and must not submit a billable image as a connection test.

If setup interrupted a pending image request, return to `../yiai-image/SKILL.md`
and complete that request. Otherwise finish with the configuration result.
