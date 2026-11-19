# Progress

Updated after every completed step.

## Now

M7 is done; starting M8 (GitHub and automations). M0's spikes wait for your gcloud/ADC login, Model Garden access and ~$10 of spend.

## Plan revision (before coding)

- [x] **2026-11-18** Open questions answered: first apps, the requirements agent and its backlog, private package registries, GitHub App, Claude Code as proxy, providers, GCP setup, priorities, volume, timeline, Laya data, repos and delivery, decisions accepted.
- [x] **2026-11-18** New rules recorded: `README.md` is updated at the end of every milestone, and this file after every step.
- [x] **2026-11-18** Settled how features in one backlog build on each other: each feature merges into main once approved, and the next starts from the updated main.
- [x] **2026-11-18** Wrote the shared decisions for the revision.
- [x] **2026-11-18** Designed the new and changed tasks (six designers; model switched to Sonnet 5.5 for the build).
- [x] **2026-11-18** Applied the edits: 13 new tasks, M6.5b removed, 6 new calls (C43 to C48); the plan now has 180 tasks. Consistency sweep done and its gaps fixed.
- [x] **2026-11-18** Checked the result and fixed the gaps.
- [x] **2026-11-18** You asked to start building (Sonnet 5.5, fanned-out workflows), so the revised plan stands as approved.

## M1 Foundation (done)

- [x] **2026-11-18** M1.1 Repo scaffold and domain types (uv project, `swfactory` package, CI workflow, 9 tests).
- [x] **2026-11-18** M1.2 Settings and redacting logs (24 tests).
- [x] **2026-11-18** M1.3a factory.yaml models
- [x] **2026-11-18** M1.3b Per-file models, validation, JSON Schema, default profiles
- [x] **2026-11-18** M1.4 Definition loader, `factory init`, `factory def validate`
- [x] **2026-11-18** M1.5a Postgres service, engine and test database fixtures
- [x] **2026-11-18** M1.5b Tables, migration, `factory db upgrade`
- [x] **2026-11-18** M1.6 Event bus
- [x] **2026-11-18** M1.7 Definition store, app sync
- [x] **2026-11-18** M1.8 Job queue
- [x] **2026-11-18** M1.8b Backlogs
- [x] **2026-11-18** M1 exit check run end to end: 126 default tests, 59 database tests, ruff and pyright clean.

## M2 The job loop (done)

- [x] **2026-11-18** M2.1 Graph state and pure routing
- [x] **2026-11-18** M2.2a Engine protocol, registry and fake engine
- [x] **2026-11-18** M2.2b Sandbox protocols, fake sandbox and fake git
- [x] **2026-11-18** M2.3 Checkpointer on PostgreSQL 18
- [x] **2026-11-18** M2.4 Idempotent agent step and a minimal artifact store
- [x] **2026-11-18** M2.5a Nodes, `build_job_graph`, happy path and rounds
- [x] **2026-11-18** M2.5b Verify node and fix attempts
- [x] **2026-11-18** M2.5c Human gate and error handlers
- [x] **2026-11-18** M2.6 Commands and the jobs CLI
- [x] **2026-11-18** M2.7a Worker loop, leases and global concurrency
- [x] **2026-11-18** M2.7b Commands and cancel in the worker
- [x] **2026-11-18** M2.7c Crash and resume at every node
- [x] **2026-11-18** M2.7d Poison jobs, retry of failed jobs and graceful shutdown
- [x] **2026-11-18** M2.8 Board query
- [x] **2026-11-18** M2 exit check: 212 default tests, 166 database tests, ruff and pyright clean. Widened one timing-sensitive lease test that failed once under load.

## M3 Roles, contracts, triage, requirements, and the first half of M5 (done)

- [x] **2026-11-18** M3.1 Role files and `RoleLibrary` (ten roles)
- [x] **2026-11-18** M3.2 Output contracts
- [x] **2026-11-18** M3.3 Fencing untrusted text
- [x] **2026-11-18** M3.4 Prompt rendering and trimming
- [x] **2026-11-18** M3.5 Volume storage, dedupe and redaction for the artifact store
- [x] **2026-11-18** M3.6 Real prompts and the contract repair flow
- [x] **2026-11-18** M3.7 Rules-based triage and `reviewers_for`
- [x] **2026-11-18** M3.8 Triage and reviewer selection wired into the loop
- [x] **2026-11-18** M3.9 Requirements job: graph, gate and approval
- [x] **2026-11-18** M5.1 Render the LiteLLM config
- [x] **2026-11-18** M5.3 Gateway client: keys, spend, prices, probes
- [x] **2026-11-18** M5.4 Catalog refresh, schedule and models CLI
- [x] **2026-11-18** M5.5 Profiles: resolution and validation
- [x] **2026-11-18** M5.6 Fixed model selection with recorded fallback
- [x] **2026-11-18** M5.7 Budgets, reservations and pricing
- [x] **2026-11-18** M3 exit check: 413 default tests, 221 database tests, ruff and pyright clean. Fixed the cause of recurring timing failures in the worker tests (leases of 1 s were too tight under load; now 3 s).
- [ ] M5.2 (gateway services in compose, needs M4.3a), M5.8 (wire into `run_agent_step`) and M5.9 wait for M4.

## M4 Sandboxes, verify gate, local git and the second half of M5 (done)

- [x] **2026-11-19** M4.1 Runner images and runner definitions
- [x] **2026-11-19** M4.2 Hardened container template
- [x] **2026-11-19** M4.3a Socket proxy, core network and repos volume
- [x] **2026-11-19** M4.3b Docker sandbox provider and isolated containers
- [x] **2026-11-19** M4.4 Sandbox reaper
- [x] **2026-11-19** M4.4b Workspace size limit, disk high-water mark and output caps
- [x] **2026-11-19** M4.5 Local git service
- [x] **2026-11-19** M4.5b Local folders: seed, fetch and deliver into the folder
- [x] **2026-11-19** M4.6 Verify gate
- [x] **2026-11-19** M4.7 Browser smoke screenshots
- [x] **2026-11-19** M4.8a Sandbox, gate and local git wired into the graph
- [x] **2026-11-19** M4.8b Isolated reviewer containers and screenshot attachments
- [x] **2026-11-19** M4.9 Close jobs that wait too long
- [x] **2026-11-19** M5.2 Gateway services in compose, behind a route allowlist
- [x] **2026-11-19** M5.8 Selection, run keys and spend wired into `run_agent_step`
- [x] **2026-11-19** M5.9 Spend reconciliation and the reservation sweep
- [x] **2026-11-19** M4 and M5 checked by me: 546 default, 289 database and 49 Docker tests pass; ruff and pyright clean; no containers, volumes or networks left over. The exit-check agent hung for 3 hours and was stopped.
- [ ] **Needs you:** M5's live checks (`factory models refresh` against Vertex, the live probe test) need gcloud ADC, `GOOGLE_CLOUD_PROJECT`, and the Claude models enabled in Model Garden.

## M6 Engines (done)

- [x] **2026-11-19** M6.1 Runner image with locked engine CLIs
- [x] **2026-11-19** M6.2 Engine base helpers, registry and stub CLI harness
- [x] **2026-11-19** M6.3 Direct engine
- [x] **2026-11-19** M6.4 OpenCode engine
- [x] **2026-11-19** M6.5 Claude Code engine
- [x] **2026-11-19** M6.6 Gemini CLI engine
- [x] **2026-11-19** M6.7 Flag silent model swaps
- [x] **2026-11-19** M6.8 Builder continuity (fresh and resume)
- [x] **2026-11-19** M6.8b Commit trailers
- [x] **2026-11-19** M6.8c Worker with real engines
- [x] **2026-11-19** M6.9 Opt-in end-to-end test per engine
- [x] **2026-11-19** Security fixes: instruction-file cleanup is one fail-closed `find ... -exec rm` and covers every engine's config; errors are logged without URLs that could hold a run key.
- [ ] **Needs you:** live runs of each engine against real models (gcloud ADC, Model Garden access).

## M7 API, dashboard, CLI, local release (done)

- [x] **2026-11-19** M7.1 API app, health, auth and mode safety
- [x] **2026-11-19** M7.2 Board, jobs and actions routes, and `submit_command`
- [x] **2026-11-19** M7.3 Server-sent events
- [x] **2026-11-19** M7.4a Definition writer
- [x] **2026-11-19** M7.4b Apps, models, profiles, definition and metrics routes
- [x] **2026-11-19** M7.4c Per-app access lists
- [x] **2026-11-19** M7.4d First-use registration (`apps/<slug>.yaml`)
- [x] **2026-11-19** M7.4e Backlog routes and actions
- [x] **2026-11-19** M7.5 Dashboard v2, and M7.5b Backlogs view
- [x] **2026-11-19** M7.6 CLI parity, backlog import and the Claude Code skill; M7.6b budget top-up; M7.6c point-and-run (`factory run`)
- [x] **2026-11-19** M7.7 Local release (image, compose, make); M7.7b gateway config rendering; M7.7c package mirror with private registries
- [x] **2026-11-19** M7.9 Worker heartbeats and stuck-job detector; M7.9b notifications and the API watchdog
- [x] **2026-11-19** M7.10 CI workflow
- [x] **2026-11-19** M7.8 Acceptance replay of v1's four features: the helpers and report are built and tested; the live replay is opt-in and skips without models.
- [x] **2026-11-19** M7 checked by me: 1016 default tests (run with Postgres down, so none depends on the database), all database tests (run in chunks), 80 Docker tests and 42 dashboard tests pass; `make up` brings the whole stack healthy in 38 s and `/readyz` shows one worker, the database and the definition.
- [x] **2026-11-19** Fixes made while checking: old CLI tests rewritten for the API-based CLI (who-asked now comes from the signed-in user); the gateway stack tests got a mock-model compose override (the gateway now reads a rendered config); a loader test no longer depends on a developer's `.env`; a unit test that was quietly querying the dev database now uses a stub; the notification webhook now reaches the API and worker in compose.
- [ ] **Needs you:** the live acceptance replay (`make up` then `pytest -m e2e tests/e2e/test_v1_parity.py`) and real registry connections need Vertex credentials, models and your registries.

## Milestones

Order for now: everything runs on your Mac. M0, then M1 to M8, then M10, M11 and M12. M9 (the GCP team instance) and the team-only parts of M13 wait until you ask for them. The private package mirror moves earlier, because your first apps need private packages.

| Milestone | Status |
|---|---|
| M0 Spikes | not started |
| M1 Foundation | done |
| M2 Loop with fakes | done |
| M3 Roles, contracts, triage | done |
| M4 Sandboxes, verify gate, local git | done |
| M5 Gateway, models, cost | done in code; live Vertex checks need your setup |
| M6 Engines | done in code; live runs need your setup |
| M7 API, dashboard, CLI, local release | done; live replay needs your setup |
| M8 GitHub and automations | not started |
| M10 Scoring and benchmarks | not started |
| M11 Laya | not started |
| M12 Improver | not started |
| M9 GCP deployment | later, when you ask |
| M13 Hardening | the package mirror early; the rest later |
| G Production gate for company repositories | later |
