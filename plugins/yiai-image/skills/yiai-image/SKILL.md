---
name: yiai-image
description: YiAI-image is the default skill for raster image generation and editing, and for diagnosing YiAI connection failures. Automatically use when the user asks to generate, draw, create, modify, retouch, recolor, or remove backgrounds from images, including artwork, illustrations, icons, and visual assets. No skill name, provider, or model mention is needed. Uses the AigcRouter API with a user key collected in conversation. Not for charts, screenshots, or vector-only diagrams.
---

# YiAI-image

Use the bundled Python 3 standard-library client. Resolve `scripts/image_client.py`
relative to this SKILL.md; do not assume the current directory is the skill folder.
Use `python3` or `python` according to the available interpreter.
All commands in this document are for Codex to execute internally. Do not ask the
user to execute a script, open a terminal, or configure an environment variable.

## Automatic Invocation

Treat this as the default workflow for image generation and image editing,
including ordinary requests such as "draw a bicycle", "create a logo image",
"change this image's background", and visual assets needed while building an
application. The user does not need to mention AigcRouter, Sunburst, or a skill
name. Respect an explicit request to use another provider or workflow.

Load this skill, check credentials, and complete its workflow without asking the
user to select an image tool. Do not require `$yiai-image` in the request.
Keep automatic invocation enabled in `agents/openai.yaml`.

For installation as the user's default image workflow, installers should merge
[assets/default-image-routing.md](assets/default-image-routing.md) into the
user-level `AGENTS.md`, preserving existing instructions. This is an installation
step, not something to repeat on image requests. The routing rule complements
automatic skill discovery; it cannot override higher-priority host instructions.

During installation as the default image workflow, use the supported Codex
configuration command `codex features disable image_generation` to disable the
competing native client image tool. Fully quit and restart the Codex desktop
application and its app-server process after changing this setting. A new window
or chat can reuse the old backend and keep `feature.image_generation=true` even
when a fresh `codex features list` process reports false. Verify the effective
feature in a new desktop request before claiming that routing is fixed. Do not
terminate a running backend automatically. This does not disable image viewing
or this skill's HTTP client. Disabling the local feature alone does not prevent
a gateway from injecting a hosted image tool or instructions that force its use.
For Sub2API accounts dedicated to this workflow, the server's Codex image bridge
policy "Remove client image tools" (`codex_image_generation_explicit_tool_policy:
"strip"`) suppresses both client-declared native image tools and automatic hosted
bridge injection. Merely disabling hosted-tool injection still allows native
tools declared by the client. Apply server settings only with appropriate access
and authorization; do not ask the user for administrator credentials here.
If the gateway requires native image generation, report that specific routing
conflict. A completed native image call followed by an empty desktop response is
not evidence that this client failed. Inspect existing output before retrying a
potentially billable request. Do not claim that a skill or user-level instruction
overrides a higher-priority native-image requirement, or that editing local
gateway source has changed the running service.

## Execute Through Completion

Reading this file is preparation, not completion of an image request. Next run
the bundled client's `status` command. When a key is configured, run `generate`
or `edit`, wait for the command to finish, and inspect and display the saved
image. Use the HTTP client described here for this workflow. A hosted native
`image_generation_call` alone is not evidence that this skill ran or that the
requested model and saved API key were used.

Every image request must finish with a visible result: an absolute-path Markdown
image, a direct in-chat request for a missing key, or the specific error that
prevented completion. Do not return an empty final message or stop after saying
that the skill was loaded. If a higher-priority native-image requirement prevents
this workflow, explain that conflict instead of silently switching paths.

The native tool's instruction to finish with no text applies to that native tool,
not to this HTTP client. A failed YiAI command requires a nonempty final reply
in the user's language explaining its `error`. Tool output alone is not the final
reply. Do not mark a failed command as success or stop after a progress update.

Defaults:
- Base URL: `https://api.aigcrouter.org`
- Image model: `gpt-image-2.5-sunburst` (the router's model identifier; preserve it).
- Output directory: `output/aigcrouter-images` inside the user's current workspace.

## First Use and Credentials

For a standalone request to configure the plugin, replace a key, or change saved
preferences, use [YiAI Settings](../setup/SKILL.md). The plugin's Setup entry also
opens that skill. Continue ordinary image requests below without requiring users
to complete a separate setup first.

Run `python <skill-dir>/scripts/image_client.py status` first. This reads local
configuration without making a network request or revealing the key.

If `api_key_configured` is false, ask for the user's **AigcRouter user API key
directly in the conversation**, using an available user-input tool or a normal
chat reply. Ask in the user's language and explain that Codex will save it in
the local user configuration and continue the pending image request. Do not
send the user a setup command or ask them to configure the key themselves.

Wait for the user's reply. Preserve the pending image prompt, reference files,
and requested output while waiting; do not ask the user to repeat the request.
Do not request an administrator key, invent a key, or use another provider's key.
This setup prompt applies only when the key is missing or must be replaced, not
on every image request. Reuse a key already supplied for this setup in the same
conversation rather than asking for it again.

Once the user supplies the key, Codex must save it automatically:

1. Start `python <skill-dir>/scripts/image_client.py configure --key-stdin` in
   an interactive command session (for `exec_command`, use `tty: true`).
2. The command uses hidden input when attached to a terminal. Send the supplied
   key and a newline through the session's stdin tool, such as `write_stdin`.
   When a tool supports direct process input without a terminal, passing the
   key via that stdin channel also works. Never interpolate it into a command.
3. Wait for configuration to complete and check `status` without printing the
   key. Then resume the original generation or edit request automatically.

The key is saved in the local user's configuration directory as JSON. Never
echo it, put it in command arguments, shell history, source files, or result
messages. If the available tools cannot supply stdin, use a tool's structured
file-writing capability to save the configuration in the user configuration
path returned by `status`, with user-only permissions where supported. Do not
write credentials in the project or use shell commands containing the key. If
neither method is available, report the specific tooling limitation; do not
replace the requested in-chat setup with instructions for the user to run code.

An already configured `AIGCROUTER_API_KEY` overrides the saved key. Optional `AIGCROUTER_BASE_URL` and
`AIGCROUTER_IMAGE_MODEL` override the saved defaults. Explicit CLI options take
precedence over environment/configuration. Keep these defaults unless the user
requests an override. Do not silently switch models when one is unavailable.
The status result includes `configuration_sources` and
`saved_api_key_configured` without revealing credentials. To change saved URL or
model preferences while preserving the local key, run `configure --keep-key`
with only the requested options; this does not prompt for the key again.

## Generate and Edit

Submit one image by default. Preserve a complete image prompt the user supplies;
when the user gives a goal rather than a prompt, write a suitable prompt without
adding unrelated subject matter. Pass prompts as a single argument or use a UTF-8
`--prompt-file` when shell quoting would be fragile.

```text
python <skill-dir>/scripts/image_client.py generate --prompt "A red bicycle on a white background"
python <skill-dir>/scripts/image_client.py generate --prompt-file prompt.txt --size 1536x1024
python <skill-dir>/scripts/image_client.py generate --prompt "A city skyline" --aspect-ratio 16:9 --resolution 4k
python <skill-dir>/scripts/image_client.py edit --image reference.png --prompt "Make the background green"
```

Use repeatable `--image` for multiple references and optional `--mask` for editing.
`--size`, `--aspect-ratio`, `--resolution`, `--quality`, `--n`, `--model`,
`--base-url`, and `--output-dir` are available. Do not send optional image
parameters unless they are useful for the request.

Map image dimensions from the user's request:

- Exact pixels such as "1536x1024" or "1024*1536": use `--size WIDTHxHEIGHT`.
- A ratio such as "16:9", "9:16", "4:3", "3:4", or "1:1": use
  `--aspect-ratio WIDTH:HEIGHT`; add `--resolution 1k`, `2k`, or `4k` only when
  the user specifies a size tier. Ratio-only requests use 2k.
- Common descriptions: landscape/widescreen means `16:9`, portrait/phone
  wallpaper means `9:16`, and square means `1:1`, unless the user gives another
  ratio.

Use either `--size` or `--aspect-ratio`, never both. Ratios above 3:1 are
rejected. The client converts ratios to dimensions divisible by 16, keeps 4k
outputs within 3840x2160 total pixels, and reports `requested_size`. For edits,
omit size options unless the user asks to change output dimensions. Relative image and output paths resolve against the current workspace.
Keep outputs within that workspace unless the user requests another destination.

The client saves returned base64, data URLs, or downloaded image URLs as files
and prints one final JSON record on stdout, including on nonzero exit. It never
prints image base64. Before sending a request it writes a unique `result-*.json`
receipt in the output directory and flushes its absolute `result_file` path to
stderr. The receipt is updated with the result or error when the command ends.
Inspect a saved image with an available image-viewing tool before describing its
appearance. Show the result using Markdown image syntax with its absolute path.
Report the actual error when generation fails; do not claim an image was created.

## Long-Running Jobs and Failures

For a standalone request to diagnose YiAI networking, run `diagnose` below and
report its JSON. Do not generate an image or request a key for this diagnostic.

Synchronous generation is the default (600-second HTTP timeout). Keep an ongoing
command session and wait for it; do not launch another generation because the
first command is still running. The client does not retry generation POSTs.

When an exec tool returns a session or cell ID, continue waiting on that same
session until it exits. Yielding tool output is not generation failure. Read the
final stdout JSON even when the exit code is nonzero. On `failed` or `cancelled`,
display any saved `files` and give a short final reply containing the concrete
`error` and `result_file` path. Do not end with an empty message or only "failed".

After a conversation reconnect or missing command output, inspect the existing
session and its receipt before submitting anything. Read a receipt without an API
request or credentials using:

```text
python <skill-dir>/scripts/image_client.py result <absolute-result-file-path>
```

Use the `result_file` printed for this request; if it was lost, locate the matching
`result-*.json` by `started_at`, command, and output directory. Do not assume the
latest receipt belongs to this request when multiple jobs run. A `completed`
receipt has saved images to display. A `failed` or `cancelled` receipt has an
error to report. A `processing`, `pending`, or `queued` receipt with a `task_id`
can be resumed by querying that same task. `running` means no final local result is recorded, not proof the
server failed or that resubmission is safe. Resume an existing command, or query
the same async task ID when available. If neither is possible, explain that the
outcome is unknown and stop without another generation. A plugin cannot force a
final chat message while the host's model connection is disconnected.

### Network Diagnosis

Run this internally after a network error, or when the user asks to check YiAI
connectivity:

```text
python <skill-dir>/scripts/image_client.py diagnose
```

This sends one unauthenticated GET to the configured `/v1/models` endpoint with
a 15-second timeout. It sends no API key or image prompt and cannot submit a
generation. `HTTP 401` shows that an HTTP response was received; it does not
validate credentials, the model, quota, or the generation endpoint. The report
includes proxy/certificate configuration flags without their secret values.

Use the actual `error_code`, `exception_type`, `os_errno`, and `winerror` from the
failure receipt or diagnostic instead of describing every network error as a
timeout. An immediate failure is not evidence that generation hit its timeout.

- `dns_resolution_failed`: check the API or proxy hostname and DNS.
- `tls_certificate_verification_failed`: check system time and trusted CA/proxy
  certificates. Keep TLS verification enabled.
- `connection_refused`: check the Base URL and whether the local proxy is running.
- `network_access_denied`: check OS/firewall and host execution permissions. If
  the host supports an approved network-enabled execution mode, follow that
  permission flow for a diagnostic. Respect denials and never bypass sandbox
  restrictions or disable firewall/security settings automatically.
- `network_timeout`, `connection_reset`, or `tls_error`: inspect the network,
  proxy and server connection; a POST outcome can still be unknown.

`submission_status: not_sent` identifies failures known to happen before the
image POST, such as DNS or initial certificate verification. `unknown` cannot
establish whether the server accepted it. `not_applicable` means the failing
request was a GET, such as a task query or image download. A download failure
must not trigger generation again. Do not automatically retry image POSTs.

Diagnostics on this computer cannot establish another computer's network state.
For a remote user, have their Codex run this same diagnostic through conversation
and share its sanitized JSON. Do not ask the user to execute scripts or expose
their API key or proxy credentials.

If the deployment has asynchronous image tasks enabled, use:

```text
python <skill-dir>/scripts/image_client.py generate --prompt "A city park" --async
python <skill-dir>/scripts/image_client.py task imgtask_example
```

Submission returns a task ID immediately. Query that same ID with the same key;
`task` saves images once the task completes. If it is still processing, wait a
few seconds before querying again. Async support requires server-side object
storage; if the endpoint is disabled, explain the error and choose synchronous
generation only after establishing that no task was accepted.

On 401/403, report the authentication or permission error and request a replacement
key or suitable permissions as appropriate. On quota/model errors, report them
without silently changing models. After a timeout or ambiguous POST failure,
report that the server may have accepted the request; do not automatically retry
or fall back to another generation endpoint because that can create another
billable request. A failed download after successful generation also must not
trigger a new generation.
