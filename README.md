# software-factory-platform

A platform that turns a requirement into reviewed, merged code. For each feature it runs spec, build, verify, four independent reviews, approve and deliver, using coding agents on any model provider. It runs on your machine in Docker, many apps and features at a time.

**Status: under construction.** Milestones M1 to M6 are built: foundation, the job loop, roles and contracts, sandboxes with the verify gate and local git, the model gateway with cost control, and the coding-agent adapters. Still to come: the local release with the API, dashboard and point-and-run (M7) and GitHub (M8). See [progress.md](progress.md) for the step-by-step status.

## What works now (M1 and M2)

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

Run a real job (needs the gateway running and the Claude and Gemini models you use enabled):

```bash
docker compose --profile build build runner runner-web
uv run factory gateway render && docker compose up -d gateway gateway-edge
uv run factory jobs create --app demo --profile team-gemini "Add a hello command"   # OpenCode
uv run factory jobs create --app demo --profile claude-vertex "Add a hello command" # Claude Code
uv run factory worker --exit-when-idle                                              # real engines
```

An opt-in end-to-end test builds one small feature per engine on cheap models: `FACTORY_E2E_ENGINES=opencode,claude_code uv run pytest -m e2e` (skipped unless the gateway and credentials are present).

**Not yet verified against real models.** The adapter tests use stub CLIs and fixtures written by hand from each CLI's documented output, because no credentials were available; the live checks (`factory models refresh`, the end-to-end tests) need your gcloud login and the models enabled in Model Garden, and may turn up differences in the real CLIs' output. Sandboxes still have open internet access: the `allowed_hosts` list is not enforced until the egress proxy and internal job networks (M13).

## Set up and run

You need Docker, [uv](https://docs.astral.sh/uv/) and Python 3.12 or 3.13 (uv fetches it).

```bash
uv sync
mkdir -p secrets && openssl rand -hex 16 > secrets/pg_password
cp .env.example .env
docker compose up -d --wait postgres

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
export FACTORY_TEST_DATABASE_URL="postgresql+psycopg://factory:$(cat secrets/pg_password)@127.0.0.1:5432/postgres"
uv run pytest -m db                             # needs the Postgres container
uv run pytest -m docker                         # builds and uses real containers; needs the runner images
```

## Next

M7: the API, dashboard and CLI, `factory run --repo ... --req ...` (point at a repo with a requirement), the private package mirror, and a one-command local release. Then GitHub (M8).
