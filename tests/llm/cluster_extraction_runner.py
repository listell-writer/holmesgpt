"""Standalone runner for the cluster extraction eval.

Iterates every (case, model, variant) combination, calls the model directly via
litellm, and writes:
  - <out>.jsonl  : one row per run with full detail
  - <out>.md     : human-readable markdown summary

Usage:
    poetry run python tests/llm/cluster_extraction_runner.py \\
        --output tests/llm/fixtures/test_cluster_extraction/_reports/2026-04-29

Env vars:
    CLUSTER_EXTRACTION_MODELS  comma-separated litellm model IDs (overrides the
                               defaults in test_cluster_extraction.py)
    AWS_*                      standard Bedrock credentials when models use the
                               bedrock/ prefix

The pytest test (tests/llm/test_cluster_extraction.py) is still the ground
truth for what each run does - this script imports its helpers directly so
the eval logic stays in one place.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.llm.test_cluster_extraction import (  # noqa: E402
    PROMPT_VARIANTS,
    ClusterExtractionCase,
    _build_prompt,
    _get_models,
    _load_cases,
    _normalize_answer,
    evaluate_answer,
)


# Approximate Bedrock on-demand pricing per 1M tokens (USD).
# Used only for the rolled-up cost column in the markdown report - replace with
# AWS Cost Explorer numbers if you need precise figures.
_BEDROCK_PRICING_PER_M_TOKENS = {
    "claude-opus-4-7": (15.00, 75.00),
    "claude-opus-4-6": (15.00, 75.00),
    "claude-opus-4-5": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def _resolve_pricing(model_id: str) -> Optional[Tuple[float, float]]:
    lowered = model_id.lower()
    for key, price in _BEDROCK_PRICING_PER_M_TOKENS.items():
        if key in lowered:
            return price
    return None


def _bedrock_model_id(model: str) -> str:
    """Strip the leading 'bedrock/' if present so we can pass to boto3 Converse."""
    return model[len("bedrock/") :] if model.startswith("bedrock/") else model


_bedrock_client = None


def _get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        region = os.environ.get("AWS_REGION_NAME") or os.environ.get(
            "AWS_REGION", "us-east-1"
        )
        _bedrock_client = boto3.client("bedrock-runtime", region_name=region)
    return _bedrock_client


def _expected_for(case: ClusterExtractionCase, variant: str) -> Optional[str]:
    return case.expected_for(variant)


def _run_one(
    case: ClusterExtractionCase, model: str, variant: str
) -> Dict[str, Any]:
    prompt = _build_prompt(
        conversation_history=case.conversation_history,
        available_clusters=case.available_clusters,
        channel_id=case.channel_id,
        custom_extraction_prompt=case.custom_extraction_prompt,
        include_cluster_list=(variant == "with_cluster_list"),
    )

    started = time.perf_counter()
    try:
        client = _get_bedrock_client()
        # Note: do not pass `temperature` - newer Claude models (Opus 4.7+) reject
        # it with a ValidationException. maxTokens alone is enough to force a
        # short, structured answer for this eval.
        response = client.converse(
            modelId=_bedrock_model_id(model),
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 64},
        )
        latency_ms = response.get("metrics", {}).get("latencyMs") or int(
            (time.perf_counter() - started) * 1000
        )
        raw = ""
        for block in response["output"]["message"].get("content", []):
            if "text" in block:
                raw += block["text"]
        usage = response.get("usage", {}) or {}
        prompt_tokens = usage.get("inputTokens")
        completion_tokens = usage.get("outputTokens")
        pricing = _resolve_pricing(model)
        if pricing and prompt_tokens is not None and completion_tokens is not None:
            in_rate, out_rate = pricing
            cost_usd = (prompt_tokens * in_rate + completion_tokens * out_rate) / 1e6
        else:
            cost_usd = None
        error = None
    except Exception as e:
        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = ""
        prompt_tokens = None
        completion_tokens = None
        cost_usd = None
        error = f"{type(e).__name__}: {e}"

    answer = _normalize_answer(raw)
    expected = _expected_for(case, variant)

    if error is not None:
        passed = False
        fail_reason = "error"
    else:
        passed, fail_reason = evaluate_answer(case, variant, answer)

    return {
        "case_id": case.id,
        "case_description": case.description.strip(),
        "case_tags": case.tags,
        "model": model,
        "variant": variant,
        "expected": expected,
        "forbidden_clusters": list(case.forbidden_clusters),
        "raw_answer": raw,
        "normalized_answer": answer,
        "passed": passed,
        "fail_reason": fail_reason,
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": cost_usd,
        "error": error,
    }


def _aggregate(rows: List[Dict[str, Any]]):
    by_pair: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_pair[(r["model"], r["variant"])].append(r)
    return by_pair


def _fmt_pct(passed: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{passed}/{total} ({100.0 * passed / total:.0f}%)"


def _fmt_latency(values: List[int]) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "n/a"
    return f"p50 {int(statistics.median(values))}ms  p95 {int(_p95(values))}ms"


def _p95(values: List[int]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, int(round(0.95 * (len(s) - 1))))
    return float(s[k])


def _fmt_cost(values: List[Optional[float]]) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "n/a"
    return f"${sum(values):.4f}"


def _write_markdown(
    rows: List[Dict[str, Any]],
    cases: List[ClusterExtractionCase],
    models: List[str],
    out_path: Path,
) -> None:
    by_pair = _aggregate(rows)
    case_ids = [c.id for c in cases]
    case_by_id = {c.id: c for c in cases}

    lines: List[str] = []
    lines.append("# Cluster Extraction Eval Report")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    lines.append("")
    lines.append(
        f"- **Cases:** {len(cases)}  &nbsp;&nbsp; **Models:** {len(models)}  "
        f"&nbsp;&nbsp; **Variants:** {len(PROMPT_VARIANTS)}  "
        f"&nbsp;&nbsp; **Total runs:** {len(rows)}"
    )
    lines.append("")

    lines.append("## Summary by model × variant")
    lines.append("")
    lines.append(
        "| Model | Variant | Pass rate | Bias fails | Misses | Errors | Latency | Total cost |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for model in models:
        for variant in PROMPT_VARIANTS:
            bucket = by_pair.get((model, variant), [])
            total = len(bucket)
            passed = sum(1 for r in bucket if r["passed"])
            bias = sum(1 for r in bucket if r.get("fail_reason") == "bias")
            misses = sum(1 for r in bucket if r.get("fail_reason") == "miss")
            errors = sum(1 for r in bucket if r["error"])
            latencies = [r["latency_ms"] for r in bucket if r["error"] is None]
            costs = [r["cost_usd"] for r in bucket]
            lines.append(
                f"| `{model}` | `{variant}` | {_fmt_pct(passed, total)} | "
                f"{bias} | {misses} | {errors} | "
                f"{_fmt_latency(latencies)} | {_fmt_cost(costs)} |"
            )
    lines.append("")

    lines.append("## Per-case results")
    lines.append("")
    header = "| Case |"
    sep = "|---|"
    for model in models:
        for variant in PROMPT_VARIANTS:
            header += f" {model.split('/')[-1].split('-')[1] if '-' in model else model[:6]}/{variant[:4]} |"
            sep += "---|"
    lines.append(header)
    lines.append(sep)
    for case_id in case_ids:
        row = f"| `{case_id}` |"
        for model in models:
            for variant in PROMPT_VARIANTS:
                match = next(
                    (
                        r
                        for r in rows
                        if r["case_id"] == case_id
                        and r["model"] == model
                        and r["variant"] == variant
                    ),
                    None,
                )
                if match is None:
                    cell = "—"
                elif match["error"]:
                    cell = "⚠️"
                elif match["passed"]:
                    cell = "✅"
                elif match.get("fail_reason") == "bias":
                    cell = f"🚨 `{match['normalized_answer'] or '∅'}`"
                else:
                    cell = f"❌ `{match['normalized_answer'] or '∅'}`"
                row += f" {cell} |"
        lines.append(row)
    lines.append("")

    lines.append("## Failures and errors")
    lines.append("")
    failed = [r for r in rows if not r["passed"]]
    if not failed:
        lines.append("_None._")
    else:
        for r in failed:
            case = case_by_id[r["case_id"]]
            tag = {
                "bias": "🚨 BIAS",
                "miss": "❌ MISS",
                "error": "⚠️ ERROR",
            }.get(r.get("fail_reason", ""), "❌")
            lines.append(
                f"### {tag} `{r['case_id']}` — `{r['model']}` — `{r['variant']}`"
            )
            lines.append("")
            lines.append(f"**Description:** {case.description.strip()}")
            lines.append("")
            lines.append(f"- **Expected:** `{r['expected']}`")
            if r.get("forbidden_clusters"):
                lines.append(
                    f"- **Forbidden:** `{', '.join(r['forbidden_clusters'])}`"
                )
            lines.append(
                f"- **Got (normalized):** `{r['normalized_answer'] or '∅'}`"
            )
            if r["error"]:
                lines.append(f"- **Error:** `{r['error']}`")
            else:
                quoted = (r["raw_answer"] or "").replace("\n", " \\n ")[:200]
                lines.append(f"- **Raw answer:** `{quoted}`")
            lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        help="Output path stem (without extension). Writes <stem>.jsonl and <stem>.md.",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=None,
        help="Run only the named case (id). May be passed multiple times.",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=None,
        help="Run only the named model. May be passed multiple times. Overrides "
        "CLUSTER_EXTRACTION_MODELS.",
    )
    parser.add_argument(
        "--variant",
        action="append",
        default=None,
        choices=PROMPT_VARIANTS,
        help="Run only the named variant. May be passed multiple times.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke mode: first case only, first model only, current variant only.",
    )
    args = parser.parse_args()

    cases = _load_cases()
    if args.case:
        cases = [c for c in cases if c.id in set(args.case)]
    models = args.model or _get_models()
    variants = args.variant or list(PROMPT_VARIANTS)

    if args.smoke:
        cases = cases[:1]
        models = models[:1]
        variants = ["current"]

    if not cases or not models or not variants:
        print("Nothing to run.", file=sys.stderr)
        return 2

    out_stem = Path(args.output)
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_stem.with_suffix(".jsonl")
    md_path = out_stem.with_suffix(".md")

    total = len(cases) * len(models) * len(variants)
    print(
        f"Running {total} combinations: {len(cases)} cases × {len(models)} models "
        f"× {len(variants)} variants",
        flush=True,
    )

    rows: List[Dict[str, Any]] = []
    with jsonl_path.open("w", encoding="utf-8") as fh:
        i = 0
        for case in cases:
            for model in models:
                for variant in variants:
                    i += 1
                    label = f"[{i:>3}/{total}] {case.id} | {model} | {variant}"
                    print(label, end="", flush=True)
                    try:
                        row = _run_one(case, model, variant)
                    except Exception as e:
                        row = {
                            "case_id": case.id,
                            "case_description": case.description.strip(),
                            "case_tags": case.tags,
                            "model": model,
                            "variant": variant,
                            "expected": _expected_for(case, variant),
                            "raw_answer": "",
                            "normalized_answer": "",
                            "passed": False,
                            "latency_ms": None,
                            "prompt_tokens": None,
                            "completion_tokens": None,
                            "cost_usd": None,
                            "error": f"runner-exception: {type(e).__name__}: {e}",
                        }
                        traceback.print_exc()
                    fh.write(json.dumps(row) + "\n")
                    fh.flush()
                    rows.append(row)
                    status = "✅" if row["passed"] else ("⚠️ " if row["error"] else "❌")
                    extra = (
                        f"  -> {status}  ({row['latency_ms']}ms"
                        + (f", err={row['error']}" if row["error"] else "")
                        + ")"
                    )
                    print(extra, flush=True)

    _write_markdown(rows, cases, models, md_path)

    pass_count = sum(1 for r in rows if r["passed"])
    print(
        f"\nDone. {pass_count}/{len(rows)} passed.\n"
        f"  JSONL: {jsonl_path}\n"
        f"  Markdown: {md_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
