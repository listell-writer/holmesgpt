"""GitHub Actions reporting functionality."""

import logging
import os
from typing import Dict, List, Optional, Tuple

from tests.llm.utils.braintrust import get_braintrust_url
from tests.llm.utils.braintrust_history import (
    BRAINTRUST_ORG,
    BRAINTRUST_PROJECT,
    BenchmarkMetrics,
    HistoricalComparison,
    HistoricalComparisonDetails,
    compare_with_benchmark,
    get_benchmark_baseline,
)
from tests.llm.utils.test_results import TestStatus


def _fmt_tokens(value: Optional[int]) -> str:
    """Format a token count: comma-separated if present, dash if absent/zero."""
    if value is not None and value > 0:
        return f"{value:,}"
    return "—"


def _format_diff_pct(diff: Optional[float]) -> str:
    """Format a diff percentage with arrow indicator, bold if >25%."""
    if diff is None:
        return "—"
    if abs(diff) < 10:
        return "±0%"
    bold = abs(diff) > 25
    arrow = "↑" if diff > 0 else "↓"
    indicator = f"{arrow}{abs(diff):.0f}%"
    return f"**{indicator}**" if bold else indicator


def _calc_diff_pct(current: Optional[float], baseline: Optional[float]) -> Optional[float]:
    """Calculate percentage difference: positive = current is higher."""
    if not current or not baseline or baseline == 0:
        return None
    return (current - baseline) / baseline * 100


def _generate_comparison_tables(
    sorted_results: List[dict],
    comparison_map: Dict[str, HistoricalComparison],
    benchmark: Dict[str, BenchmarkMetrics],
) -> str:
    """Generate separate comparison tables for time, cost, tokens, and cached tokens.

    Each table has columns: Test case | This branch | master | Diff
    """
    lines: List[str] = []

    # Build rows with data for all metrics
    rows: List[dict] = []
    for result in sorted_results:
        test_name = result.get("test_case_name", "")
        model = result.get("model", "")
        key = f"{test_name}:{model}"
        comparison = comparison_map.get(key)
        baseline = benchmark.get(key)

        rows.append({
            "name": f"{test_name} ({model})" if model else test_name,
            "current_time": result.get("holmes_duration"),
            "baseline_time": baseline.duration if baseline else None,
            "current_cost": result.get("cost"),
            "baseline_cost": baseline.cost if baseline else None,
            "current_total_tokens": result.get("total_tokens", 0) or 0,
            "baseline_total_tokens": baseline.total_tokens if baseline else None,
            "current_cached_tokens": result.get("cached_tokens"),
            "baseline_cached_tokens": baseline.cached_tokens if baseline else None,
        })

    # --- Time comparison table ---
    has_time_data = any(r["baseline_time"] is not None for r in rows)
    if has_time_data:
        lines.append("\n**Time comparison (seconds):**\n")
        lines.append("| Test case | This branch | master | Diff |")
        lines.append("| --- | --- | --- | --- |")
        for r in rows:
            cur = f"{r['current_time']:.1f}s" if r["current_time"] else "—"
            base = f"{r['baseline_time']:.1f}s" if r["baseline_time"] else "—"
            diff = _format_diff_pct(_calc_diff_pct(r["current_time"], r["baseline_time"]))
            lines.append(f"| {r['name']} | {cur} | {base} | {diff} |")
        lines.append("")

    # --- Cost comparison table ---
    has_cost_data = any(r["baseline_cost"] is not None for r in rows)
    if has_cost_data:
        lines.append("\n**Cost comparison:**\n")
        lines.append("| Test case | This branch | master | Diff |")
        lines.append("| --- | --- | --- | --- |")
        for r in rows:
            cur = f"${r['current_cost']:.4f}" if r["current_cost"] else "—"
            base = f"${r['baseline_cost']:.4f}" if r["baseline_cost"] else "—"
            diff = _format_diff_pct(_calc_diff_pct(r["current_cost"], r["baseline_cost"]))
            lines.append(f"| {r['name']} | {cur} | {base} | {diff} |")
        lines.append("")

    # --- Total tokens comparison table ---
    has_token_data = any(r["baseline_total_tokens"] is not None for r in rows)
    if has_token_data:
        lines.append("\n**Total tokens comparison:**\n")
        lines.append("| Test case | This branch | master | Diff |")
        lines.append("| --- | --- | --- | --- |")
        for r in rows:
            cur_val = r["current_total_tokens"]
            cur = f"{cur_val:,}" if cur_val else "—"
            base_val = r["baseline_total_tokens"]
            base = f"{base_val:,}" if base_val else "—"
            diff = _format_diff_pct(
                _calc_diff_pct(
                    float(cur_val) if cur_val else None,
                    float(base_val) if base_val else None,
                )
            )
            lines.append(f"| {r['name']} | {cur} | {base} | {diff} |")
        lines.append("")

    # --- Cached tokens comparison table ---
    has_cached_data = any(r["baseline_cached_tokens"] is not None for r in rows)
    if has_cached_data:
        lines.append("\n**Cached tokens comparison:**\n")
        lines.append("| Test case | This branch | master | Diff |")
        lines.append("| --- | --- | --- | --- |")
        for r in rows:
            cur_val = r["current_cached_tokens"]
            cur = f"{cur_val:,}" if cur_val is not None else "—"
            base_val = r["baseline_cached_tokens"]
            base = f"{base_val:,}" if base_val is not None else "—"
            diff = _format_diff_pct(
                _calc_diff_pct(
                    float(cur_val) if cur_val is not None else None,
                    float(base_val) if base_val is not None else None,
                )
            )
            lines.append(f"| {r['name']} | {cur} | {base} | {diff} |")
        lines.append("")

    if not lines:
        lines.append("\n_No benchmark data available for comparison._\n")

    # Note missing tables
    missing = []
    if not has_cost_data:
        missing.append("cost")
    if not has_token_data:
        missing.append("total tokens")
    if not has_cached_data:
        missing.append("cached tokens")
    if missing:
        lines.append(
            f"_Benchmark has no {', '.join(missing)} data. "
            "Will appear after the next weekly benchmark run._\n"
        )

    return "\n".join(lines)


def _generate_historical_details_section(
    details: HistoricalComparisonDetails,
    sorted_results: Optional[List[dict]] = None,
    comparison_map: Optional[Dict[str, HistoricalComparison]] = None,
    benchmark: Optional[Dict[str, BenchmarkMetrics]] = None,
) -> str:
    """Generate a collapsible details section with benchmark comparison tables.

    Args:
        details: HistoricalComparisonDetails with experiment info
        sorted_results: Current test results for comparison tables
        comparison_map: Map of test:model to comparison data
        benchmark: Map of test:model to benchmark metrics

    Returns:
        Markdown string with collapsible details section
    """
    lines = ["<details>", "<summary><b>Benchmark Comparison Details</b></summary>\n"]

    # Filter description
    if details.filter_description:
        lines.append(f"**Baseline:** {details.filter_description}\n")

    # Status
    if details.status:
        lines.append(f"**Status:** {details.status}\n")
    else:
        lines.append(
            f"**Status:** Success - {details.metrics_count} test/model combinations loaded\n"
        )

    # Experiments used
    if details.experiments:
        lines.append(f"\n**Benchmark experiment{'s' if len(details.experiments) > 1 else ''}:**\n")
        for exp in details.experiments:
            exp_url = f"https://www.braintrust.dev/app/{BRAINTRUST_ORG}/p/{BRAINTRUST_PROJECT}/experiments/{exp.id}"
            created_info = f" (created: {exp.created[:10]})" if exp.created else ""
            lines.append(f"- [{exp.name}]({exp_url}){created_info}")
        lines.append("")

    # Comparison tables
    if sorted_results and comparison_map and benchmark:
        lines.append(
            _generate_comparison_tables(sorted_results, comparison_map, benchmark)
        )

    # Errors
    if details.errors:
        lines.append("\n**Errors:**\n")
        lines.append("```")
        for error in details.errors:
            lines.append(error)
        lines.append("```\n")

    # Document comparison thresholds
    lines.append("**Comparison indicators:**")
    lines.append("- `±0%` — diff under 10% (within noise threshold)")
    lines.append("- `↑N%`/`↓N%` — diff 10-25%")
    lines.append("- **`↑N%`**/**`↓N%`** — diff over 25% (significant)\n")

    lines.append("</details>\n")
    return "\n".join(lines)


def handle_github_output(sorted_results: List[dict]) -> None:
    """Generate and write GitHub Actions report files."""
    # Generate markdown report (always compare against weekly benchmark when possible)
    markdown, _, total_regressions = generate_markdown_report(sorted_results, True)

    # Always write markdown report
    with open("evals_report.md", "w", encoding="utf-8") as file:
        file.write(markdown)

    if os.environ.get("GENERATE_REGRESSIONS_FILE") and total_regressions > 0:
        with open("regressions.txt", "w", encoding="utf-8") as file:
            file.write(f"{total_regressions}")


def generate_markdown_report(
    sorted_results: List[dict],
    include_historical: bool,
) -> Tuple[str, List[dict], int]:
    """Generate markdown report from sorted test results.

    Args:
        sorted_results: List of test result dictionaries
        include_historical: Whether to fetch and include historical comparison

    Returns:
        Tuple of (markdown, sorted_results, total_regressions)
    """
    # Check if running on a specific branch (for cross-branch comparison)
    eval_branch = os.environ.get("EVAL_BRANCH", "")
    if eval_branch:
        markdown = f"## Results of HolmesGPT evals (branch: `{eval_branch}`)\n\n"
    else:
        markdown = "## Results of HolmesGPT evals\n\n"

    # Fetch benchmark baseline for comparison (latest weekly ci-benchmark run)
    benchmark: Dict[str, BenchmarkMetrics] = {}
    comparison_map: Dict[str, HistoricalComparison] = {}
    historical_details: Optional[HistoricalComparisonDetails] = None
    if include_historical:
        try:
            benchmark, historical_details = get_benchmark_baseline()
            if benchmark:
                comparison_map = compare_with_benchmark(sorted_results, benchmark)
                logging.info(
                    f"Loaded benchmark baseline: {len(benchmark)} test/model combinations"
                )
        except Exception as e:
            historical_details = HistoricalComparisonDetails(status=f"API error: {e}")
            logging.warning(f"Failed to fetch benchmark baseline: {e}")

    # Count results by test type and status
    ask_holmes_total = 0
    ask_holmes_passed = 0
    ask_holmes_regressions = 0
    ask_holmes_mock_failures = 0
    ask_holmes_skipped = 0
    ask_holmes_setup_failures = 0

    for result in sorted_results:
        status = TestStatus(result)

        if result["test_type"] == "ask":
            ask_holmes_total += 1
            if status.is_skipped:
                ask_holmes_skipped += 1
            elif status.is_setup_failure:
                ask_holmes_setup_failures += 1
            elif status.passed:
                ask_holmes_passed += 1
            elif status.is_regression:
                ask_holmes_regressions += 1
            elif status.is_mock_failure:
                ask_holmes_mock_failures += 1
    # Generate summary lines
    if ask_holmes_total > 0:
        markdown += f"- ask_holmes: {ask_holmes_passed}/{ask_holmes_total} test cases were successful, {ask_holmes_regressions} regressions"
        if ask_holmes_skipped > 0:
            markdown += f", {ask_holmes_skipped} skipped"
        if ask_holmes_setup_failures > 0:
            markdown += f", {ask_holmes_setup_failures} setup failures"
        if ask_holmes_mock_failures > 0:
            markdown += f", {ask_holmes_mock_failures} mock failures"
        markdown += "\n"
    # Generate detailed table.
    # Column order: Sub-agent invocations + Sub-agent turns + Total turns sit
    # right next to Turns so a reader can quickly compare parent vs total work.
    detail_header = (
        "| Status | Test case | Time | Sub-agents | Sub-agent turns | Turns | Total turns | Tools | "
        "Cost | Total tokens | Input | Max input | Output | Max output | Cached | Non-cached | "
        "Reasoning | Compactions |"
    )
    detail_separator = (
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | "
        "--- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    markdown += "\n\n" + detail_header + "\n" + detail_separator + "\n"

    # Track totals for summary row
    total_time = 0.0
    total_cost = 0.0
    total_tokens_sum = 0
    total_prompt_tokens_sum = 0
    total_completion_tokens_sum = 0
    total_cached_tokens_sum = 0
    total_non_cached_tokens_sum = 0
    total_reasoning_tokens_sum = 0
    max_completion_per_call_max = 0
    max_prompt_per_call_max = 0
    total_compactions = 0
    total_turns = 0
    total_subagent_invocations = 0
    total_subagent_turns = 0
    total_total_turns = 0
    total_tools = 0
    time_count = 0
    turns_count = 0
    subagent_invocations_count = 0
    subagent_turns_count = 0
    total_turns_count = 0
    tools_count = 0

    # Per-(subagent variant) subtotals — only emitted when more than one
    # variant ran in this report, so a single-variant run doesn't get a
    # redundant subtotal row that duplicates the grand Total.
    from collections import defaultdict

    class _Subtotal:
        __slots__ = (
            "tests", "time_sum", "time_count", "cost_sum",
            "tokens_sum", "input_sum", "output_sum",
            "cached_sum", "non_cached_sum", "reasoning_sum",
            "max_completion", "max_prompt", "compactions",
            "turns_sum", "turns_count",
            "subagent_invocations_sum", "subagent_invocations_count",
            "subagent_turns_sum", "subagent_turns_count",
            "total_turns_sum", "total_turns_count",
            "tools_sum", "tools_count",
        )

        def __init__(self) -> None:
            for slot in self.__slots__:
                setattr(self, slot, 0)

    subtotals: "defaultdict[str, _Subtotal]" = defaultdict(_Subtotal)
    # Detail rows are emitted in their incoming sort order (conftest already
    # sorts alphabetically by test_case_name, which puts the same eval's
    # subagent_off / subagent_on variants right next to each other thanks to
    # the [variant] suffix in clean_test_case_id). All subtotal rows are
    # emitted together at the end, just above the grand Total.
    detail_rows: List[str] = []

    for result in sorted_results:
        test_case_name = result["test_case_name"]
        model = result.get("model", "")

        braintrust_url = get_braintrust_url(
            result.get("braintrust_span_id"),
            result.get("braintrust_root_span_id"),
        )
        if braintrust_url:
            test_case_name = f"[{test_case_name}]({braintrust_url})"

        status = TestStatus(result)

        # Format time (plain, no inline comparison)
        exec_time = result.get("holmes_duration")
        time_str = f"{exec_time:.1f}s" if exec_time and exec_time > 0 else "—"
        if exec_time and exec_time > 0:
            total_time += exec_time
            time_count += 1

        # Format turns (parent LLM calls)
        num_llm_calls = result.get("num_llm_calls")
        if num_llm_calls and num_llm_calls > 0:
            turns_str = str(num_llm_calls)
            total_turns += num_llm_calls
            turns_count += 1
        else:
            turns_str = "—"

        # Format sub-agent invocations: how many times dispatch_agent was
        # called. None means the field wasn't recorded (older results); 0
        # means subagents were available but not used.
        num_subagent_invocations = result.get("num_subagent_invocations")
        if num_subagent_invocations is not None:
            subagent_invocations_str = str(int(num_subagent_invocations))
            total_subagent_invocations += int(num_subagent_invocations)
            subagent_invocations_count += 1
        else:
            subagent_invocations_str = "—"

        # Format sub-agent turns: total LLM calls those dispatched children burned.
        num_subagent_llm_calls = result.get("num_subagent_llm_calls")
        if num_subagent_llm_calls is not None:
            subagent_turns_str = str(int(num_subagent_llm_calls))
            total_subagent_turns += int(num_subagent_llm_calls)
            subagent_turns_count += 1
        else:
            subagent_turns_str = "—"

        # Format total turns: parent + subagent (the user-facing "this run took
        # N LLM calls overall" number).
        total_turns_value = result.get("total_turns")
        if total_turns_value is None and num_llm_calls is not None:
            total_turns_value = int(num_llm_calls) + int(
                num_subagent_llm_calls or 0
            )
        if total_turns_value is not None and total_turns_value > 0:
            total_turns_row_str = str(int(total_turns_value))
            total_total_turns += int(total_turns_value)
            total_turns_count += 1
        else:
            total_turns_row_str = "—"

        # Format tool calls
        tool_call_count = result.get("tool_call_count")
        if tool_call_count and tool_call_count > 0:
            tools_str = str(tool_call_count)
            total_tools += tool_call_count
            tools_count += 1
        else:
            tools_str = "—"

        # Format cost (plain, no inline comparison)
        cost = result.get("cost", 0)
        cost_str = f"${cost:.4f}" if cost and cost > 0 else "—"
        if cost and cost > 0:
            total_cost += cost

        # Extract token counts
        total_tokens = result.get("total_tokens", 0) or 0
        prompt_tokens = result.get("prompt_tokens", 0) or 0
        completion_tokens = result.get("completion_tokens", 0) or 0
        cached_tokens = result.get("cached_tokens")
        reasoning_tokens = result.get("reasoning_tokens", 0) or 0
        max_completion = result.get("max_completion_tokens_per_call", 0) or 0
        max_prompt = result.get("max_prompt_tokens_per_call", 0) or 0
        num_compactions = result.get("num_compactions", 0) or 0

        # Compute total_tokens from parts if not reported directly
        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens

        # Non-cached = prompt - cached (only meaningful when both are known)
        if prompt_tokens > 0 and cached_tokens is not None:
            non_cached_tokens = prompt_tokens - cached_tokens
        elif prompt_tokens > 0:
            non_cached_tokens = None  # cached unknown, can't compute
        else:
            non_cached_tokens = None

        # Accumulate totals
        total_tokens_sum += total_tokens
        total_prompt_tokens_sum += prompt_tokens
        total_completion_tokens_sum += completion_tokens
        if cached_tokens is not None:
            total_cached_tokens_sum += cached_tokens
        if non_cached_tokens is not None:
            total_non_cached_tokens_sum += non_cached_tokens
        total_reasoning_tokens_sum += reasoning_tokens
        max_completion_per_call_max = max(max_completion_per_call_max, max_completion)
        max_prompt_per_call_max = max(max_prompt_per_call_max, max_prompt)
        total_compactions += num_compactions

        # Format for display
        total_tokens_str = _fmt_tokens(total_tokens)
        input_str = _fmt_tokens(prompt_tokens)
        output_str = _fmt_tokens(completion_tokens)
        cached_tokens_str = f"{cached_tokens:,}" if cached_tokens is not None else "—"
        non_cached_tokens_str = f"{non_cached_tokens:,}" if non_cached_tokens is not None else "—"
        reasoning_str = _fmt_tokens(reasoning_tokens)
        max_completion_str = _fmt_tokens(max_completion)
        max_prompt_str = _fmt_tokens(max_prompt)
        compactions_str = str(num_compactions) if num_compactions > 0 else "—"

        row = (
            f"| {status.markdown_symbol} | {test_case_name} | {time_str} | "
            f"{subagent_invocations_str} | {subagent_turns_str} | {turns_str} | "
            f"{total_turns_row_str} | {tools_str} | {cost_str} | "
            f"{total_tokens_str} | {input_str} | {max_prompt_str} | "
            f"{output_str} | {max_completion_str} | {cached_tokens_str} | "
            f"{non_cached_tokens_str} | {reasoning_str} | {compactions_str} |"
        )

        # Determine variant for subtotal grouping. None = no matrix axis was
        # parametrized, in which case this row goes in the "" bucket and we
        # won't emit a subtotal (would just duplicate the grand Total).
        subagents_enabled = result.get("subagents_enabled")
        if subagents_enabled is True:
            variant = "subagent_on"
        elif subagents_enabled is False:
            variant = "subagent_off"
        else:
            variant = ""
        detail_rows.append(row)

        # Roll into the per-variant subtotal bucket
        st = subtotals[variant]
        st.tests += 1
        if exec_time and exec_time > 0:
            st.time_sum += exec_time
            st.time_count += 1
        if cost and cost > 0:
            st.cost_sum += cost
        st.tokens_sum += total_tokens
        st.input_sum += prompt_tokens
        st.output_sum += completion_tokens
        if cached_tokens is not None:
            st.cached_sum += cached_tokens
        if non_cached_tokens is not None:
            st.non_cached_sum += non_cached_tokens
        st.reasoning_sum += reasoning_tokens
        st.max_completion = max(st.max_completion, max_completion)
        st.max_prompt = max(st.max_prompt, max_prompt)
        st.compactions += num_compactions
        if num_llm_calls and num_llm_calls > 0:
            st.turns_sum += num_llm_calls
            st.turns_count += 1
        if num_subagent_invocations is not None:
            st.subagent_invocations_sum += int(num_subagent_invocations)
            st.subagent_invocations_count += 1
        if num_subagent_llm_calls is not None:
            st.subagent_turns_sum += int(num_subagent_llm_calls)
            st.subagent_turns_count += 1
        if total_turns_value and total_turns_value > 0:
            st.total_turns_sum += int(total_turns_value)
            st.total_turns_count += 1
        if tool_call_count and tool_call_count > 0:
            st.tools_sum += tool_call_count
            st.tools_count += 1

    # Subtotal rows: emit them only when more than one variant produced data
    # — a single-variant run would have a subtotal that just duplicates the
    # grand Total. Order off → on so the report reads "baseline → matrix".
    variants_seen = [v for v in subtotals if subtotals[v].tests > 0 and v]
    variant_order = {"subagent_off": 0, "subagent_on": 1}
    variants_seen.sort(key=lambda v: (variant_order.get(v, 50), v))

    def _format_subtotal(label: str, st: "_Subtotal") -> str:
        avg_t = f"{st.time_sum / st.time_count:.1f}s" if st.time_count else "—"
        avg_si = (
            f"{st.subagent_invocations_sum / st.subagent_invocations_count:.1f}"
            if st.subagent_invocations_count
            else "—"
        )
        avg_st_turns = (
            f"{st.subagent_turns_sum / st.subagent_turns_count:.1f}"
            if st.subagent_turns_count
            else "—"
        )
        avg_turns = (
            f"{st.turns_sum / st.turns_count:.1f}" if st.turns_count else "—"
        )
        avg_tt = (
            f"{st.total_turns_sum / st.total_turns_count:.1f}"
            if st.total_turns_count
            else "—"
        )
        avg_tools = (
            f"{st.tools_sum / st.tools_count:.1f}" if st.tools_count else "—"
        )
        cost_str = f"${st.cost_sum:.4f}" if st.cost_sum else "—"
        return (
            f"| | _{label}_ | _{avg_t} avg_ | _{avg_si} avg_ | "
            f"_{avg_st_turns} avg_ | _{avg_turns} avg_ | _{avg_tt} avg_ | "
            f"_{avg_tools} avg_ | _{cost_str}_ | "
            f"_{_fmt_tokens(st.tokens_sum)}_ | _{_fmt_tokens(st.input_sum)}_ | "
            f"_{_fmt_tokens(st.max_prompt)}_ | _{_fmt_tokens(st.output_sum)}_ | "
            f"_{_fmt_tokens(st.max_completion)}_ | _{_fmt_tokens(st.cached_sum)}_ | "
            f"_{_fmt_tokens(st.non_cached_sum)}_ | _{_fmt_tokens(st.reasoning_sum)}_ | "
            f"_{st.compactions if st.compactions else '—'}_ |"
        )

    # 1. All detail rows in incoming sorted order (variants for the same eval
    #    are already adjacent because of the [variant] suffix on the test_case_name).
    for row in detail_rows:
        markdown += row + "\n"

    # 2. All subtotal rows, clustered just above the grand Total. Skip when
    #    there's only one variant (subtotal would duplicate the Total).
    if len(variants_seen) > 1:
        for variant in variants_seen:
            markdown += _format_subtotal(
                f"Subtotal · {variant}", subtotals[variant]
            ) + "\n"

    # Add summary row
    avg_time_str = f"{total_time / time_count:.1f}s" if time_count > 0 else "—"
    avg_turns_str = f"{total_turns / turns_count:.1f}" if turns_count > 0 else "—"
    avg_subagent_inv_str = (
        f"{total_subagent_invocations / subagent_invocations_count:.1f}"
        if subagent_invocations_count > 0
        else "—"
    )
    avg_subagent_turns_str = (
        f"{total_subagent_turns / subagent_turns_count:.1f}"
        if subagent_turns_count > 0
        else "—"
    )
    avg_total_turns_str = (
        f"{total_total_turns / total_turns_count:.1f}"
        if total_turns_count > 0
        else "—"
    )
    avg_tools_str = f"{total_tools / tools_count:.1f}" if tools_count > 0 else "—"
    total_cost_str = f"${total_cost:.4f}" if total_cost > 0 else "—"
    total_tokens_total_str = _fmt_tokens(total_tokens_sum)
    total_prompt_str = _fmt_tokens(total_prompt_tokens_sum)
    total_completion_str = _fmt_tokens(total_completion_tokens_sum)
    total_cached_tokens_str = _fmt_tokens(total_cached_tokens_sum)
    total_non_cached_tokens_str = _fmt_tokens(total_non_cached_tokens_sum)
    total_reasoning_str = _fmt_tokens(total_reasoning_tokens_sum)
    max_completion_max_str = _fmt_tokens(max_completion_per_call_max)
    max_prompt_max_str = _fmt_tokens(max_prompt_per_call_max)
    total_compactions_str = str(total_compactions) if total_compactions > 0 else "—"
    markdown += (
        f"| | **Total** | **{avg_time_str}** avg | "
        f"**{avg_subagent_inv_str}** avg | **{avg_subagent_turns_str}** avg | "
        f"**{avg_turns_str}** avg | **{avg_total_turns_str}** avg | "
        f"**{avg_tools_str}** avg | **{total_cost_str}** | "
        f"**{total_tokens_total_str}** | **{total_prompt_str}** | "
        f"**{max_prompt_max_str}** | **{total_completion_str}** | "
        f"**{max_completion_max_str}** | **{total_cached_tokens_str}** | "
        f"**{total_non_cached_tokens_str}** | **{total_reasoning_str}** | "
        f"**{total_compactions_str}** |\n"
    )

    # Add footer explaining benchmark comparison status
    if not benchmark and historical_details and historical_details.status:
        markdown += (
            f"\n_Benchmark comparison unavailable: {historical_details.status}_\n"
        )

    # Add collapsible details section with comparison tables
    if historical_details:
        markdown += _generate_historical_details_section(
            historical_details,
            sorted_results=sorted_results if comparison_map else None,
            comparison_map=comparison_map or None,
            benchmark=benchmark or None,
        )

    return (
        markdown,
        sorted_results,
        ask_holmes_regressions,
    )
