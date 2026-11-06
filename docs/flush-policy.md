# On-demand memory extraction

Bridge confirms writes before advancing capture progress. For v2, it leaves extraction to EverOS unless the agent decides that a particular task needs the latest saved content now.

Use explicit flush at a meaningful boundary: a handoff, completion of a workflow, a topic change, or a search that depends on the latest saved messages. Do not call it mechanically after every memory write. [EverOS's production guidance](https://docs.evermind.ai/cookbook/quickstart) does not require flushing each turn; Cloud processes an idle session after two hours without new messages. That Cloud behavior is not a guarantee for every self-hosted or legacy server.

## Choose one target

The MCP tool `memory_flush` and the CLI `everos flush` share the same behavior. Both require a nonempty `reason` and exactly one target:

| Field | Meaning |
| --- | --- |
| `session_id` / `--session-id` | The EverOS session ID returned by a structured write |
| `conversation_id` / `--conversation-id` | The host's native session ID shown in the prompt hook; resolves through an existing local capture ledger |

These IDs are different. Do not pass a native conversation UUID as an EverOS session ID. A conversation with no captured writes yet has nothing for this command to extract. The command does not read or upload the current final response before the host has saved it. If a handoff needs a conclusion immediately, write that conclusion with `memory_remember`, then use its returned target ID when necessary.

Example MCP arguments, replacing the placeholder with the actual write receipt:

```json
{
  "session_id": "ID_FROM_WRITE_RECEIPT",
  "reason": "The handoff needs the latest saved decision",
  "api_version": "v2"
}
```

From a terminal:

```bash
everos flush --session-id ID_FROM_WRITE_RECEIPT \
  --reason "The handoff needs the latest saved decision" --api-version v2

everos flush --conversation-id NATIVE_SESSION_ID \
  --reason "The completed workflow is ready for handoff"
```

`api_version` is optional. Direct session IDs use the configured version by default; conversation IDs use their capture ledger. After a version change, explicitly select a preserved old target with `api_version` if needed. `kind=practices` selects the v1 agent-memory endpoint; ordinary memories use the default `episodes`. Conversation IDs only support `episodes`.

## Interpret the result

- **Add confirmed:** the service accepted the submitted messages. Capture can advance its cursor. This alone does not prove extraction or search visibility.
- **Flush confirmed:** the service returned a recognized `extracted` or `no_extraction` result. The corresponding local pending confirmation can be cleared.
- **Case or search visibility:** a separate readback is needed. A flush response does not guarantee a practice case or immediate search results.

A failed flush preserves its pending state; it does not resend an already confirmed add. For captured conversations, flush and capture updates use the same session lock so a concurrent write cannot lose its pending confirmation. Local `pending` means no local flush confirmation has been recorded. It is not a live query of the Cloud buffer and is not cleared merely because two hours have elapsed.

For practice records, an `extracted` v2 add can trigger the existing bounded case readback. An accepted but buffered add reports extraction as pending and does not spend a polling interval waiting for a case. `EVEROS_CASE_WAIT` controls the readback when it applies.

## Automatic and legacy paths

- v2 `memory_remember`, practice recording, transcript capture, MiMo, and local-memory synchronization confirm add without automatic flush.
- SessionEnd and PreCompact still capture remaining messages. They do not automatically flush v2.
- Normal and forced sweeps retain local-memory synchronization and legacy v1 recovery; neither automatically flushes v2 sessions.
- Doubao's existing `everos-doubao-ingest --current --flush` remains an explicit targeted option. Its default capture path does not flush v2.
- v1 retains its compatibility flush behavior. Practice writes use both versions only when v2 is configured and legacy v1 access is enabled.
- Before switching away from v1, Bridge retains its old-buffer confirmation requirement. Switching away from v2 preserves the old target for explicit handling instead of silently forcing extraction or discarding its pending metadata.

For capture-ledger diagnostics, an unconfirmed v2 target from a version switch is retained in `deferred_flush["v2"]`. A conversation flush with `api_version="v2"` can confirm that target without clearing the current version's pending write. New capture ledgers also bind the backend, user, and agent identity; a changed identity is rejected instead of silently reusing the previous mapping. Older ledgers without that field retain their compatibility path.

All explicit writes, including flush, obey `EVEROS_WRITE=0` and quota controls. These changes do not alter the synchronous add confirmation used for recoverable capture. They do not establish a measured MCU saving or an improvement in extraction quality.

After updating the Bridge, reconnect MCP or open a new agent session to load `memory_flush` and the new rules. Existing processes may still expose the previous tool list. See [rule synchronization](rule-sync.md) and the [protocol](../PROTOCOL.md).
