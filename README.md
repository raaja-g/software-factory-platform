# software-factory-platform

A platform that turns a requirement into reviewed, merged code. For each feature it runs spec, build, verify, four independent reviews, approve and deliver, using coding agents on any model provider. It runs on your machine in Docker, many apps and features at a time.

**Status: under construction.** Milestones M1 to M8 and M10 to M12 are built: foundation, the job loop, roles and contracts, sandboxes with the verify gate and local git, the model gateway with cost control, the coding-agent adapters, the API, dashboard and CLI with a one-command local release, GitHub, scoring with a Metrics page, benchmarks, the Laya router, and now the improver. Still to come: the live pass with real models, the GitHub App and your registries (see below). See [progress.md](progress.md).

## What works now (M1 to M12)

- A Python package, `swfactory`, with a `factory` command line.
- Settings from the environment, and logs that redact secrets.
- The definition repo format (apps, profiles, runners, models) with a validator:
  - `factory init <dir>` creates a definition repo.
  - `factory def validate <dir>` checks it and reports errors with file and line.
- A PostgreSQL 18 schema with migrations (`factory db upgrade`), an event log and an event bus.
- `factory def sync` loads a definition and registers its apps (`factory apps list`).
- A per-app job queue with leases (`factory jobs create`, `factory jobs list`) and backlogs: ordered lists of jobs where each starts only after the one before it has merged.

- **The job loop (M2),** on PostgreSQL with scripted fake agents, a fake sandbox and fake git. A job goes through every stage: triage, spec, build, verify, four parallel reviews, approve, deliver. It does rounds and rework when reviewers ask for changes, and handles one reviewer failing.
  - It is durable: the graph is checkpointed in Postgres, so a worker can be killed at any node and a job resumes without repeating finished steps.
  - People decide at a gate: `factory jobs approve|rework|reject|retry|cancel`. Each action is tied to the wait it answers, and an approval is pinned to the commit the person saw.
  - A worker claims jobs with leases, runs up to a set number at once, backs off and finally fails a job that keeps crashing it, and shuts down cleanly.
  - `factory jobs board` prints the board as JSON.

- **Roles and contracts (M3).** Ten engine-neutral role files (`defaults/roles/*.md`: triage, requirements, spec-writer, builder, four reviewers, approver, judge, improver). Every output has a strict contract (a review starts with `VERDICT: PASS` or `VERDICT: CHANGES`, a decision with `APPROVE` or `ESCALATE`); a near miss gets one repair attempt and is never read as a pass. Untrusted text (issue bodies, repo files) is fenced as data, and prompts are trimmed to fit a model's context, with huge diffs cut at file boundaries and sent to a person. Artifacts are stored deduplicated and redacted. Rules-based triage picks the reviewers.
- **Requirements jobs (M3).** A requirement becomes a `requirements.md` and an ordered backlog that a person approves; its feature jobs then run one after another, each merged before the next starts.
- **Models and cost (first half of M5).** The LiteLLM gateway config is rendered from your definition (`factory gateway render`); a gateway client handles per-run keys, spend and probes; a model catalog records what is actually available (`factory models refresh`, `factory models list`); profiles choose a model per role, with a recorded fallback; budgets reserve spend per run so parallel runs cannot overspend.

- **Sandboxes and the verify gate (M4).** Each job gets its own hardened container (non-root, read-only root, no capabilities, no credentials, its own network) on two volumes, started through a filtered Docker socket proxy. Runner images (`swfactory/runner`, `swfactory/runner-web` with Chromium) build with `docker compose --profile build build runner runner-web`. After every build the platform itself checks the work in a clean clone of the job branch: setup commands, the app's tests, protected paths, and browser screenshots for web apps. Reviewers get their own isolated copy that can run tests but not edit. A reaper removes orphaned containers, volumes and networks, and workspace size, disk space and output are capped.
- **Local git and your folders (M4).** Finished work is merged with a pinned head: only the commit that was tested and approved is merged. For a local folder, the factory keeps its own copy and fast-forwards your folder's main afterwards, or waits for you if your tree is dirty. Jobs that wait for a person too long are closed, keeping their branch.
- **Model gateway and cost (M5).** LiteLLM runs behind a route-allowlisting proxy (`docker compose up -d gateway gateway-edge`); every agent step gets a short-lived key capped to its model and budget, spend is read back and reconciled (including crashed runs), and a failing model falls back once to the next in the profile.

To use the gateway you need these in `.env`: `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `LITELLM_DB_PASSWORD`, `GOOGLE_CLOUD_PROJECT`, and an Application Default Credentials file for Vertex AI (`gcloud auth application-default login`); optional `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`. Then:

```bash
uv run factory gateway render
docker compose up -d gateway gateway-edge
uv run factory models refresh
```

- **Coding engines (M6).** Agent steps run behind one `Engine` interface with four adapters: OpenCode (the default for team profiles, any model through the gateway), Claude Code (`claude-vertex` profile, Claude models, or other models as an experiment), Gemini CLI (optional), and a direct single call to the gateway for steps that need no tools (approver, judges). The CLIs are installed in the runner image from a committed lockfile (OpenCode 1.19.5, Claude Code 2.1.322, Gemini CLI 0.68.0). Each adapter enforces the role's tool policy (read-only roles cannot write files, reviewers can run tests but not edit), ignores project-level engine config in the repo, reports usage and cost per run, and detects when a model was silently swapped. Builder commits carry trailers naming the job, role and model. A builder can start each round fresh (default) or resume its session.

- **API, dashboard and CLI (M7).** `make up` starts the whole stack (Postgres, the model gateway behind its route filter, the package mirror, a filtered Docker socket proxy, the API with the dashboard, and the worker) and prints a one-time login URL.
  - The dashboard is a live board (server-sent events) with a card per job: stage, round, cost, the model each role used, and approve, rework, reject, retry or cancel at a gate. It has pages for backlogs, apps, models, profiles and the active definition. Model-written and repo-written text is rendered as escaped text, behind a content-security policy.
  - Every `/api` call needs a token, even locally. Roles (viewer, submitter, approver, admin) apply, and an app can limit who sees it. Who approved is taken from the signed-in user, never from what a client says. Tokens expire (90 days) and can be listed and revoked (`factory tokens ...`).
  - **Point at a repo with a requirement:** `factory run --repo ~/work/my-app --req ./requirement.md` (or `--req -` for stdin, or the dashboard's New run form). The repo is registered on first use. A requirements agent turns the text into a full requirements document and an ordered backlog; you approve it once; the feature jobs then run one after another, each merged before the next starts. `factory backlog list|show|resume|skip|cancel` manages them.
  - **Local folders:** the factory works in its own copy and fast-forwards your folder's main afterwards; if your working tree is dirty or main moved, the job waits with a clear reason.
  - **Budgets:** a job that stops at its budget waits for you, and an admin can top it up (`factory jobs add-budget`) within a hard limit.
  - **Private packages:** sandboxes install through a read-only package mirror (`pkg-mirror`) that holds the registry credentials (Artifact Registry, GitHub Packages or a token registry, configured under `registries:` in your definition, credentials from `.env`), so code in a sandbox can neither see a credential nor publish a package.
  - **Health and alerts:** workers send heartbeats, a stuck-job detector runs in the API, and notices (stuck or failed jobs, long waits, no live worker, 80% and 100% of the daily budget, disk space) go to a Slack- or Google Chat-style webhook set in `FACTORY_NOTIFY_WEBHOOK_URL` (`factory notify test`).
  - The gateway's config is rendered from your definition at start and whenever the definition changes; `FACTORY_DEFINITION_PATH` and `FACTORY_HOST_REPOS_ROOT` in `.env` are required.

Not verified live: the acceptance replay of v1's four features with real models (`tests/e2e/test_v1_parity.py`, opt-in) has not been run, and the registry connections to real Artifact Registry and GitHub Packages are tested only against mock upstreams. The engines and the gateway still have not run against real models.

- **GitHub (M8).** Repos can be GitHub repos as well as folders, through a GitHub App (or, on your own machine only, a fine-grained token in `FACTORY_GITHUB_TOKEN`).
  - **No push credential in a sandbox.** Git over the network runs in a separate, hardened container that works on a copy of the repo the platform owns, fed by bundles; it holds a short-lived token limited to one repo and one permission, talks only to the pinned GitHub host, and ignores any git config or hooks in the repo. The API process never holds the App's private key; it asks a token broker in the orchestrator.
  - **Delivery you can trust.** Only the commit that was tested and approved is pushed or merged: if the head moves, or a rebase changes it, the job re-verifies, or goes back to you. Every step is checked before it acts, so a crash never opens a second PR or merges twice. Each PR carries a `factory/review` check with the verdicts, models and cost.
  - **CI safety.** A repo is registered with `github.ci_safe: false` by default, which means nothing is pushed until a person approves in the factory, because a push could run the repo's CI with its secrets. Set it to true only when your CI is safe for `factory/**` branches.
  - **Issues in, outcomes out.** Label an issue `factory` and it becomes a job once per labelling (a finished job is never re-run when its issue is still open). An issue from someone without write access needs a person's approval. When the job ends, the label is removed and one comment says what happened. A PR closed without merging closes its job.
  - **PR comment commands.** `/factory approve`, `rework <note>` and `reject` on the PR, checked against GitHub's own permissions; a lost webhook is recovered by polling. `rework` on a delivered PR starts a follow-up job on the same branch.
  - **Also:** `factory apps create` makes a new repo with auto-merge on; cron schedules and signed webhooks start jobs with per-automation caps; the definition repo is synced from GitHub and PRs to it get a validation check; the package mirror can read GitHub Packages through the App.

- **Scores and the Metrics page (M10, part A).** Every finished job is scored without a model first: gate result, fix attempts, rounds, the decision, whether a human stepped in, how it was delivered, whether it fell back to another model, time and cost. These roll up into a quality badge (`good`, `fair` or `poor`) shown on done cards. A nightly pass marks a merged job `reverted` if a revert commit lands later.
  - **Judges.** Four starter scorers (`merge-readiness`, `spec-adherence`, `scope-creep`, `process-compliance`) ask a model from a different family than the builder to grade a sample of jobs (10% by default; `--force` runs them all). They live in `scorers/` in your definition repo. A definition repo made before M10 has none: copy `defaults/scorers/` and `defaults/roles/judge.md` into it and run `factory def sync`. A job is always scored against the definition it was created under.
  - **Your labels come first.** `factory score label demo:001 merge-readiness A` stores your verdict, and the judge's answer stays hidden until you have given yours. A judge counts toward the Metrics page only after 30 labels, once it agrees with you well enough; until then every judge row says `uncalibrated (n of 30 labels)` and counts for nothing.
  - **The Label page** (`#/label`, needs the approver role) is the quick way to give those labels. It lists finished jobs whose judge has already answered, oldest first, with what was asked, the diff, the test result and the reviewers' decision. Press `1`, `2` or `3` (or click) to give `A`, `B` or `C`; the judge's answer is shown only after you have labelled, with whether you agree. A progress bar shows how far you are from 30 labels (at least 5 passing and 5 failing). Jobs with no judge verdict yet are counted but not listed, because a label only counts against a verdict: run `factory score REF --force --scorer merge-readiness` for those, or set `sampling_rate: 1.0` on the scorer in your definition so every new job is judged.
  - **Commands.** `factory score REF [--scorer NAME] [--force] [--no-wait]` queues the judges on the worker and waits; `factory score calibration` shows how each judge agrees with you; `factory jobs show REF` has a Scores block.
  - **Metrics page** (`#/metrics`): pass rate by role, model, engine and profile, rounds per job, escalation rate, cost per merged job, fallback rate, and each scorer's pass rate.

Not verified live: the judges have never run against a real model, because no real builder run has succeeded yet. They are tested with fakes, and the live stack was checked up to the point where a model is needed.

- **Benchmarks (M10, part B).** A benchmark suite replays real tasks, each pinned to the commit before the work was done, against different configurations (models, profiles, triage modes), so you can compare them on cost and quality. Quality is a hidden check the agent never sees: a test file that must pass on the result. Judges are shown next to it but never rank anything until calibrated.
  - **Your own suites.** `factory bench add --from-job demo:003 --suite mine` turns a merged job into a task: the base commit, the acceptance items as success criteria, and the tests the job added as hidden checks (committed to your definition repo under `benchmarks/mine/`). A reference run checks that the hidden checks fail on the base commit and pass on the real merge, so a broken check is marked invalid instead of counting against a model.
  - **The shipped suite.** `v1-parity` replays the four jobs of the first version of the factory against three configurations. Import its repo with `docker compose exec orchestrator factory bench seed v1-parity`, then `factory bench estimate v1-parity` (12 trials, up to $120.48, against a $40 suite cap) and `factory bench run v1-parity --config gemini --yes`.
  - **Safe by design.** Every run has its own budget cap, checked before each trial starts; trials run one at a time on `bench/<run>/<task>/<config>` branches and never touch your main branch; a run that hits its cap stops with `stopped_budget` and still reports the trials it finished.
  - **Reports.** `factory bench report RUN` shows, per configuration, the hidden-check pass rate (with a 95% interval), rounds, cost and time, and a recommendation. The Benchmarks page (`#/benchmarks`) lists runs, plots cost against quality and starts a run only after showing the estimate.

- **Laya, an optional router (M11).** A small model in its own container that answers the triage questions (`kind`, `size`, `risk`, `touches_ui`) and, per role, which model tier to use. It has no published port, sits only on the internal `core-net`, is never attached to a job's network, and needs a key. Its weights are baked into the image, so it needs no network. It is off until you start it, and every question starts in **shadow**: Laya answers, the answer is recorded next to the rules' or the fixed model's, and the rules or the fixed model still decide. If the router is down, the job carries on with the rules and records `laya fallback: router error`.
  - **Start it.** Put `LAYA_API_KEY=<random>` and `FACTORY_LAYA_URL=http://router:8000` in `.env`, then `docker compose --profile laya up -d --build router` (the first build downloads about 6 GB and takes a few minutes). `router/README.md` has the details, including how the image was pinned.
  - **The `llm` triage mode.** A cheap direct model call that answers the triage questions under the triage contract, as an alternative to the rules. Set `triage.modes` per question to `rules`, `llm`, `shadow` or `laya`.
  - **Teach it.** `factory laya export --question touches_ui` writes your labelled rows (from outcomes: review verdicts, rounds, cost, your triage corrections, and benchmark groups). `factory laya import --question touches_ui --file rows.csv --source <name> --origin <url> --licence <licence>` adds outside rows (CSV with a `state` column and one label column named after the question); each file can be imported once, and imported rows never count as held-out data. `factory laya fit --question touches_ui` needs at least 50 labelled rows and makes a candidate head that is not active.
  - **What fitting does, honestly.** The installed Laya (0.4.31) cannot train its classifier on your data. A fitted head is Laya's own calibration, fitted on your rows: it makes the confidence and the route (auto, review) trustworthy, so Laya takes over only when it is confident. It never changes which option wins, so Laya's accuracy stays that of the underlying model. Expect it to answer some questions and defer the rest.
  - **Switching on needs proof.** `factory laya evaluate --question tier.builder --suite v1-parity` runs a benchmark of the fixed choice against Laya and prints `switch: yes` only if the quality matches at lower cost; it refuses a suite whose tasks the head trained on. Then `factory laya activate --question tier.builder --version <n>` and set `mode: laya` yourself. Nothing switches itself on. `factory laya status` shows each question's mode, head and row counts.

- **The improver (M12).** A periodic, human-gated job that turns your failures into definition changes. It is off by default (`improver.enabled: false` in `factory.yaml`). When on, it counts failed runs (scorer and review signals included); once there are `failed_run_threshold` of them (25 by default) or the oldest is older than `max_failure_age_days` (7), it groups the evidence by role and, for each role group, one agent proposes **one focused change** to that role's prompt, profile or scorer. It then runs your benchmark suite (`bench_suite`, e.g. `v1-parity`) before and after, within `max_cycle_usd` ($120 by default), and opens a pull request to the definition repo with the numbers, the signals it addressed and any regressions. **It can never merge**: a person reviews and merges, and the merged change reaches the factory through `factory def sync`.
  - **What it may touch.** Only `roles/<own role>.md`, `profiles/*.yaml`, applicable `scorers/*.md`, and budget keys in `factory.yaml`; never benchmarks, apps or providers. Every proposal is read back from git and checked against exact paths, regular-file modes and size limits, then validated as a whole definition, and it may not move a judge into the same model family as a builder it judges. A proposal that breaks a rule is recorded with the rule and not pushed.
  - **Commands.** `factory improve status` shows the failures counted and whether the trigger is due; `factory improve run [--role R]` starts a cycle now, even when disabled; `factory improve list` shows improvements, newest first. The Improvements page on the dashboard shows each one's evidence, diff summary, benchmark comparison and cost. Setting it up: in `factory.yaml` set `improver: {enabled: true, bench_suite: v1-parity}` and run `factory def sync`.

Not verified live (M12): no improvement has been proposed against a real model, since the failures it needs come from real jobs and a capable model is not wired yet. The graph, the guardrails and the database parts are tested with fakes and real git repositories.

Checked on the live stack (M11): the router starts healthy on `core-net` only with no published port; a job in shadow mode records four triage rows with router version `laya 0.4.31` in about a second; with the router stopped the job still runs and the error is recorded; `export`, `import` and the `fit` refusal work. Not verified live: no head has been fitted on real outcomes or evaluated against a real model, and no job has reached the build step, because no model provider is configured yet.

Not verified live (M10 part B): no benchmark has run against a real model. The estimate, seeding and the page were checked on the live stack, and the trials themselves are tested with fakes.

Not verified live (M8): none of M8 has run against real GitHub. It is tested against a fake GitHub and local git repos; the GitHub App setup in your org (spike S11 in the plan) and a real PR round trip are still to do.

Run a real job from the command line (needs the gateway running and the Claude and Gemini models you use enabled):

```bash
docker compose --profile build build runner runner-web
uv run factory gateway render && docker compose up -d gateway gateway-edge
uv run factory jobs create --app demo --profile team-gemini "Add a hello command"   # OpenCode
uv run factory jobs create --app demo --profile claude-vertex "Add a hello command" # Claude Code
uv run factory worker --exit-when-idle                                              # real engines
```

An opt-in end-to-end test builds one small feature per engine on cheap models: `FACTORY_E2E_ENGINES=opencode,claude_code uv run pytest -m e2e` (skipped unless the gateway and credentials are present).

**Not yet verified against real models.** The adapter tests use stub CLIs and fixtures written by hand from each CLI's documented output, because no credentials were available; the live checks (`factory models refresh`, the end-to-end tests) need your gcloud login and the models enabled in Model Garden, and may turn up differences in the real CLIs' output. Sandboxes still have open internet access: the `allowed_hosts` list is not enforced until the egress proxy and internal job networks (M13).

## Connect real models

The factory talks to models only through its gateway, and every provider is one block in `factory.yaml`. The shipped defaults already name the providers (`gemini`, `groq`, `openrouter`, `lm_studio`, `ollama`, `vertex`); you add the keys and the model ids.

1. **Keys go in `.env`** (git-ignored), under the names the provider blocks expect: `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`. The definition only ever holds the variable *names*. After adding or changing a key, recreate the gateway so it picks it up: `docker compose up -d gateway` (a restart does not re-read `.env`).
2. **Add a model alias** in your definition repo, for example `gpt-oss-120b@groq: { provider: groq, upstream_id: "openai/gpt-oss-120b", strength: mid, price_in_per_mtok: 0, price_out_per_mtok: 0 }`. A model with no price is treated as unavailable, so set 0 for free tiers. Free tiers also have small per-minute token limits, so use them for light roles and as fallbacks, not for the build step.
3. **Local models:** LM Studio and Ollama run on your machine and are reached as `host.docker.internal`. Point the provider at your port (`base_url: http://host.docker.internal:4321/v1` for LM Studio on 4321).
4. **Point a profile at the aliases** and an app at the profile, then `factory def sync` and `factory models refresh`. `factory models list` shows, per model, whether it is available and whether it passed a real tool-call test. A model that cannot call tools is refused for the coding roles; a timeout or rate limit while probing is shown as unknown, not as "no tools".
5. **Keep reviewers and judges in a different model family** from the builder; the definition refuses a profile that does not.

## Laya training data

`factory laya import` takes rows you provide; `docs/laya-public-data.md` describes a builder that makes `kind`, `size` and `touches_ui` rows from public datasets (CC-BY, MIT and ODC-By only, with attribution and provenance recorded). `factory laya fit` trains on your own rows plus a sample of imported ones (at most 1,000 by default, `--max-rows`; about 0.6 s per row on a CPU) and creates a candidate head; it never switches anything on.

## Quickstart (local release)

1. Prerequisites: Docker Desktop, [uv](https://docs.astral.sh/uv/), and Google Application Default Credentials for Vertex AI: `gcloud auth application-default login`, then `gcloud auth application-default set-quota-project <your-project>`.
2. Create your definition repo: `uv run factory init ~/factory-definition`.
3. Copy `.env.example` to `.env` and set `FACTORY_DEFINITION_PATH` (the full path of that folder), `GOOGLE_CLOUD_PROJECT`, the `LITELLM_*` values, and `FACTORY_HOST_REPOS_ROOT`, the folder your repos live under, such as `/Users/you/work`. Docker Desktop must share that folder (Settings, Resources, File sharing).
4. `make up` builds the image, creates `secrets/pg_password` and `secrets/api_token` if missing, and starts the stack.
5. Open **`http://127.0.0.1:7300`** (or `http://localhost:7300`) and sign in. The password is `FACTORY_LOGIN_PASSWORD` from your `.env` (at least 8 characters; pick your own). If you have not set one, the password is the contents of `secrets/api_token`. You stay signed in for 30 days (`FACTORY_SESSION_DAYS`); sessions are stored on the server (as a hash, never the cookie's secret), end at sign out and when the login user, password or API token changes; five wrong passwords lock the form for a minute. Prefer the terminal? `make dashboard` (or `factory dashboard`) opens the dashboard already signed in.
6. Link the skill: `ln -s "$PWD/claude-skill/factory" ~/.claude/skills/factory`.
7. Run it on a repo: `factory run --repo ~/work/my-app --req ./requirement.md`.
8. `make down` stops the stack and keeps your data.

**Trusted repos only on a Mac.** Docker Desktop has no gVisor, so sandboxes share the Linux VM kernel. Run the factory only on repos you trust. The API needs the token even locally: every `/api` call without it returns 401.

Services: `postgres`, `migrate`, `gateway-db`, `gateway`, `gateway-edge`, `gateway-migrate`, `docker-proxy`, `api`, `ui` and `orchestrator`. Only three ports are published, all on `127.0.0.1` and all in the 7xxx range so they do not collide with other apps: `7300` is the **dashboard** (not 7000: macOS reserves that for AirPlay Receiver, which answers `localhost:7000` with a 403; its own `ui` container: a locked-down nginx that serves the static files and forwards only `/api` and `/login` to the API), `7080` is the **API** (for the CLI and other clients; it serves no dashboard in this stack) and `7400` is the model gateway. `make test-db` starts Postgres on `127.0.0.1:7432` for the `db` tests; `make image` builds the multi-arch image.

## Set up and run (development)

You need Docker, [uv](https://docs.astral.sh/uv/) and Python 3.12 or 3.13 (uv fetches it).

```bash
uv sync
mkdir -p secrets && openssl rand -hex 16 > secrets/pg_password
cp .env.example .env
make test-db   # Postgres on 127.0.0.1:7432 plus migrations

uv run factory init ~/factory-definition
uv run factory def validate ~/factory-definition
uv run factory db upgrade
uv run factory def sync ~/factory-definition
uv run factory apps list
uv run factory jobs create --app demo --title "Say hello" "Add a hello command"
uv run factory jobs list

# run the whole loop with a scripted fake agent (a reviewer asks for changes, then passes)
uv run factory worker --fake-script tests/fixtures/fake/rework.yaml --exit-when-idle
uv run factory jobs board --app demo
```

Run the tests:

```bash
uv run pytest                                   # no Docker or network needed
export FACTORY_TEST_DATABASE_URL="postgresql+psycopg://factory:$(cat secrets/pg_password)@127.0.0.1:7432/postgres"
uv run pytest -m db                             # needs the Postgres container
uv run pytest -m docker                         # builds and uses real containers; needs the runner images
```

## Next

The live pass: a capable model provider (Groq, OpenRouter, LM Studio with a larger model, or Vertex through gcloud), so a real job can reach build and merge; then the judges, a benchmark run and a first Laya fit run for real; a GitHub token or App for a real pull request; and your private registries. Deferred until you ask: the GCP deployment (M9) and the team-only parts of M13.
