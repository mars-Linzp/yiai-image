# Saving Credentials From Conversation

This procedure is for Codex, not for the user to execute. Reuse the key supplied
for the current setup request and preserve URL/model preferences not changed by
the user. Saving credentials does not require a network or generation request.

## Check Write Access First

Run `python <client> check-config`. It reports the actual `config_path` and
`configuration_writable`, using a disposable empty file in that directory. It
does not read or change the saved key. This matters on Windows installations
where the Python process sees a redirected user configuration directory.

If it fails, inspect `error_code`, `os_errno`, and `winerror`. A user configuration
path outside the workspace is not itself proof that writing is prohibited. Use
the host's supported approval or permission mechanism for this specific path
when permitted. Respect actual denials; do not bypass sandbox controls, change
directory ACLs, move credentials into the project, or invent another store.
Do not claim an on-screen access label proves that a subprocess can write.

## Use A Non-Interactive Input Pipe

Run `python <client> configure --key-stdin`, providing the user's key and a newline
as process stdin. Supply input when starting the process in the same tool call.
Use a tool with a structured process-input argument or a trusted runtime's
subprocess API with stdin piped, shell execution disabled and no visible window.
For example, Node's `child_process.spawn` can use `stdio: ["pipe", "pipe", "pipe"]`,
`shell: false`, `windowsHide: true`, then `child.stdin.end(providedKey + "\n")`.
Keep the key out of shell text and process arguments, and never print it.

Do not use a PTY, `tty: true`, `getpass()`, or `write_stdin` to an interactive
console for this workflow. Windows console getpass can read a different channel
than the one receiving tool stdin. The client now rejects a terminal with
`stdin_pipe_required` instead of waiting indefinitely.

Bound the child process to 15 seconds. On timeout, stop that child and report the
input-channel error; do not repeat it through several interactive transports.
Check `status` before declaring that an interrupted save failed, since the file
may already have been replaced. Do not reveal a key while verifying it.

## If The Host Cannot Provide A Pipe

If an authorized structured file-writing API is available, it may atomically
save `api_key`, `base_url`, and `model` as JSON at the verified `config_path`,
preserving unspecified preferences and using user-only permissions where
supported. Follow the same host access restrictions as the CLI. Do not create
credential files in the plugin or workspace and do not embed the key in a shell
command. If no permitted input or writing channel exists, report that concrete
limitation without asking the user to run scripts or repeat their key.

After saving, run `status` and report the configured state and any environment
override. Do not claim the API key was checked against the image provider.
