# Arize AX: verified working notes (2026-10-16)

Everything marked VERIFIED was run against our account today. Keys live in `.env` and are never printed.

```bash
cd ~/Documents/CODING/grid && set -a && source .env && set +a
S="$ARIZE_SPACE_ID"   # "Default Space" ($ARIZE_SPACE_ID)
```

## 1. Region / profile fix (VERIFIED)

- Our account is on the **global US host `api.arize.com`**. Any `--region` value breaks it:
  `us-east-1b` (what the `US` alias maps to) gives 404, `us-central-1a` has no DNS, and `eu-west-1a`/`ca-central-1a` give 401.
- Fix: set `region = ""` in `~/.arize/profiles/default.toml`. The hosts are already `api.arize.com` / `otlp.arize.com` / `flight.arize.com`.
  This is already applied. To recreate it, run `ax profiles create` interactively and leave region blank. Don't pass `--region`.
- Check: `ax spaces list` then `ax projects list --space "$S"`. The second command shows `grid-dispatcher` (APPLICATION).

## 2. Traces (VERIFIED: arriving)

`grid/sponsors/arize.py` calls `arize.otel.register(space_id, api_key, project_name)` without an endpoint, so it uses the default `otlp.arize.com`. That is correct for this account, so no fix is needed.

```bash
ax spans export grid-dispatcher --space "$S" -l 100 --start-time 2026-10-16T00:00:00Z --stdout > spans.json
ax traces export grid-dispatcher --space "$S" --start-time 2026-10-16T00:00:00Z --stdout
ax spans export grid-dispatcher --space "$S" --trace-id TRACE_ID --stdout
```

Seen today:
- `dispatcher` AGENT spans. These already carry `eval.constraint_check.*` and `eval.grounded_savings.*` (label/score/explanation) plus `grid.plan_id`, `grid.savings.tonight_usd`, `grid.violations` and `session.id`.
- `bedrock.converse` LLM spans (OpenInference, with input and output messages).
- `tool *` spans and FastAPI spans.

Span kind filter column: `attributes.openinference.span.kind = 'LLM'`.

## 3. Dataset, experiments and a code evaluator via the SDK (VERIFIED)

The Python SDK lives in the ax tool venv: `~/.local/share/uv/tools/arize-ax-cli/bin/python`. In the app, use `uv add arize`.
The working script is `/tmp/arize-scripts/smoke.py`. It created dataset `grid-smoke-ds` and experiments `smoke-v1-naive` (mean 0.67) and `smoke-v2-fixed` (mean 1.0). Its core:

```python
from arize import ArizeClient
from arize.experiments.evaluators.types import EvaluationResult
c = ArizeClient(api_key=os.environ["ARIZE_API_KEY"]); S = os.environ["ARIZE_SPACE_ID"]
c.datasets.create(name="grid-smoke-ds", space=S, force_http=True,
    examples=[{"question": "...", "expected": "pass"}, ...])     # no id/created_at fields
def task(dataset_row): return ...                                # one call per example
def matches_expected(output, dataset_row):                       # code evaluator
    ok = output == dataset_row["expected"]
    return EvaluationResult(score=float(ok), label="pass" if ok else "fail", explanation="...")
exp, df = c.experiments.run(name="v1", dataset="grid-smoke-ds", space=S, task=task,
                            evaluators=[matches_expected], force_http=True)
```

Gotchas:
- The evaluator must return a string **label**. A bare float gives `422 eval label column must be string`.
- Evaluator args are bound by name: `output`, `dataset_row`, `input`, `expected`, `metadata`.
- An **LLM judge with no AX integration**: write an evaluator function that calls Bedrock itself through boto3, using the AWS session creds in `.env`, and returns an `EvaluationResult`. It runs locally and the scores still upload into AX.

## 4. Compare two experiments (VERIFIED)

```bash
ax experiments list --dataset grid-smoke-ds --space "$S"
for e in smoke-v1-naive smoke-v2-fixed; do
  ax experiments export $e --dataset grid-smoke-ds --space "$S" --stdout > $e.json
  jq '[.[].additional_properties["eval.matches_expected.score"]] | add/length' $e.json
done
```

In the UI, open Datasets > grid-smoke-ds, tick both experiments, then click Compare. You get a side-by-side view with per-example eval diffs.

## 5. Server-side evaluators and tasks (from the skill docs, NOT run)

The space has no AI integrations yet: `ax ai-integrations list --space "$S"` returns nothing. Supported providers are `OPEN_AI`, `ANTHROPIC`, `AWS_BEDROCK`, `VERTEX_AI`, `GEMINI`, `AZURE_OPEN_AI`, `CUSTOM` (OpenAI-compatible) and `LITELLM`.

- **Bedrock needs an IAM role that Arize assumes.** Our STS session keys are not enough:
  `ax ai-integrations create --name bedrock --provider AWS_BEDROCK --provider-metadata '{"role_arn":"arn:aws:iam::ACCT:role/ArizeBedrockRole"}' --enable-default-models`
- **Mistral is the quicker judge:**
  `ax ai-integrations create --name mistral --provider CUSTOM --base-url https://api.mistral.ai/v1 --api-key "$MISTRAL_API_KEY" --model-name mistral-large-latest`

LLM-judge evaluator:

```bash
ax evaluators create-template-evaluator --name grounded --space "$S" --template-name grounded \
  --commit-message v1 --ai-integration-id INT_ID --model-name MODEL --include-explanations \
  --use-function-calling --classification-choices '{"grounded":1,"ungrounded":0}' \
  --template 'Input: {input}\nOutput: {output}\nIs every $ figure supported by the input? Answer grounded or ungrounded.'
```

Code evaluator. The `--code` is a class only; imports go in a separate file:

```bash
ax evaluators create-code-evaluator ...   # see .claude/skills/arize-evaluator/references/cli-reference.md
```
- The class must subclass `CodeEvaluator`.
- It must import exactly `from arize.experimental.datasets.experiments.evaluators.base import EvaluationResult, CodeEvaluator`.
- `evaluate(self, *, output=None, **kw)` needs named params, otherwise the run silently cancels at 0/0/0.

Run it on spans as a task:

```bash
ax tasks create-evaluation --name grounded-backfill --task-type TEMPLATE_EVALUATION \
  --project grid-dispatcher --space "$S" \
  --evaluators '[{"evaluator_id":"EVAL_ID","column_mappings":{"input":"attributes.input.value","output":"attributes.output.value"}}]' \
  --query-filter "attributes.openinference.span.kind = 'AGENT'" --no-continuous
ax tasks trigger-run TASK_ID --data-start-time ... --data-end-time ... --max-spans 100 --wait
```

Caveat: the eval index lags the trace store by **1-2 h**, so a run over spans from the last hour scores 0 spans. With a 3:30 deadline, show the span evals we already attach plus the SDK experiments instead.
