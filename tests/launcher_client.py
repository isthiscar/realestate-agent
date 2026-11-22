import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main(ext):
    env = dict(os.environ, GITHUB_TOKEN_REALESTATE="${user_config.github_token}", REALESTATE_AUTO_UPDATE="true",
               DATA_GO_KR_API_KEY=open("/home/claude/realestate-agent/.env").read().split("DATA_GO_KR_API_KEY=")[1].split()[0])
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(ext, "launcher.py")], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", len(tools.tools), [t.name for t in tools.tools][-6:])
            res = await s.call_tool("agent_update", {"force": True})
            print(res.content[0].text[:600])
            res = await s.call_tool("get_unsold_housing", {"location": "경북 칠곡군", "months": 3})
            print(res.content[0].text[:200])
asyncio.run(main(sys.argv[1]))
