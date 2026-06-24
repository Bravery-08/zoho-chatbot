# backend/scripts/diagnose.py
"""
Phase 3 — Router and threshold diagnostic.

Run from the backend/ directory:
    python scripts/diagnose.py

What it does:
    1. Classifies each test message through the LLM router.
    2. For company-classified messages, runs retrieval and records scores.
    3. Prints a per-message table and a summary with a MIN_SCORE suggestion.

Edit TEST_MESSAGES to match your actual SOP content before running.
"""

import sys
import os
import time

# Put backend/ on the path so `app.*` imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from llama_index.core.retrievers import VectorIndexRetriever
from app.config import TOP_K, MIN_SCORE
from app.router import classify_query
import app.rag as rag


# ── Test messages ──────────────────────────────────────────────────────────────
# Three buckets. Edit these to match your SOPs before running.
#
# IN_SOP:      topics explicitly covered in your knowledge base.
#              Router should say "company"; retrieval should find chunks.
#
# OUT_OF_SOP:  topics your KB has nothing on.
#              Router should say "company" (they're business-adjacent),
#              but retrieval should find nothing → escalate.
#
# GENERAL:     questions with no connection to the company at all.
#              Router should say "general" and skip retrieval entirely.
#
# AMBIGUOUS:   boundary cases — interesting to see how each lever handles them.

IN_SOP = [
    "what is the refund policy?",
    "how long does a refund take to process?",
    "can I get a refund after 7 days?",
    "how many sick leaves do I get per year?",
    "how do I apply for leave?",
    "what documents do I need when joining the company?",
    "how do I report a data breach?",
    "what is a SEV-1 incident?",
]

OUT_OF_SOP = [
    "what are your pricing plans?",
    "do you offer a free trial?",
    "what countries do you ship to?",
    "how do I integrate your API with Salesforce?",
    "do you have a mobile app?",            # ← replaces the SLA question
]

GENERAL = [
    "what is the capital of France?",
    "explain how machine learning works",
    "translate thank you into Japanese",
    "who won the FIFA World Cup in 2022?",
    "write me a Python function to sort a list",
]

AMBIGUOUS = [
    "what is a CSAT score?",
    "how do I handle a difficult customer?",
    "what is Freshdesk?",
    "what does escalation mean?",
    "how do I write a good incident report?",
]

TEST_MESSAGES = (
    [("in_sop",      m) for m in IN_SOP] +
    [("out_of_sop",  m) for m in OUT_OF_SOP] +
    [("general",     m) for m in GENERAL] +
    [("ambiguous",   m) for m in AMBIGUOUS]
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def retrieve_top_scores(message: str) -> list[float]:
    """Run retrieval and return all scores, unfiltered."""
    retriever = VectorIndexRetriever(index=rag._index, similarity_top_k=TOP_K)
    nodes = retriever.retrieve(message)
    return [round(n.score, 4) for n in nodes if n.score is not None]


def action_label(classification: str, scores: list[float]) -> str:
    if classification == "general":
        return "→ general LLM"
    if not scores:
        return "ESCALATE  (no chunks)"
    top = scores[0]
    if top >= MIN_SCORE:
        above = sum(1 for s in scores if s >= MIN_SCORE)
        return f"ANSWER    ({above} chunks, top={top:.4f})"
    return f"ESCALATE  (top={top:.4f} < {MIN_SCORE})"


# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    print("\n" + "=" * 72)
    print("  WhatsApp RAG Bot — Phase 3 Diagnostic")
    print("=" * 72)

    print("\nLoading RAG index (this takes ~30s the first time)...")
    rag.load_index()
    print("Index ready.\n")

    results = []

    # Table header
    col_msg    = 44
    col_bucket = 11
    col_router = 9
    col_action = 32
    header = (
        f"{'MESSAGE':<{col_msg}} "
        f"{'BUCKET':<{col_bucket}} "
        f"{'ROUTER':<{col_router}} "
        f"{'ACTION':<{col_action}}"
    )
    print(header)
    print("-" * len(header))

    for bucket, message in TEST_MESSAGES:
        # Router call — small model, fast, but still a network round trip
        classification = classify_query(message)

        if classification == "general":
            scores = []
        else:
            scores = retrieve_top_scores(message)

        action = action_label(classification, scores)

        results.append({
            "bucket":         bucket,
            "message":        message,
            "classification": classification,
            "scores":         scores,
        })

        print(
            f"{message[:col_msg]:<{col_msg}} "
            f"{bucket:<{col_bucket}} "
            f"{classification:<{col_router}} "
            f"{action:<{col_action}}"
        )

        time.sleep(0.3)   # gentle rate-limit buffer between router calls

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  SUMMARY")
    print("=" * 72)

    # 1. Router accuracy
    print("\n── Router accuracy ──")

    in_sop_company   = [r for r in results if r["bucket"] == "in_sop"     and r["classification"] == "company"]
    in_sop_general   = [r for r in results if r["bucket"] == "in_sop"     and r["classification"] == "general"]
    out_company      = [r for r in results if r["bucket"] == "out_of_sop" and r["classification"] == "company"]
    out_general      = [r for r in results if r["bucket"] == "out_of_sop" and r["classification"] == "general"]
    gen_general      = [r for r in results if r["bucket"] == "general"    and r["classification"] == "general"]
    gen_company      = [r for r in results if r["bucket"] == "general"    and r["classification"] == "company"]

    def check(count, total, good_label, bad_label):
        icon = "✓" if count == total else "✗"
        return f"  {icon}  {good_label}: {count}/{total}" + (
            f"  ← {total - count} misclassified as {bad_label}" if count < total else ""
        )

    print(check(len(in_sop_company), len(IN_SOP),  "IN_SOP   → company", "general"))
    print(check(len(gen_general),    len(GENERAL),  "GENERAL  → general", "company"))

    if in_sop_general:
        print("\n  ⚠  These IN_SOP messages were sent to the general LLM (bad — no SOP grounding):")
        for r in in_sop_general:
            print(f"       • {r['message']}")
        print("     Fix: tighten ROUTER_SYSTEM_PROMPT to cover these topics explicitly.")

    if gen_company:
        print("\n  ⚠  These GENERAL messages were routed to RAG (wastes a retrieval call, but harmless):")
        for r in gen_company:
            print(f"       • {r['message']}")
        print("     Fix: add counter-examples to ROUTER_SYSTEM_PROMPT.")

    # 2. Score distribution
    print("\n── Score distribution (company-classified queries only) ──")

    in_sop_scores    = [r["scores"][0] for r in results if r["bucket"] == "in_sop"    and r["scores"]]
    out_sop_scores   = [r["scores"][0] for r in results if r["bucket"] == "out_of_sop" and r["scores"]]
    ambig_scores     = [r["scores"][0] for r in results if r["bucket"] == "ambiguous"  and r["scores"]]

    def fmt_scores(scores):
        if not scores:
            return "(none)"
        return "  ".join(f"{s:.4f}" for s in sorted(scores, reverse=True))

    print(f"\n  IN_SOP top scores    : {fmt_scores(in_sop_scores)}")
    print(f"  OUT_OF_SOP top scores: {fmt_scores(out_sop_scores)}")
    print(f"  AMBIGUOUS top scores : {fmt_scores(ambig_scores)}")

    # 3. MIN_SCORE recommendation
    print(f"\n── MIN_SCORE tuning ──")
    print(f"\n  Current MIN_SCORE = {MIN_SCORE}")

    if in_sop_scores and out_sop_scores:
        min_in    = min(in_sop_scores)
        max_out   = max(out_sop_scores)

        if min_in > max_out:
            gap_lo    = max_out
            gap_hi    = min_in
            suggested = round((gap_lo + gap_hi) / 2, 2)
            print(f"  Score gap: {gap_lo:.4f} (highest out-of-SOP) → {gap_hi:.4f} (lowest in-SOP)")
            print(f"  Suggested MIN_SCORE = {suggested}  (midpoint of gap)")
            if abs(suggested - MIN_SCORE) < 0.01:
                print(f"  ✓  Current value is already well-placed. No change needed.")
            else:
                print(f"  → Set MIN_SCORE={suggested} in backend/.env and restart.")
        else:
            print(f"  ✗  Scores overlap: in-SOP min={min_in:.4f}, out-of-SOP max={max_out:.4f}")
            print(f"     No clean threshold exists. Two likely causes:")
            print(f"       1. Your SOPs don't cover some IN_SOP test topics — add content.")
            print(f"       2. Your OUT_OF_SOP topics are too semantically close to SOP content.")
            print(f"       Check the overlap cases in the table above and adjust test messages first.")

    elif not in_sop_scores:
        print("  ✗  No IN_SOP queries reached retrieval (all went to general LLM).")
        print("     Router is over-generalising — fix ROUTER_SYSTEM_PROMPT first.")
    else:
        print("  ✓  All OUT_OF_SOP queries went to general LLM. Only in-SOP scores available.")
        if in_sop_scores:
            print(f"     In-SOP min score: {min(in_sop_scores):.4f}")
            print(f"     Setting MIN_SCORE anywhere below {min(in_sop_scores):.4f} will answer all in-SOP queries.")

    print("\n" + "=" * 72 + "\n")


if __name__ == "__main__":
    run()