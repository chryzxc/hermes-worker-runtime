# hermes-worker-runtime

External worker lanes for [Hermes Agent](https://github.com/NousResearch/hermes-agent) Kanban.

Assign a card to an explicit `wr:*` lane (`wr:codex`, `wr:claude`, `wr:tests`, …) and this
runtime claims it, runs it through an external coding agent over the
[Agent Client Protocol](https://agentclientprotocol.com) or through a deterministic
command, and writes the outcome back to Hermes. **Hermes stays the lifecycle authority.**
The runtime does not patch Hermes and is not a second orchestrator.

```
Hermes board ──ready wr:* card──▶ worker-runtime daemon ──claim (CAS)──▶ Hermes
                                        │ spawn detached supervisor
                                        ▼  (recorded as the task's worker_pid)
                                  supervisor ── heartbeats / ownership / timeout
                                        │ ACP session or argv, in the Hermes workspace
                                        ▼
                                  Codex · Claude Code · OpenCode · pytest …
                                        │ normalized result
                                        ▼
                  complete / review / block (expected_run_id)  or  exit code → Hermes
```

Design notes, the Hermes surfaces used and the failure-mode table are in
[docs/implementation-plan.md](docs/implementation-plan.md).

## What it guarantees

- **Explicit lanes only.** Only assignees listed in config are claimed. Hermes'
  dispatcher already skips `wr:*` assignees because they are not profiles, so each card has
  exactly one executor.
- **Fails closed.** An unknown key, a shell-string command, a bad assignee or a duplicate
  lane is a config error, and the daemon refuses to start.
- **Ownership-fenced writes.** Every transition carries `expected_run_id`. A stale
  supervisor cannot finish a newer run.
- **Hermes-native failure handling.** The supervisor is the task's `worker_pid`, so
  Hermes' crash detection, retry budget, rate-limit cooldown (exit 75) and breaker
  (exit 78) apply to external workers unchanged.
- **Whole-tree cancellation.** Workers run in their own process group. Reclaims,
  timeouts and lost claims kill the whole group, and orphans are swept if a supervisor is
  SIGKILLed.
- **Minimal environment.** Children get `PATH HOME USER LOGNAME SHELL LANG LC_ALL TERM
  TMPDIR`, plus only what a lane lists. `HERMES_KANBAN_*` is never forwarded. Logs are
  redacted: the values of secret-named env vars and token-shaped strings are masked.
- **No card text in commands.** Command argv comes only from trusted config.

## Install

Requires a Hermes Agent install. The runtime uses that install's `hermes_cli` and venv.

```bash
git clone https://github.com/chryzxc/hermes-worker-runtime ~/Projects/hermes-worker-runtime
ln -sfn ~/Projects/hermes-worker-runtime ~/.hermes/plugins/worker-runtime
hermes plugins enable worker-runtime        # installs PyYAML + agent-client-protocol if missing
cp ~/Projects/hermes-worker-runtime/examples/worker-runtime.yaml ~/.hermes/worker-runtime.yaml
$EDITOR ~/.hermes/worker-runtime.yaml
hermes worker-runtime doctor
```

## Usage

```bash
hermes worker-runtime doctor   # config, mode (pid-tracked / claim-only), per-lane availability
hermes worker-runtime lanes    # the validated lane registry as JSON
hermes worker-runtime tick     # one discovery/claim pass, then exit
hermes worker-runtime run      # the daemon (foreground; one per HERMES_HOME)
```

Then assign cards:

```bash
hermes kanban create "Fix flaky login test" --assignee wr:codex
hermes kanban create "Nightly test run" --assignee wr:tests
```

To run the daemon under launchd, see
[examples/com.hermes.worker-runtime.plist](examples/com.hermes.worker-runtime.plist).
Supervisors are detached, so restarting the daemon does not interrupt running work.

## Configuration

`$HERMES_HOME/worker-runtime.yaml` (full example: [examples/worker-runtime.yaml](examples/worker-runtime.yaml)).

| Lane key | Meaning |
|---|---|
| `assignee` | `wr:<name>`, lowercase. The only thing that makes a card executable. |
| `adapter` | `command` or `acp` |
| `command` | argv list for `command` lanes. Shell strings are rejected. |
| `agent` / `agent_command` | ACP preset (`codex`, `claude`, `opencode`) or an explicit argv |
| `concurrency` | max running cards for this lane (default 1) |
| `timeout_seconds` | wall-clock limit (default 3600) |
| `on_success` | `complete` or `review` (default `complete` for commands, `review` for agents) |
| `on_failure` | `block` (blocked, needs input) or `fail` (exit code → Hermes retry budget). Default `block` for commands, `fail` for agents. |
| `reviewer` | optional Hermes profile to hand reviews to |
| `permission_policy` | how to answer ACP permission prompts: `allow` or `deny` |
| `env.pass` / `env.set` | extra variables to forward or set |
| `instructions` | appended to the Hermes worker context for agent lanes |
| `kill_grace_seconds` | SIGTERM → SIGKILL grace for the worker group (default 10) |

### Result mapping

| Worker outcome | Hermes |
|---|---|
| success | `complete`, or `review` if `on_success: review` |
| agent reports `needs_review`, or prose without a result block | `review` |
| agent reports `blocked`, or refuses | `block` (needs_input) |
| failed / timed out, `on_failure: block` | `block` (needs_input) |
| failed / timed out, `on_failure: fail` | exit 1 → Hermes crash accounting |
| rate limited | exit 75 → requeued without spending the failure budget |
| auth failure or missing executable | exit 78 → Hermes breaker |
| reclaimed, lost claim, or signalled | no write; the process group is killed |

Agents are asked to end with a fenced `hermes-result` JSON block
(`{"status": "success|needs_review|blocked|failed", "summary": "..."}`). If an agent
answers in prose instead, the card goes to review rather than being marked done.

If Hermes ever drops the private `_set_worker_pid` helper, the runtime switches to
**claim-only** mode. Failures are then written as blocks, and dead supervisors are
recovered by claim TTL. `doctor` shows the active mode.

## ACP agents

| Preset | Launch | Auth |
|---|---|---|
| `codex` | `npx -y @zed-industries/codex-acp@0.16.0` | `codex login` or `OPENAI_API_KEY` (add it to `env.pass`) |
| `claude` | `npx -y @agentclientprotocol/claude-agent-acp@0.81.2` | Claude Code login or `ANTHROPIC_API_KEY` |
| `opencode` | `opencode acp` | OpenCode's own config |

Presets are pinned, so upgrades are deliberate. For the `npx` presets, `doctor` checks only
that `npx` exists; the package itself is fetched on first run. Only the fake test agent
and the command path have been exercised end to end so far. Live runs against Codex,
Claude and OpenCode are still pending.

## Development

```bash
# pytest is not part of the Hermes venv; install it to a side directory
~/.hermes/hermes-agent/venv/bin/python -m pip install --target .pytest-lib pytest
PYTHONPATH=.pytest-lib:$HOME/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python -m pytest
```

The tests run against the real Hermes kanban kernel in a temporary `HERMES_HOME`. They
spawn real supervisors and a scriptable fake ACP agent
([tests/fake_acp_agent.py](tests/fake_acp_agent.py)).

## License

MIT
