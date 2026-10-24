"""Local installer source receipts; automatic recovery cannot choose its own source."""
import json
from pathlib import Path
import re
import subprocess

from config_helpers import write_text
from platform_runtime import windows_rules
import win_rules as rulebook


def git_state(repo):
    try:
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                       text=True, encoding="utf-8", stderr=subprocess.DEVNULL).strip()
        dirty = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"],
                                        text=True, encoding="utf-8", stderr=subprocess.DEVNULL).strip()
        return head, bool(dirty)
    except (OSError, subprocess.CalledProcessError):
        return None, True


def receipt_path(installer):
    return installer.everos / "state/install/windows-source.json"


def codex_home(installer):
    return Path(installer.environment.get("CODEX_HOME", str(installer.home / ".codex")))


def record(installer, runtime=None):
    path = receipt_path(installer)
    previous = rulebook.load(path)
    head, dirty = git_state(installer.repo)
    documents = {k: windows_rules(v) for k, v in rulebook.bundle(installer.repo / "plugin").items()}
    receipt = {"schema_version": 1, "source_root": str(installer.repo), "source_revision": head,
               "source_dirty": dirty, "codex_home": str(codex_home(installer).resolve()),
               "rules_revision": rulebook.revision(documents),
               "runtime": str(runtime) if runtime else previous.get("runtime")}
    if installer.dry:
        print("  [dry-run] 记录 Windows 已确认安装源：" + str(installer.repo))
    else:
        write_text(path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", 0o600)


def verify(installer):
    """Read only. Reject a stale/unconfirmed task before either repair mode."""
    import tomllib
    receipt = rulebook.load(receipt_path(installer))
    advice = " Run the intended source's install.ps1 explicitly, then update the logon task source."
    if receipt.get("schema_version") != 1 or not receipt.get("source_revision"):
        raise ValueError("Unconfirmed recovery source: installer source receipt is missing." + advice)
    try:
        same_source = Path(receipt["source_root"]).samefile(installer.repo)
        same_home = Path(receipt["codex_home"]).resolve() == codex_home(installer).resolve()
    except (KeyError, OSError, TypeError):
        same_source = same_home = False
    if not same_source or not same_home:
        raise ValueError("Stale recovery task: source or target differs from the confirmed installation." + advice)
    head, dirty = git_state(installer.repo)
    if not head or dirty or receipt.get("source_dirty"):
        raise ValueError("Unconfirmed recovery source: checkout must be clean and installer receipt confirmed." + advice)
    try:
        ancestor = subprocess.run(["git", "-C", str(installer.repo), "merge-base", "--is-ancestor",
                                  receipt["source_revision"], head], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        ancestor = False
    if not ancestor:
        raise ValueError("Stale recovery source: checkout is older than or diverges from the installed revision." + advice)
    root = codex_home(installer)
    path = root / "config.toml"
    config = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for value in config.get("mcp_servers", {}).get("everos", {}).get("args", []):
        if isinstance(value, str) and Path(value).name == "mcp_server.py" and Path(value).exists():
            recorded = receipt.get("runtime")
            try:
                same_runtime = bool(recorded) and Path(value).parents[2].samefile(recorded)
            except OSError:
                same_runtime = False
            if not same_runtime:
                raise ValueError("Unconfirmed installed runtime; preserve potentially newer wiring." + advice)
    override = root / "AGENTS.override.md"
    native = override if override.exists() and override.read_text(encoding="utf-8").strip() else root / "AGENTS.md"
    if native.exists():
        marker = re.search(r"<!-- rules-revision: ([a-f0-9]+) -->", native.read_text(encoding="utf-8"))
        if marker and marker[1] != receipt.get("rules_revision"):
            raise ValueError("Unconfirmed installed rules; preserve potentially newer native instructions." + advice)
