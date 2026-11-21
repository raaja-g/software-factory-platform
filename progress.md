# Progress

Updated after every completed step.

## Now

M12 is done (the improver). All planned milestones are built; what remains is the live pass (real models, a GitHub token or App, registries) and the deferred GCP work. M0's spikes wait for your gcloud/ADC login, Model Garden access and ~$10 of spend.

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

## Live pass with real models (in progress)

- [x] **2026-11-21** Wired real providers: Ollama, LM Studio (port 4321), Gemini, Groq (free) and OpenRouter (free) in a local definition, with an `api-free` profile. Keys live only in `.env`
- [x] **2026-11-21** Real jobs now run the whole pipeline on real models (prepare, triage, spec, build). Found and fixed on the way: the gateway rejected every run key (Enterprise-only `tags`) and returned spend summaries; the Docker proxy denied container archive access; the tool-call probe recorded timeouts and LM Studio's `tool_choice` rejection as "no tool support" (so capable models were refused); the runner image left its home owned by root
- [x] **2026-11-21** **Every build failed for one reason**: OpenCode auto-rejected the write of `build.md` because it is outside the repo (`permission requested: external_directory ... auto-rejecting`). Four models failed identically. Now only `/work/.factory/out/*` is allowed for write roles, proven with the real OpenCode binary
- [x] **2026-11-21** Public training data imported (1,039 kind, 9,747 size, 264 touches_ui rows, licences recorded). `factory laya fit` fixed for real data (no idle transaction; capped rows); candidate heads are being fitted
- [x] **2026-11-21** Host ports moved to 7xxx so they do not collide with other apps: dashboard 7300, API 7080, gateway 7400, test Postgres 7432 (a test fails if a published port leaves the range)
- [x] **2026-11-21** The dashboard is its own container (`ui`: locked-down nginx that serves the static files and forwards only `/api` and `/login` to the API); the API no longer serves it in the compose stack (`FACTORY_SERVE_WEB=false`) and accepts the UI's origin through `FACTORY_PUBLIC_URL`. Tested with the real image in front of a stand-in API
- [x] **2026-11-21** A real login page (username and password) replaces the one-time-link instructions: `FACTORY_LOGIN_USER` and `FACTORY_LOGIN_PASSWORD` (defaults: the local user name and the API token), constant-time checks, a lockout after 5 wrong attempts a minute, a 30-day session (`FACTORY_SESSION_DAYS`) and a Sign out button. `factory dashboard` / `make dashboard` still opens it signed in. The password is set in the git-ignored `.env`, never as a default in the repo
- [x] **2026-11-21** Dashboard sessions are now server-side (security review): each login gets a random secret, the server stores only its sha256 in `app.web_sessions` (migration `m13_01_web_sessions`), checks expiry itself, ends the session at sign out, and rejects every session when the login user, password or API token changes. The old fixed cookie value is refused. Tests were written first and failed on the old code
- [x] **2026-11-21** First real-model jobs merged end to end (`demo:019`, `demo:020`): `gemini-lite` specs and builds, the real verify gate runs `uv run pytest`, four Groq reviewers and the approver pass it, and the merge lands in about two minutes. The 20B local model as builder often ends its turn without writing `build.md`, so the cloud model builds for now. All four reviewers answer PASS in about 6 seconds, so review quality is not yet proven.
- [x] **2026-11-21** The verify gate's safety-net commit no longer takes build junk (`__pycache__`, `*.pyc`, `node_modules`, `.venv`, caches, `.DS_Store`) when the repo has no `.gitignore`; the first real job had merged bytecode. 20 tests on a real git repository; the 12 local git loop tests pass against the rebuilt `runner-web` image.
- [x] **2026-11-21** Judges ran on a real model for the first time. Bug found and fixed: every judge in one scoring pass shared one occurrence number, so judges after the first reused the first judge's stored answer (`unknown label 'followed'`). Each judge now gets its own; two tests fail on the old code, and all four judges give their own verdict on the live stack. The four scorers now use the free Groq models. They stay `uncalibrated` until you label jobs: 30 labels with at least 5 passes and 5 fails, kappa 0.6 or more.
- [x] **2026-11-21** The dashboard moved from port 7000 to **7300**: macOS reserves 7000 for AirPlay Receiver (`ControlCenter` listens on `*:7000`, IPv4 and IPv6), so `localhost:7000` answered 403 and the page and login failed unless the exact IP was typed. The API's origin check now treats `localhost` and `127.0.0.1` as the same site for the dashboard's own address only (same scheme and port; the API address stays exact). Checked live: page and login return 200 by both names, a foreign origin is still refused.
- [ ] **Next:** label about 30 jobs (34 are queued on the demo app) so `merge-readiness` calibrates; then a benchmark and a Laya evaluation on real jobs; rotate the provider keys; a GitHub token or App (as raaja-g), registries, GCP later.

## M12 Improver (done)

- [x] **2026-11-21** M12.1 improver config, `improvements` columns, app-less improver jobs; M12.2 failure signals and the trigger; M12.3 proposal guardrails; M12.4 definition workspace (seed the sandbox, read back, commit and push a branch); M12.5 benchmark comparison and the PR body; M12.6 the improver graph; M12.7 periodic trigger, cycles, leases and CLI; M12.8 improvements API and dashboard page
- [x] **2026-11-21** Independent hostile review of the guardrails found 2 high and 4 medium problems, all proved with reproductions and all fixed with tests that fail on the old code: the "before" side of every check came from the agent's own sandbox repo (it has a shell, so it could commit a forbidden change first and then make a small allowed edit); the cross-family check used the proposed builder against base aliases; budget keys were too wide (`job_max_usd: 9999`, `daily_usd: 0` passed); the improver could change its own judges' thresholds; PR bodies let agent text through as links, images and a forged result table; the strength check hid a downgrade behind a weak fallback. Also: narrower GitHub permissions, `model_family` hardened against provider prefixes, role frontmatter locked, branch names that cannot collide after a merged PR, queued improvements that time out, fail-closed on a missing definition or unreadable cost
- [x] **2026-11-21** Checked by me: 1842 default tests; the improver database tests; ruff and pyright clean. Disk was at 89% and the worker's 85% guard made 32 unrelated tests fail until the guard was raised for the test run
- [x] **2026-11-21** Live on the stack: `factory improve status` counted the 13 real failures of my own jobs (not due yet), `list` is empty, `run` refuses without `improver.bench_suite`; the improvements API answers (200 list, 422 without a suite). A real cycle was not run: it needs a capable model
- [x] **2026-11-21** Worker reconnect after a Postgres restart: the checkpointer pool now validates a connection when it hands it out (before: every idle connection failed one step and three failures poisoned the job). A new test kills the pool's connections and reproduces the production `AdminShutdown` error on the old code; on the live stack a job ran normally right after Postgres was restarted
- [x] **2026-11-21** The catalog's capability probe recorded `supports_tools = false` for the local `gemma4` model, so the selector (correctly) refuses it for OpenCode roles; a local model for real coding runs needs one with tool support
- [ ] **Known gaps:** the plan's redraft rule only forbids pure appends, so a prepend or middle insert still passes; no rate limit on starting cycles; viewers can see signal details across apps
- [ ] **Needs you:** a capable model and real failures before the improver can propose anything real; set `improver: {enabled: true, bench_suite: v1-parity}` to switch it on

## M11 Laya (done)

- [x] **2026-11-20** M11.1 router image and compose service (hash-locked, weights baked, non-root, no published port); M11.2 RouterClient that never raises; M11.3 triage through Laya per question with rules last; M11.4 the `llm` triage mode and contract; M11.5 tier choice in the model selector (shadow and laya); M11.6 outcome labels; M11.7 `factory laya export`; M11.8 fit with versioned heads; M11.9 benchmark fixed vs laya and the documented switch; M11.10 `factory laya import`
- [x] **2026-11-20** Spike S7 was never run, so M11.1 derived its facts locally (base image digest, Hugging Face commit and file hashes, the real laya 0.4.31 package, torch wheels) and wrote them to `router/README.md`
- [x] **2026-11-20** Checked by me: 1560 default tests; database tests (72 Laya/model, 448 integration and unit, 147 graph, 15 crash-resume, 12 loop); 4 router image tests (incl. multi-arch) and 29 other Docker tests; 47 UI tests; ruff and pyright clean
- [x] **2026-11-20** Live on the stack: router healthy on `core-net` only; shadow-mode job records four triage rows (`laya 0.4.31`, ~1.1 s); router stopped, job still runs and the error is recorded; export, import and the fit refusal work
- [x] **2026-11-20** Found by live checking and fixed: **no job could get past `prepare` on the real stack** because the Docker socket proxy denies archive calls unless `ALLOW_ARCHIVE` is set (the Docker tests use the local socket, so none saw it); the graph error handler never logged why a step failed (now logs `node.failed` with the cause); `laya export` and `evaluate` wrote to a relative folder the container user cannot write (now `FACTORY_LAYA_EXPORT_DIR`); the import success line now matches the plan
- [ ] **Important caveat:** laya 0.4.31 cannot train its classifier on our data. A fitted head is Laya's own calibration (confidence and route), so Laya never changes which option wins; it can only decide when to answer. The benchmark proof before switching a question on still applies
- [ ] **Needs you:** a model provider so a job can reach the build step; the synthetic or public training rows (see the data options in the chat); a real fit and evaluation

## M10 Scoring and benchmarks

Part A, scoring and metrics (done)

- [x] **2026-11-20** M10.1 objective signals; M10.2 nightly revert detector; M10.3 judge scorers; M10.4 scoring service and the score node; M10.5 judge calibration against your labels; M10.6 Metrics API, Metrics page and quality badge
- [x] **2026-11-20** Added a worker channel for judges (`score_requests`): the API has no agent runner, so `POST /score` rescored only the objective rows and skipped every judge
- [x] **2026-11-20** Checked by me: 1282 default tests, all database tests (355 + 131 graph + 15 crash-resume + 12 loop), 46 dashboard tests, ruff and pyright clean; live stack migrated to `m10_03b_score_requests`, `/api/metrics`, `factory score`, labelling and `jobs show` work
- [x] **2026-11-20** Found by live checking and fixed: `GET /scores` defined twice (the second shadowed the first and broke `factory jobs show`; judge view is now `/judges`, plus a test that no two routes share a method and path); hardcoded migration revisions in tests; the nightly test left a row that made reruns fail
- [ ] **Needs you:** a real builder run (model access) to see judges run live; existing definition repos need the starter scorers copied in (see README)

Part B, benchmarks (done)

- [x] **2026-11-20** M10.7 benchmark definitions and check files; M10.8 `factory bench add`; M10.9 estimate and run start; M10.10 tick and budget cap; M10.11 benchmark jobs, hidden checks and trial recording; M10.12 git prepare at the base commit; M10.12b reference checks; M10.13 configuration overrides, run budget scope, routes and CLI; M10.14 report and Benchmarks page; M10.15 the v1-parity suite
- [x] **2026-11-20** Security fix: the hidden checks sanitize the checks box first (`bench/sanitize.py`). Files an agent could commit to take over the test runner (`conftest.py`, `sitecustomize.py`, `*.pth`, `pytest.py` and friends, pytest config) are restored from the base commit or deleted, listed in the result as `sanitized`, and the check command runs under `bash -c`.
- [x] **2026-11-20** Checked by me: 1380 default tests; database tests (91 benchmark files twice, 145 graph, full integration and unit chunk, 15 crash-resume, 12 loop); 29 Docker tests; 47 UI tests; ruff and pyright clean. Live stack at `m10_05_bench_task_checks`; `factory bench list` and `estimate v1-parity` (12 trials, $120.48, as the plan says) work
- [x] **2026-11-20** Found by live checking and fixed: the orchestrator could not write to `/repos` (the image never created it for user 10001), so `factory bench seed` and any new local repo failed; now chowned in the image, with a test; `bench seed` output matches the plan; three database tests did not expect the new default app
- [ ] **Needs you:** a real benchmark run (model access, up to $120 for the full suite) and a real merged job for `factory bench add`; existing definition repos need `benchmarks/` and the `v1-parity` app copied from `defaults/`

## M8 GitHub and automations (done)

- [x] **2026-11-20** M8.1 GitHub App and PAT auth; M8.2 GitHub client with a serial mutation queue
- [x] **2026-11-20** M8.3 Gitops container on a platform-owned repo with a hardened credential helper
- [x] **2026-11-20** M8.4a PR body and the `factory/review` check; M8.4b GitHubGit with idempotent delivery; M8.4c draft PR at approve; M8.4d delivery pinned to the approved head
- [x] **2026-11-20** M8.5 Issue intake, webhook and merge detection; M8.5b issue outcomes and withdrawing a PR
- [x] **2026-11-20** M8.6 PR comment commands; M8.6b follow-up jobs and holding the app slot
- [x] **2026-11-20** M8.7 New repos (`factory apps create`); M8.7b point-and-run for GitHub repos
- [x] **2026-11-20** M8.8a Cron schedules; M8.8b signed webhooks
- [x] **2026-11-20** M8.9 Definition sync and check run; M8.10 token broker and App key isolation; M8.10b GitHub Packages tokens for the mirror
- [x] **2026-11-20** M8 checked by me: 1198 default tests (the suite ran with Postgres down), all database tests (in chunks), the Docker and dashboard tests (43). The exit-check agent ran 25 minutes with no result, so I stopped it and checked everything myself.
- [x] **2026-11-20** Fixes found while checking: the log handler kept a closed stream from a test runner and printed "Logging error" noise into CLI output (now writes to the current stderr, and httpx stays quiet); the PyPI proxy of the package mirror served the wheel for a `.metadata` request, so every `uv` install through the mirror failed with a hash mismatch (fixed and verified with a real install); the mirror's publish test used `curl`, which the runner image doesn't have.
- [ ] **Needs you:** the GitHub App in your org and a real PR round trip (spike S11); the two private-registry mirror tests need a test registry.

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
| M8 GitHub and automations | done; live GitHub needs your App |
| M10 Scoring and benchmarks | not started |
| M11 Laya | not started |
| M12 Improver | not started |
| M9 GCP deployment | later, when you ask |
| M13 Hardening | the package mirror early; the rest later |
| G Production gate for company repositories | later |
