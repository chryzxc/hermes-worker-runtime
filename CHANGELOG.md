# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- Hermes 0.21.5 refused to enable the plugin: its dependency resolver merged our `pytest<9`
  dev pin with Hermes' `pytest==9.1.1`. The dev pin is now `pytest>=8`.
- ACP lanes: a crashed agent now fails the run within about a second. Previously the run
  waited for the lane timeout, because the ACP connection never rejects in-flight requests
  when the agent's output closes.
- ACP lanes: on `hermes kanban reclaim` the supervisor now stops the agent and exits within
  Hermes' SIGTERM-to-SIGKILL window. Previously it was SIGKILLed before writing its exit
  trailer.
- Daemon logs now go to stderr when run as `hermes worker-runtime run`. Previously Hermes'
  logging setup redirected them into Hermes' `agent.log`, leaving service log files empty.

### Added
- `doctor` reports whether the Hermes Kanban dispatcher is running. Its crash sweep books
  failed runs in `pid-tracked` mode.

### Changed
- README: document the dispatcher requirement; add a troubleshooting entry for cards
  stuck in `running`.
- README: install with `hermes plugins install chryzxc/hermes-worker-runtime`, and
  document updating, pinning and a development checkout. Add a Contributing section.

## [0.1.0] - 2026-09-25

First release.

### Added
- `hermes worker-runtime run | tick | lanes | doctor` plugin commands (also
  `python -m hermes_worker_runtime`).
- Fail-closed YAML lane config at `$HERMES_HOME/worker-runtime.yaml`: explicit
  `wr:*` assignees, argv-only commands, strict key validation.
- Pull-based scheduler: Hermes compare-and-swap claims, per-lane concurrency
  recomputed from the board every tick, and one daemon per `HERMES_HOME` enforced
  by a lock.
- Detached per-run supervisors recorded as the card's `worker_pid`, so Hermes'
  crash detection, retry budget, rate-limit cooldown (exit 75) and breaker
  (exit 78) apply to external workers. A claim-only fallback is used when that
  isn't possible.
- Ownership-fenced lifecycle writes (`expected_run_id`), claim and liveness
  heartbeats, lane timeouts, and whole-process-group cancellation on reclaim,
  timeout or lost claim, plus orphan sweeping after a supervisor is SIGKILLed.
- `command` adapter for deterministic lanes (tests, lint, builds).
- Generic ACP adapter with pinned presets for Codex (`codex-acp` 0.16.0),
  Claude Code (`claude-agent-acp` 0.81.2) and OpenCode, plus `agent_command`
  for any ACP agent. Structured `hermes-result` parsing, permission policy,
  and auth and rate-limit classification.
- Minimal child environment (`HERMES_KANBAN_*` never forwarded) and secret
  redaction in task logs.
- Git change evidence (`changed_files`, `preexisting_changes`) in card metadata.
- launchd and systemd examples.
- Test suite against the real Hermes kanban kernel, including a scriptable fake
  ACP agent. CI on Ubuntu and macOS against a pinned Hermes commit.

[Unreleased]: https://github.com/chryzxc/hermes-worker-runtime/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/chryzxc/hermes-worker-runtime/releases/tag/v0.1.0
