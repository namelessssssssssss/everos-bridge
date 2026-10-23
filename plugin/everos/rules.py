"""Versioned Bridge instructions. Local source and install evidence, never user memory."""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
import re

BEGIN = "<!-- everos-bridge:begin -->"
END = "<!-- everos-bridge:end -->"
PATTERN = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)


def bundle(plugin: Path) -> dict:
    return {"rules": (plugin / "rules/everos-rules.md").read_text().strip(),
            "skill": (plugin / "skills/everos-memory/SKILL.md").read_text().strip()}


def revision(documents: dict) -> str:
    return hashlib.sha256(json.dumps(documents, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def transition(documents: dict, previous: dict) -> dict:
    """Keep the last real transition on no-op reinstalls, until another rule revision."""
    current = revision(documents)
    old = previous.get("documents", {})
    if not isinstance(old, dict) or any(not isinstance(v, str) for v in old.values()):
        old = {}
    if (previous.get("revision") == current and old == documents
            and isinstance(previous.get("changes"), str) and "previous_revision" in previous):
        return previous
    changes = []
    for name, text in documents.items():
        changes.extend(difflib.unified_diff(old.get(name, "").splitlines(), text.splitlines(),
                                          fromfile="previous/" + name, tofile="current/" + name, lineterm=""))
    return {"schema_version": 1, "revision": current,
            "previous_revision": previous.get("revision") if previous.get("revision") == revision(old) else None,
            "baseline": "previous_install" if old and previous.get("revision") == revision(old) else previous.get("baseline", "unavailable_review_all"),
            "documents": documents, "changes": "\n".join(changes)}


def managed_text(old: str, documents: dict, prefix: str = "") -> str:
    """Replace only our block; reject damaged/duplicate boundaries instead of guessing."""
    matches = list(PATTERN.finditer(old))
    if old.count(BEGIN) != len(matches) or old.count(END) != len(matches) or len(matches) > 1:
        raise ValueError("EverOS managed rule boundaries are damaged or duplicated; preserve file and repair first")
    block = BEGIN + "\n<!-- rules-revision: " + revision(documents) + " -->\n" + documents["rules"] + "\n" + END
    if matches:
        return PATTERN.sub(lambda _: block, old)
    if prefix and old.strip():
        raise ValueError("EverOS dedicated rule filename already contains unowned content; preserve file and resolve collision")
    lead = prefix + old
    separator = ("\n" if lead.endswith("\n") else "\n\n") if lead else ""
    return lead + separator + block + "\n"


def notice(plugin: Path) -> str:
    return ("Bridge规则版本=" + revision(bundle(plugin))[:12] + "。首次接入或规则版本变化时，"
            "调用memory_rules(section=changes)对照增量，再读rules及所需skill；"
            "按宿主原生指令入口同步规则并回读。文件安装成功不等于Agent已阅读或活动会话已重载。")


def read(plugin: Path, state: Path, section: str = "rules", offset: int = 0, limit: int = 6000) -> str:
    """Read-only even before credentials or installer setup; old caches describe their own rules."""
    installed = load(state / "current.json")
    current = transition(bundle(plugin), installed)
    text = current["changes"] if section == "changes" else current["documents"][section]
    if offset > len(text):
        raise ValueError("rule offset exceeds text length")
    end = min(len(text), offset + limit)
    report = load(state / "targets.json")
    return json.dumps({"revision": current["revision"], "previous_revision": current["previous_revision"],
                       "baseline": current.get("baseline"),
                       "installation_revision": installed.get("revision"),
                       "source_matches_install": current["revision"] == installed["revision"] if installed.get("revision") else None,
                       "guidance": "源码与安装版本不一致时先确认版本并重连，勿用旧缓存回滚原生规则。",
                       "section": section, "text": text[offset:end], "offset": offset,
                       "total_chars": len(text), "next_offset": end if end < len(text) else None,
                       "native_targets": report.get("targets", []) if report.get("revision") == current["revision"] else [],
                       "native_scope": report.get("scope") if report.get("revision") == current["revision"] else None,
                       "evidence": "source_rules_and_file_sync_only_not_agent_acknowledgment"}, ensure_ascii=False)
