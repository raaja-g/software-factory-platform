# Progress

Updated after every completed step.

## Now

Building M1 (foundation). M0's spikes wait for your gcloud/ADC login, Model Garden access and ~$10 of spend.

## Plan revision (before coding)

- [x] **2026-11-18** Open questions answered: first apps, the requirements agent and its backlog, private package registries, GitHub App, Claude Code as proxy, providers, GCP setup, priorities, volume, timeline, Laya data, repos and delivery, decisions accepted.
- [x] **2026-11-18** New rules recorded: `README.md` is updated at the end of every milestone, and this file after every step.
- [x] **2026-11-18** Settled how features in one backlog build on each other: each feature merges into main once approved, and the next starts from the updated main.
- [x] **2026-11-18** Wrote the shared decisions for the revision.
- [x] **2026-11-18** Designed the new and changed tasks (six designers; model switched to Sonnet 5.5 for the build).
- [x] **2026-11-18** Applied the edits: 13 new tasks, M6.5b removed, 6 new calls (C43 to C48); the plan now has 180 tasks. Consistency sweep done and its gaps fixed.
- [x] **2026-11-18** Checked the result and fixed the gaps.
- [x] **2026-11-18** You asked to start building (Sonnet 5.5, fanned-out workflows), so the revised plan stands as approved.

## M1 Foundation (in progress)

- [x] **2026-11-18** M1.1 Repo scaffold and domain types (uv project, `swfactory` package, CI workflow, 9 tests).
- [x] **2026-11-18** M1.2 Settings and redacting logs (24 tests).
- [ ] M1.3a factory.yaml models (partly written; its agent timed out, being re-run)
- [ ] M1.3b Per-file models, validation, JSON Schema, default profiles
- [ ] M1.4 Definition loader, `factory init`, `factory def validate`
- [ ] M1.5a Postgres service, engine and test database fixtures (partly written; being re-run)
- [ ] M1.5b Tables, migration, `factory db upgrade`
- [ ] M1.6 Event bus
- [ ] M1.7 Definition store, app sync
- [ ] M1.8 Job queue
- [ ] M1.8b Backlogs

## Milestones

Order for now: everything runs on your Mac. M0, then M1 to M8, then M10, M11 and M12. M9 (the GCP team instance) and the team-only parts of M13 wait until you ask for them. The private package mirror moves earlier, because your first apps need private packages.

| Milestone | Status |
|---|---|
| M0 Spikes | not started |
| M1 Foundation | in progress |
| M2 Loop with fakes | not started |
| M3 Roles, contracts, triage | not started |
| M4 Sandboxes, verify gate, local git | not started |
| M5 Gateway, models, cost | not started |
| M6 Engines | not started |
| M7 API, dashboard, CLI, local release | not started |
| M8 GitHub and automations | not started |
| M10 Scoring and benchmarks | not started |
| M11 Laya | not started |
| M12 Improver | not started |
| M9 GCP deployment | later, when you ask |
| M13 Hardening | the package mirror early; the rest later |
| G Production gate for company repositories | later |
