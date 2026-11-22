"""
시도별 시뮬레이션(샌드박스): 최신 코드 + 실데이터(실거래·K-apt·청약홈·KOSIS·건축물대장), 위치만 목업.
위치(브이월드)가 목업이라 거리·지도는 의미 없고, 데이터 매칭·추정·문장·파일 생성을 점검.

  python tests/sim_sandbox.py            # 전체
  python tests/sim_sandbox.py 서울 부산   # 일부(이름에 포함된 것)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tests"))
_envf = Path(os.environ.get("REALESTATE_ENV", "/home/claude/realestate-agent/.env"))
for line in (_envf.read_text(encoding="utf-8").splitlines() if _envf.exists() else []):
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
os.environ.setdefault("REALESTATE_CACHE_DIR", str(ROOT / "agent" / "cache"))
sys.argv, ARGS = sys.argv[:1], sys.argv[1:]

import server_core as S  # noqa: E402
import market as M  # noqa: E402
from verify import slide_text  # noqa: E402

M.init(S)
OUT = ROOT / "tests" / "out_sim"

# (이름, 지번주소, 자산명, 본 건 미분양, 법정동코드, 대략 좌표)
TARGETS = [
    ("서울 은평 메디알레", "서울특별시 은평구 대조동 88", "힐스테이트 메디알레", 109, "1138010600", (37.6120, 126.9234)),
    ("서울 강동 디아테온", "서울특별시 강동구 길동 336-1", "디 아테온", 58, "1174010600", (37.5381, 127.1427)),
    ("부산 신평 중앙하이츠", "부산광역시 사하구 신평동 678", "중앙하이츠 더힐", None, "2638010400", (35.0987, 128.9642)),
    ("부산 가야 스카이엘", "부산광역시 부산진구 가야동 3-11", "가야역 롯데캐슬 스카이엘", 78, "2623010900", (35.1556, 129.0398)),
    ("대구 침산 에듀포레", "대구광역시 북구 침산동 1", "북구청역 푸르지오 에듀포레", 15, "2723010500", (35.8848, 128.5855)),
    ("인천 검단 로열파크씨티", "인천광역시 서구 왕길동 133-3", "왕길역 로열파크씨티 푸르지오", 233, "2829010800", (37.5981, 126.6702)),
    ("대전 가양 더와이즈", "대전광역시 동구 가양동 452-1", "힐스테이트 가양 더와이즈", 10, "3011011300", (36.3427, 127.4483)),
    ("울산 신정 노르웨이숲", "울산광역시 남구 신정동 1", "남울산 노르웨이숲", None, "3114010400", (35.5370, 129.3080)),
    ("세종 산울 엘리프", "세종특별자치시 산울동 1", "엘리프세종6-3", 4, "3611011900", (36.4800, 127.2550)),
    ("경기 평촌 어바인퍼스트", "경기도 안양시 동안구 호계동 1269", "평촌 어바인퍼스트", 104, "4117310400", (37.3720, 126.9620)),
    ("강원 속초 힐스테이트", "강원특별자치도 속초시 금호동 622-40", "힐스테이트 속초", 50, "5121010500", (38.2040, 128.5900)),
    ("충북 청주 테크노폴리스", "충청북도 청주시 흥덕구 송절동 1195", "해링턴 플레이스 테크노폴리스", 6, "4311311600", (36.6640, 127.4060)),
    ("충남 아산 자이1단지", "충청남도 아산시 용화동 137-1", "아산자이 그랜드파크 1단지", 47, "4420010700", (36.7840, 127.0130)),
    ("전북 익산 자이", "전북특별자치도 익산시 마동 434", "익산자이 그랜드파크", 4, "5214011200", (35.9460, 126.9650)),
    ("전남 순천 푸르지오", "전남광주통합특별시 순천시 덕암동 142", "순천 푸르지오 더 퍼스트", 21, "1215011400", (34.9470, 127.5190)),
    ("광주 금호 위파크", "전남광주통합특별시 서구 금호동 1", "위파크 마륵공원", None, "1224012900", (35.1380, 126.8590)),
    ("경북 경주 황성", "경상북도 경주시 황성동 45", "힐스테이트 황성", 43, "4713012500", (35.8640, 129.2210)),
    ("경남 창원 하버팰리스", "경상남도 창원시 마산회원구 양덕동 1", "창원 롯데캐슬 하버팰리스", 2, "4812710500", (35.2200, 128.5810)),
    ("제주 서귀포 형남6차", "제주특별자치도 서귀포시 서홍동 384-5", "서귀포 형남아파트6차", 45, "5013010400", (33.2560, 126.5590)),
    # 거래가 드문 군 지역(엣지 케이스)
    ("경북 울진 두산위브(희소)", "경상북도 울진군 울진읍 읍남리 503", "울진역센트럴두산위브", None, "4793025022", (36.9905, 129.3975)),
    ("강원 양양 우미린(희소)", "강원특별자치도 양양군 강현면 강선리 987", "양양우미린디오션", None, "5183035024", (38.1040, 128.6360)),
    ("충북 음성 자이(신축)", "충청북도 음성군 금왕읍 무극리 776", "음성자이센트럴시티", None, "4377025321", (36.9930, 127.5880)),
    ("강원 고성 데시앙(신축·희소)", "강원특별자치도 고성군 토성면 아야진리 876", "아야진라메르데시앙", None, "5182033023", (38.2690, 128.5560)),
    ("경북 칠곡 유라밸(기준)", "경상북도 칠곡군 석적읍 중리 647", "우방아이유쉘유라밸", 310, "4785025622", (36.07438, 128.40691)),
]


def install(addr: str, bjd: str, pt: tuple[float, float]) -> None:
    parts = M.split_address(addr)

    async def ensure_point(loc):
        loc["lat"], loc["lon"] = pt
        loc.setdefault("address", addr)
        loc.setdefault("lawd_cd", bjd[:5])
        return loc

    async def rg(lat, lon):
        return {"text": addr, "bjd": bjd, "sido": parts["sido"], "sgg": parts["sgg"], "umd": parts["umd"]}

    async def gp(a):
        h = int(hashlib.md5(a.encode()).hexdigest()[:8], 16)
        ang = (h % 360) * math.pi / 180
        d = ((h >> 9) % 14) / 10
        return pt[0] + d * math.cos(ang) / 111, pt[1] + d * math.sin(ang) / (111 * math.cos(math.radians(pt[0])))

    async def none(*a, **k):
        return None

    async def empty(*a, **k):
        return []

    async def basemap(lat, lon, zoom, w, h, bm="GRAPHIC"):
        return {"lat": lat, "lon": lon, "zoom": zoom, "w": w, "h": h, "png": None}

    async def map_image(*a, **k):
        raise S.ApiError("목업: 지도 없음")

    S.ensure_point = ensure_point
    S.map_image = map_image
    M.reverse_geocode, M.geocode_parcel, M.geocode_road = rg, gp, none
    M.place_bbox, M.basemap = empty, basemap
    M.parcel_polygon = empty


async def run_one(t) -> dict:
    name, addr, asset, unsold_n, bjd, pt = t
    install(addr, bjd, pt)
    M._KAPT_STATE.update(consec_fail=0, open=False)
    t0 = time.time()
    rec = {"이름": name}
    try:
        r = await M.market_report(addr, asset_name=asset, target_unsold_units=unsold_n, make_pdf=False,
                                  out_dir=str(OUT / name))
    except Exception as e:
        rec.update(오류=f"{type(e).__name__}: {e}", 추적=traceback.format_exc()[-500:])
        return rec
    rec["초"] = round(time.time() - t0, 1)
    ps = r.get("분양정보") or {}
    est = r.get("시세추정") or {}
    rec.update({
        "슬라이드": len(r["슬라이드"]), "분양매칭": ps.get("주택명"), "비교면적": (r.get("비교면적") or {}).get("구간"),
        "미분양": (r["미분양"] or {}).get("시군구") and f"{r['미분양']['시군구']} {(r['미분양'].get('준공후') or [None])[-1]}",
        "비교단지": len(r["비교단지"]), "추이단지": ((r["진단"] or {}).get("비교단지") or {}).get("추이단지수"),
        "전세추정": (est.get("전세") or {}).get("방법"), "매매추정": (est.get("매매") or {}).get("방법"),
        "전세가율": {k: v.get("전세가율") for k, v in (est.get("시군구전세가율") or {}).items()},
        "경고": r["경고"],
    })
    txt = slide_text(r["PPT"])
    bad = [w for w in ("None", "nan", "2083", "2097", "2098", "{", "}") if w in txt]
    rec["글자오류"] = bad
    # 분양정보 매칭 점검: 번호·지명 충돌
    if ps.get("주택명") and S._num_conflict(S.norm_name(ps["주택명"]), S.norm_name(asset)):
        rec["분양오매칭"] = True
    return rec


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sel = [t for t in TARGETS if not ARGS or any(a in t[0] for a in ARGS)]
    recs = []
    for t in sel:
        rec = await run_one(t)
        recs.append(rec)
        flag = "ERR" if rec.get("오류") else ("WARN" if rec.get("글자오류") or rec.get("분양오매칭") or not rec.get("미분양") else "OK ")
        print(flag, json.dumps(rec, ensure_ascii=False)[:420], flush=True)
    (ROOT / "tests" / "sim_result.json").write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
    n_ok = sum(1 for r in recs if not r.get("오류"))
    print(f"\n완료 {n_ok}/{len(recs)}")


if __name__ == "__main__":
    asyncio.run(main())
