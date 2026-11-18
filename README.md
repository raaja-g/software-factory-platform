# software-factory-platform

A platform that turns a requirement into reviewed, merged code. For each feature it runs spec, build, verify, four independent reviews, approve and deliver, using coding agents on any model provider. It runs on your machine in Docker, many apps and features at a time.

**Status: under construction.** Milestones M1 (foundation), M2 (the job loop) and M3 (roles and contracts) are done, along with the first half of M5 (models and cost). See [progress.md](progress.md) for the step-by-step status.

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

Still fake: no real coding agent runs yet. Sandboxes, the verify gate and local git come next (M4), then the rest of M5 and the engine adapters (M6).

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
```

## Next

M4: hardened Docker sandboxes, the verify gate, and local git (including delivery into a folder on your Mac). Then the gateway services and run keys (rest of M5), and the coding-agent adapters (M6).
