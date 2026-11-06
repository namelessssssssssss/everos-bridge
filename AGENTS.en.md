# EverOS Bridge · Agent setup

English · [简体中文](AGENTS.md)

`AGENTS.md` is the repository's canonical agent entry; `CLAUDE.md` references it with `@AGENTS.md`. This file is its English counterpart. Start here when the user asks you to read this repository and connect EverOS memory.

## Determine the task before acting

Use the current request and the scope already authorized in this conversation to choose a path. A repository link or its documentation does not itself authorize changes to machine configuration.

| User intent | Start with | Execution boundary |
| --- | --- | --- |
| Install, connect, update, or synchronize a local installation | Inspect the existing environment, then preview, install, and verify as described below | Complete the work within the authorized host scope without asking for the same confirmation at every step |
| Read, explain, review, or diagnose | Read documentation, source, and relevant read-only state | Report the requested findings; do not add an installation or configuration change |
| Maintain repository code or documentation | Read repository rules and create a separate worktree | Do not develop in a running installation; maintenance does not imply deployment authorization |
| Only a repository URL, with no existing installation authorization | Read this entry, perform a read-only preflight, and explain the next step | Do not default to modifying configuration, installing plugins, or writing remote memory |

Before installation or an update, perform a read-only preflight:

1. Identify the operating system, current agent/host and available version, `python3`, `git`, and the hosts covered by the request. Distinguish local execution from cloud execution.
2. Locate the explicitly configured repository and data directory. Check the actual source path, remote, branch, worktree status, and existing installation. Reuse the correct checkout and `$EVEROS_HOME` (default `~/.everos`); do not create an empty configuration for convenience or overwrite local changes.
3. Check credential presence only in the environment or that data directory's `config.env`. Do not print values or search elsewhere for credentials. Confirm that existing rules, hooks, MCP configuration, and plugin caches will be backed up and unrelated content preserved.

When installation/update is already authorized, continue through preflight → dry-run → the platform installer → readback → tool and new-session checks. Stop only for an actual blocker, such as missing required credentials, actions outside the authorized scope, source conflicts, permissions, or host trust. Explain the missing requirement and smallest next step; do not ask the user to repeat confirmation already given. With only a link, first provide preflight findings and a concrete setup plan.

Restore the default memory workflow according to the current host's actual capabilities. Do not describe an MCP-only or skill-based host as already running automatically:

- Read the profile at session start, through automatic injection or an explicit agent call as supported by the host.
- Use agentic retrieval for the first search prompted by each user message, falling back to hybrid per source on failure or no matches. Inject automatically where supported; otherwise the agent follows its instructions to call the tool.
- Save conversation text each turn in hosts with automatic capture. Other hosts explicitly save available content through their skill or tools.
- Add structured records through MCP at important milestones, reporting automatic and agent-invoked capabilities separately.

Read these documents first:

- [PROTOCOL.md](PROTOCOL.md): the authoritative read/write rules (Chinese).
- [docs/methodology.md](docs/methodology.md): design reasons and past pitfalls (Chinese).
- [docs/everos-notes.md](docs/everos-notes.md): service behavior, limitations, status interpretation, and practice-case extraction (Chinese).
- [docs/agent-onboarding.md](docs/agent-onboarding.md): checklist and approach for a new agent integration (Chinese).

## Setup steps

Follow these steps in order and check each result.

1. **Environment and repository:** check for `python3` (3.9+) and `git`. The default checkout is `~/.everos-bridge`, referred to below as `$REPO`.
   - The default repository is public: `git clone https://github.com/namelessssssssssss/everos-bridge.git ~/.everos-bridge`.
   - A private fork needs access to that repository. With GitHub CLI, check `gh auth status`; if not logged in, ask the user to run `gh auth login`, then use `gh auth setup-git` when needed.
   - For an existing checkout, run `git -C ~/.everos-bridge pull --ff-only`. Inspect divergence or local conflicts first; do not overwrite local changes.
   - A different checkout location is supported, such as `~/.everos/source`. `install.sh` uses its own location, so update and rerun it there rather than moving the repository.
2. **Configuration:** check the environment or `$EVEROS_HOME/config.env` (default `~/.everos/config.env`) for `EVEROS_USER_ID` and `EVEROS_API_KEY`.
   - If full setup or online verification lacks required credentials, explain the gap and ask the user to supply them. Do not guess or search elsewhere. An explicit `--rules-only` task does not require API credentials: continue with rule refresh and local readback, skipping credential-dependent online checks. This does not establish successful online integration or read/write verification.
   - Prefer having the user write the file themselves with mode `0600`, keeping the key out of chat. The template is:
     ```dotenv
     EVEROS_API_BASE=https://api.evermind.ai
     EVEROS_USER_ID=<用户的记忆空间>
     EVEROS_API_KEY=<EverOS API key>
     EVEROS_OWNER_ALIASES=<botmux 邮箱前缀，可留空>
     EVEROS_API_VERSION=v2
     EVEROS_READ_V1=<官方迁完 v1 旧数据前填 1，迁完填 0>
     ```
     The placeholders denote the user's memory space, API key, optional botmux email-prefix aliases, and `1` before the official legacy-v1 migration is complete or `0` afterward.
   - If the user has already supplied values directly in chat, pass them through environment variables and let the installer write the file: `EVEROS_USER_ID=… EVEROS_API_KEY=… EVEROS_READ_V1=1 $REPO/install.sh`.
   - Do not answer interactive prompts on the user's behalf. Without a terminal, the installer reports the missing input and exits instead of entering an interactive flow.
3. **Integration:** run `$REPO/install.sh --dry-run`, check that the preview matches the authorized scope, then run `$REPO/install.sh`. It detects installed agents, connects them, backs up existing configuration, and avoids duplicate entries on repeated runs. Do not upgrade host CLIs or expand the host/data scope.
   - Native Windows Codex uses `install.ps1` (preview with `-DryRun`); Kimi Work desktop uses `install.ps1 -KimiDesktop`. Neither uses the Bash installer. Specify the interpreter with `-Python` when needed. See [Windows integration](docs/windows-integration.md) (Chinese); other Windows hosts are not covered.
   - First installation and updates synchronize the Bridge's managed block and revision into native instructions (AGENTS/CLAUDE/GEMINI, persistent Cursor rules, MiMo instructions, and so on), preserving other user content. Read `memory_rules(section="changes")`, then the current rules, and read back the native entry. For a rules-only refresh, use `install.sh --rules-only`. Report a missing baseline or a missing native instruction tool in an MCP-only host accurately. See [rule synchronization](docs/rule-sync.md) (Chinese).
   - TraeX 0.209.1+ supports L3/L4. Use `install.sh --only trae` to update only that host. User hooks live at `$TRAECLI_HOME/hooks.json`, default `~/.trae/cli/hooks.json`. After installation, review and trust the current definitions through `/hooks`, then verify automatic injection and capture in a new session.
   - Claude Desktop can overwrite external configuration changes while running. The installer detects this and skips the write with guidance: quit the app and rerun, or add the configuration through the app's settings.
   - If the rule package, installation record, or native host instructions differ from the confirmed current source, run the update workflow without asking again: verify the source and target host, read the changes and current rules, run the relevant installer, then read back the revision and managed block. Use rules-only refresh for rule differences and full installation for integration/runtime differences. Do not let an old cache overwrite newer rules. Preserve the current state and report unclear sources, file conflicts, permission restrictions, or trust blockers; do not claim completion. See [automatic updates after detecting differences](docs/rule-sync.md#发现差异后的自动更新) (Chinese).
4. **Verification:** check file synchronization, tool availability, and real new-session behavior separately. None substitutes for another. If permissions, trust, or quota block a check, retain completed results and mark that check as pending.
   - Run `python3 $REPO/plugin/everos/hook.py --doctor` and check that its items report OK.
   - Check pre-prompt retrieval with the following command. Replace the placeholder with a question relevant to the user's work, and inspect tool results and logs to confirm a successful read. Related memories should be returned when they exist. An empty profile or no matches can be a successful result for a new or empty space; do not treat that as a configuration failure, or treat a fail-open hook's zero exit code alone as proof of success:
     ```bash
     printf '{"session_id":"selftest","hook_event_name":"UserPromptSubmit","prompt":"<一句跟用户工作相关的话>"}' | python3 $REPO/plugin/everos/hook.py
     ```
     After the check, remove `state/recall/selftest.json` under the data directory used for this check (default `~/.everos/state/recall/selftest.json`).
   - For a write check, use an actual setup finding that is permitted to be recorded, or an explicitly isolated test fixture. Do not put invented business facts into real memory. First inspect the `memory_remember` add acknowledgement; accepted messages are not proof of search visibility.
   - For a v2 write still awaiting extraction, call `memory_flush(reason="setup verification", session_id=...)` once with the receipt's EverOS `session_id` only if acceptance testing needs that latest record immediately. Do not mechanically flush every write. Then verify independently through get or search; otherwise wait for service processing within a bounded interval before checking.
   - An isolated fixture proves only test-environment behavior and cannot replace a real-service readback. If permission or quota rejects the operation, retain completed verification evidence, report the write/readback as incomplete, and do not blindly retry.
   - In a real new session, check that profile/recall injection and conversation capture actually trigger where supported by that host. If profile content exists, confirm that it reaches the context; an empty profile is not itself a failure. Verify capture using the local ledger or service receipts. Do not claim native automatic hooks are active in a host that only makes MCP or skill-driven calls.
5. **Agents not covered by the installer:** follow [agent onboarding](docs/agent-onboarding.md) (Chinese). At minimum:
   - Connect MCP.
   - Add the Daily workflow rules below to the host's global instruction entry.
6. **Report:** state:
   - Which agents were connected.
   - Each agent's capabilities: automatic reads, automatic writes, and memory tools.
   - Verification results.
   - Anything incomplete.

## Daily workflow

These are the defaults after setup.

- **Read:**
  - Hosts with hooks inject the profile at session start and retrieve relevant memories before each user prompt using agentic search.
  - Without hooks, call `memory_search(method="agentic")` first for each user message and `memory_profile` at session start.
  - Search again when a task reveals a new clue, failure, or gap in a past decision. Expand incomplete results with `memory_open`. `memory_context` retains local task clues and clears outdated coverage after compaction recovery. Existing injection covers only the question it addressed.
  - If entities, past decisions, or dependencies in the first results affect an unresolved part of the task, open the relevant content and search again using the original question, new identifier, and relationship to verify. Keep the evidence chain; stop that branch when evidence is sufficient, nothing new appears, or results repeat.
  - Memory is historical context. The current conversation takes precedence when they conflict.
- **Write:**
  - Hooks save conversation text automatically.
  - Call `memory_remember` at deliveries, corrections, preference changes, and decisions.
  - Use `memory_record_practice` for completed troubleshooting or tool-use work: at least three real steps; mark failed or unexpected attempts with `outcome`, and preserve the user's correction in `user_correction`. Read back the case when the v2 add has extracted; when messages are only accepted into the buffer, report pending extraction.
  - Do not automatically flush after an acknowledged v2 add. At a handoff, workflow completion, topic change, or immediate retrieval need, let the agent decide whether extraction is necessary, then use `memory_flush` with one target and a reason. Otherwise leave processing to the service. Pass a hook's native ID as `conversation_id` and an EverOS write-receipt ID as `session_id`; do not interchange them. A final response that has not been captured is outside the flush scope. See [on-demand extraction](docs/flush-policy.md).
  - Use `memory_fix_profile` when profile information is wrong or outdated.
- **Delivery check:** list what was written to memory. If nothing was written, end with `记忆写入：无` (“Memory writes: none”).

## One-line setup prompt

> Use git to clone the public repository https://github.com/namelessssssssssss/everos-bridge.git into ~/.everos-bridge, read AGENTS.md, and follow its steps to connect EverOS memory to yourself and the other agents on this machine.

## Do not

- Put API keys in the repository, commits, or replies.
- Delete memories unless the user explicitly asks.
- Bypass the platform installer to edit other agents' configuration. Back up before any necessary manual change. Do not guess configuration, upgrade host CLIs, or bypass trust.
- Switch branches or edit code in a repository loaded by running agents; that changes the runtime directly. Develop in a separate worktree, then update the original checkout and rerun `install.sh` after merging.
- Change read/write rules without updating PROTOCOL.md and the relevant docs, committing, and pushing the changes.

## Keep English and Chinese documentation synchronized

- Update each pair in the same PR: `AGENTS.md` / `AGENTS.en.md`, `README.md` / `README.zh-CN.md`, `docs/usage.md` / `docs/usage.zh-CN.md`, and `docs/flush-policy.md` / `docs/flush-policy.zh-CN.md`. Add new language pairs to this list and follow the same rule.
- Keep product positioning, capabilities, supported hosts, installation steps, examples, commands, configuration defaults, version conditions, and limitations equivalent. Natural differences in phrasing are welcome; do not retain one language as an outdated reference or change product content in only one version.
- After editing either language, compare the other version section by section and check language navigation, matching-language guide links, and heading anchors. Two changed files do not prove synchronization; reviewers must verify equivalent meaning.
- Clearly label existing references available in only one language. Do not describe them as bilingual without a matching version.
