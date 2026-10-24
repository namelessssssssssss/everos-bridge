"""Windows-owned config helpers, independent of the Unix installer.

Initially aligned with install_config.py; changes here never alter Unix code.
"""
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
from datetime import datetime

def backup(path):
    """Create a unique backup before any write, including host CLI writes."""
    path = Path(path)
    if not path.is_file():
        return
    suffix = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex
    destination = path.with_name(path.name + ".bak-everos-" + suffix)
    descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output, path.open("rb") as source:
        shutil.copyfileobj(source, output)
    original = path.stat()
    os.utime(destination, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.chmod(destination, original.st_mode & 0o600)


def write_text(path, text, mode=None):
    """Keep permissions and replace atomically; never truncate an existing file."""
    path = Path(path)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text and (mode is None or os.name == "nt" or path.stat().st_mode & 0o777 == mode):
        return
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    permissions = mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o600)
    fd, temporary = tempfile.mkstemp(prefix=".everos-install-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
        os.chmod(temporary, permissions)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def env_values(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def active_toml_lines(text):
    result, multiline = [], None
    for index, line in enumerate(text.splitlines(keepends=True)):
        if multiline is None:
            result.append((index, line))
        # Track delimiters outside comments and basic strings.
        position, quote = 0, None
        while position < len(line):
            if multiline:
                end = line.find(multiline, position)
                if end < 0:
                    break
                position, multiline = end + 3, None
            elif quote:
                if line[position] == "\\" and quote == '"':
                    position += 2
                elif line[position] == quote:
                    quote, position = None, position + 1
                else:
                    position += 1
            elif line[position] == "#":
                break
            elif line[position:position + 3] in ('"""', "'''"):
                multiline, position = line[position:position + 3], position + 3
            elif line[position] in "\"'":
                quote, position = line[position], position + 1
            else:
                position += 1
    return result


def table_headers(text):
    result = []
    for index, line in active_toml_lines(text):
        match = re.match(r"^\s*(\[\[?.*?\]\]?)\s*(?:#.*)?$", line.rstrip())
        if match:
            header = match.group(1)
            size = 2 if header.startswith("[[") else 1
            header = "[" * size + header[size:-size].strip() + "]" * size
            result.append((index, header))
    return result


def enable_codex_hooks(text):
    lines = text.splitlines(keepends=True)
    headers = table_headers(text)
    active = {index for index, _ in active_toml_lines(text)}
    features = [index for index, header in headers if header in ("[features]", '["features"]', "['features']")]
    if len(features) > 1:
        raise ValueError("Codex config has duplicate [features] tables; repair it before installing")
    if features:
        start = features[0]
        end = next((index for index, _ in headers if index > start), len(lines))
        key = re.compile(r"^\s*(?:hooks|\"hooks\"|'hooks')\s*=")
        body = [line for index, line in enumerate(lines[start + 1:end], start + 1)
                if index not in active or not key.match(line)]
        if not lines[start].endswith("\n"):
            lines[start] += "\n"
        lines[start + 1:end] = ["hooks = true\n"] + body
        return "".join(lines)
    # Root-level dotted assignments cannot coexist with a newly added table.
    root_end = headers[0][0] if headers else len(lines)
    feature_key = r'(?:features|"features"|\'features\')'
    hook_key = r'(?:hooks|"hooks"|\'hooks\')'
    dotted = re.compile(r"^\s*" + feature_key + r"\s*\.\s*" + hook_key + r"\s*=")
    matches = [index for index in range(root_end) if index in active and dotted.match(lines[index])]
    if matches:
        first = matches[0]
        lines = [line for index, line in enumerate(lines) if index not in matches[1:]]
        lines[first] = "features.hooks = true\n"
        return "".join(lines)
    if any(index in active and re.match(r"^\s*" + feature_key + r"(?:\s*=|\s*\.)", line)
           for index, line in enumerate(lines[:root_end])):
        raise ValueError("Use a [features] table in Codex config before enabling hooks")
    return text.rstrip() + ("\n\n" if text.strip() else "") + "[features]\nhooks = true\n"


def _codex_everos_table(text):
    lines = text.splitlines(keepends=True)
    headers = table_headers(text)
    table = re.compile(r"^\[(?:mcp_servers|\"mcp_servers\"|'mcp_servers')\s*\.\s*(?:everos|\"everos\"|'everos')\]$")
    matches = [index for index, header in headers if table.match(header)]
    if len(matches) > 1:
        raise ValueError("Codex config has duplicate [mcp_servers.everos] tables")
    if not matches:
        return lines, None, None, []
    start = matches[0]
    end = next((index for index, _ in headers if index > start), len(lines))
    active = {index for index, _ in active_toml_lines(text)}
    key = re.compile(r"^\s*(?:tool_timeout_sec|\"tool_timeout_sec\"|'tool_timeout_sec')\s*=")
    keys = [index for index in range(start + 1, end) if index in active and key.match(lines[index])]
    if len(keys) > 1:
        raise ValueError("Codex everos tool_timeout_sec is duplicated")
    return lines, start, end, keys


def _positive_timeout(value, label):
    if isinstance(value, bool):
        raise ValueError(label + " must be a positive finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(label + " must be a positive finite number") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(label + " must be a positive finite number")
    return number


def codex_tool_timeout(text):
    lines, _, _, keys = _codex_everos_table(text)
    if not keys:
        return 0
    value = lines[keys[0]].split("=", 1)[1].split("#", 1)[0].strip().replace("_", "")
    return _positive_timeout(value, "Codex everos tool_timeout_sec")


def set_codex_tool_timeout(text, minimum):
    """Raise only EverOS's tool timeout, preserving larger user settings."""
    lines, start, _, keys = _codex_everos_table(text)
    if codex_tool_timeout(text) >= minimum:
        return text
    entry = "tool_timeout_sec = {:g}\n".format(minimum)
    if start is None:
        return text.rstrip() + ("\n\n" if text.strip() else "") + "[mcp_servers.everos]\n" + entry
    if keys:
        lines[keys[0]] = entry
    else:
        if not lines[start].endswith("\n"):
            lines[start] += "\n"
        lines.insert(start + 1, entry)
    return "".join(lines)
