# Laya router sidecar

Optional service (compose profile `laya`) that answers triage and tier questions. It sits on `core-net` only, publishes no port, and is never attached to a job network.

## Start it

```sh
# .env
LAYA_API_KEY=<random>                 # the router refuses to start without it
FACTORY_LAYA_URL=http://router:8000
docker compose --profile laya up -d --build router
docker compose ps router              # healthy
docker compose port router 8000       # prints nothing: no published port
```

Weights are baked into the image, so the router needs no network. Wire contract: laya-serve's `/health` (open), `/v1/systemone` and `/v1/systemone/batch` (bearer `LAYA_API_KEY`). `shim.py` adds the response header `x-factory-router-version: laya 0.4.31` and `head_version` on every answer (`<question>@v<n>` when a fitted head answers it, else null).

Fitted heads live in the `laya-heads` volume at `/heads`. To switch a question on, follow the benchmark steps in the main README (`factory laya evaluate`, then `factory laya activate`, then `mode: laya`).

## Fitted heads (M11.8)

`factory laya fit --question <q>` posts the labelled rows to `POST /v1/factory/fit`; the shim stores `/heads/<question>/v<n>/{train.csv,questions.json,meta.json,head.json}` as a candidate and activates nothing. `POST /v1/factory/heads/activate` (`question_id`, `version`, `head_sha256`) re-hashes `head.json` first and then writes `/heads/<question>/active`, which a restart reads back. `GET /v1/factory/heads` lists them. A batch request may carry `pins: {question_id: version}` to answer one question from a candidate without activating it. A head answers only while its stored question definition hash equals the one in the request; otherwise the question is served unfitted (`head_version: null`). All three routes need the bearer key. Mount `/heads` as a volume (compose does: `laya-heads`).

What a head is: laya 0.4.31 cannot train a classifier head on its encoder (that needs the separate `vegaml` engine, which is not in the locks), so a head is laya's own calibration fitted on the rows: a temperature plus a histogram-binning map (`Agent.fit_temperatures`, `Agent.fit_binning`), stored as `head.json` and applied per question around each prediction. It changes confidence and probabilities, never which option wins. `metrics.encoder_accuracy` is the unfitted encoder's accuracy on the training rows.

## Switching a question from shadow to laya

The switch is a definition change made by a person; code never makes it.

1. Evaluate a fitted head on a holdout suite: `factory laya evaluate --question <q> --suite <s>`
   (at least 2 repetitions; refused when the suite's tasks are in the head's training rows). It
   benchmarks `fixed` against `laya` pinned to that head version, prints the table, writes
   `var/laya/eval-<q>-<run8>.md` and ends with `switch: yes` or `switch: no (<reasons>)`. Quality
   holds when neither pass rate is lower and every task finished; laya must also be at least 10%
   cheaper per task.
2. `factory laya activate --question <q> --version <n>`. It is refused unless that version's
   recorded evaluation says `switch: yes`; it makes the version the only active head in the router
   and records who and when. Activating an older version is the rollback; no head is deleted.
   Refitting only creates a new candidate and never changes the active head.
3. Change the definition: for a tier set `profiles/<p>.yaml` `roles.<role>.mode: laya`; for
   triage set `triage.modes.<q>: laya`. Commit it on the personal instance, or open a PR on the
   team instance with the evaluation report attached (`DefinitionWriter` does either).

Switching back means setting `shadow` again, or activating an earlier head.

## Bump Laya, torch, the weights revision or the base image (together)

1. Edit `requirements.in` / `torch.in`, then run `router/regen-locks.sh` (`uv pip compile --universal --generate-hashes`; torch comes only from the PyTorch CPU index, everything else from PyPI, and the CUDA packages torch pulls on Linux are left out).
2. Change `revision=` in `docker/router.Dockerfile`, then rebuild `router/weights.sha256` from `https://huggingface.co/api/models/convaiinnovations/laya/revision/<sha>?blobs=true` (LFS files carry a `sha256`; for the small json files hash the downloaded file).
3. Change the `FROM python:3.11-slim-trixie@sha256:...` digest (`docker buildx imagetools inspect python:3.11-slim-trixie`).
4. `docker compose --profile laya build router`, then `uv run pytest -m docker tests/integration/test_router_image.py`.

## Derived locally (spike S7 not run)

`ref/spikes.md` does not exist, so these facts were read directly (2026-11-20, no credentials, no spend).

- Base image: `python:3.11-slim-trixie` index digest `sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535` (amd64 and arm64 both build from it).
- Weights: Hugging Face `convaiinnovations/laya` at commit `fd4f9dbcf4fde222fd43174bdd4c3b08b789e4e1`, per-file sha256 in `weights.sha256` (17 files, all `.safetensors` and `.json`; the checkpoint ships safetensors, so no pickle is ever fetched). Three checkpoints are baked (english at the repo root, `multilingual/`, `typed-decisions/`), about 2.3 GB. Laya's own `PINNED_REVISIONS` lists an older root commit (`55cf4c4e...`); we pin the current head instead.
- Package: `laya==0.4.31` (PyPI, Apache-2.0, pure Python wheel). Entry point `laya-serve = laya.serve:main`; `laya.serve.create_app()` returns a FastAPI app. Endpoints `/health`, `/v1/route`, `/v1/systemone`, `/v1/systemone/batch`. With `LAYA_API_KEY` set every route except `/health` needs `Authorization: Bearer <key>`, and the docs/openapi routes are off. A question needs `type`, `instructions` and `criteria` (a choice's options).
- Weight location: built-in checkpoint names are Hub repo ids, so offline loading would need a populated HF cache. Instead `LAYA_EXTRA_MODELS` re-points `english`, `multilingual` and `typed-decisions` at the baked local directories (`/opt/laya-weights`, `/opt/laya-weights/multilingual`, `/opt/laya-weights/typed-decisions`). `LAYA_MODELS=english` and `LAYA_DEFAULT_MODEL=english` preload only the English checkpoint.
- Version header: laya-serve has no such header, so `shim.py` wraps the app with one HTTP middleware (header on every response; `head_version` injected into the two answer routes). It runs `laya.serve.create_app()` as is, so the plan's own-implementation fallback was not needed.
- Not in laya-serve: there is no `fit` endpoint. `Agent.fit_temperatures` and `fit_binning` exist, but M11.8 must add the fit endpoint to `shim.py` itself.
- Memory: about 2.0 GiB resident with the English checkpoint, idle and after requests. `mem_limit` is 3g (2.0 x 1.5). Startup prints a warning that the English checkpoint ships a temperature outside [0.5, 5] for `choice:11+` and clamps it to 0.5, so confidence on 11-plus-option choices is uncalibrated (our questions have at most 6 options).
- Locks: `torch==2.14.0+cpu` for Linux and Windows from `https://download.pytorch.org/whl/cpu` (x86_64 and aarch64 wheels, cp311), plus `torch==2.14.0` for macOS; `requirements.lock` has laya[serve] and 50 or so pinned dependencies with hashes (transformers 5.23.0, huggingface-hub 2.4.0, numpy 2.4.6 or 2.5.5 by marker).

### Differences from the plan

- `compose.yaml` sets `LAYA_API_KEY: ${LAYA_API_KEY:-}`, not `${LAYA_API_KEY:?...}`. Compose checks required variables for every service, even in an inactive profile, so `:?` broke `docker compose config`, `up postgres` and `make test-db` for anyone not running Laya. The shim exits 1 with `LAYA_API_KEY is required` on an empty key instead.
- `compose.gcp.yaml` does not exist (M9 is deferred), so its router entry and test were skipped.
- laya-serve rejects (422) a question without `instructions`, and the triage questions in `models/triage.py` had none; M11.8 added one to each.
- M11.8: a head is a calibration (temperature and binning), not a trained classifier head, because laya 0.4.31's encoder agent has no `fit`.

## Importing synthetic rows

`factory laya import --question <q> --file <csv> --source <name> --origin <url> --licence <text>`
loads a CSV with the columns `state,<question>` (`state` a JSON object, every label one the
question serves). The whole file is checked first; a bad line refuses the file and stores nothing.
The same bytes (sha256) can be imported only once, under any name.

Synthetic rows are for a first fit only: together with your own rows they can reach the 50-row
floor before enough real jobs exist. The licence and origin are kept with the dataset
(`app.laya_datasets`). Imported rows are tagged `external:<name>` in exports, `factory laya fit`
and `factory laya status`. A head fitted on them is an ordinary candidate: it still needs a passing
evaluation on our own benchmark tasks before activation. Replace synthetic rows with real
outcomes as they accumulate.
