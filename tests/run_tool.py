import asyncio,json,sys,time
sys.path.insert(0,'/home/claude/realestate-agent'); sys.argv=['x']
import realestate_mcp as S, market as M, mock_vw
M.init(S); mock_vw.install_all(S)
S.IMAGE_DIR = __import__('pathlib').Path('/home/claude/tests/out/부동산지도')
S.REPORT_WAIT = 3
async def go():
    r = await S.create_market_report("경북 칠곡군 석적읍 중리 647", target_unsold_units=310, target_sale_price=28100, target_jeonse=14100,
                                     make_pdf=True)
    d = json.loads(r); print(d.get('상태'), d.get('job_id'), d.get('안내'))
    while d.get('상태') == '작성 중':
        r = await S.check_market_report(d['job_id']); d = json.loads(r); print(d.get('상태'))
    print(json.dumps({k:d.get(k) for k in ('PPT','PDF','PDF변환','경고')},ensure_ascii=False))
    print(json.dumps(d['문장초안(bullets)']['prices'],ensure_ascii=False))
    u = json.loads(await S.get_unsold_housing("칠곡군", 6)); print(u.get('시군구'), u.get('준공후'), u.get('오류'))
asyncio.run(go())
