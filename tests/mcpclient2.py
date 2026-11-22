import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    env = dict(os.environ, VWORLD_API_KEY="${user_config.vworld_api_key}", DATA_GO_KR_API_KEY=open("/home/claude/realestate-agent/.env").read().split("DATA_GO_KR_API_KEY=")[1].split()[0])
    params = StdioServerParameters(command=sys.executable, args=["/tmp/mcpbcheck/src/server.py"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", len(tools.tools), [t.name for t in tools.tools][-5:])
            t = next(t for t in tools.tools if t.name == "create_market_report")
            print("props:", list(t.input_schema["properties"].keys()))
            res = await s.call_tool("get_unsold_housing", {"location": "경북 칠곡군", "months": 4, "target_units": 310})
            print(res.content[0].text[:600])
asyncio.run(main())
