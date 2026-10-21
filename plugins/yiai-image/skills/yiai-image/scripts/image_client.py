#!/usr/bin/env python3
"""YiAI-image: a dependency-free client for the AigcRouter Images API."""

import argparse
import base64
import binascii
import getpass
import http.client
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


DEFAULT_BASE_URL = "https://api.aigcrouter.org"
DEFAULT_MODEL = "gpt-image-2.5-sunburst"
MAX_RESPONSE = 128 * 1024 * 1024
MAX_IMAGE = 32 * 1024 * 1024
RESOLUTION_EDGES = {"1k": 1024, "2k": 2048, "4k": 3840}
MAX_OUTPUT_PIXELS = 3840 * 2160
MAX_ASPECT_RATIO = 3


class ClientError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def config_path():
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    # Preserve configured credentials across skill renames.
    return root / "aigcrouter-image" / "config.json"


def read_config():
    path = config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ClientError("Cannot read local configuration; run configure again.") from None
    if not isinstance(value, dict):
        raise ClientError("Invalid local configuration; run configure again.")
    return value


def normalize_base_url(value):
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)) or not parsed.netloc:
        raise ClientError("Base URL must use HTTPS (HTTP is allowed for localhost testing).")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClientError("Base URL must not contain credentials, a query, or a fragment.")
    return value if value.endswith("/v1") else value + "/v1"


def settings(args):
    saved = read_config()
    key = os.environ.get("AIGCROUTER_API_KEY") or saved.get("api_key", "")
    base = args.base_url or os.environ.get("AIGCROUTER_BASE_URL") or saved.get("base_url") or DEFAULT_BASE_URL
    model = args.model or os.environ.get("AIGCROUTER_IMAGE_MODEL") or saved.get("model") or DEFAULT_MODEL
    return {"api_key": str(key).strip(), "base_url": normalize_base_url(base), "model": model}


def configure(args):
    saved = read_config()
    if args.keep_key:
        key = str(saved.get("api_key", "")).strip()
        if not key:
            raise ClientError("No saved API key. Ask the user for their key in the conversation and save it with --key-stdin.")
    else:
        if not args.key_stdin and not sys.stdin.isatty():
            raise ClientError("Run configure in an interactive terminal for hidden key input, or use --key-stdin.")
        if args.key_stdin and not sys.stdin.isatty():
            key = sys.stdin.readline().strip()
        else:
            key = getpass.getpass("YiAI-image user API key: ").strip()
    if not key or any(char.isspace() for char in key):
        raise ClientError("A non-empty API key without whitespace is required.")
    base = args.base_url if args.base_url is not None else saved.get("base_url") or DEFAULT_BASE_URL
    normalize_base_url(base)
    model = (args.model if args.model is not None else saved.get("model") or DEFAULT_MODEL).strip()
    if not model:
        raise ClientError("The image model must not be empty.")
    value = {"api_key": key, "base_url": base, "model": model}
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o777 if os.name == "nt" else 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.write("\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"configured": True, "config_path": str(path.resolve()), "base_url": base, "model": value["model"]}


def redact(message, key):
    message = str(message)
    if key:
        message = message.replace(key, "[REDACTED]")
    return message[:1000]


def request(url, key="", body=None, content_type="application/json", timeout=600, limit=MAX_RESPONSE):
    headers = {"Accept": "application/json" if key else "image/*", "User-Agent": "aigcrouter-image-skill/1"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if body is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            content = response.read(limit + 1)
            if len(content) > limit:
                raise ClientError("Response exceeds the download limit; do not resubmit generation.")
            return content, response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        # Do not emit raw HTTP bodies: providers can include request headers in them.
        detail = ""
        try:
            error = json.loads(exc.read(16384)).get("error", {})
            detail = error.get("message", "") if isinstance(error, dict) else str(error)
        except (ValueError, AttributeError):
            pass
        raise ClientError(redact("HTTP {}: {}".format(exc.code, detail or exc.reason), key)) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException):
        raise ClientError("Network request failed or timed out. A submitted generation may still be running; do not automatically resubmit.") from None


def json_request(url, key, payload=None, timeout=600, content_type="application/json"):
    body = json.dumps(payload).encode("utf-8") if isinstance(payload, dict) else payload
    raw, _ = request(url, key, body, content_type, timeout)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ClientError("Server returned invalid JSON; do not resubmit generation.") from None
    if not isinstance(value, dict):
        raise ClientError("Server returned an unexpected response; do not resubmit generation.")
    if value.get("error"):
        error = value["error"]
        detail = error.get("message", "Image request failed") if isinstance(error, dict) else str(error)
        raise ClientError(redact(detail, key))
    return value


def image_suffix(content, content_type=""):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content[:6] in {b"GIF87a", b"GIF89a"}:
        return ".gif"
    raise ClientError("The returned content is not a supported image; do not resubmit generation.")


def image_bytes(item, timeout):
    try:
        if item.get("b64_json"):
            content = base64.b64decode(item["b64_json"], validate=True)
        else:
            url = item.get("url", "")
            if url.startswith("data:image/") and ";base64," in url:
                content = base64.b64decode(url.split(",", 1)[1], validate=True)
            else:
                parsed = urllib.parse.urlsplit(url)
                local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
                    raise ClientError("The API did not return a valid HTTPS image URL.")
                if parsed.username or parsed.password:
                    raise ClientError("Image URLs must not contain credentials.")
                # Image hosts never receive the API credential, even on the same host.
                content, _ = request(url, timeout=timeout, limit=MAX_IMAGE)
    except (ValueError, binascii.Error, TypeError):
        raise ClientError("Invalid image data; do not resubmit generation.") from None
    if not content or len(content) > MAX_IMAGE:
        raise ClientError("Image is empty or exceeds 32 MiB; do not resubmit generation.")
    return content


def save_images(value, output_dir, timeout):
    if not isinstance(value, dict):
        raise ClientError("Unexpected image result; do not resubmit generation automatically.")
    data = value.get("data")
    if not isinstance(data, list) or not data:
        raise ClientError("No images in the response; do not resubmit generation automatically.")
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for item in data:
        if not isinstance(item, dict):
            raise ClientError("Invalid image entry in the response.")
        try:
            content = image_bytes(item, timeout)
        except ClientError as exc:
            raise ClientError(str(exc) + " Do not regenerate. Already saved files: " + json.dumps(paths)) from None
        path = destination / ("image-" + uuid.uuid4().hex + image_suffix(content))
        with path.open("xb") as stream:
            stream.write(content)
        paths.append(str(path))
    return paths


def multipart(payload, images, mask):
    boundary = "aigcrouter-" + uuid.uuid4().hex
    parts = []
    for name, value in payload.items():
        parts.append(("--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(boundary, name, value)).encode("utf-8"))
    files = [("image[]", path) for path in images]
    if mask:
        files.append(("mask", mask))
    for index, (field, filename) in enumerate(files):
        path = Path(filename)
        if path.stat().st_size > MAX_IMAGE:
            raise ClientError("Input images must not exceed 32 MiB each.")
        content = path.read_bytes()
        suffix = image_suffix(content)
        mime = mimetypes.guess_type("image" + suffix)[0] or "application/octet-stream"
        header = "--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"input-{}{}\"\r\nContent-Type: {}\r\n\r\n".format(boundary, field, index, suffix, mime)
        parts.extend([header.encode("ascii"), content, b"\r\n"])
    parts.append(("--" + boundary + "--\r\n").encode("ascii"))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def image_size(value):
    text = value.strip().lower()
    if text == "auto":
        return text
    match = re.fullmatch(r"([1-9][0-9]{0,4})\s*[x*]\s*([1-9][0-9]{0,4})", text)
    if not match:
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT, for example 1536x1024, or auto")
    return "{}x{}".format(int(match.group(1)), int(match.group(2)))


def aspect_ratio(value):
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*[:/]\s*([0-9]+(?:\.[0-9]+)?)\s*", value)
    if not match or float(match.group(1)) <= 0 or float(match.group(2)) <= 0:
        raise argparse.ArgumentTypeError("use WIDTH:HEIGHT, for example 16:9")
    width, height = float(match.group(1)), float(match.group(2))
    if max(width, height) / min(width, height) > MAX_ASPECT_RATIO:
        raise argparse.ArgumentTypeError("aspect ratio must not exceed 3:1")
    return width, height


def size_from_aspect_ratio(ratio, resolution):
    width, height = ratio
    scale = RESOLUTION_EDGES[resolution] / max(width, height)
    pixels = width * scale * height * scale
    if pixels > MAX_OUTPUT_PIXELS:
        scale *= (MAX_OUTPUT_PIXELS / pixels) ** 0.5
    output_width = max(16, int(width * scale / 16) * 16)
    output_height = max(16, int(height * scale / 16) * 16)
    return "{}x{}".format(output_width, output_height)


def parser():
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--base-url")
    shared.add_argument("--model")
    shared.add_argument("--timeout", type=positive_int, default=600)
    shared.add_argument("--output-dir", default="output/aigcrouter-images")
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status", parents=[shared])
    setup = commands.add_parser("configure", parents=[shared])
    key_mode = setup.add_mutually_exclusive_group()
    key_mode.add_argument("--key-stdin", action="store_true", help="Read the API key from stdin instead of a hidden prompt")
    key_mode.add_argument("--keep-key", action="store_true", help="Keep the saved API key and update preferences without reading stdin")
    for name in ("generate", "edit"):
        command = commands.add_parser(name, parents=[shared])
        prompt = command.add_mutually_exclusive_group(required=True)
        prompt.add_argument("--prompt")
        prompt.add_argument("--prompt-file")
        dimensions = command.add_mutually_exclusive_group()
        dimensions.add_argument("--size", type=image_size, help="Exact output size such as 1536x1024, or auto")
        dimensions.add_argument("--aspect-ratio", type=aspect_ratio, help="Output aspect ratio such as 16:9, 9:16, or 4:3")
        command.add_argument("--resolution", type=str.lower, choices=sorted(RESOLUTION_EDGES), help="Size tier used with --aspect-ratio; defaults to 2k")
        command.add_argument("--quality")
        command.add_argument("--n", type=positive_int, default=1)
        command.add_argument("--async", action="store_true", dest="asynchronous")
        if name == "edit":
            command.add_argument("--image", action="append", required=True)
            command.add_argument("--mask")
    task = commands.add_parser("task", parents=[shared])
    task.add_argument("task_id")
    return root


def run(args):
    if args.command == "configure":
        return configure(args)
    config = settings(args)
    key, base, model = config["api_key"], config["base_url"], config["model"]
    if args.command == "status":
        saved = read_config()
        sources = {}
        for name, option, environment in (
            ("api_key", None, "AIGCROUTER_API_KEY"),
            ("base_url", args.base_url, "AIGCROUTER_BASE_URL"),
            ("model", args.model, "AIGCROUTER_IMAGE_MODEL"),
        ):
            sources[name] = "command_line" if option else "environment" if os.environ.get(environment) else "saved" if saved.get(name) else "default"
        if not key:
            sources["api_key"] = "missing"
        return {"api_key_configured": bool(key), "saved_api_key_configured": bool(str(saved.get("api_key", "")).strip()), "base_url": base, "model": model, "configuration_sources": sources, "config_path": str(config_path().resolve())}
    if not key:
        raise ClientError("API key required. Ask the user for their AigcRouter user API key in the conversation, save it internally, and resume the pending image request.")
    if args.command == "task":
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.task_id):
            raise ClientError("Invalid task ID.")
        task = json_request(base + "/images/tasks/" + args.task_id, key, timeout=args.timeout)
        result = {"task_id": args.task_id, "status": task.get("status")}
        if task.get("status") == "failed":
            raise ClientError(redact("Image task failed: " + str(task.get("error", "unknown error")), key))
        if task.get("status") == "completed":
            payload = task.get("result") or {"data": [{"url": task.get("image_url", "")}]}
            result["files"] = save_images(payload, args.output_dir, args.timeout)
        return result
    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    if not prompt.strip():
        raise ClientError("The image prompt must not be empty.")
    if args.resolution and not args.aspect_ratio:
        raise ClientError("--resolution requires --aspect-ratio; use --size for an exact size.")
    size = args.size or (size_from_aspect_ratio(args.aspect_ratio, args.resolution or "2k") if args.aspect_ratio else None)
    payload = {"model": model, "prompt": prompt, "n": args.n}
    if size:
        payload["size"] = size
    if args.quality:
        payload["quality"] = args.quality
    content_type = "application/json"
    endpoint = "/images/generations" if args.command == "generate" else "/images/edits"
    if args.command == "edit":
        payload, content_type = multipart(payload, args.image, args.mask)
    if args.asynchronous:
        endpoint += "/async"
    value = json_request(base + endpoint, key, payload, args.timeout, content_type)
    if args.asynchronous:
        task_id = value.get("task_id") or value.get("id")
        if not task_id:
            raise ClientError("No task ID returned; do not resubmit generation automatically.")
        result = {"task_id": task_id, "status": value.get("status", "processing"), "model": model}
    else:
        result = {"status": "completed", "model": model, "files": save_images(value, args.output_dir, args.timeout)}
    if size:
        result["requested_size"] = size
    return result


def main():
    args = parser().parse_args()
    try:
        value = run(args)
        print(json.dumps(value, ensure_ascii=True))
        return 0
    except (ClientError, OSError, ValueError) as exc:
        # Avoid printing raw filesystem errors, which can contain sensitive URLs.
        message = str(exc) if isinstance(exc, ClientError) else "Local configuration or file operation failed. Check paths and permissions."
        print(json.dumps({"error": message}, ensure_ascii=True), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(json.dumps({"error": "Cancelled. A submitted generation may still be running; do not automatically resubmit."}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
