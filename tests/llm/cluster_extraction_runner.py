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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
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
    case: ClusterExtractionCase,
    model: str,
    variant: str,
    run_index: int = 0,
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
        #
        # No `toolConfig` is passed: this eval mirrors relay's
        # extract_message_cluster, which is a pure prompt -> text-answer call.
        # Tool calling is not in scope here.
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
        "run_index": run_index,
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
    lines.append(
        "_Each cell shows `passes/runs` across all repeats. Bias failures (model picked "
        "a forbidden cluster) are flagged 🚨._"
    )
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
                matches = [
                    r
                    for r in rows
                    if r["case_id"] == case_id
                    and r["model"] == model
                    and r["variant"] == variant
                ]
                if not matches:
                    cell = "—"
                else:
                    n = len(matches)
                    n_pass = sum(1 for r in matches if r["passed"])
                    n_bias = sum(1 for r in matches if r.get("fail_reason") == "bias")
                    n_err = sum(1 for r in matches if r["error"])
                    if n_pass == n:
                        cell = f"✅ {n_pass}/{n}"
                    elif n_bias > 0:
                        cell = f"🚨 {n_pass}/{n}"
                    elif n_err > 0 and n_pass == 0:
                        cell = f"⚠️ {n_pass}/{n}"
                    else:
                        cell = f"❌ {n_pass}/{n}"
                row += f" {cell} |"
        lines.append(row)
    lines.append("")

    lines.append("## Failures and errors")
    lines.append("")
    # Group failures by (case, model, variant). For each group, summarize how
    # many of the N runs failed and the modal answer. Cleaner than one entry
    # per repeat when --runs > 1.
    by_triple: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_triple[(r["case_id"], r["model"], r["variant"])].append(r)

    failure_groups = []
    for triple, group in by_triple.items():
        n = len(group)
        n_pass = sum(1 for r in group if r["passed"])
        if n_pass == n:
            continue  # all passed; not a failure group
        failure_groups.append((triple, group, n, n_pass))

    if not failure_groups:
        lines.append("_None._")
    else:
        for (case_id, model, variant), group, n, n_pass in failure_groups:
            case = case_by_id[case_id]
            n_bias = sum(1 for r in group if r.get("fail_reason") == "bias")
            n_err = sum(1 for r in group if r["error"])
            if n_bias > 0:
                tag = "🚨 BIAS"
            elif n_err > 0 and n_pass == 0:
                tag = "⚠️ ERROR"
            else:
                tag = "❌ MISS"
            lines.append(
                f"### {tag} `{case_id}` — `{model}` — `{variant}` "
                f"({n - n_pass}/{n} failed)"
            )
            lines.append("")
            lines.append(f"**Description:** {case.description.strip()}")
            lines.append("")
            lines.append(f"- **Expected:** `{group[0]['expected']}`")
            if group[0].get("forbidden_clusters"):
                lines.append(
                    f"- **Forbidden:** `{', '.join(group[0]['forbidden_clusters'])}`"
                )
            # Distinct normalized answers and their counts.
            answer_counts: Dict[str, int] = defaultdict(int)
            for r in group:
                if not r["passed"]:
                    answer_counts[r["normalized_answer"] or "∅"] += 1
            ans_summary = ", ".join(
                f"`{a}` ×{c}" for a, c in sorted(
                    answer_counts.items(), key=lambda kv: -kv[1]
                )
            )
            lines.append(f"- **Got (normalized, fail runs):** {ans_summary}")
            errors = [r["error"] for r in group if r.get("error")]
            if errors:
                lines.append(f"- **Errors:** {len(errors)}; first: `{errors[0]}`")
            # Show the first non-empty raw answer so the reviewer can see flavor.
            sample_raw = next(
                (r["raw_answer"] for r in group if not r["passed"] and r["raw_answer"]),
                "",
            )
            if sample_raw:
                quoted = sample_raw.replace("\n", " \\n ")[:200]
                lines.append(f"- **Sample raw answer:** `{quoted}`")
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
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of times to run each (case, model, variant) combination. "
        "Use >1 to get noise bars on accuracy and latency. Default: 1.",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Number of model calls to issue concurrently. Default 1 (sequential). "
        "Bedrock has per-model RPM/TPM limits - 4-8 is usually safe.",
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

    runs = max(1, args.runs)
    parallel = max(1, args.parallel)
    combos = len(cases) * len(models) * len(variants)
    total = combos * runs
    print(
        f"Running {total} calls: {len(cases)} cases × {len(models)} models "
        f"× {len(variants)} variants × {runs} runs (parallel={parallel})",
        flush=True,
    )

    # Build the task list. Outer index is run, so all combos are sampled once
    # before any combo is sampled twice - this spreads any time-correlated
    # noise (rate limiting, transient throttles) across runs.
    tasks: List[Tuple[int, ClusterExtractionCase, str, str]] = []
    for run_idx in range(runs):
        for case in cases:
            for model in models:
                for variant in variants:
                    tasks.append((run_idx, case, model, variant))

    rows: List[Dict[str, Any]] = []
    fh_lock = Lock()
    counter = {"i": 0}

    def _do_one(task):
        run_idx, case, model, variant = task
        try:
            row = _run_one(case, model, variant, run_index=run_idx)
        except Exception as e:
            row = {
                "case_id": case.id,
                "case_description": case.description.strip(),
                "case_tags": case.tags,
                "model": model,
                "variant": variant,
                "run_index": run_idx,
                "expected": _expected_for(case, variant),
                "forbidden_clusters": list(case.forbidden_clusters),
                "raw_answer": "",
                "normalized_answer": "",
                "passed": False,
                "fail_reason": "error",
                "latency_ms": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "cost_usd": None,
                "error": f"runner-exception: {type(e).__name__}: {e}",
            }
            traceback.print_exc()
        return row

    with jsonl_path.open("w", encoding="utf-8") as fh:
        if parallel <= 1:
            for task in tasks:
                row = _do_one(task)
                counter["i"] += 1
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                rows.append(row)
                status = (
                    "✅" if row["passed"]
                    else ("🚨" if row.get("fail_reason") == "bias"
                    else ("⚠️ " if row["error"] else "❌"))
                )
                run_idx = row["run_index"]
                print(
                    f"[{counter['i']:>4}/{total}] r{run_idx + 1} {row['case_id']:34} | "
                    f"{row['model'].split('/')[-1][:30]:30} | {row['variant']:18} -> "
                    f"{status} ({row['latency_ms']}ms)",
                    flush=True,
                )
        else:
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = [pool.submit(_do_one, t) for t in tasks]
                for fut in as_completed(futures):
                    row = fut.result()
                    with fh_lock:
                        counter["i"] += 1
                        fh.write(json.dumps(row) + "\n")
                        fh.flush()
                        rows.append(row)
                        status = (
                            "✅" if row["passed"]
                            else ("🚨" if row.get("fail_reason") == "bias"
                            else ("⚠️ " if row["error"] else "❌"))
                        )
                        run_idx = row["run_index"]
                        print(
                            f"[{counter['i']:>4}/{total}] r{run_idx + 1} {row['case_id']:34} | "
                            f"{row['model'].split('/')[-1][:30]:30} | {row['variant']:18} -> "
                            f"{status} ({row['latency_ms']}ms)",
                            flush=True,
                        )

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
