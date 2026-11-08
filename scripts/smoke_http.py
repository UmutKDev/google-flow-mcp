"""Smoke-test a running server over Streamable HTTP (spends nothing).

    python scripts/smoke_http.py http://localhost:8080/mcp [--token SECRET] [--status]
"""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--token")
    ap.add_argument("--status", action="store_true", help="also call flow_status (starts Chrome)")
    args = ap.parse_args()

    http_client = None
    if args.token:
        from mcp.shared._httpx_utils import create_mcp_http_client

        http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {args.token}"})
    async with streamable_http_client(args.url, http_client=http_client) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("server:", init.server_info.name, init.server_info.version)
            tools = await session.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))
            res = await session.call_tool("flow_estimate_video_cost", {"mode": "text", "duration_s": 8})
            print("estimate:", res.content[0].text.replace("\n", " "))
            if args.status:
                res = await session.call_tool("flow_status", {})
                print("status:", json.loads(res.content[0].text))


if __name__ == "__main__":
    asyncio.run(main())
