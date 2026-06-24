# backend/zoho_mcp/smoke_test.py
"""
Phase 0 smoke test — connect to the Zoho MCP server in isolation.

Run from the backend/ directory:

    python -m zoho_mcp.smoke_test list
    python -m zoho_mcp.smoke_test call <tool_name> '{"json": "args"}'

`list` prints every exposed tool with its required/optional params and writes a
machine-readable schema dump to eval_results/tools_schema.json. Use the exact
tool names it prints when you fill in corpus.jsonl.

`call` invokes a single tool. In Phase 0, only call READ-ONLY tools — you have
not stood up the write/operations servers yet, and you should not.
"""
import argparse
import asyncio
import json
import logging
import os

from zoho_mcp.client import ZohoMCPClient, result_to_text, tool_schema_to_dict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zoho_smoke")

SCHEMA_OUT = os.path.join(os.path.dirname(__file__), "eval_results", "tools_schema.json")


async def cmd_list():
    async with ZohoMCPClient() as zoho:
        tools = await zoho.list_tools()

    print(f"\n{len(tools)} tool(s) exposed by the Zoho MCP server:\n")
    for t in tools:
        first_line = (t.description or "").strip().split("\n")[0]
        print(f"  • {t.name}")
        if first_line:
            print(f"      {first_line}")
        schema = t.inputSchema or {}
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        for key, spec in props.items():
            marker = "*" if key in required else " "
            print(f"        {marker} {key}: {spec.get('type', 'any')}")

    os.makedirs(os.path.dirname(SCHEMA_OUT), exist_ok=True)
    with open(SCHEMA_OUT, "w", encoding="utf-8") as f:
        json.dump([tool_schema_to_dict(t) for t in tools], f, indent=2)
    print(f"\nSchema dumped to: {SCHEMA_OUT}")
    print("→ Copy these exact tool names into corpus.jsonl (the 'expected_tool' field).")


async def cmd_call(name: str, args_json: str):
    arguments = json.loads(args_json) if args_json else {}
    print(f"\nCalling '{name}' with args: {arguments}\n")
    async with ZohoMCPClient() as zoho:
        result = await zoho.call_tool(name, arguments)
    print(f"isError = {getattr(result, 'isError', False)}")
    print("--- result ---")
    print(result_to_text(result))


def main():
    parser = argparse.ArgumentParser(description="Zoho MCP smoke test (Phase 0).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="Connect, list all tools, dump schemas to disk.")
    call_p = sub.add_parser("call", help="Call one tool by name (READ-ONLY tools in Phase 0).")
    call_p.add_argument("tool", help="Exact tool name from the schema dump.")
    call_p.add_argument("args", nargs="?", default="", help="JSON object of arguments.")
    args = parser.parse_args()

    if args.cmd == "list":
        asyncio.run(cmd_list())
    elif args.cmd == "call":
        asyncio.run(cmd_call(args.tool, args.args))


if __name__ == "__main__":
    main()
