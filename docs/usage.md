# Usage guide

English · [简体中文](usage.zh-CN.md)

[Back to README](../README.md)

This guide covers routine use of EverOS Bridge. [PROTOCOL.md](../PROTOCOL.md) remains the authoritative read/write policy; detailed host and service documentation is currently in Chinese.

Features marked **0.10.0+** require that Bridge version. Earlier installations retain their previous automatic extraction behavior.

## Agent tools

You can ask your agent to save a decision, find earlier work, or correct a profile item in ordinary language. These are the shared tools available to connected agents:

| Tool | Purpose |
| --- | --- |
| `memory_profile` | Read the user's profile |
| `memory_search` / `memory_open` | Find memories, then expand their saved snapshots |
| `memory_recent` | Review recent memory entries |
| `memory_context` | Keep local task clues and retrieval context for a session |
| `memory_remember` | Record facts, decisions, preferences, and corrections |
| `memory_record_practice` | Record a troubleshooting trajectory with at least three real steps |
| `memory_flush` (0.10.0+) | Explicitly request extraction for one saved session when the task needs it |
| `memory_fix_profile` | Correct profile items using their IDs and a reason |
| `memory_rules` | Read the installed rules and changes |

New writes default to v2. Practice records also write to v1 only when v2 is configured and `EVEROS_READ_V1=1`; disabling legacy access also disables that dual-write. See [write results](#understand-write-results) for confirmation and extraction behavior.

## Configuration

The default data directory is `~/.everos`; set `EVEROS_HOME` to use another location. Credentials are read from `config.env` in that directory, with nonempty environment variables taking precedence.

```dotenv
EVEROS_API_BASE=https://api.evermind.ai
EVEROS_USER_ID=your-memory-space
EVEROS_API_KEY=your-api-key
EVEROS_API_VERSION=v2
```

Replace the placeholders locally. Use unquoted values in `config.env` for compatibility with older Bridge versions. The Unix installer creates this file with mode `0600`; native Windows uses ACL protection. Do not commit the file or paste its contents into a conversation or issue.

| Option | Default | Purpose |
| --- | --- | --- |
| `EVEROS_API_VERSION` | `v2` | API version; `v1` is retained for compatibility |
| `EVEROS_READ_V1` | Disabled | Set to `1` to merge legacy v1 reads and enable practice dual-write in v2 mode |
| `EVEROS_AGENT_ID` | `<user_id>_agent` | Assistant identity shared across hosts |
| `EVEROS_AGENTIC_TIMEOUT` | `180` | Total agentic retrieval budget, in seconds |
| `EVEROS_CASE_WAIT` | `30` | Maximum practice-case readback wait, in seconds; from 0.10.0, polling runs only if the v2 add reports `extracted`; `0` disables it |
| `EVEROS_INJECT` | Enabled | Set to `0` to disable automatic injection |
| `EVEROS_WRITE` | Enabled | Set to `0` to disable all Bridge writes |

Add the unquoted line `EVEROS_WRITE=0` to disable capture, MCP write tools, profile edits, flush, and local-memory synchronization. `EVEROS_INJECT=0` does not disable manual retrieval.

## Terminal search

The full installer creates `everos` under `~/.local/bin`. For command entry points only, run `./install.sh --cli-only` (or preview with `--cli-only --dry-run`). This does not configure hosts, change credentials, or call the API. Existing conflicting command files are preserved and reported.

```bash
everos search "What did we decide about the cache?"
everos search "How did we diagnose the timeout?" --kind practices --top-k 10
everos search "Earlier migration constraints" --method agentic --timeout 240
everos search "That earlier timeout" --context "Now checking the retry policy"
```

| Argument | Accepted values / behavior |
| --- | --- |
| Query | 1–500 characters; quote queries containing spaces |
| `--kind` | `all` (default), `episodes`, or `practices` |
| `--top-k` | `5` by default; accepts `1–100` or `-1` for server selection |
| `--method` | `hybrid` (CLI default) or `agentic` |
| `--timeout` | Positive, finite seconds; overrides the configured budget |
| `--context` | Additional task clues, up to 500 characters |
| `--session-id` | Optional session identifier for local retrieval context |

`top-k` applies per memory type, so combined results may exceed it. Expand a returned ID with `everos open em_... --offset 0 --limit 4000`, replacing `em_...` with the actual ID, then follow the returned continuation offset. These are locally saved snapshots of retrieved memories, not full original transcripts or guarantees of current truth. On another device, search again to obtain a local snapshot ID.

Search and open do not write remote memories. Search can save local result snapshots; it does not update the hook's session deduplication ledger.

| Exit code | Meaning |
| --- | --- |
| `0` | Completed, including no matches or partial success; read any partial-result notice |
| `1` | Configuration or operation failure, including all retrieval sources failing |
| `2` | Invalid arguments |
| `130` | Interrupted by the user |

`everos --help` and `everos search --help` require no credentials or network access.

## On-demand extraction (0.10.0+)

For v2, saving messages and extracting a memory are separate steps. Bridge confirms each add, then leaves extraction to EverOS unless the agent decides the latest saved content is needed now. Useful boundaries include a handoff, a completed workflow, a topic change, or a search that depends on the latest saved messages. [EverOS Cloud processes an idle session after two hours without new messages](https://docs.evermind.ai/cookbook/quickstart); that behavior should not be assumed for every self-hosted or legacy server.

Use `memory_flush` from an agent or `everos flush` from a terminal. Supply a nonempty reason and **exactly one** target:

| MCP argument / CLI flag | Target |
| --- | --- |
| `session_id` / `--session-id` | The EverOS session ID returned by a structured write |
| `conversation_id` / `--conversation-id` | The host's native session ID shown in the prompt hook; requires an existing local capture ledger |

The native conversation ID is not the EverOS session ID. A conversation without captured writes has nothing for this command to extract. Flush does not capture or upload an unsent final response. For an immediate handoff, first save the conclusion with `memory_remember`, then flush its returned target if needed.

Example MCP arguments, using the actual ID from the write receipt:

```json
{
  "session_id": "ID_FROM_WRITE_RECEIPT",
  "reason": "The handoff needs the latest saved decision",
  "api_version": "v2"
}
```

Equivalent terminal examples:

```bash
everos flush --session-id ID_FROM_WRITE_RECEIPT \
  --reason "The handoff needs the latest saved decision" --api-version v2

everos flush --conversation-id NATIVE_SESSION_ID \
  --reason "The completed workflow is ready for handoff"
```

`--api-version v1|v2` is optional: direct session IDs default to the configured API version; conversation IDs use their capture ledger. After switching versions, pass the old version explicitly to flush a preserved old target. `--kind episodes|practices` defaults to `episodes`; use `practices` for the v1 agent-memory endpoint. Conversation targets support only `episodes`. MCP uses `api_version` and `kind` for the same options.

In 0.10.0+, v2 remember/practice writes, conversation lifecycle events, MiMo capture, local-memory synchronization, and normal or forced sweeps do not automatically flush. SessionEnd and PreCompact still capture remaining messages. Legacy v1 automatic flush behavior is preserved, including its old-buffer confirmation before a version switch. Sweeps still synchronize local memory and recover pending v1 flushes; they do not force v2 extraction or replay unsent transcripts.

An acknowledged add can advance capture progress. A failed flush retains its pending confirmation without resending that add. Local pending state records the absence of a confirmed local flush; it does not prove that Cloud still has unprocessed messages. A successful flush also does not guarantee immediate search visibility or a practice case. All explicit flushes obey `EVEROS_WRITE=0` and quota controls. After upgrading, reconnect MCP or open a new agent session to load the new tool and instructions.

## Retrieval timeouts

Hooks and MCP `memory_search` start with `agentic`; the standalone CLI defaults to `hybrid`. Failure, timeout, or no matches can trigger per-source hybrid fallback within the same deadline. Legacy v1 queries always use hybrid.

The default total agentic budget is 180 seconds. Precedence is per-call MCP `timeout` / CLI `--timeout`, then `EVEROS_AGENTIC_TIMEOUT` in the environment, then `config.env`, then the default.

The host's outer timeout must exceed this budget. After increasing the persistent budget, rerun the installer to update supported host limits. Claude Code, Codex plugin, and cloud static hook definitions default to 190 seconds; increasing their retrieval budget beyond 180 seconds also requires increasing the `UserPromptSubmit` timeout and reviewing updated hooks. See [the protocol](../PROTOCOL.md) for host-specific limits.

All-source technical failure is reported as an error, not “no memories.” Partial success retains usable results and reports missing sources.

## Host-specific setup

| Host | Notes |
| --- | --- |
| Codex | Plugin installation copies files. Rerun the installer after updates, then open a new session and review current hook definitions. |
| Kimi Code | The installer also checks `~/.kimi-code/bin/kimi` when `kimi` is absent from `PATH`. Profile injection occurs on the first prompt. |
| Kimi Work desktop | App upgrades may overwrite runtime configuration. Rerun `install.sh`, or `install.ps1 -KimiDesktop` on Windows, then open a new session. |
| TraeX | Requires 0.209.1 or later for lifecycle hooks. Use `./install.sh --only trae` to update only TraeX, then review hooks through `/hooks`. [Details](traex-integration.md). |
| MiMoCode / MiMo Desktop | Uses the Plugin API instead of `hooks.json`. The installer merges MCP, instructions, and plugin settings; changes take effect in new sessions. |
| Claude Desktop | Quit the app before installation, or configure MCP through its settings. A running app can overwrite external configuration changes. |
| Native Windows | `install.ps1` covers Codex and, with `-KimiDesktop`, Kimi Work. Other Windows hosts are not covered. [Details](windows-integration.md). |
| Doubao Work cloud | No native MCP registration or hooks. A user skill calls the shared MCP server through `everos-mcp` and invokes `everos-doubao-ingest`; execution depends on the agent. [Details](doubao-integration.md). |

Linked host guides are in Chinese. New integrations should follow the [onboarding checklist](agent-onboarding.md) and verify tools, instructions, injection, and capture separately.

### Claude Code on the web

The cloud environment needs its own setup; a local plugin installation does not carry over.

1. Allow access to `api.evermind.ai`. Where supported, configure a network secret with an `Authorization: Bearer <key>` header so the key stays outside the virtual machine. Otherwise, supply `EVEROS_API_KEY` through the environment's secret mechanism.
2. Set `EVEROS_USER_ID`. Add `EVEROS_READ_V1=1` only if legacy reads are needed. The default repository is public and needs no GitHub token. To select a fork, set `EVEROS_BRIDGE_REPO=owner/repo`; for a private fork, supply `GH_TOKEN` with read access to that repository.
3. Use [`cloud/setup.sh`](../cloud/setup.sh) as the setup script. It installs the Bridge under `/opt/everos-bridge`.
4. Merge [`cloud/settings.snippet.json`](../cloud/settings.snippet.json) into the personal repository's `.claude/settings.json`, and [`cloud/mcp.snippet.json`](../cloud/mcp.snippet.json) into `.mcp.json`. Preserve unrelated settings; keep personal wiring out of shared team repositories.
5. Start a new cloud session and verify recall and capture. The hook template gates execution on `CLAUDE_CODE_REMOTE` and the installed files.

## Updates

For macOS / Linux:

```bash
git -C ~/.everos-bridge pull --ff-only
~/.everos-bridge/install.sh
python3 ~/.everos-bridge/plugin/everos/hook.py --doctor
```

On Windows, update the checkout and rerun the matching `install.ps1` command. Reconnect MCP or start a new session to load updated tools; review updated hook definitions when prompted. Keep the checkout and old plugin versions available to sessions that still use them.

The installer supports custom checkout locations. Normal installation backs up configuration before changes. `--dry-run` previews setup without changing configuration or making API calls.

## Troubleshooting

### Check installation

```bash
python3 ~/.everos-bridge/plugin/everos/hook.py --doctor
```

Doctor performs connectivity checks. Inspect `~/.everos/state/hook.log` for operational errors, but keep private text and credentials out of reports. Verify new-session injection and a real write/readback separately from configuration checks.

On Bridge 0.9.3 and earlier, if installation succeeds but authentication fails, check for quoted values in `config.env`. Those versions retain surrounding quotes at runtime; use unquoted values for compatibility.

### Recover from a hard quota error

```bash
everos quota status
everos quota retry --resource mcu
# For retrieval quota instead:
everos quota retry --resource retrieval
```

`status` reads the local circuit breaker; it is not a cloud balance query. After replenishing quota, `retry` allows the next related request to probe recovery. The command itself makes no network request and does not claim quota has recovered. Without manual retry, one probe is allowed after the default 15-minute pause.

MCU and retrieval quotas are separate, so an MCU refusal does not disable reads. Ordinary rate-limit responses are handled separately from explicit hard quota errors.

Failed capture retains its source position rather than marking messages as sent. From **0.10.0**, a sweep flushes only pending legacy v1 sessions and synchronizes local memory; v2 extraction is left to the service or an [explicit targeted flush](#on-demand-extraction-0100). A sweep does not replay unsent transcripts. Recovery requires bounded replay from the retained cursor and a readback check.

### Understand write results

| Result | Meaning |
| --- | --- |
| `accumulated` | Messages were accepted into the buffer; extraction is pending |
| `extracted` | Extraction completed; search visibility and practice-case creation still need separate checks |
| `no_extraction` | A flush did not extract new content; this alone does not establish a failure or missing data |

Practice recording requires at least three real steps. Record actual failed attempts, unexpected findings, and user corrections; do not invent steps to meet the threshold. From **0.10.0**, the existing bounded case readback runs only when the v2 add returns `extracted`. An accepted but buffered add reports that extraction is pending and defers case polling; it does not promise that a case or searchable memory already exists. A missing case is not necessarily a failed write. In v2 mode, practice dual-write to v1 is enabled only by `EVEROS_READ_V1=1`.

For service-specific limitations and historical tests, see [EverOS notes](everos-notes.md). This guide describes current behavior; it does not change the Bridge's extraction policy.

## Development

Use a separate Git worktree for changes if agents load your checkout at runtime.

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
```

These tests use temporary configuration, mocked hosts, and simulated APIs. They do not require real credentials or write remote memories. Native Windows tests live in `windows/tests` and run in a separate [CI workflow](../.github/workflows/windows.yml).

For bug reports, include the Bridge commit/version, host and OS, reproduction steps, and sanitized errors. Keep API keys and private conversations out of reports. For host adapters, verify recall and capture separately in a real new session; offline tests alone cannot establish that the host loaded and trusted the integration.

Start with [agent instructions](../AGENTS.md) and the [onboarding checklist](agent-onboarding.md) (Chinese). Core code is in [`plugin/everos/`](../plugin/everos/), installation code in [`scripts/`](../scripts/) and [`windows/`](../windows/), and cloud templates in [`cloud/`](../cloud/). Design background is in [methodology](methodology.md); managed instruction updates are covered in [rule synchronization](rule-sync.md).
