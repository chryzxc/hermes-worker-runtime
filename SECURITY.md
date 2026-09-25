# Security Policy

The Worker Runtime launches coding agents and commands on your machine, so
security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through
[GitHub security advisories](https://github.com/chryzxc/hermes-worker-runtime/security/advisories/new).
Include the version, your configuration (with secrets removed) and steps to reproduce.
You'll get an acknowledgement within a few days.

## Scope

In scope:

- Card content (title, body, comments) influencing a command argv, the child
  environment, or runtime behaviour beyond the agent prompt
- Secrets leaking into task logs or card metadata despite redaction
- `HERMES_KANBAN_*` or other runtime credentials reaching a worker
- A worker process surviving reclaim, timeout or ownership loss
- A stale run being able to write to a newer run's card

Out of scope: what an agent does inside its workspace with the permissions you
grant it (`permission_policy: allow`). Use `deny`, a sandboxed agent, or a
container lane if you need stronger isolation.

## Supported versions

Only the latest release receives fixes.
