# Hermes Worker Runtime — Implementation Plan

Audited against the installed Hermes Agent checkout (`~/.hermes/hermes-agent`,
commit `439eb0395e`). Source context: [`project-context.md`](project-context.md).

## 1. Audit findings (what Hermes already gives us)

| Need | Hermes surface (public unless noted) | Consequence |
|---|---|---|
| Non-profile assignees are not spawned | `_dispatch_lane_task` → `profile_exists(assignee)` false → `skipped_nonspawnable` | `wr:*` cards stay `ready` untouched. **No dispatcher patching.** |
| Atomic claim | `kanban_db.claim_task(conn, id, ttl_seconds=, claimer=)` — CAS `ready→running`, opens a run, enforces parent gate | We pass our own `claimer` lock (`<host>:<pid>:wr-<uuid>`). The host prefix keeps Hermes' host-local crash detection working. |
| Claim liveness | `heartbeat_claim(conn, id, claimer=)` → `False` when ownership is lost | The supervisor uses this call both to keep the claim alive and to detect lost ownership. |
| Stale-detection liveness | `kanban_db_dispatch.heartbeat_worker(conn, id, expected_run_id=)` | Called on a slower cadence (each call appends an event). |
| Ownership-fenced terminators | `complete_task / request_review / block_task(..., expected_run_id=)` | A stale supervisor can't finish a newer run. |
| Workspace | `resolve_workspace(task, board=)` (scratch/dir/worktree), `set_workspace_path` | Hermes stays authoritative; we never create hidden checkouts. |
| Task prompt | `build_worker_context(conn, id)` | The same context native workers get (body, parents, comments, prior runs). |
| Per-task log | `worker_log_path(id)` = `<board>/logs/<id>.log` | `hermes kanban tail` works for our runs. |
| Crash / rate-limit / terminal-provider handling | `detect_crashed_workers` classifies a dead `worker_pid` using the log trailer `[kanban-worker-exit] rc=N`: `75` = rate-limited (requeue, no failure), `78` = terminal provider (breaker trips now), other = crash (counts toward `failure_limit`) | If our supervisor is the recorded `worker_pid`, Hermes' breaker, requeue and quota semantics apply to external workers unchanged. |
| Reclaim / max-runtime kill | `_terminate_reclaimed_worker` SIGTERMs `worker_pid` (fingerprinted), then SIGKILLs | The supervisor must forward termination to the child's whole process group. |
| Recording `worker_pid` | **private** `kanban_db_dispatch._set_worker_pid` | Isolated in `hermes_api.py` behind a capability check. Without it the runtime runs in *claim-only mode* (see §6). This is the one upstream extension request: a public `record_worker_pid(conn, task_id, pid)`. |
| Plugin surface | `register_cli_command`, `VALID_HOOKS` (incl. `on_kanban_dispatch_tick`), `spawn_task` | v1 registers the `hermes worker-runtime …` CLI. The daemon is a separate process, so it never holds the gateway's dispatch lock. |
| ACP | `agent-client-protocol` 0.9 is already in the Hermes venv (`acp.spawn_agent_process`). Adapters: `@zed-industries/codex-acp`, `@agentclientprotocol/claude-agent-acp`, `opencode acp` | One generic ACP adapter replaces per-CLI output parsers. |

The gateway also enforces a global `kanban.max_in_progress` cap, and that count includes our running cards. This is intentional: there is one budget per host.

## 2. Architecture

```text
hermes worker-runtime run   (daemon, one per HERMES_HOME — flock)
   │  every poll_interval:
   │   for board in configured boards:
   │     running_by_lane = count(status=running, assignee=lane)   ← DB is the only state
   │     for ready card with assignee ∈ registry (explicit, wr:-prefixed):
   │        claim_task(claimer=<host>:<daemon-pid>:wr-<uuid>)
   │        spawn detached supervisor ──────────────┐
   │        record worker_pid = supervisor pid      │
   ▼                                                ▼
                                  python -m hermes_worker_runtime.supervisor
                                     ├─ resolve workspace (Hermes), git snapshot
                                     ├─ adapter.launch()  (child in own process group,
                                     │                     filtered env, cwd = workspace)
                                     ├─ loop: poll / heartbeat_claim / ownership / timeout
                                     ├─ SIGTERM (Hermes reclaim) → adapter.cancel() → exit, no writes
                                     └─ collect_result → normalize → lifecycle:
                                          success      → complete_task | request_review (per lane)
                                          needs_review → request_review
                                          blocked      → block_task(kind=needs_input)
                                          failed/timed_out → trailer rc=1, exit 1  (Hermes crash path)
                                          rate_limited → trailer rc=75, exit 75   (Hermes requeue)
                                          unavailable/auth → trailer rc=78, exit 78 (breaker)
                                          cancelled    → exit 143, no writes
```

The runtime has no queue, task table, or state file. Everything it needs to recover after a restart can be derived from the Hermes DB.

### Modules

| Module | Responsibility |
|---|---|
| `config.py` | Load `$HERMES_HOME/worker-runtime.yaml`. **Fail closed**: an unknown key, adapter, or preset, a non-`wr:` assignee, a duplicate assignee, or a non-list command all raise before anything is claimed. |
| `registry.py` | Maps assignee to lane to adapter factory. Only explicit assignees resolve. `claud-code` stays a config error / stranded card. |
| `hermes_api.py` | **The only module that imports Hermes.** Thin compat layer that also probes for private helpers. |
| `scheduler.py` | Discovery, per-lane concurrency, claim, supervisor spawn, child reaping. Keeps no state of its own. |
| `supervisor.py` | Per-task process: runs the lifecycle, heartbeats, handles cancellation and timeouts, performs lifecycle writes. |
| `adapters/base.py` | `WorkerAdapter`: `available() / launch() / poll() / cancel() / collect_result()`. |
| `adapters/command.py` | Deterministic command lane. Commands come only from config, never from card text. |
| `adapters/acp.py` | Generic ACP client plus presets (`codex`, `claude`, `opencode`). |
| `result.py` | `WorkerResult` model plus parsing of the agent's structured `hermes-result` block. |
| `envpolicy.py` | Env allowlist and secret redaction for logs and summaries. |
| `procutil.py` | Process-group spawn and TERM→KILL escalation. |
| `gitstate.py` | Before/after `git status --porcelain` snapshot → `changed_files` metadata. |

## 3. Security boundaries

- **Explicit lanes only.** Assignees must be declared *and* `wr:`-prefixed. An unknown `wr:` assignee is never claimed, so it shows up in Hermes' `stranded_in_ready` diagnostics.
- **Commands from trusted config only.** Commands are argv lists with no shell. Card text reaches ACP agents only as the prompt; it is never passed as argv to the command adapter.
- **Environment.** The child gets only `PATH HOME USER LOGNAME SHELL LANG LC_ALL TERM TMPDIR` plus the lane's `env.pass` list and its `env.set` values. `HERMES_KANBAN_*` claim material is never passed, so the external agent cannot drive Kanban lifecycle writes. Informational `WR_TASK_ID`, `WR_RUN_ID`, and `WR_WORKSPACE` are set.
- **Secrets.** Log lines and summaries go through `redact()`, which masks key/token-shaped values and the values of any env var whose name contains `KEY|TOKEN|SECRET|PASSWORD|AUTH`. The environment is never logged.
- **Confinement.** `cwd` is set to the Hermes workspace. The child runs in its own session/process group, so cancellation kills grandchildren too. The ACP permission policy defaults to `allow` (the agent's own sandbox applies) and can be set to `deny`.
- **Claim ownership.** Every write carries `expected_run_id`. The supervisor stops the child the moment `heartbeat_claim` returns False or `current_run_id` changes.

## 4. Failure modes → handling

| Failure | Handling |
|---|---|
| CLI executable missing | `available()` false → exit 78 (terminal provider): the breaker blocks the card with a clear reason. |
| CLI auth expired | ACP `auth_required` / auth-shaped error → exit 78. |
| Quota exhausted | Rate-limit-shaped error text or exit → exit 75. Hermes requeues without counting a failure and applies its cooldown. |
| Task already claimed | `claim_task` returns None → skip. |
| Child crashes | Nonzero exit → `failed` → exit 1 → Hermes crash accounting. |
| Exit 0 without a meaningful result | ACP: empty final message → `failed`. Command: output tail is the evidence. |
| Worker hangs | Lane `timeout_seconds` → cancel → `timed_out` → exit 1. `task.max_runtime_seconds` is still enforced by Hermes. |
| Claim expiry / heartbeat failure | `heartbeat_claim` False → cancel child, exit without writes. |
| Card reclaimed mid-run | Hermes SIGTERMs the supervisor → child group killed → no writes. |
| Workspace disappears | `resolve_workspace` error → exit 1. A missing cwd at poll time → cancel → failed. |
| Worktree dirty beforehand | Recorded in metadata (`preexisting_changes`), never cleaned. |
| Writes after cancellation | The entire process group gets SIGTERM, then SIGKILL after a grace period, before the supervisor exits. |
| Structured-output drift | ACP is versioned. A missing `hermes-result` block falls back to `needs_review` with the final message. |
| Adapter version breaks | Isolated to one preset (argv only). The runtime is unaffected. |
| Runtime daemon restarts | Supervisors run detached and keep going. The new daemon derives concurrency from the DB. |
| Supervisor dies | Hermes `detect_crashed_workers` reaps it via the recorded `worker_pid` (claim-only mode: TTL expiry). |
| Host reboot | The worker PID fingerprint no longer matches, so Hermes reaps the card. |
| Duplicate daemons on the same host | `flock` on `$HERMES_HOME/worker-runtime/daemon.lock` → second daemon refuses to start. |
| Duplicate daemons on other hosts | `claim_task` CAS prevents double execution. Per-lane concurrency is per host (documented). |

## 5. Restart / recovery semantics

1. A daemon restart is a no-op for running work. Supervisors are session leaders and are not children of the daemon's lifetime.
2. A supervisor restart is never attempted. The card returns to `ready` through Hermes (crash reap or TTL), and the next tick claims it fresh with a **new run id**. Any late write from an old supervisor is rejected by `expected_run_id`.
3. After a reboot, fingerprinted PIDs don't match, Hermes reaps the cards, and they get re-run.

## 6. Modes

- **pid-tracked** (default when `_set_worker_pid` exists): full Hermes crash, rate-limit, and terminal-provider semantics.
- **claim-only** (fallback if a Hermes upgrade removes the private helper): the supervisor handles failures itself with `block_task(kind="transient")`. A dead supervisor is recovered by claim TTL expiry (default 15 min). `hermes worker-runtime doctor` reports the active mode.

## 7. Phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Config, registry, compat layer, scheduler, supervisor, command adapter, env policy, lifecycle mapping, CLI (`run`, `tick`, `lanes`, `doctor`), tests against a real temp Hermes DB | **in this commit** |
| 2 | Generic ACP adapter + Codex preset (`codex-acp`), fake-ACP-agent tests | **in this commit** |
| 3 | Reliability: daemon lock, rate-limit/auth classification, stale-process cleanup via process groups, restart tests | **in this commit (core)**; soak testing pending |
| 4 | `claude` / `opencode` presets (defined; need live validation), launchd/systemd units | presets included, live validation pending |
| Later | `on_kanban_dispatch_tick` wake-up hook, remote/container lanes, upstream `record_worker_pid()` API | not started |

## 8. Testing strategy

- Unit: config validation (fail-closed cases), env filtering and redaction, result parsing, lifecycle mapping.
- Integration (real `hermes_cli.kanban_db`, temp `HERMES_HOME`):
  - command lane success → `done`, `review` mode → `review`, failure → `blocked`, `failed` → exit 1 + trailer;
  - timeout → `timed_out`;
  - reclaim mid-run → child process group killed, no lifecycle write;
  - ownership loss → cancel;
  - unknown `wr:` assignee never claimed;
  - concurrency cap respected;
  - fake ACP agent (built with the `acp` agent-side SDK) → `needs_review` with parsed summary.
  - orphan sweep after a supervisor is SIGKILLed; daemon lock exclusivity;
  - ACP: structured result, prose → review, blocked, permission allow/deny, env filtering,
    auth failure → exit 78, timeout, missing executable.
- Plugin smoke: `hermes plugins enable worker-runtime` + `hermes worker-runtime doctor|tick`
  in a throwaway `HERMES_HOME`.
- Run: see README → *Development*. Current: 75 tests passing against Hermes 0.21.4.

## 9. Findings during implementation

- `hermes_cli.kanban_db.connect` / `resolve_workspace` / `set_workspace_path` /
  `set_branch_name` / `heartbeat_worker` are compat shims since the Sep 2026 decomposition;
  `hermes_api` imports them from `kanban_db_connect` / `kanban_db_workspace` /
  `kanban_db_dispatch` directly.
- `create_task(initial_status=...)` only accepts `running`/`blocked`; `ready` is the default.
- Hermes SIGKILLs a reclaimed worker ~5 s after SIGTERM, so the supervisor uses a 3 s grace
  on signal-driven cancellation and the scheduler sweeps orphaned groups via runfiles.
- A bare `429` rate-limit heuristic misclassifies `429 passed, 1 failed` as a quota wall
  (exit 75 is requeued without spending the failure budget → infinite retry). The detector
  requires context (`HTTP 429`, `too many requests`, …).
- On macOS a dead process group still holds its unreaped leader; `terminate_group` now
  waits on the direct child after the group is gone.
