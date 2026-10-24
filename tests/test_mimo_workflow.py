"""Run the installed Plugin API callback shapes through Node and the real Python hook."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from test_core_regressions import IsolatedCore, core, hook
import recall

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node') and os.name != 'nt', 'MiMo uses its Unix python3 launcher; Windows installer targets Codex')
class MimoWorkflowCallbacks(IsolatedCore):
    def test_tool_output_reminder_and_compaction_reset_use_shared_core(self):
        source = f'''
import {{ server }} from {json.dumps((ROOT/'plugin/mimo/everos.mjs').as_uri())};
const plugin = await server({{}}, {{hook:{json.dumps(str(ROOT/'plugin/everos/hook.py'))}}});
const result = {{title:"fixture", output:"error E_MIMO_FIXTURE", metadata:{{keep:true}}}};
await plugin["tool.execute.after"]({{sessionID:"mimo-contract", tool:"Bash", callID:"x", args:{{}}}}, result);
if (!result.output.includes("任务中检索提醒") || !result.metadata.keep) throw Error("output reminder missing");
const compact = {{context:["keep compaction context"]}};
await plugin["experimental.session.compacting"]({{sessionID:"mimo-contract"}}, compact);
if (!compact.context[0].includes("keep") || !compact.context.some(x=>x.includes("memory_open"))) throw Error("compaction context missing");
console.log(JSON.stringify({{reminded:true, compacted:true}}));
'''
        env = dict(os.environ, EVEROS_INJECT='1', EVEROS_API_BASE='http://127.0.0.1:9')
        result = subprocess.run(['node', '--input-type=module', '-e', source], env=env, text=True,
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['reminded'])
        state = core.load_state(recall.state_path('mimo-contract'))
        self.assertEqual(state['generation'], 1)
        self.assertEqual(state['coverage'], {})

    def test_before_callback_does_not_rewrite_or_cancel_the_pending_tool(self):
        recall.save_state(recall.state_path('mimo-before'), {'profile_injected': True})
        source = f'''
import {{ server }} from {json.dumps((ROOT/'plugin/mimo/everos.mjs').as_uri())};
const plugin = await server({{}}, {{hook:{json.dumps(str(ROOT/'plugin/everos/hook.py'))}}});
const out = {{args:{{command:"fixture"}}}};
await plugin["tool.execute.before"]({{sessionID:"mimo-before", tool:"apply_patch", callID:"x"}}, out);
if (out.cancel || out.args.command !== "fixture") throw Error("tool input mutated");
const context = {{system:["keep original"]}};
await plugin["experimental.chat.system.transform"]({{sessionID:"mimo-before"}}, context);
if (context.system[0] !== "keep original" || !context.system.some(x=>x.includes("任务中检索提醒"))) throw Error("next model context missing");
console.log(JSON.stringify(out));
'''
        env = dict(os.environ, EVEROS_INJECT='1', EVEROS_API_BASE='http://127.0.0.1:9')
        result = subprocess.run(['node', '--input-type=module', '-e', source], env=env, text=True,
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'args': {'command': 'fixture'}})
        self.api.assert_not_called()
