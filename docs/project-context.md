# Hermes Worker Runtime — Project Context

## Executive Summary

**Hermes Worker Runtime** is a standalone Hermes Agent plugin that extends Hermes Kanban so tasks can be executed by workers other than native Hermes profiles.

The runtime is **not** another orchestrator, workflow engine, task manager, or replacement for Hermes Kanban. Hermes remains the source of truth for task state, dependencies, lifecycle, workspaces, and audit history.

The plugin adds an **external execution layer** that can claim Kanban cards assigned to explicitly configured non-Hermes worker lanes and execute them through external coding agents or deterministic commands.

Examples of worker types:

- Codex CLI
- Claude Code
- OpenCode
- other compatible coding-agent runtimes
- deterministic shell commands such as tests, lint, builds, scanners, or validators

A key architectural rule is:

> **Do not reimplement generic coding-agent adapters if an existing standard or mature integration can be reused. Prefer ACP or existing harness/adapter layers where practical.**

The runtime should focus on the Hermes-specific worker lifecycle: task claiming, workspace binding, supervision, heartbeat, cancellation, result normalization, and transition back into Hermes Kanban.

---

## Problem

Hermes Kanban natively dispatches tasks to Hermes profiles. This works well for Hermes-managed agents, but there are valid cases where a task should be executed by another runtime.

Examples:

- use Codex for a repository-wide implementation task;
- use Claude Code for a difficult refactor;
- use OpenCode for a task that benefits from its runtime/tooling;
- run tests or security scans without spending LLM tokens;
- execute a specialized local or containerized worker;
- integrate future agent runtimes without modifying Hermes core.

Current Hermes worker-lane design recognizes non-Hermes/external worker lanes, but external CLI lanes are not yet a fully paved native path.

The project exists to fill that gap **without forking Hermes**.

---

## Core Goal

Enable this model:

```text
Hermes Kanban
      |
      +--> Hermes profile task
      |        |
      |        +--> native Hermes dispatcher
      |
      +--> external worker task
               |
               +--> Hermes Worker Runtime
                         |
                         +--> Codex
                         +--> Claude Code
                         +--> OpenCode
                         +--> deterministic command
```

Hermes Worker Runtime should make an external worker behave like a well-supervised Kanban executor while preserving Hermes as lifecycle authority.

---

## Target Users

Primary target:

- developers using Hermes Agent with multiple coding-agent runtimes;
- developers running long-lived autonomous Kanban workflows;
- users who want deterministic, non-LLM workers inside the same task board;
- advanced personal engineering setups where different executors are chosen for different task types.

Secondary target:

- plugin authors who want an execution abstraction for custom worker types;
- teams experimenting with mixed AI/local/container worker pools.

---

## Core Use Cases

### External coding-agent worker

A Kanban card is assigned to:

```text
wr:codex
```

The native Hermes dispatcher recognizes it as a non-profile lane and does not spawn a Hermes worker.

Hermes Worker Runtime:

1. discovers the eligible card;
2. atomically claims it;
3. receives/resolves the Hermes workspace;
4. launches Codex in that workspace;
5. supervises the process;
6. keeps the claim alive;
7. captures structured output;
8. normalizes the result;
9. moves the card to review, blocked, done, or failure handling according to the worker result.

### Deterministic worker

A task is assigned to:

```text
wr:test
```

The runtime executes:

```text
npm test
```

No LLM is involved.

The same worker lifecycle is used for:

- tests;
- lint;
- builds;
- static scans;
- migration checks;
- deterministic validators.

### Mixed worker fleet

Different cards on the same Hermes board may use:

```text
forge
wr:codex
wr:claude
wr:opencode
wr:test
```

Hermes profiles and external lanes coexist.

---

## Architectural Principles

### 1. Hermes Kanban remains canonical

Do not create another:

- task database;
- queue;
- dependency graph;
- task status model;
- review system;
- orchestration engine.

Hermes owns:

```text
ready
running
review
blocked
done
archived
```

The runtime only executes cards.

### 2. Pull-based external lanes for v1

Do not monkey-patch the native Hermes dispatcher.

Current Hermes exposes an internal `spawn_fn` seam, but the public plugin API does not currently provide a clean `register_worker_lane()` / `register_spawn_fn()` API, and normal dispatch performs Hermes-profile validation before calling the spawn function.

Therefore the safer v1 architecture is:

```text
external assignee
      |
      +--> native dispatcher marks/skips as non-spawnable
      |
      +--> Worker Runtime observes/discovers it
      |
      +--> atomically claims the task
```

This follows Hermes' existing control-plane/external-lane pattern rather than fighting it.

### 3. Explicit lane ownership

Never treat every unknown assignee as executable.

Use a namespace or explicit registry, for example:

```text
wr:codex
wr:claude
wr:opencode
wr:test
```

A typo such as:

```text
claud-code
```

must remain a configuration error, not become an arbitrary command execution path.

### 4. Reuse existing agent protocols/adapters

Do not write thousands of lines of custom parsing for every coding CLI unless unavoidable.

Investigate and reuse:

- Agent Client Protocol (ACP);
- existing Codex/Claude/OpenCode ACP adapters;
- mature generic coding-agent harness libraries;
- structured CLI output modes;
- MCP only where it improves lifecycle integration.

The unique value of this project is **Hermes worker lifecycle integration**, not recreating a universal coding-agent SDK.

### 5. External workers do not own Kanban policy

The worker should receive:

- task context;
- workspace;
- execution constraints;
- optional skills/instructions.

It should not be allowed to arbitrarily manipulate unrelated cards.

For MVP, the **runtime wrapper** should own Kanban lifecycle mutations.

---

## Proposed Architecture

```text
                    Hermes Agent

                  Hermes Kanban DB
                         |
                 Dispatcher / Hooks
                         |
       +-----------------+------------------+
       |                                    |
Hermes profiles                       external lanes
       |                                    |
native dispatch                    Worker Runtime Plugin
                                            |
                                   Worker Registry
                                            |
                    +-----------------------+-------------------+
                    |                       |                   |
                 Codex                  Claude Code          OpenCode
                    |
                    +-----------------------+
                                            |
                                      Command worker
                                            |
                                 tests / lint / build
```

---

## Main Components

### Worker Registry

Maps explicit lane identifiers to worker adapters.

Conceptual configuration:

```yaml
lanes:
  codex:
    assignee: wr:codex
    adapter: codex
    concurrency: 2

  claude:
    assignee: wr:claude
    adapter: claude-code
    concurrency: 1

  tests:
    assignee: wr:test
    adapter: command
    command:
      - npm
      - test
```

### Scheduler

Responsible for:

- discovering eligible external-lane cards;
- respecting per-lane concurrency;
- claiming tasks atomically;
- avoiding duplicate claims;
- coordinating runtime-owned worker processes.

It must not become a second orchestration engine.

### Worker Adapter

Common adapter contract:

```text
available()
launch()
poll()
cancel()
collect_result()
```

Each adapter normalizes executor-specific behavior.

### Process Supervisor

Tracks:

- PID/process handle;
- task ID;
- run ID;
- workspace;
- lane;
- start time;
- timeout;
- cancellation state;
- log stream.

### Heartbeat Manager

External workers will not automatically participate in Hermes worker activity heartbeats.

The runtime must deterministically maintain claim liveness while the child process is healthy.

Do not rely on the LLM remembering to heartbeat.

### Result Normalizer

External workers should map into a common result model.

Example:

```yaml
status: needs_review

summary: >
  Added token refresh handling and regression tests.

metadata:
  changed_files:
    - src/auth/token.ts
  tests:
    - npm test -- auth

artifacts: []

exit_code: 0
```

Possible normalized statuses:

- success;
- needs_review;
- blocked;
- failed;
- rate_limited;
- cancelled;
- timed_out.

### Hermes Lifecycle Adapter

Responsible for turning normalized worker results into valid Hermes transitions.

For MVP, keep this trusted logic in the runtime wrapper rather than exposing unrestricted Kanban mutation to external agents.

---

## Workspace Requirements

Hermes workspace/worktree resolution remains authoritative.

External workers must run inside the workspace attached to the claimed task.

The runtime must not create its own hidden checkout unless explicitly required by a future worker type.

Concept:

```text
Hermes task
    |
Hermes workspace
    |
Worker Runtime
    |
Codex/Claude/OpenCode cwd = same workspace
```

This preserves compatibility with existing Hermes workflows, including Software Delivery.

---

## Integration with Hermes Software Delivery

The projects must remain independent.

Hermes Worker Runtime can be installed and used without Software Delivery.

Software Delivery can optionally assign implementation/review/test cards to Worker Runtime lanes.

Example:

```text
Software Delivery
      |
HIGH-risk implementation
      |
assign wr:codex
      |
Worker Runtime
```

However:

- Software Delivery owns software-delivery policy;
- Worker Runtime owns execution;
- neither project should import the other's internal business logic.

If a feature only improves governed Software Delivery behavior, implement it in Software Delivery instead.

---

## Security Boundaries

### Explicit commands only

For deterministic lanes, commands must come from trusted configuration.

Do not allow arbitrary Kanban card text to become shell commands.

### Workspace confinement

Workers should be launched with the narrowest practical workspace scope.

### Environment filtering

Do not automatically pass the entire parent environment to external CLIs.

Define clear environment propagation rules.

### Secrets

Do not log:

- API keys;
- authentication tokens;
- full environment dumps;
- secret command arguments.

### Cancellation

If a card is reclaimed, cancelled, or ownership changes, the runtime must stop the worker.

A stale external worker must not continue modifying a repository after its task has been reassigned.

### Claim ownership

All lifecycle writes must validate the current task/run ownership.

A stale worker must not be able to complete a newer run.

---

## Failure Modes to Plan For

- CLI executable missing;
- CLI authentication expired;
- provider quota exhausted;
- task already claimed by another runtime;
- child process crashes;
- child exits successfully without meaningful result;
- worker hangs;
- task claim expires;
- heartbeat fails;
- card is reclaimed while worker is running;
- workspace disappears;
- worktree becomes dirty unexpectedly;
- external worker modifies files after cancellation;
- structured output format changes;
- worker CLI version breaks adapter;
- runtime restarts while workers are active;
- duplicate runtime processes compete for the same external lane.

The implementation plan must address restart/recovery semantics explicitly.

---

## MVP Scope

MVP should prove the lifecycle, not support every coding agent.

Recommended sequence:

### Phase 1 — deterministic command worker

Support:

```text
wr:command
```

Prove:

- discovery;
- atomic claim;
- workspace resolution;
- process launch;
- heartbeat;
- timeout;
- cancellation;
- logging;
- lifecycle completion.

### Phase 2 — one external coding agent

Add a single agent adapter, preferably using an existing structured protocol/adapter.

Codex is a reasonable first candidate.

### Phase 3 — reliability

Add:

- crash recovery;
- restart handling;
- concurrency limits;
- rate-limit classification;
- stale-process cleanup.

### Phase 4 — additional adapters

Add Claude Code and OpenCode only after the adapter abstraction is validated.

---

## Future Extensions

Possible later additions:

- MCP lifecycle bridge for workers;
- remote/container worker lanes;
- worker pools on another machine;
- dynamic lane capability discovery;
- optional quota-aware selection;
- richer telemetry;
- worker health/capability registry;
- upstream Hermes `register_worker_lane()` support if Hermes exposes a stable API.

Do not build these into MVP unless required.

---

## Non-Goals

Hermes Worker Runtime is NOT:

- a replacement for Hermes Kanban;
- a replacement for Hermes profiles;
- a new software-delivery methodology;
- a generic multi-agent orchestrator;
- a task planner;
- a PR review system;
- a model router;
- a new coding-agent protocol;
- an AI dashboard;
- a full terminal manager.

---

## Planner / Executor Notes

Before implementation planning:

1. Audit the current Hermes plugin API and worker-lane documentation for the exact installed/target version.
2. Verify the safest supported way for a plugin background service to inspect/claim external-lane cards.
3. Confirm run ownership and lifecycle APIs that can be safely used from the plugin wrapper.
4. Investigate ACP and existing coding-agent harnesses before designing custom CLI adapters.
5. Design command-worker lifecycle first.
6. Avoid internal Hermes monkey-patching unless there is no supported path.
7. Keep all worker-lane configuration explicit and fail closed.
8. Keep Software Delivery integration optional.
9. Treat external CLI version drift as an adapter compatibility concern, not a reason to redesign the entire runtime.
10. Prefer small upstream Hermes extension requests over maintaining a fork if one missing public extension point becomes necessary.

---

## Success Criteria

The project succeeds when:

> A Hermes Kanban card assigned to a configured external lane can be safely claimed, executed in the correct Hermes workspace by an external or deterministic worker, supervised through completion/cancellation/failure, and returned to the correct Hermes lifecycle state—without modifying Hermes core and without introducing a second orchestration system.
