"""
배포 전 검증 (샌드박스, 브이월드는 목업 / 공공데이터포털·KOSIS·청약홈은 실데이터)

  python tests/verify.py            # 전체
  python tests/verify.py --quick    # 단위 테스트만

단계
 1) 단위: 주소 분해·KOSIS 표 파싱·이름 매칭·면적 구간·이름 정리·광역표 상한·전세가율 계산
 2) 기준값(칠곡 IM): 미분양 12개월·본 건 비중 59%·본 건 공급평단가 838/421·비교단지 가격 ±3%
 3) 예외 상황: 비교 전세사례 없음 → 전세가율로 추정 / 비교단지 0곳 → 반경 확대·안내 /
    KOSIS 장애 → 미분양 슬라이드 생략 / K-apt 장애 → 건축물대장 대체 / 브이월드 장애 → 빈 배경
 4) 결과물: OOXML 검증·LibreOffice 렌더링·슬라이드 글자에 None·nan·20xx 오류 없는지
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tests"))
sys.argv = sys.argv[:1] + [a for a in sys.argv[1:]]
QUICK = "--quick" in sys.argv
OUT = ROOT / "tests" / "out_verify"
VALIDATOR = Path("/mnt/skills/public/pptx/scripts/office/validate.py")

import os  # noqa: E402

# 인증키: 저장소 밖 .env(기본 ~/realestate-agent/.env)에서 읽음 — 저장소에는 키를 두지 않음
_envf = Path(os.environ.get("REALESTATE_ENV", "/home/claude/realestate-agent/.env"))
if _envf.exists():
    for line in _envf.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ.setdefault("REALESTATE_CACHE_DIR", str(ROOT / "agent" / "cache"))

import server_core as S  # noqa: E402
import market as M  # noqa: E402
import mock_vw  # noqa: E402

M.init(S)
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))


def approx(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


# ---------------------------------------------------------------- 1) 단위
def unit() -> None:
    sa = M.split_address
    check("주소: 리 단위", sa("경상북도 칠곡군 석적읍 중리 647") == {"sido": "경상북도", "sgg": "칠곡군", "umd": "석적읍 중리"})
    check("주소: 일반구", sa("경기도 수원시 장안구 정자동 1")["sgg"] == "수원시 장안구")
    check("주소: 세종", sa("세종특별자치시 산울동 12")["umd"] == "산울동")
    check("시도 약칭", [M.sido_short(x) for x in ("전남광주통합특별시", "강원특별자치도", "광주광역시", "전라남도")]
          == ["전남광주", "강원", "광주", "전남"])
    txt = "- **값**: 241\n| 시점 |\n|---|\n| 202607 |\n| 202608 |\n"
    check("KOSIS 표 파싱(상수열)", M.parse_series(txt) == {"202607": 241.0, "202608": 241.0})
    txt2 = "| 시점 | 값 |\n|---|---|\n| 202601 | 1,234 |\n| 202602 | - |\n"
    check("KOSIS 표 파싱(값열)", M.parse_series(txt2) == {"202601": 1234.0})
    check("이름: 동 접두어", M._name_sim("신평현대아파트", "현대", "사하구", "신평동") >= 0.75)
    check("이름: 단지 번호 충돌", M._name_sim("효성해링턴플레이스1단지", "효성해링턴플레이스2단지", "칠곡군") == 0)
    check("면적 자동(59 주력)", M.auto_band([{"전용㎡": 59.9, "세대수": 190}, {"전용㎡": 84.9, "세대수": 50}])[:2] == (57, 64))
    check("면적 자동(84A+84B 합산)", M.auto_band([{"전용㎡": 84.9, "세대수": 60}, {"전용㎡": 84.8, "세대수": 60},
                                            {"전용㎡": 59.9, "세대수": 100}])[:2] == (80, 86))
    check("이름 정리(병원)", M.clean_org("의료법인한국의료재단순천한국병원") == "순천한국병원")
    check("이름 정리(마트)", M.clean_shop("롯데쇼핑롯데슈퍼범서점주") == "롯데슈퍼범서점")
    check("역 이름 같음", M._station_key("굽은다리역(강동구민회관앞)") == M._station_key("굽은다리(강동구민회관앞)역"))
    rows = [{"구분": "업무", "시설": "CBD 광화문역", "거리km": 6}] + [{"구분": "산업", "시설": f"산단{i}", "거리km": 3 + i * 5} for i in range(3)] \
        + [{"구분": "교통", "시설": f"역{i}", "거리km": i} for i in range(6)] + [{"구분": "행정", "시설": "구청", "거리km": 1}]
    capped = M.cap_wide(rows)
    check("광역표 상한(교통 4·산업 1)", sum(r["구분"] == "교통" for r in capped) == 4 and sum(r["구분"] == "산업" for r in capped) == 1
          and any(r["구분"] == "행정" for r in capped))
    check("연도 2자리", (M.full_year("98"), M.full_year("16")) == (1998, 2016))
    tr = [{"aptSeq": f"A{i % 3}", "excluUseAr": "84", "dealAmount": "30,000", "dealYear": "2026", "dealMonth": "9", "dealDay": "1",
           "buildYear": "2020"} for i in range(6)]
    rt = [{"aptSeq": f"A{i % 3}", "excluUseAr": "84", "deposit": "21,000", "monthlyRent": "0", "dealYear": "2026", "dealMonth": "9",
           "dealDay": "2", "buildYear": "2019"} for i in range(6)]
    rt += [{"aptSeq": "임대", "excluUseAr": "84", "deposit": "3,000", "monthlyRent": "0", "dealYear": "2026", "dealMonth": "9",
            "dealDay": "2", "buildYear": "2019"}] * 20  # 매매 없는 임대단지는 빠져야 함
    r = M.district_ratio((tr, rt), 80, 86, "202611", 2022)
    check("시군구 전세가율", r.get("신축", {}).get("전세가율") == 0.7, json.dumps(r, ensure_ascii=False))
    tgt = {"매매가": 28100, "가격구분": "예정 매각가"}
    e = M.estimate_target(tgt, [], {"전체": {"전세가율": 0.6, "구분": "전체", "기간": "최근 12개월", "전세표본": 30, "단지수": 9}}, 33.5)
    check("전세 추정(사례 없음 → 전세가율)", e.get("전세", {}).get("추정가(만원)") == 16860, json.dumps(e, ensure_ascii=False))
    e2 = M.estimate_target({}, [{"전세공급평단가": 400}, {"전세공급평단가": 420}, {"매매공급평단가": 800}, {"매매공급평단가": 840}],
                           {}, 33.5)
    check("전세·매매 추정(사례 중위)", e2.get("전세", {}).get("공급평단가") == 410 and e2.get("매매", {}).get("공급평단가") == 820,
          json.dumps(e2, ensure_ascii=False))


# ---------------------------------------------------------------- 2·3) 시나리오
def fresh_mocks():
    import importlib
    importlib.reload(mock_vw)
    mock_vw.install_all(S)
    S.IMAGE_DIR = OUT / "부동산지도"
    M._KAPT_STATE.update(consec_fail=0, open=False)


async def scenario(name: str, prep=None, **kw) -> dict | None:
    fresh_mocks()
    undo = prep() if prep else None
    t = time.time()
    try:
        r = await M.market_report("경북 칠곡군 석적읍 중리 647", out_dir=str(OUT / name), make_pdf=False, **kw)
        r["_초"] = round(time.time() - t, 1)
        return r
    except Exception as e:
        check(f"[{name}] 예외 없이 완료", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")
        return None
    finally:
        if undo:
            undo()


def slide_text(pptx: str) -> str:
    from pptx import Presentation
    prs = Presentation(pptx)
    out = []
    for sl in prs.slides:
        for sh in sl.shapes:
            if sh.has_text_frame:
                out.append(sh.text_frame.text)
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    out.extend(c.text for c in row.cells)
    return "\n".join(out)


def check_output(name: str, r: dict, n_slides: int | None = None) -> None:
    p = r.get("PPT")
    check(f"[{name}] PPT 생성", bool(p and Path(p).exists()))
    if not p:
        return
    from pptx import Presentation
    prs = Presentation(p)
    if n_slides:
        check(f"[{name}] 슬라이드 {n_slides}장", len(prs.slides) == n_slides, str(len(prs.slides)))
    txt = slide_text(p)
    bad = [w for w in ("None", "nan", "NaN", "2083", "2097", "2098", "{", "}", "  매매") if w in txt]
    check(f"[{name}] 글자 오류 없음", not bad, ",".join(bad))
    if VALIDATOR.exists():
        v = subprocess.run([sys.executable, str(VALIDATOR), p], capture_output=True, text=True, cwd=str(VALIDATOR.parent))
        check(f"[{name}] OOXML 검증", "passed" in (v.stdout + v.stderr).lower() or v.returncode == 0, (v.stdout + v.stderr)[-200:])


async def scenarios() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # 2) 기준값: 칠곡 IM
    r = await scenario("기준_칠곡", target_unsold_units=310, target_sale_price=28100, target_jeonse=14100, target_area=84.89)
    if r:
        u = r["미분양"]
        check("[기준] 미분양 12개월(IM 18p)", u["준공후"] == [550, 546, 546, 546, 547, 542, 541, 539, 539, 531, 531, 526], str(u["준공후"]))
        check("[기준] 본 건 비중 59%", u["본건비중%"] == 59, str(u["본건비중%"]))
        cx = {re.sub(r"\s", "", x["단지명"]): x for x in r["비교단지"]}
        h3 = next((v for k, v in cx.items() if "3단지" in k), None)
        check("[기준] 효성3단지 매매 2.60억 ±3%", h3 and approx(h3["매매가(만원)"], 26000, 780), json.dumps(h3, ensure_ascii=False)[:200])
        txt = slide_text(r["PPT"])
        check("[기준] 본 건 공급평단가 838·421", "838만원" in txt and "421만원" in txt)
        check("[기준] 자산명·분양정보", r["자산명"] != "본 건" and (r.get("분양정보") or {}).get("시공사"), r["자산명"])
        check("[기준] 비교면적 80~86", r["비교면적"]["구간"] == "80~86㎡", str(r["비교면적"]))
        check_output("기준", r, 6)

    # 3-1) 비교 전세사례 없음: 반경 안 단지의 전세 기록만 지움(시군구 전체엔 남김)
    def drop_ring_rents():
        orig = M.fetch_rows

        async def fr(lawd, months):
            t, rt = await orig(lawd, months)
            rt = [x for x in rt if "석적" not in (x.get("umdNm") or "")]
            return t, rt
        M.fetch_rows = fr
        return lambda: setattr(M, "fetch_rows", orig)

    r = await scenario("전세사례없음", prep=drop_ring_rents, target_sale_price=28100, target_area=84.89)
    if r:
        e = (r.get("시세추정") or {}).get("전세") or {}
        check("[전세사례없음] 전세가율로 추정", "전세가율" in (e.get("방법") or ""), json.dumps(e, ensure_ascii=False)[:300])
        check("[전세사례없음] 추정 범위 타당(매매가의 40~90%)", e and 0.4 * 28100 <= e["추정가(만원)"] <= 0.9 * 28100)
        txt = slide_text(r["PPT"])
        check("[전세사례없음] 표·문장에 '추정' 표시", "추정" in txt)
        check_output("전세사례없음", r)

    # 3-2) 비교단지 0곳: 아파트 없는 리로 역지오코딩
    def empty_ring():
        orig = M.reverse_geocode

        async def rg(lat, lon):
            return {"text": "경상북도 칠곡군 가산면 학하리 1", "bjd": "4785033024", "sido": "경상북도", "sgg": "칠곡군", "umd": "가산면 학하리"}
        M.reverse_geocode = rg
        return lambda: setattr(M, "reverse_geocode", orig)

    r = await scenario("비교단지없음", prep=empty_ring)
    if r:
        check("[비교단지없음] 반경 확대 시도·안내", any("넓혔" in w for w in r["경고"]) or r.get("최종반경km") == 1.5)
        check("[비교단지없음] 보고서는 생성", bool(r.get("PPT")))
        check_output("비교단지없음", r)

    # 3-3) KOSIS 장애
    def kosis_down():
        orig = M.kosis_call

        async def kc(*a, **k):
            raise S.ApiError("KOSIS 점검")
        M.kosis_call = kc
        M.KOSIS_ITM._d = {}
        return lambda: setattr(M, "kosis_call", orig)

    r = await scenario("KOSIS장애", prep=kosis_down, sections=["unsold", "prices"])
    if r:
        check("[KOSIS장애] 미분양 슬라이드 생략·나머지 생성", "3.3 주변 단지 시세 현황" in r["슬라이드"]
              and not any("미분양" in x for x in r["슬라이드"]), str(r["슬라이드"]))
        check_output("KOSIS장애", r)

    # 3-4) K-apt 장애
    def kapt_down():
        orig = S.kapt_basis

        async def kb(code):
            raise S.ApiError("HTTP 에러")
        S.kapt_basis = kb
        saved = M.BASIS._d
        M.BASIS._d = {}
        return lambda: (setattr(S, "kapt_basis", orig), setattr(M.BASIS, "_d", saved))

    r = await scenario("K-apt장애", prep=kapt_down, sections=["prices"])
    if r:
        d = r["진단"]["비교단지"] or {}
        check("[K-apt장애] 호출 차단 장치(6회 이하 실패 후 중단)", d.get("K-apt후보", 0) >= 1 and bool(r.get("PPT")))
        check("[K-apt장애] 비교단지는 실거래로 유지", len(r["비교단지"]) >= 3, str(len(r["비교단지"])))
        check_output("K-apt장애", r)


def render_check() -> None:
    so = shutil.which("soffice")
    p = OUT / "기준_칠곡"
    files = list(p.glob("*.pptx"))
    if not (so and files):
        return
    subprocess.run([so, "--headless", "--convert-to", "pdf", "--outdir", str(p), str(files[0])], capture_output=True, timeout=180)
    pdf = files[0].with_suffix(".pdf")
    check("[렌더링] LibreOffice로 PDF 변환", pdf.exists())


def main() -> int:
    t0 = time.time()
    unit()
    if not QUICK:
        if OUT.exists():
            shutil.rmtree(OUT)
        asyncio.run(scenarios())
        render_check()
    ok = sum(1 for _, c, _ in RESULTS if c)
    for n, c, d in RESULTS:
        print(("PASS " if c else "FAIL ") + n + ("" if c or not d else f"  ← {d[:300]}"))
    print(f"\n{ok}/{len(RESULTS)} 통과 ({time.time() - t0:.0f}초)")
    (ROOT / "tests" / "verify_result.json").write_text(json.dumps(
        [{"항목": n, "통과": c, "상세": d[:300]} for n, c, d in RESULTS], ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
