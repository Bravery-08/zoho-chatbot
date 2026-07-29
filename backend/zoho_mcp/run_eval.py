# backend/zoho_mcp/run_eval.py
"""
Phase 0 eval harness — measures the contract before any of this touches WhatsApp.

For each command in the corpus, it asks the LLM (your production Groq model) to
choose a tool and arguments, given the real Zoho MCP tool list, then scores:

  • tool-selection accuracy  — did it pick the expected tool?
  • argument correctness     — did it fill the expected args (on correct picks)?
  • latency (p50 / p95)
  • token usage / estimated cost

It does NOT execute any Zoho tool. Scoring tool *selection* needs the live tool
schema but not live data, which keeps the harness fast, deterministic, and safe
to run repeatedly. (Use smoke_test.py to actually execute read tools by hand.)

Run from the backend/ directory:

    python -m zoho_mcp.run_eval                       # fetch tools live, then score
    python -m zoho_mcp.run_eval --schema-file eval_results/tools_schema.json   # offline
    python -m zoho_mcp.run_eval --limit 5             # quick smoke of the harness itself
"""
import argparse
import asyncio
import json
import logging
import os
import re
from datetime import datetime

from groq import Groq, BadRequestError as GroqBadRequestError

from zoho_mcp.client import ZohoMCPClient, tools_to_groq_schema
from zoho_mcp.config import (
    GROQ_API_KEY,
    AGENT_MODEL,
    PRICE_PER_MTOK_INPUT,
    PRICE_PER_MTOK_OUTPUT,
)
from zoho_mcp.agent import _ROUTING_PROMPT as AGENT_SYSTEM_PROMPT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zoho_eval")

HERE = os.path.dirname(__file__)
DEFAULT_CORPUS = os.path.join(HERE, "corpus.jsonl")
RESULTS_DIR = os.path.join(HERE, "eval_results")

# ── Loading ───────────────────────────────────────────────────────────────────

def load_corpus(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            rows.append(json.loads(stripped))
    return rows


def load_tools_from_schema(path: str) -> list[dict]:
    """Build Groq function tools directly from a dumped tools_schema.json."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": (t.get("description") or "").strip(),
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
            },
        }
        for t in raw
    ]


async def gather_tools(schema_file: str | None) -> list[dict]:
    if schema_file:
        log.info("Loading tool schema from %s (offline mode)", schema_file)
        return load_tools_from_schema(schema_file)
    async with ZohoMCPClient() as zoho:
        mcp_tools = await zoho.list_tools()
    log.info("Fetched %d tools live from Zoho MCP", len(mcp_tools))
    _EXCLUDED = frozenset({"ZohoBooks_list_contacts", "ZohoCRM_createRecords"})
    return [t for t in tools_to_groq_schema(mcp_tools)
        if t["function"]["name"] not in _EXCLUDED]


# ── Scoring ───────────────────────────────────────────────────────────────────

def _norm(v):
    return v.strip().lower() if isinstance(v, str) else v


def score_args(expected: dict, predicted: dict):
    """Per-key match over the *expected* args (a subset check, not strict equality)."""
    if not expected:
        return 1.0, []
    hits, mismatches = 0, []
    for key, exp in expected.items():
        got = predicted.get(key, "<missing>")
        if _norm(got) == _norm(exp):
            hits += 1
        else:
            mismatches.append({"key": key, "expected": exp, "got": got})
    return hits / len(expected), mismatches


def tool_is_correct(expected_tool, predicted_tool) -> bool:
    # expected_tool: a string, a list of acceptable names, or None (= expect no call)
    if expected_tool is None:
        return predicted_tool is None
    if isinstance(expected_tool, list):
        return predicted_tool in expected_tool
    return predicted_tool == expected_tool


def _parse_failed_generation(error: GroqBadRequestError) -> tuple[str | None, dict]:
    """
    When Groq rejects a tool call for schema violations (HTTP 400), it still
    returns the model's attempted generation in:
        error.response.json()["error"]["failed_generation"]

    Parse that string to recover the intended tool name and args so the row
    can be scored instead of being lost to an unhandled exception.

    Pattern emitted by Groq: <function=TOOL_NAME>{...json...}</function>
    """
    try:
        body = error.response.json()
        failed_gen = body.get("error", {}).get("failed_generation", "")
        match = re.search(r"<function=([^>]+)>(.*?)</function>", failed_gen, re.DOTALL)
        if match:
            tool_name = match.group(1).strip()
            try:
                args = json.loads(match.group(2).strip())
            except json.JSONDecodeError:
                args = {}
            return tool_name, args
    except Exception:
        pass
    return None, {}


def run_one(client: Groq, tools: list[dict], row: dict) -> dict:
    import time

    start = time.time()
    schema_error = False
    schema_error_msg = None

    try:
        completion = client.chat.completions.create(
            model=AGENT_MODEL,
            messages=[
                {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": row["command"]},
            ],
            tools=tools,
            tool_choice="auto",
            temperature=0,
            max_tokens=512,
        )
        latency_ms = int((time.time() - start) * 1000)

        msg = completion.choices[0].message
        calls = msg.tool_calls or []
        predicted_tool = calls[0].function.name if calls else None
        try:
            predicted_args = json.loads(calls[0].function.arguments) if calls else {}
        except (json.JSONDecodeError, TypeError):
            predicted_args = {}

        usage = completion.usage
        prompt_tokens     = getattr(usage, "prompt_tokens", 0)
        completion_tokens = getattr(usage, "completion_tokens", 0)
        total_tokens      = getattr(usage, "total_tokens", 0)

    except GroqBadRequestError as exc:
        # Groq rejected the model's tool call for schema violations (e.g.
        # integer field got a string value). Recover the intended tool + args
        # from failed_generation so the row is still scored correctly.
        latency_ms = int((time.time() - start) * 1000)
        predicted_tool, predicted_args = _parse_failed_generation(exc)
        schema_error = True
        schema_error_msg = str(exc)[:300]
        # Token counts aren't available on a 400 — record zeros.
        prompt_tokens = completion_tokens = total_tokens = 0
        log.warning("  [schema_error] %s — recovered tool=%s from failed_generation",
                    row.get("id"), predicted_tool)

    correct = tool_is_correct(row.get("expected_tool"), predicted_tool)
    if correct:
        args_score, mismatches = score_args(row.get("expected_args", {}), predicted_args)
    else:
        args_score, mismatches = 0.0, []

    return {
        "id": row.get("id"),
        "command": row["command"],
        "category": row.get("category", ""),
        "expected_tool": row.get("expected_tool"),
        "predicted_tool": predicted_tool,
        "tool_correct": correct,
        "schema_error": schema_error,
        "schema_error_msg": schema_error_msg,
        "expected_args": row.get("expected_args", {}),
        "predicted_args": predicted_args,
        "args_score": round(args_score, 3),
        "arg_mismatches": mismatches,
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    idx = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
    return s[idx]


def summarize(results: list[dict]) -> dict:
    n = len(results)
    tool_acc = sum(r["tool_correct"] for r in results) / n if n else 0
    correct_rows = [r for r in results if r["tool_correct"]]
    arg_acc = (sum(r["args_score"] for r in correct_rows) / len(correct_rows)) if correct_rows else 0
    latencies = [r["latency_ms"] for r in results]
    total_in = sum(r["prompt_tokens"] for r in results)
    total_out = sum(r["completion_tokens"] for r in results)
    cost = total_in / 1e6 * PRICE_PER_MTOK_INPUT + total_out / 1e6 * PRICE_PER_MTOK_OUTPUT
    schema_errors = sum(1 for r in results if r.get("schema_error"))

    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"] or "uncategorized", []).append(r["tool_correct"])

    return {
        "n": n,
        "tool_selection_accuracy": round(tool_acc, 3),
        "arg_accuracy_on_correct_tool": round(arg_acc, 3),
        "schema_errors": schema_errors,
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "est_cost_usd": round(cost, 4),
        "accuracy_by_category": {c: round(sum(v) / len(v), 3) for c, v in by_cat.items()},
    }


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Zoho MCP tool-selection eval (Phase 0).")
    parser.add_argument("--corpus", default=DEFAULT_CORPUS)
    parser.add_argument("--schema-file", default=None,
                        help="Score against a dumped tools_schema.json instead of connecting live.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    if not GROQ_API_KEY:
        raise SystemExit("GROQ_API_KEY is not set in your .env.")

    corpus = load_corpus(args.corpus)
    if args.limit:
        corpus = corpus[: args.limit]
    log.info("Loaded %d corpus command(s)", len(corpus))

    tools = asyncio.run(gather_tools(args.schema_file))
    tool_names = {t["function"]["name"] for t in tools}

    # Catch corpus rows whose expected_tool isn't a real tool — a common slip
    # after the schema changes. These will always score as misses otherwise.
    for row in corpus:
        exp = row.get("expected_tool")
        names = exp if isinstance(exp, list) else ([exp] if exp else [])
        for nm in names:
            if nm not in tool_names:
                log.warning("Corpus %s expects tool '%s' not present in the schema.",
                            row.get("id"), nm)

    client = Groq(api_key=GROQ_API_KEY)
    results = []
    for row in corpus:
        r = run_one(client, tools, row)
        flag = "OK" if r["tool_correct"] else "XX"
        log.info("[%s] %-5s expected=%s got=%s args=%.2f %dms",
                 flag, r["id"], r["expected_tool"], r["predicted_tool"],
                 r["args_score"], r["latency_ms"])
        results.append(r)

    summary = summarize(results)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(RESULTS_DIR, f"eval-{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f, indent=2)

    print("\n" + "=" * 60)
    print("PHASE 0 EVAL SUMMARY")
    print("=" * 60)
    print(f"Commands evaluated      : {summary['n']}")
    print(f"Tool-selection accuracy : {summary['tool_selection_accuracy'] * 100:.1f}%")
    print(f"Arg accuracy (correct)  : {summary['arg_accuracy_on_correct_tool'] * 100:.1f}%")
    if summary["schema_errors"]:
        print(f"Schema errors (400s)    : {summary['schema_errors']}  "
              f"← tool was correct but arg types mismatched; "
              f"_coerce_integers in client.py should eliminate these on next run")
    print(f"Latency p50 / p95       : {summary['latency_p50_ms']} / {summary['latency_p95_ms']} ms")
    print(f"Tokens in / out         : {summary['total_input_tokens']} / {summary['total_output_tokens']}")
    if summary["est_cost_usd"]:
        print(f"Estimated cost          : ${summary['est_cost_usd']}")
    print("By category             :")
    for cat, acc in summary["accuracy_by_category"].items():
        print(f"    {cat:<30} {acc * 100:.1f}%")
    print(f"\nFull results written to : {out_path}")

    failures = [r for r in results if not r["tool_correct"]]
    if failures:
        print(f"\n{len(failures)} tool-selection miss(es):")
        for r in failures:
            print(f"    {r['id']}: \"{r['command'][:60]}\" → expected {r['expected_tool']}, got {r['predicted_tool']}")


if __name__ == "__main__":
    main()