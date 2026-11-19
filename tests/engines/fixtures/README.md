# Engine fixtures

Recorded CLI output that the stub harness (`tests/fakes/stub_sandbox.py`, `tests/engines/stubs/stubcli`)
replays. The stub CLIs `claude`, `opencode`, `gemini` and `socat` are symlinks to `stubcli`.

Status: M0's spikes have not been run and no model credentials were available, so every fixture here
is WRITTEN BY HAND from the CLI's documented output format. None was recorded against a real CLI.
Re-record them inside the runner image against the gateway with a cheap alias once M0 has run.

Pinned versions (come from research notes; M0 must confirm): OpenCode 1.19.5, Claude Code 2.1.322,
Gemini CLI 0.68.0 (see the version notes in the M6.1 runner image).

## claude_code
- `success.jsonl`: hand-written, 2026-11-19. To record:
  `claude -p --output-format stream-json --verbose "<prompt>"` in the runner image with
  `ANTHROPIC_BASE_URL` set to the gateway and `ANTHROPIC_API_KEY` set to a run key for a cheap alias.

- `resume.jsonl`, `max_turns.jsonl`, `budget.jsonl`, `swapped_model.jsonl`, `classifier_fallback.jsonl`, `not_enabled.jsonl`: hand-written 2026-11-19 from the documented stream-json result format (`modelUsage` keyed by model, `total_cost_usd` cumulative on resume). Re-record once M0 has run.

## gemini_cli
Pinned Gemini CLI 0.68.0 (M6.1 pin; M0 must confirm). All five files are hand-written, 2026-11-19, from
the documented `-o json` shape (`response`, `stats.models.<name>.tokens`, `error`). `session_id` and
`init.model` are placed at the top level by assumption. To record (inside the runner image, through the
localhost forwarder, cheap alias): `gemini -p "..." -m <alias> -o json --approval-mode yolo --skip-trust`
with `GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:<port>` and `GEMINI_API_KEY=<run key>`. `fallback.json`
and `init_swap.json` are edited copies of a recording (a second model in `stats.models`; a different
`init.model`). `turn_limit.json` goes with exit 53, `rate_limit.json` with exit 1.

## opencode
Added by M6.4 with the same rule: pinned version, date, exact recording command.

`swapped_*` fixtures are copies of a recorded fixture with the model name edited, to test fallback
detection.

## direct
- `direct/*.json`: hand-written 2026-11-19 from the documented OpenAI chat-completions and Anthropic
  messages response formats (LiteLLM unified endpoints); `budget_error.json` follows LiteLLM's budget
  error body. Not recorded against a real gateway. Re-record once M0 has run. `shot.png` is a 1x1 PNG.
