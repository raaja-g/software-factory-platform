# Laya training rows from public datasets

`src/swfactory/laya/public_data.py` builds `state,<question>` CSVs for `factory laya import` from public Hugging Face datasets. It makes no database call, needs no token and installs no `datasets` library: it reads the hub API and the datasets-server `/rows` API with plain `httpx`, one request per second at most, retrying 429s.

The CSVs and provenance files are local-only: `var/` is git-ignored and they are never committed or pushed.

Only `kind`, `size` and `touches_ui` have an honest public source. **`risk` and every `tier.*` question have none** (no public dataset says how risky a change is for this factory, or which model tier suffices for its tasks), so the builder refuses them and nothing is invented. Label those from your own jobs.

## Rebuild

```sh
uv run python -m swfactory.laya.public_data --question all --built-on 2026-11-20
```

| Option | Meaning |
| --- | --- |
| `--question kind\|size\|touches_ui\|all` | which CSVs to build (default `all`) |
| `--out-dir DIR` | default `var/laya/public` (git-ignored) |
| `--limit-per-dataset N` | most source rows read per dataset (default 5000). A split that fits is read whole; a larger one is sampled in seeded random windows |
| `--seed N` | default 0; drives window choice and balancing |
| `--built-on YYYY-MM-DD` | written into the provenance; the code never reads the clock |
| `--max-ratio N` | cap every class at N times the smallest class that has 10 rows (default 3; `0` turns balancing off) |
| `--include-synthetic` | also read SWE-smith (synthetic issues); off by default |
| `--keep-repo-name` | put the source repo in `app` instead of `unknown` |
| `--offline-fixture DIR` | read `<owner>__<name>.json` fixtures instead of the network (used by the unit tests) |
| `--min-interval S` | seconds between requests (default 1.0) |

The same arguments and the same upstream revisions give the same bytes. The run that produced the numbers below used `--question all --seed 0 --limit-per-dataset 6600 --built-on 2026-11-20`. Reading a few thousand rows per dataset takes about 10 minutes for SWE-rebench (about 0.5 MB per 50 rows) and a couple of minutes for the others.

Each question writes into `var/laya/public/`:

- `<q>.csv`: every dataset used, balanced. Import this one **or** the per-dataset files, never both.
- `<q>.<source>.csv`: the rows of that one dataset, so each import carries its own licence and origin.
- `<q>.provenance.json`: arguments, build date, state shape, the licence, URL, revision (the hub commit sha), rows read, rows dropped by reason, rows used per class and the label rule of every dataset, plus the sha256 and ready-made `--source/--origin/--licence` values of every file, and the datasets skipped and why.

Every file is parsed with the importer's own `read_import_file` before the build finishes, so a file that would be refused never reaches disk silently.

## What a row looks like

`state` is built by the repo's own `triage_state(job, app, max_tokens=512)`, so it has the keys of a real job (`title`, `feature`, `app`, `stack`, `labels`, `ui_globs`) and the same trim: the feature text is halved until the JSON estimate is under 512 tokens and ends in ` [omitted]`. Title and feature come from the issue or PR title and body (HTML comments removed), or for the SWE datasets from the first line and the rest of the issue text. Fixed values:

- `app`: `unknown` (what a job with no repo URL shows). Using the repo name would let the model learn the repo, not the change: in the first build 84 of 106 `refactor` rows were Kubernetes. `--keep-repo-name` restores it.
- `stack`: `unknown`; `labels`: `[]` (the source labels are the answer, so they must not appear in the state); `ui_globs`: the app default (a test checks it equals `AppDef`'s).

**Secrets.** A row whose title or body contains an email address, an API key or token pattern (the repo's `swfactory.logging.redact` patterns plus AWS, Slack, Stripe-style keys and `password=` assignments) is dropped, never redacted and kept. GitHub `@handles`, names and links in the text are not removed: they are public, but you may not want them in a training set. (Re-running the detector on the JSON `state` shows false hits such as `\n@dataclass.x`; these are decorators after an escaped newline, not addresses, and the check that counts runs on the text.)

**Dedupe.** Exact states are kept once; a state that appears with two labels is dropped; at most 3 rows per digit-normalised title and label (stops runs of `Bump x from 1.2 to 1.3`).

**Balancing.** Each class is capped at 3 times the smallest class with at least 10 rows (the fit minimum), picked with a seeded shuffle. A class under 10 rows is kept whole and reported as a warning.

## Datasets and label rules

All licences were read from the dataset card (`cardData.license` on the hub API) at build time; a dataset whose card has no licence, or one outside CC-BY-4.0, MIT, Apache-2.0, ODC-By and CC0, is skipped by the code and listed in `skipped_datasets`.

| Dataset | Licence | Revision used | Used for |
| --- | --- | --- | --- |
| [Raibek/requests-github-issues](https://huggingface.co/datasets/Raibek/requests-github-issues) | CC-BY-4.0 | `c2a2325c4c1b` | kind |
| [open-index/open-github-issues](https://huggingface.co/datasets/open-index/open-github-issues) (`issues` config) | ODC-By-1.0 | `f7bdbac081d5` | kind, size |
| [SWE-Gym/SWE-Gym](https://huggingface.co/datasets/SWE-Gym/SWE-Gym) | MIT | `bb94ed9e39bb` | size, touches_ui |
| [SWE-bench/SWE-bench_Multilingual](https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual) | MIT | `846e647b9f33` | size, touches_ui |
| [nebius/SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench) (`test` split) | CC-BY-4.0 | `89cdfbab4ab1` | size, touches_ui |
| [SWE-bench/SWE-smith](https://huggingface.co/datasets/SWE-bench/SWE-smith) | MIT | read at build | size, touches_ui, **synthetic, off unless `--include-synthetic`** |

**Not used: `princeton-nlp/SWE-bench_Verified`** (and its `SWE-bench/` mirror). Its card names no licence, so by the rule above it is out, although it is the only source of human difficulty buckets. If you decide the SWE-bench repository's MIT licence covers it, adding a `DatasetSpec` with `<15 min fix` to S, `15 min - 1 hour` to M, `1-4 hours` and `>4 hours` to L is a few lines, but that is your call, not the builder's.

Label rules (the exact tables are `KIND_PHRASES`, `SIZE_PR_LABELS`, `UI_EXTENSIONS` in `public_data.py`):

- **kind** (Raibek, open-index): the GitHub labels of the issue or PR. A label is normalised (lower case, `/ : _ -` to spaces, one leading `kind`, `type`, `C`, `T`, `area`, `pr` word removed) and matched **exactly** against a phrase table: bug, bug report, bugfix, defect, regression, crash to `bugfix`; feature, feature request, new feature, enhancement, improvement to `feature`; docs, documentation to `docs`; refactor, refactoring, cleanup, tech debt, code quality to `refactor`; ci, ci/cd, build, build system, dependencies, deps, infra, infrastructure, devops, packaging, github actions, docker to `infra`. No fuzzy matching, so `Question/Not a bug` is not a bug. Unknown labels are ignored; no known label drops the row (this removes about 95% of rows); two different kinds drop the row.
- **size from PR labels** (open-index, pull requests only): the bot label `size/XXS|XS|S` to S, `size/M` to M, `size/L|XL|XXL` to L. These are Kubernetes-style buckets of lines changed (roughly S under 30 lines, M 30-99, L 100 or more).
- **size from patches** (SWE-Gym, SWE-bench Multilingual, SWE-rebench, SWE-smith): from the gold patch: S = one file and at most 20 added or removed lines; L = 6 or more files or 300 or more lines; M = everything else.
- **touches_ui from patches** (same four): A if any changed file outside `test`, `tests`, `__tests__`, `doc`, `docs`, `documentation` directories ends in `.css .scss .sass .less .html .htm .jsx .tsx .vue .svelte` or sits under a `templates/` or `static/` directory; otherwise B. `.js` and `.ts` do not count (they are as often Node code as browser code).

## What was built (2026-11-20, seed 0)

The owner asked for 1,000+ rows per question. `kind` (1,039) and `size` (9,747) reach it; `touches_ui` does not: only 66 rows (0.7% of the 9,105 usable SWE rows) are UI patches, so with the 3x cap it has 264 rows (66 A, 198 B), above the 50-row fit floor but far below 1,000. SWE-Bench Verified (500 rows at most) is unlicensed and not used.

**kind**: 1039 rows {'bugfix': 318, 'docs': 164, 'feature': 318, 'infra': 133, 'refactor': 106} (before balancing {'bugfix': 1069, 'docs': 164, 'feature': 466, 'infra': 133, 'refactor': 106})

| File | Rows | Per class | Licence |
| --- | --- | --- | --- |
| `kind.csv` | 1039 | {'bugfix': 318, 'docs': 164, 'feature': 318, 'infra': 133, 'refactor': 106} | mixed: CC-BY-4.0, ODC-By-1.0 |
| `kind.pub-requests-issues.csv` | 125 | {'bugfix': 32, 'docs': 27, 'feature': 51, 'infra': 15} | CC-BY-4.0 |
| `kind.pub-open-github-issues.csv` | 914 | {'bugfix': 286, 'docs': 137, 'feature': 267, 'infra': 118, 'refactor': 106} | ODC-By-1.0 |

Read and dropped (reason: count): Raibek/requests-github-issues read 6528, {'ambiguous_label': 6, 'near_duplicate': 9, 'no_label': 6249, 'secret_or_email': 9}; open-index/open-github-issues read 6600, {'ambiguous_label': 57, 'near_duplicate': 10, 'no_label': 4811, 'secret_or_email': 39}.


**size**: 9747 rows {'L': 1843, 'M': 5432, 'S': 2472} (before balancing {'L': 1843, 'M': 5432, 'S': 2472})

| File | Rows | Per class | Licence |
| --- | --- | --- | --- |
| `size.csv` | 9747 | {'L': 1843, 'M': 5432, 'S': 2472} | mixed: CC-BY-4.0, MIT, ODC-By-1.0 |
| `size.pub-open-github-issues.csv` | 673 | {'L': 197, 'M': 139, 'S': 337} | ODC-By-1.0 |
| `size.pub-swe-gym.csv` | 2363 | {'L': 164, 'M': 1477, 'S': 722} | MIT |
| `size.pub-swe-bench-multilingual.csv` | 298 | {'L': 13, 'M': 129, 'S': 156} | MIT |
| `size.pub-swe-rebench.csv` | 6413 | {'L': 1469, 'M': 3687, 'S': 1257} | CC-BY-4.0 |

Read and dropped (reason: count): open-index/open-github-issues read 6600, {'duplicate': 1, 'no_label': 2775, 'not_a_pull_request': 3088, 'secret_or_email': 63}; SWE-Gym/SWE-Gym read 2438, {'conflicting_label': 7, 'duplicate': 20, 'secret_or_email': 30}; SWE-bench/SWE-bench_Multilingual read 300, {'secret_or_email': 2}; nebius/SWE-rebench read 6600, {'conflicting_label': 24, 'duplicate': 61, 'secret_or_email': 89}.


**touches_ui**: 264 rows {'A': 66, 'B': 198} (before balancing {'A': 66, 'B': 9039})

| File | Rows | Per class | Licence |
| --- | --- | --- | --- |
| `touches_ui.csv` | 264 | {'A': 66, 'B': 198} | mixed: CC-BY-4.0, MIT |
| `touches_ui.pub-swe-gym.csv` | 57 | {'A': 3, 'B': 54} | MIT |
| `touches_ui.pub-swe-bench-multilingual.csv` | 9 | {'A': 3, 'B': 6} | MIT |
| `touches_ui.pub-swe-rebench.csv` | 198 | {'A': 60, 'B': 138} | CC-BY-4.0 |

Read and dropped (reason: count): SWE-Gym/SWE-Gym read 2438, {'duplicate': 27, 'secret_or_email': 30}; SWE-bench/SWE-bench_Multilingual read 300, {'secret_or_email': 2}; nebius/SWE-rebench read 6600, {'duplicate': 85, 'secret_or_email': 89}.


## Attribution you must keep

CC-BY-4.0 and ODC-By require credit, a link to the licence and a note that the data was changed. Keep this block (or the provenance files) with any copy of the CSVs or any model card for a head fitted on them:

- Issue and PR titles and bodies from **Raibek/requests-github-issues** (CC-BY-4.0, <https://creativecommons.org/licenses/by/4.0/>), dataset by Raibek, derived from GitHub issues of psf/requests. Changed: filtered, mapped to `kind` labels, trimmed.
- Issue and PR titles and bodies from **open-index/open-github-issues** (ODC-By 1.0, <https://opendatacommons.org/licenses/by/1-0/>), by open-index, built from the GitHub REST and GraphQL APIs. Changed: sampled, filtered, mapped to `kind` and `size` labels, trimmed.
- Issue text and patch statistics from **nebius/SWE-rebench** (CC-BY-4.0), Nebius. Changed: reduced to a title, trimmed text and a label computed from the patch.
- Issue text and patch statistics from **SWE-Gym/SWE-Gym** and **SWE-bench/SWE-bench_Multilingual** (MIT, <https://opensource.org/license/mit>); keep the copyright notices of those datasets.
- If used, **SWE-bench/SWE-smith** (MIT), synthetic.

The datasets' own licences cover the dataset as compiled. The issue and PR text inside them was written by many people under the terms of each upstream repository; the datasets-server and card licences do not change that. Treat the rows as internal training data and do not republish the CSVs without checking the upstream repositories you care about.

## Import

`factory laya import --question <q> --file <csv> --source <name> --origin <url> --licence <licence>`: `--source` is the dataset name stored with the rows (lower case letters, digits, `.`, `_`, `-`, at most 40 characters; unique per question; shown as `external:<name>`), `--origin` is where the data came from, `--licence` is stored verbatim. The same file bytes can be imported once, under any name, so import the per-dataset files **or** the combined file.

Per dataset (each carries one licence):

```sh
factory laya import --question kind --file var/laya/public/kind.pub-requests-issues.csv \
  --source pub-requests-issues --origin https://huggingface.co/datasets/Raibek/requests-github-issues --licence CC-BY-4.0
factory laya import --question kind --file var/laya/public/kind.pub-open-github-issues.csv \
  --source pub-open-github-issues --origin https://huggingface.co/datasets/open-index/open-github-issues --licence ODC-By-1.0

factory laya import --question size --file var/laya/public/size.pub-open-github-issues.csv \
  --source pub-open-github-issues --origin https://huggingface.co/datasets/open-index/open-github-issues --licence ODC-By-1.0
factory laya import --question size --file var/laya/public/size.pub-swe-gym.csv \
  --source pub-swe-gym --origin https://huggingface.co/datasets/SWE-Gym/SWE-Gym --licence MIT
factory laya import --question size --file var/laya/public/size.pub-swe-bench-multilingual.csv \
  --source pub-swe-bench-multilingual --origin https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual --licence MIT
factory laya import --question size --file var/laya/public/size.pub-swe-rebench.csv \
  --source pub-swe-rebench --origin https://huggingface.co/datasets/nebius/SWE-rebench --licence CC-BY-4.0

factory laya import --question touches_ui --file var/laya/public/touches_ui.pub-swe-gym.csv \
  --source pub-swe-gym --origin https://huggingface.co/datasets/SWE-Gym/SWE-Gym --licence MIT
factory laya import --question touches_ui --file var/laya/public/touches_ui.pub-swe-bench-multilingual.csv \
  --source pub-swe-bench-multilingual --origin https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual --licence MIT
factory laya import --question touches_ui --file var/laya/public/touches_ui.pub-swe-rebench.csv \
  --source pub-swe-rebench --origin https://huggingface.co/datasets/nebius/SWE-rebench --licence CC-BY-4.0
```

Or one file per question, whose `--licence` has to say it is mixed (the exact strings are in each `<q>.provenance.json` under `files`):

```sh
factory laya import --question kind --file var/laya/public/kind.csv --source public-kind \
  --origin "https://huggingface.co/datasets/Raibek/requests-github-issues, https://huggingface.co/datasets/open-index/open-github-issues" \
  --licence "mixed: CC-BY-4.0, ODC-By-1.0"
```

(`size.csv` and `touches_ui.csv` likewise; take their `--origin` and `--licence` from the provenance file.) With `--include-synthetic` the SWE-smith rows are in a separate `<q>.pub-swe-smith-synthetic.csv`: import it only if you want them, and prefer to leave it out of the combined file by not using that file.

## Caveats

- **Domain shift.** These are public open-source issues and PRs (Kubernetes, CPython, ClickHouse, Rust, TiDB, PyTorch and similar; Python libraries for the SWE sets), not this factory's job descriptions. Laya trained on them learns what those maintainers write. They are for a first fit that reaches the 50-row floor, not a replacement for your own outcomes, and a head built on them still has to pass `factory laya evaluate` on your own benchmark before activation.
- **Label proxies.** `kind` is what a maintainer's label says, in repos that label consistently, and only about 5% of rows carry a usable label. `size` from patches measures the diff (files, lines), not effort, and the issue text rarely says how big the fix was; the PR-label rows are lines changed by a bot. `touches_ui` from paths is a weak proxy: Python-heavy sources, `.js`/`.ts` not counted, and a UI file touched is not the same as "changes browser behaviour". The size rules pool two meanings (lines in PR labels, files plus lines in patches) into one label.
- **Class balance.** `kind` is capped at 3 times its smallest class (`refactor`, mostly Kubernetes `kind/cleanup`); `size` is mostly M; `touches_ui` is the weak one: UI patches are under 1% of the SWE sources, so the balanced set is small. Pass `--max-ratio 0` to keep every row (about 9,000 `touches_ui` rows, nearly all B) if you would rather weight classes yourself.
- **Leaky text.** Issue templates (`**What happened**`) and conventional-commit prefixes in PR titles correlate with `kind` more than the change does. Real job descriptions may not have them.
- **Synthetic rows.** SWE-smith issues are written by an LLM for bugs that were injected on purpose, and its patches are tiny: it adds almost no UI and no L rows, which is why it is opt-in and kept in its own file.
- **No risk, no tiers.** See the top: nothing public labels them honestly.
