---
name: factory
description: Queue and steer software-factory jobs and backlogs from Claude Code by calling the `factory` CLI (create a job, list, show, approve, rework, reject, retry, cancel, import a markdown backlog, resume or skip a backlog).
argument-hint: <feature> | list | show <job> | approve <job> | rework <job> <note> | reject <job> | retry <job> | cancel <job> | import <backlog.md> | backlog list | backlog show <backlog> | backlog resume <backlog> | backlog skip <backlog> | backlog cancel <backlog> | run <repo> <requirement file>
---

# /factory

Drive the software factory through its CLI. This skill only calls `factory`; it never edits the
repo or job files, never runs the build loop itself and never retries on its own.

## Which app

Use `$FACTORY_APP` when set. Otherwise run `factory apps list`; when exactly one app is listed use
it. Otherwise ask the person which app to use.

## Arguments

| Argument | Command |
| --- | --- |
| `<feature>` | `factory jobs create --app <app> "<feature>"` |
| `list` | `factory jobs list --app <app>` |
| `show <job>` | `factory jobs show <job>` |
| `approve <job>` | `factory jobs approve <job>` |
| `rework <job> <note>` | `factory jobs rework <job> --note "<note>"` |
| `reject <job>` | `factory jobs reject <job>` |
| `retry <job>` | `factory jobs retry <job>` |
| `cancel <job>` | `factory jobs cancel <job>` |
| `import <backlog.md>` | `factory backlog import <app> <backlog.md>` |
| `backlog list` | `factory backlog list --app <app>` |
| `backlog show <backlog>` | `factory backlog show <backlog>` |
| `backlog resume <backlog>` | `factory backlog resume <backlog>` |
| `backlog skip <backlog>` | `factory backlog skip <backlog>` |
| `backlog cancel <backlog>` | `factory backlog cancel <backlog>` |
| `run <repo> <requirement file>` | `factory run --repo <repo> --req <requirement file>` |

A job is `<app>:<number>`, for example `todo:3`. A backlog is `<app>:b<number>`.

## Rules

- Call only the one CLI line for the argument. Do not edit files in the repo or the job.
- If the CLI exits non-zero, show its message as it is. Do not retry and do not work around it.
- Reply in one line with the result and the dashboard link the CLI prints.
