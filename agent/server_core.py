"""
부동산 에이전트 MCP 서버
========================
주소·지번·도로명·아파트명을 넣으면 공공 API에서 바로 조회합니다.

  - 실거래가 12종 (아파트/오피스텔/연립다세대/단독다가구 매매·전월세, 분양권, 상업업무용, 토지, 공장창고)
  - 공시지가·토지특성·토지이용계획·토지소유 (브이월드)
  - 공동주택가격·개별주택가격 (브이월드)
  - 건축물대장 (표제부·총괄표제부·층별·전유부·주택가격 등), 건축/주택 인허가
  - 공동주택 단지정보 (K-apt), 청약홈 분양정보·경쟁률·당첨가점·통계
  - 지도 이미지 (항공사진/일반지도 + 지적도/용도지역 오버레이)
  - 미분양 (KOSIS, 국가데이터처 MCP 시범서비스 경유) · 주변시설 직선거리
  - 시장·입지 보고서 PPT/PDF (IM 양식: 자산개요·광역/협역 입지·미분양·거래추이·주변시세)
  - GitHub 자동 업데이트(agent_update)

실행:  python realestate_mcp.py            (MCP stdio 서버)
점검:  python realestate_mcp.py --selftest "서울 동대문구 휘경동 172"
캐시:  python realestate_mcp.py --init      (전국 아파트 단지목록 갱신)

키 설정: 같은 폴더의 .env 파일
  DATA_GO_KR_API_KEY=공공데이터포털 일반 인증키
  VWORLD_API_KEY=브이월드 인증키
  VWORLD_DOMAIN=localhost
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import math
import os
import re
import statistics
import sys
import time
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated, Any, Optional

import httpx
from pydantic import Field

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server, Image
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server, Image

try:
    from PIL import Image as PILImage, ImageDraw
except ImportError:  # Pillow 없으면 오버레이 합성만 생략
    PILImage = None
    ImageDraw = None

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
# 자동 업데이트 시 코드 폴더가 바뀌므로 캐시는 확장 폴더의 고정 위치(런처가 지정)를 씀
_cache_env = os.environ.get("REALESTATE_CACHE_DIR", "").strip()
CACHE_DIR = Path(_cache_env) if _cache_env and not _cache_env.startswith("${") else BASE_DIR / "cache"


def _load_env() -> None:
    p = BASE_DIR / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default).strip()
    return "" if v.startswith("${") else v  # 치환 안 된 확장 설정값은 빈 값 처리


DATA_KEY = _env("DATA_GO_KR_API_KEY")
VW_KEY = _env("VWORLD_API_KEY")
VW_DOMAIN = os.environ.get("VWORLD_DOMAIN", "localhost").strip()
_img_env = os.environ.get("REALESTATE_IMAGE_DIR", "").strip()
IMAGE_DIR = (Path(_img_env) / "부동산지도") if _img_env and not _img_env.startswith("${") else BASE_DIR / "images"

DG = "https://apis.data.go.kr"
OD = "https://api.odcloud.kr/api"
VW = "https://api.vworld.kr"

logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("realestate")


class ApiError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP (재시도 + 메모리 캐시)
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 30 * 60
_sem = asyncio.Semaphore(6)
_sem_dg = asyncio.Semaphore(4)  # 공공데이터포털은 동시 호출을 조금 줄여 초당 제한·회사망 타임아웃을 피함


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": "realestate-mcp/1.0"},
            follow_redirects=True,
        )
    return _client


_TRANSIENT_MARKERS = (b"LIMITED_NUMBER_OF_SERVICE_REQUESTS", b"SERVICE_TIMEOUT", b"HTTP_ERROR", b"DB_ERROR",
                      b"UNKNOWN_ERROR", b"APPLICATION_ERROR", b"HTTP ROUTING ERROR", b"Service Unavailable")


def _bad_body(r: httpx.Response) -> tuple[bool, bool]:
    """(오류 응답인가, 잠시 뒤 재시도할 만한가). 오류 응답은 캐시하지 않음."""
    head = r.content[:1200]
    if r.status_code in (429, 502, 503, 504):
        return True, True
    if not head.strip():
        return True, True
    if any(m in head for m in _TRANSIENT_MARKERS):
        return True, True
    if b"cmmMsgHeader" in head or b"returnAuthMsg" in head or b"OpenAPI_ServiceResponse" in head:
        return True, False
    return False, False


async def _get(url: str, params: dict, *, cache: bool = True, retries: int = 4) -> httpx.Response:
    ck = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in ("serviceKey", "key"))
    now = time.time()
    if cache and ck in _cache and now - _cache[ck][0] < CACHE_TTL:
        return _cache[ck][1]
    last: Exception | None = None
    r: httpx.Response | None = None
    for attempt in range(retries):
        try:
            async with (_sem_dg if url.startswith(DG) else _sem):
                r = await _http().get(url, params=params)
            if r.status_code in (401, 403):
                raise ApiError(f"인증 실패({r.status_code}). 해당 API 활용신청·인증키를 확인하세요: {url.split('?')[0]}")
            bad, retry = _bad_body(r)
            if bad and retry and attempt < min(retries, 2) - 1:  # 일시 오류는 한 번만 다시(재시도 폭주 방지)
                await asyncio.sleep(1.5)
                continue
            if r.status_code >= 500 and attempt < retries - 1:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            if cache and not bad:
                _cache[ck] = (now, r)
            return r
        except ApiError:
            raise
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last = e
            await asyncio.sleep(0.8 * (attempt + 1))
    if r is not None:
        return r
    raise ApiError(f"요청 실패: {url} ({last})")


# ---------------------------------------------------------------------------
# 공공데이터포털 / 오픈클라우드 / 브이월드 호출
# ---------------------------------------------------------------------------
def _need_data_key() -> None:
    if not DATA_KEY:
        raise ApiError("공공데이터포털 인증키가 없습니다. 확장 설정(또는 .env의 DATA_GO_KR_API_KEY)에 넣어주세요.")


def _need_vw_key() -> None:
    if not VW_KEY:
        raise ApiError("이 기능은 브이월드 인증키가 필요합니다. 확장 설정(또는 .env의 VWORLD_API_KEY)에 넣어주세요.")


def _xml_items(content: bytes) -> tuple[list[dict], int]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        txt = content[:300].decode("utf-8", "ignore")
        raise ApiError(f"응답 해석 실패: {txt}")
    auth = root.findtext(".//returnAuthMsg") or root.findtext(".//ERROR_MSG")
    if auth:
        raise ApiError(f"API 오류: {auth}")
    code = (root.findtext(".//resultCode") or "").strip()
    if code not in ("", "000", "00", "0"):
        raise ApiError(f"API 오류 {code}: {root.findtext('.//resultMsg')}")
    items = [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]
    total = root.findtext(".//totalCount")
    return items, int(total) if total and total.strip().isdigit() else len(items)


async def dg_xml(path: str, params: dict) -> tuple[list[dict], int]:
    _need_data_key()
    r = await _get(f"{DG}/{path}", {"serviceKey": DATA_KEY, **params})
    return _xml_items(r.content)


async def dg_json(path: str, params: dict) -> Any:
    _need_data_key()
    r = await _get(f"{DG}/{path}", {"serviceKey": DATA_KEY, **params})
    try:
        return r.json()
    except ValueError:
        if r.content.lstrip().startswith(b"<"):
            items, total = _xml_items(r.content)
            return {"items": items, "totalCount": total}
        raise ApiError(f"응답 해석 실패: {r.text[:300]}")


def _json_items(data: Any) -> tuple[list[dict], int]:
    """data.go.kr JSON 응답(response.body.items[.item])에서 item 목록 추출."""
    resp = data.get("response", data) if isinstance(data, dict) else {}
    head = resp.get("header", {})
    code = str(head.get("resultCode", "00"))
    if code not in ("00", "000", "0"):
        raise ApiError(f"API 오류 {code}: {head.get('resultMsg')}")
    body = resp.get("body", {}) or {}
    items = body.get("items", [])
    if isinstance(items, dict):
        items = items.get("item", [])
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    if not items and isinstance(body.get("item"), dict):
        items = [body["item"]]
    total = body.get("totalCount", len(items))
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = len(items)
    return items, total


async def od_get(path: str, params: dict) -> dict:
    _need_data_key()
    r = await _get(f"{OD}/{path}", {"serviceKey": DATA_KEY, "returnType": "JSON", **params})
    try:
        return r.json()
    except ValueError:
        raise ApiError(f"청약홈 API 응답 해석 실패: {r.text[:300]}")


async def vw_json(path: str, params: dict) -> dict:
    _need_vw_key()
    p = {"key": VW_KEY, "domain": VW_DOMAIN, "format": "json", **params}
    r = await _get(f"{VW}{path}", p)
    try:
        return r.json()
    except ValueError:
        raise ApiError(f"브이월드 응답 해석 실패: {r.text[:300]}")


async def vw_search(query: str, typ: str, category: str | None = None, size: int = 5) -> list[dict]:
    params = {"service": "search", "request": "search", "version": "2.0", "size": size, "page": 1,
              "query": query, "type": typ, "errorformat": "json", "crs": "EPSG:4326"}
    if category:
        params["category"] = category
    d = await vw_json("/req/search", params)
    resp = d.get("response", {})
    if resp.get("status") != "OK":
        return []
    return resp.get("result", {}).get("items", []) or []


async def ned(op: str, params: dict, rows: int = 100, max_pages: int = 1) -> list[dict]:
    """브이월드 국가중점데이터(NED) 속성조회."""
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        d = await vw_json(f"/ned/data/{op}", {"numOfRows": rows, "pageNo": page, **params})
        block = next((v for v in d.values() if isinstance(v, dict)), {})
        if block.get("resultCode") and block.get("resultCode") not in ("", "OK"):
            raise ApiError(f"브이월드 오류: {block.get('resultMsg') or block.get('resultCode')}")
        fields = block.get("field", []) or []
        if isinstance(fields, dict):
            fields = [fields]
        out.extend(fields)
        total = int(block.get("totalCount") or 0)
        if len(out) >= total or not fields:
            break
    if not out:  # 통합 등으로 코드가 바뀐 지역: 옛 코드로 재조회
        for k in ("pnu", "ldCode"):
            old = legacy_code(params.get(k))
            if old:
                return await ned(op, {**params, k: old}, rows, max_pages)
    return out


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
NOISE_KEYS = {"rnum", "mgmBldrgstPk", "mgmPmsrgstPk", "mgmHsrgstPk", "naRoadCd", "naBjdongCd", "naUgrndCd",
              "naMainBun", "naSubBun", "crtnDay", "lastUpdtDt", "regstrSeCode", "sigunguCd", "bjdongCd",
              "platGbCd", "roadNmCd", "roadNmSeq", "roadNmbCd", "roadNmSggCd", "roadnmcd", "roadnmseq",
              "roadnmbcd", "roadnmsggcd", "landCd", "sggCd", "umdCd", "aptSeq", "bonbun", "bubun",
              "roadNmBonbun", "roadNmBubun", "roadnmbonbun", "roadnmbubun"}


def clean(d: dict) -> dict:
    """빈 값·코드 중복 필드 제거."""
    out = {}
    for k, v in d.items():
        if v is None or (isinstance(v, str) and not v.strip()) or k in NOISE_KEYS:
            continue
        if k.endswith("Cd") and (k + "Nm") in d:
            continue
        if k.endswith("Code") and (k + "Nm") in d:
            continue
        out[k] = v.strip() if isinstance(v, str) else v
    return out


def num(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def won(v: float | None) -> str | None:
    return None if v is None else f"{int(round(v)):,}"


def dumps(obj: Any, limit: int = 60000) -> str:
    s = json.dumps(obj, ensure_ascii=False, indent=1, default=str)
    if len(s) > limit:
        s = s[:limit] + "\n... (결과가 길어 잘렸습니다. 조건을 좁혀 다시 조회하세요)"
    return s


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"[\s\-·,.'\"_]|아파트|APT|apt", "", s)
    return s.lower()


SIDO_ABBR = {
    "서울": "서울특별시", "서울시": "서울특별시", "부산": "부산광역시", "부산시": "부산광역시",
    "대구": "대구광역시", "대구시": "대구광역시", "인천": "인천광역시", "인천시": "인천광역시",
    # 2026년 광주광역시+전라남도 → 전남광주통합특별시 (코드 12). '광주시'는 경기도 광주시와 겹쳐 제외
    "광주": "전남광주통합특별시", "광주광역시": "전남광주통합특별시", "전남": "전남광주통합특별시",
    "전라남도": "전남광주통합특별시", "전남광주": "전남광주통합특별시",
    "대전": "대전광역시", "대전시": "대전광역시",
    "울산": "울산광역시", "울산시": "울산광역시", "세종": "세종특별자치시", "세종시": "세종특별자치시",
    "경기": "경기도", "강원": "강원특별자치도", "강원도": "강원특별자치도", "충북": "충청북도",
    "충남": "충청남도", "전북": "전북특별자치도", "전라북도": "전북특별자치도",
    "경북": "경상북도", "경남": "경상남도", "제주": "제주특별자치도", "제주도": "제주특별자치도",
}

# 전남광주통합특별시 신규 시군구코드 → 통합 전 코드 (브이월드 일부 자료가 옛 코드로 남아 있음)
LEGACY_SGG = {
    "12210": "29110", "12240": "29140", "12270": "29155", "12300": "29170", "12330": "29200",
    "12110": "46110", "12130": "46130", "12150": "46150", "12170": "46170", "12190": "46230",
    "12710": "46710", "12720": "46720", "12730": "46730", "12740": "46770", "12750": "46780",
    "12760": "46790", "12770": "46800", "12780": "46810", "12790": "46820", "12800": "46830",
    "12810": "46840", "12820": "46860", "12830": "46870", "12840": "46880", "12850": "46890",
    "12860": "46900", "12870": "46910",
}


def legacy_code(code: str | None) -> str | None:
    if code and code[:5] in LEGACY_SGG:
        return LEGACY_SGG[code[:5]] + code[5:]
    return None


def norm_sido(text: str) -> str:
    toks = text.split()
    if toks and toks[0] in SIDO_ABBR:
        toks[0] = SIDO_ABBR[toks[0]]
    # 단지목록식 '부천원미구', '천안서북구', '고양일산동구' → '부천시 원미구'
    out = []
    for t in toks:
        if len(t) >= 5 and t.endswith("구") and "시" not in t and not t.endswith("지구"):
            out.extend([t[:2] + "시", t[2:]])
        else:
            out.append(t)
    return " ".join(out)


REGION_SUFFIX = re.compile(r"(특별시|광역시|특별자치시|특별자치도|도|시|군|구|읍|면|동|리|\d+가)$")
JIBUN_TAIL = re.compile(r"(?:^|\s)(산\s*)?(\d+)(?:\s*-\s*(\d+))?\s*(?:번지)?$")
ROAD_PAT = re.compile(r"\S+(?:로|길)\s*\d")
PARCEL_IN_ADDR = re.compile(r"(\S+?(?:동|가|리|읍|면))\s+(산\s*)?(\d+)(?:-(\d+))?")


def make_pnu(bjd: str, bun: int, ji: int, san: bool = False) -> str:
    return f"{bjd}{'2' if san else '1'}{bun:04d}{ji:04d}"


def jibun_str(bun: int, ji: int, san: bool = False) -> str:
    return f"{'산' if san else ''}{bun}" + (f"-{ji}" if ji else "")


# ---------------------------------------------------------------------------
# 지역/주소 해석
# ---------------------------------------------------------------------------
async def stan_region(name: str, rows: int = 30) -> list[dict]:
    d = await dg_json("1741000/StanReginCd/getStanReginCdList",
                      {"type": "json", "pageNo": 1, "numOfRows": rows, "flag": "Y", "locatadd_nm": name})
    if not isinstance(d, dict) or "StanReginCd" not in d:
        return []
    try:
        return d["StanReginCd"][1]["row"]
    except (KeyError, IndexError, TypeError):
        return []


async def lookup_region(text: str) -> list[dict]:
    t = norm_sido(text.strip())
    toks = t.split()
    for i in range(len(toks)):
        # 상위 지역명을 떼고 짧게 검색할 때는 동명 지역이 많으므로 넉넉히 받아서 거름
        rows = await stan_region(" ".join(toks[i:]), rows=1000 if i > 0 else 30)
        if rows:
            # 앞에서 생략된 상위 지역명(예: '부산광역시', '부천원미구')으로 거르기 — 공백·'시' 무시 비교
            def squash(s: str) -> str:
                return s.replace(" ", "").replace("시", "")
            for pre in toks[:i]:
                f = [r for r in rows if squash(pre) in squash(r["locatadd_nm"])]
                if f:
                    rows = f
            last = toks[-1]
            # 마지막 단어가 정확히 일치하는 행 우선(상동≠양상동), 리 단위가 아닌 행 우선
            rows.sort(key=lambda r: (r["locatadd_nm"].split()[-1] != last, not r["locatadd_nm"].endswith(last),
                                     r["region_cd"][8:] != "00"))
            if rows[0]["locatadd_nm"].split()[-1] != last and i > 0:
                continue  # 부분일치(양상동 등)만 있으면 더 짧은 질의로 넘어가지 않고 다음 후보 탐색
            return rows
    return []


async def _resolve_by_trade_name(q: str) -> dict | None:
    """단지목록(K-apt)에 없는 단지: 지역명 + 실거래 자료의 단지명으로 지번을 찾음."""
    toks = norm_sido(q).split()
    reg = [t for t in toks if REGION_SUFFIX.search(t) or t in SIDO_ABBR.values()]
    name = "".join(t for t in toks if t not in reg)
    if not reg or len(norm_name(name)) < 2:
        return None
    rows = await lookup_region(" ".join(reg))
    if not rows:
        return None
    code = rows[0]["region_cd"]
    if _region_level(code) == "sido":
        return None
    umd_loc = {"bjd_code": code, "umd": rows[0].get("locallow_nm"), "level": _region_level(code)}
    qn = norm_name(name)
    best: tuple[float, dict] | None = None
    for ttype in ("아파트매매", "아파트전월세", "연립다세대매매", "오피스텔매매"):
        batches = await asyncio.gather(*[fetch_tx(ttype, code[:5], ym) for ym in months_back(12)],
                                       return_exceptions=True)
        cnt: dict[tuple[str, str, str], int] = {}
        for b in batches:
            if isinstance(b, Exception):
                continue
            for it in b:
                if umd_loc["level"] == "dong" and not _same_umd(it, umd_loc):
                    continue
                nm = it.get("aptNm") or it.get("offiNm") or it.get("mhouseNm") or ""
                j = (it.get("jibun") or "").strip()
                if nm and j and "*" not in j:
                    k = (nm, j, it.get("umdNm", ""))
                    cnt[k] = cnt.get(k, 0) + 1
        for (nm, j, umd), c in cnt.items():
            n = norm_name(nm)
            r = 1.0 if (qn in n or n in qn) else fuzzy_ratio(qn, n)
            if r >= 0.75 and (best is None or (r, c) > (best[0], best[1]["c"])):
                best = (r, {"name": nm, "jibun": j, "umd": umd, "c": c})
        if best:
            break
    if not best:
        return None
    b = best[1]
    m = re.match(r"(산)?(\d+)(?:-(\d+))?", b["jibun"])
    if not m:
        return None
    bjd = code
    if _region_level(code) != "dong":  # 시군구만 입력된 경우 → 해당 동 코드 찾기
        sub = await lookup_region(f"{rows[0]['locatadd_nm']} {b['umd']}")
        if not sub:
            return None
        bjd = sub[0]["region_cd"]
    loc = _loc_from_parcel(bjd, int(m.group(2)), int(m.group(3) or 0), bool(m.group(1)), query=q,
                           source="실거래 단지명", building_name=b["name"], umd=b["umd"],
                           address=f"{rows[0]['locatadd_nm'] if _region_level(code) == 'dong' else rows[0]['locatadd_nm'] + ' ' + b['umd']} {b['jibun']}")
    if VW_KEY:
        try:
            pnu, x, y, a = await _vw_point_for_parcel(loc["address"])
            if x:
                loc["lon"], loc["lat"] = x, y
                loc["road_address"] = a.get("road")
        except ApiError:
            pass
    return loc


def _region_level(code10: str) -> str:
    if code10[2:] == "00000000":
        return "sido"
    if code10[5:] == "00000":
        return "sigungu"
    return "dong"


# ---- K-apt 전국 단지목록 캐시 ----------------------------------------------
_kapt_mem: list[dict] | None = None


async def kapt_list(force: bool = False) -> list[dict]:
    global _kapt_mem
    path = CACHE_DIR / "kapt_list.json"
    if _kapt_mem is not None and not force:
        return _kapt_mem
    stale: list[dict] | None = None
    if path.exists() and not force:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if data and time.time() - path.stat().st_mtime < 30 * 86400:
            _kapt_mem = data
            return data
        stale = data
    try:
        first = await dg_json("1613000/AptListService4/getTotalAptList4", {"pageNo": 1, "numOfRows": 5000})
        items, total = _json_items(first)
        pages = math.ceil(total / 5000)

        async def page(n: int) -> list[dict]:
            d = await dg_json("1613000/AptListService4/getTotalAptList4", {"pageNo": n, "numOfRows": 5000})
            return _json_items(d)[0]

        for chunk in await asyncio.gather(*[page(n) for n in range(2, pages + 1)]):
            items.extend(chunk)
    except Exception:
        if stale:  # 갱신 실패 시 기존 목록 사용
            _kapt_mem = stale
            return stale
        raise
    slim = [{k: it.get(k) for k in ("kaptCode", "kaptName", "bjdCode", "as1", "as2", "as3", "as4")} for it in items]
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(slim, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("단지목록 캐시 저장 실패: %s", e)
    _kapt_mem = slim
    return slim


def match_kapt(query: str, items: list[dict], bjd: str | None = None) -> list[tuple[int, dict]]:
    toks = norm_sido(query).split()
    cand = items
    if bjd:
        cand = [it for it in cand if it.get("bjdCode") == bjd] or cand
    # 지역처럼 보이는 단어는 실제로 지역명과 맞을 때만 지역 필터로 쓰고, 아니면 단지명에 남김 ('금광지구' 등)
    name_toks = []
    for t in toks:
        if REGION_SUFFIX.search(t) and len(t) >= 2 and not re.search(r"\d+단지$", t):
            base = t[:-1] if len(t) > 2 else t
            f = [it for it in cand if any((it.get(k) or "").replace(" ", "").startswith(base)
                                          or base in (it.get("as2") or "") for k in ("as1", "as2", "as3", "as4"))]
            if f:
                cand = f
                continue
        name_toks.append(t)
    base_cand = cand
    # 이름 앞 단어가 동네 이름(예: '답십리')이면 지역 필터로도 시도 → 매칭이 없으면 되돌림 ('남산 포스코더샵')
    attempts = []
    if len(name_toks) > 1 and len(name_toks[0]) >= 2:
        f = [it for it in base_cand if (it.get("as3") or "").startswith(name_toks[0])]
        if f:
            attempts.append((f, name_toks[1:]))
    attempts.append((base_cand, name_toks))
    for cand, nts in attempts:
        res = _score_names(norm_name("".join(nts) or query), cand)
        if res:
            return res
    return []


def _score_names(qn: str, cand: list[dict]) -> list[tuple[int, dict]]:
    if not qn:
        return []
    scored = []
    for it in cand:
        n = norm_name(it.get("kaptName"))
        if not n or (_num_conflict(qn, n) and n != qn):
            continue
        if n == qn:
            s = 100
        elif qn in n:
            s = 80 - min(30, len(n) - len(qn))
        elif n in qn and len(n) >= 3:
            s = 70 - min(30, len(qn) - len(n))
        else:
            continue
        scored.append((s, it))
    if not scored and len(cand) <= 3000 and len(qn) >= 3:
        # 오타·표기 차이(하이츠/하이즈, 써밋/서밋 등) 대비 유사도 매칭
        for it in cand:
            nn = norm_name(it.get("kaptName"))
            if _num_conflict(qn, nn):
                continue
            r = fuzzy_ratio(qn, nn)
            if r >= 0.75:
                scored.append((int(60 * r), it))
    scored.sort(key=lambda x: -x[0])
    return scored


def fuzzy_ratio(q: str, n: str) -> float:
    """q가 n의 일부와 얼마나 비슷한지 (0~1)."""
    if not q or not n:
        return 0.0
    best = 0.0
    for w in {len(q) - 1, len(q), len(q) + 1}:
        if w < 2 or w > len(n):
            continue
        for i in range(len(n) - w + 1):
            r = SequenceMatcher(None, q, n[i:i + w]).ratio()
            if r > best:
                best = r
    if len(n) < len(q) - 1:
        best = max(best, SequenceMatcher(None, q, n).ratio())
    return best


async def kapt_basis(kapt_code: str) -> dict:
    d = await dg_json("1613000/AptBasisInfoServiceV5/getAphusBassInfoV5", {"kaptCode": kapt_code})
    items, _ = _json_items(d)
    return items[0] if items else {}


async def kapt_detail(kapt_code: str) -> dict:
    d = await dg_json("1613000/AptBasisInfoServiceV5/getAphusDtlInfoV5", {"kaptCode": kapt_code})
    items, _ = _json_items(d)
    return items[0] if items else {}


# ---- 위치 해석 본체 --------------------------------------------------------
def _loc_from_parcel(bjd: str, bun: int, ji: int, san: bool, **extra) -> dict:
    loc = {"level": "parcel", "pnu": make_pnu(bjd, bun, ji, san), "bjd_code": bjd, "lawd_cd": bjd[:5],
           "bun": bun, "ji": ji, "san": san, "jibun": jibun_str(bun, ji, san)}
    loc.update({k: v for k, v in extra.items() if v not in (None, "")})
    return loc


def _parse_parcel_text(addr: str) -> tuple[str | None, int | None, int, bool]:
    ms = list(PARCEL_IN_ADDR.finditer(addr or ""))
    if not ms:
        return None, None, 0, False
    m = ms[-1]
    return m.group(1), int(m.group(3)), int(m.group(4) or 0), bool(m.group(2))


async def _vw_point_for_parcel(parcel_addr: str) -> tuple[str | None, float | None, float | None, dict]:
    items = await vw_search(parcel_addr, "address", "parcel", size=1)
    if not items:
        return None, None, None, {}
    it = items[0]
    p = it.get("point", {})
    return it.get("id"), num(p.get("x")), num(p.get("y")), it.get("address", {})


async def _loc_from_kapt(item: dict, query: str, others: list[dict]) -> dict:
    basis = await kapt_basis(item["kaptCode"])
    addr = basis.get("kaptAddr") or ""
    umd, bun, ji, san = _parse_parcel_text(addr)
    extra = dict(query=query, source="K-apt 단지명", address=addr, road_address=basis.get("doroJuso"),
                 building_name=item.get("kaptName"), umd=umd or item.get("as3"),
                 complex={"kaptCode": item["kaptCode"], "kaptName": item.get("kaptName")})
    if others:
        extra["other_candidates"] = [f"{o['kaptName']} ({o.get('as1', '')} {o.get('as2') or ''} {o.get('as3')})".replace("  ", " ")
                                     for o in others[:5]]
        same = [o for o in others if norm_name(o.get("kaptName")) == norm_name(item.get("kaptName"))]
        if same:
            extra["ambiguous"] = f"같은 이름의 단지가 {len(same) + 1}곳 있습니다. 사용자에게 지역(시군구·동)을 확인하세요."
    if bun is None:
        bjd = item["bjdCode"]
        loc = {"level": "dong", "bjd_code": bjd, "lawd_cd": bjd[:5], **extra}
        # 단지목록에 지번이 없는 단지(세종 등 신도시): 도로명 → 필지, 없으면 실거래 자료의 지번 사용
        if VW_KEY and basis.get("doroJuso"):
            try:
                items = await vw_search(basis["doroJuso"], "address", "road", size=1)
                if items:
                    sub = await _loc_from_vw_address(items[0], query, "K-apt 단지명+도로명")
                    sub.update({k: extra[k] for k in ("complex", "building_name", "other_candidates", "ambiguous") if k in extra})
                    return sub
            except ApiError:
                pass
        try:
            alts = await _alt_pnus_from_trades(loc)
        except ApiError:
            alts = []
        if alts:
            p = alts[0]
            sub = _loc_from_parcel(p[:10], int(p[11:15]), int(p[15:19]), p[10] == "2", **extra)
            sub["source"] = "K-apt 단지명+실거래 지번"
            return sub
        return loc
    loc = _loc_from_parcel(item["bjdCode"], bun, ji, san, **extra)
    if VW_KEY:
        try:
            pnu, x, y, _ = await _vw_point_for_parcel(f"{item.get('as1','')} {item.get('as2','')} {umd} {jibun_str(bun, ji, san)}")
            if x:
                loc["lon"], loc["lat"] = x, y
            if pnu and pnu[:10] == item["bjdCode"]:
                loc["pnu"] = pnu
        except ApiError:
            pass
    return loc


async def _loc_from_vw_address(it: dict, query: str, source: str) -> dict:
    pnu = it.get("id", "")
    a = it.get("address", {})
    p = it.get("point", {})
    parcel = a.get("parcel", "")
    umd, _, _, _ = _parse_parcel_text(parcel if re.search(r"\d", parcel) else parcel + " 0")
    if not re.fullmatch(r"\d{19}", pnu or ""):
        raise ApiError("필지코드(PNU)를 찾지 못했습니다.")
    loc = _loc_from_parcel(pnu[:10], int(pnu[11:15]), int(pnu[15:19]), pnu[10] == "2",
                           query=query, source=source, address=parcel, road_address=a.get("road"),
                           building_name=a.get("bldnm"), umd=umd, zipcode=a.get("zipcode"),
                           lon=num(p.get("x")), lat=num(p.get("y")))
    loc["pnu"] = pnu
    return loc


async def resolve(query: str) -> dict:
    """주소/지번/도로명/아파트명/지역명/PNU → 위치 정보."""
    q = " ".join((query or "").split())
    if not q:
        raise ApiError("주소나 아파트명을 입력하세요.")
    qn = norm_sido(q)

    # 1) 코드 직접 입력
    if re.fullmatch(r"\d{19}", q):
        loc = _loc_from_parcel(q[:10], int(q[11:15]), int(q[15:19]), q[10] == "2", query=q, source="PNU")
        loc["pnu"] = q
        return loc
    if re.fullmatch(r"\d{10}", q):
        return {"level": _region_level(q), "bjd_code": q, "lawd_cd": q[:5], "query": q, "source": "법정동코드"}
    if re.fullmatch(r"\d{5}", q):
        return {"level": "sigungu", "bjd_code": q + "00000", "lawd_cd": q, "query": q, "source": "시군구코드"}

    has_jibun = bool(JIBUN_TAIL.search(qn)) and not re.search(r"\d+단지$", qn)
    is_road = bool(ROAD_PAT.search(qn))

    # 2) 지역명만 입력 (예: '동대문구 휘경동', '성수동1가') — 모든 단어가 행정구역 형태일 때만
    all_region = all(REGION_SUFFIX.search(t) or t in SIDO_ABBR.values() for t in qn.split())
    if not has_jibun and all_region:
        rows = await lookup_region(qn)
        if rows:
            r = rows[0]
            code = r["region_cd"]
            loc = {"level": _region_level(code), "bjd_code": code, "lawd_cd": code[:5], "query": q,
                   "source": "법정동코드", "address": r["locatadd_nm"], "umd": r.get("locallow_nm")}
            same = [x["locatadd_nm"] for x in rows[1:]
                    if _region_level(x["region_cd"]) == loc["level"] and x["locatadd_nm"].endswith(qn.split()[-1])]
            if same:
                loc["other_candidates"] = same[:5]
            return loc

    # 3) 주소 (도로명/지번)
    if has_jibun or is_road:
        if VW_KEY:
            for cat in (("road", "parcel") if is_road else ("parcel", "road")):
                items = await vw_search(qn, "address", cat, size=3)
                if items:
                    return await _loc_from_vw_address(items[0], q, f"브이월드 {cat} 주소검색")
        # 브이월드 없이 지번 주소 해석
        m = JIBUN_TAIL.search(qn)
        if m and not is_road:
            region_txt = qn[:m.start()].strip()
            rows = await lookup_region(region_txt) if region_txt else []
            if rows:
                r = rows[0]
                bun, ji, san = int(m.group(2)), int(m.group(3) or 0), bool(m.group(1))
                return _loc_from_parcel(r["region_cd"], bun, ji, san, query=q, source="법정동코드+지번",
                                        address=f"{r['locatadd_nm']} {jibun_str(bun, ji, san)}",
                                        umd=r.get("locallow_nm"))
        if is_road and not VW_KEY:
            raise ApiError("도로명주소 해석에는 브이월드 키가 필요합니다. 지번주소로 입력하거나 키를 설정하세요.")

    # 4) 아파트(공동주택) 이름
    try:
        items = await kapt_list()
        scored = match_kapt(q, items)
    except Exception as e:  # noqa: BLE001
        log.warning("K-apt 목록 실패: %s", e)
        scored = []
    if scored:
        best = scored[0][1]
        others = [it for s, it in scored[1:6] if s >= scored[0][0] - 15]
        return await _loc_from_kapt(best, q, others)

    # 4-2) 단지목록에 없으면 실거래 자료의 단지명으로
    try:
        by_trade = await _resolve_by_trade_name(q)
    except ApiError:
        by_trade = None
    if by_trade:
        return by_trade

    # 5) 브이월드 장소/주소 검색
    if VW_KEY:
        places = await vw_search(qn, "place", size=10)
        if places:
            def rank(p):
                c = p.get("category", "")
                return (0 if ("아파트" in c or "주거" in c or "주택" in c) else 1 if "건물" in c else 2)
            places.sort(key=rank)
            pl = places[0]
            parcel = re.sub(r"\s*(도|대|전|답|임|잡)$", "", pl.get("address", {}).get("parcel", ""))
            pnu, x, y, a = await _vw_point_for_parcel(parcel)
            if pnu:
                loc = _loc_from_parcel(pnu[:10], int(pnu[11:15]), int(pnu[15:19]), pnu[10] == "2",
                                       query=q, source="브이월드 장소검색", address=a.get("parcel") or parcel,
                                       road_address=pl.get("address", {}).get("road"),
                                       building_name=pl.get("title"), lon=x, lat=y,
                                       umd=_parse_parcel_text(parcel)[0])
                loc["pnu"] = pnu
                loc["place_category"] = pl.get("category")
                return loc
        for cat in ("road", "parcel"):
            items = await vw_search(qn, "address", cat, size=3)
            if items:
                return await _loc_from_vw_address(items[0], q, f"브이월드 {cat} 주소검색")

    rows = await lookup_region(qn)
    if rows:
        r = rows[0]
        code = r["region_cd"]
        return {"level": _region_level(code), "bjd_code": code, "lawd_cd": code[:5], "query": q,
                "source": "법정동코드", "address": r["locatadd_nm"], "umd": r.get("locallow_nm")}
    raise ApiError(f"'{q}' 위치를 찾지 못했습니다. 시군구+동+번지나 정확한 단지명으로 다시 시도하세요.")


def loc_summary(loc: dict) -> dict:
    keys = ["query", "level", "address", "road_address", "building_name", "complex", "pnu", "bjd_code",
            "lawd_cd", "jibun", "lon", "lat", "source", "ambiguous", "other_candidates"]
    return {k: loc[k] for k in keys if loc.get(k) not in (None, "")}


async def ensure_point(loc: dict) -> dict:
    if loc.get("lon") and loc.get("lat"):
        return loc
    _need_vw_key()
    if loc.get("pnu"):
        d = await vw_json("/req/data", {"service": "data", "request": "GetFeature", "data": "LP_PA_CBND_BUBUN",
                                        "attrFilter": f"pnu:=:{loc['pnu']}", "geometry": "true", "size": 1,
                                        "crs": "EPSG:4326"})
        try:
            geom = d["response"]["result"]["featureCollection"]["features"][0]["geometry"]
            coords = geom["coordinates"]
            while isinstance(coords[0][0], list):
                coords = coords[0]
            xs = [c[0] for c in coords]
            ys = [c[1] for c in coords]
            loc["lon"], loc["lat"] = sum(xs) / len(xs), sum(ys) / len(ys)
            return loc
        except (KeyError, IndexError, TypeError):
            pass
    if loc.get("address"):
        items = await vw_search(loc["address"], "district" if loc.get("level") != "parcel" else "address",
                                None if loc.get("level") != "parcel" else "parcel", size=1)
        if items:
            p = items[0].get("point", {})
            loc["lon"], loc["lat"] = num(p.get("x")), num(p.get("y"))
            return loc
    raise ApiError("좌표를 찾지 못했습니다.")


# ---------------------------------------------------------------------------
# 실거래가
# ---------------------------------------------------------------------------
TX_TYPES = {
    "아파트매매": ("RTMSDataSvcAptTradeDev", "trade"),
    "아파트전월세": ("RTMSDataSvcAptRent", "rent"),
    "분양권": ("RTMSDataSvcSilvTrade", "trade"),
    "오피스텔매매": ("RTMSDataSvcOffiTrade", "trade"),
    "오피스텔전월세": ("RTMSDataSvcOffiRent", "rent"),
    "연립다세대매매": ("RTMSDataSvcRHTrade2", "trade"),
    "연립다세대전월세": ("RTMSDataSvcRHRent", "rent"),
    "단독다가구매매": ("RTMSDataSvcSHTrade", "trade"),
    "단독다가구전월세": ("RTMSDataSvcSHRent", "rent"),
    "상업업무용매매": ("RTMSDataSvcNrgTrade", "trade"),
    "토지매매": ("RTMSDataSvcLandTrade", "trade"),
    "공장창고매매": ("RTMSDataSvcInduTrade", "trade"),
}
_TX_ALIAS = [
    (("분양권", "입주권", "전매"), "분양권"),
    (("오피스텔",), "오피스텔"), (("빌라", "연립", "다세대"), "연립다세대"),
    (("단독", "다가구", "주택"), "단독다가구"), (("상가", "상업", "업무", "근린", "빌딩", "오피스"), "상업업무용매매"),
    (("토지", "땅", "대지", "임야", "농지"), "토지매매"), (("공장", "창고", "산업"), "공장창고매매"),
    (("아파트", "apt"), "아파트"),
]


def tx_type(t: str | None) -> str:
    s = re.sub(r"[\s/·_\-]", "", (t or "아파트매매")).lower()
    if s in TX_TYPES:
        return s
    rent = any(w in s for w in ("전세", "월세", "전월세", "임대", "렌트", "rent"))
    for words, base in _TX_ALIAS:
        if any(w in s for w in words):
            if base in TX_TYPES:
                return base
            return base + ("전월세" if rent else "매매")
    return "아파트전월세" if rent else "아파트매매"


def months_back(n: int, end: str | None = None) -> list[str]:
    if end and re.fullmatch(r"\d{6}", end):
        y, m = int(end[:4]), int(end[4:])
    else:
        t = dt.date.today()
        y, m = t.year, t.month
    out = []
    for _ in range(max(1, n)):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


async def fetch_tx(ttype: str, lawd: str, ymd: str) -> list[dict]:
    svc, _ = TX_TYPES[ttype]
    path = f"1613000/{svc}/get{svc}"
    items, total = await dg_xml(path, {"LAWD_CD": lawd, "DEAL_YMD": ymd, "numOfRows": 1000, "pageNo": 1})
    page = 1
    while len(items) < total and page < 20:
        page += 1
        more, _ = await dg_xml(path, {"LAWD_CD": lawd, "DEAL_YMD": ymd, "numOfRows": 1000, "pageNo": page})
        if not more:
            break
        items.extend(more)
    return items


def tx_row(it: dict, kind: str) -> dict:
    y, m, d = it.get("dealYear", ""), it.get("dealMonth", "0"), it.get("dealDay", "0")
    try:
        date = f"{y}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        date = f"{y}-{m}-{d}"
    name = it.get("aptNm") or it.get("offiNm") or it.get("mhouseNm") or ""
    area = None
    for k in ("excluUseAr", "totalFloorAr", "buildingAr", "dealArea"):
        area = num(it.get(k))
        if area:
            break
    row: dict[str, Any] = {"계약일": date}
    if name:
        row["명칭"] = name
    if it.get("aptDong", "").strip():
        row["동"] = it["aptDong"].strip()
    row["법정동"] = it.get("umdNm", "")
    if it.get("jibun", "").strip():
        row["지번"] = it["jibun"].strip()
    if area:
        row["면적㎡"] = area
    for k, label in (("floor", "층"), ("buildYear", "건축년도"), ("houseType", "주택유형"),
                     ("buildingType", "건물유형"), ("buildingUse", "건물용도"), ("landUse", "용도지역"),
                     ("jimok", "지목"), ("plottageAr", "대지면적㎡"), ("landAr", "대지권면적㎡"),
                     ("shareDealingType", "지분거래")):
        v = (it.get(k) or "").strip()
        if v:
            row[label] = v
    if kind == "trade":
        price = num(it.get("dealAmount"))
        row["거래금액(만원)"] = int(price) if price else None
        if price and area:
            row["평당가(만원/3.3㎡)"] = int(round(price / area * 3.3058))
        for k, label in (("dealingGbn", "거래유형"), ("buyerGbn", "매수자"), ("slerGbn", "매도자"),
                         ("rgstDate", "등기일"), ("ownershipGbn", "분양권구분")):
            v = (it.get(k) or "").strip()
            if v:
                row[label] = v
        if (it.get("cdealType") or "").strip() or (it.get("cdealDay") or "").strip():
            row["해제"] = (it.get("cdealDay") or "Y").strip() or "Y"
    else:
        dep, mon = num(it.get("deposit")), num(it.get("monthlyRent"))
        row["보증금(만원)"] = int(dep) if dep is not None else None
        row["월세(만원)"] = int(mon) if mon is not None else 0
        row["구분"] = "전세" if not mon else "월세"
        for k, label in (("contractType", "계약구분"), ("contractTerm", "계약기간"), ("useRRRight", "갱신요구권"),
                         ("preDeposit", "종전보증금"), ("preMonthlyRent", "종전월세")):
            v = (it.get(k) or "").strip()
            if v:
                row[label] = v
    return row


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """실거래 자료의 중복 신고(같은 날·층·면적·금액) 제거. 정보가 더 많은 행을 남김."""
    best: dict[tuple, dict] = {}
    order: list[tuple] = []
    for r in rows:
        k = (r.get("계약일"), r.get("명칭"), r.get("지번"), r.get("층"), r.get("면적㎡"), r.get("거래금액(만원)"),
             r.get("보증금(만원)"), r.get("월세(만원)"), "해제" in r)
        if k not in best:
            best[k] = r
            order.append(k)
        elif len(r) > len(best[k]):
            best[k] = r
    return [best[k] for k in order]


def _median(xs: list[float]) -> int | None:
    return int(statistics.median(xs)) if xs else None


def tx_stats(rows: list[dict], kind: str) -> dict:
    if kind == "trade":
        valid = [r for r in rows if "해제" not in r and r.get("거래금액(만원)")]
        prices = [r["거래금액(만원)"] for r in valid]
        ppa = [r["평당가(만원/3.3㎡)"] for r in valid if r.get("평당가(만원/3.3㎡)")]
        s = {"유효거래": len(valid), "해제거래": len(rows) - len(valid)}
        if prices:
            s.update({"중위가(만원)": _median(prices), "평균가(만원)": int(statistics.mean(prices)),
                      "최저(만원)": min(prices), "최고(만원)": max(prices)})
        if ppa:
            s["중위평당가(만원/3.3㎡)"] = _median(ppa)
        groups: dict[str, list[dict]] = {}
        for r in valid:
            if r.get("면적㎡"):
                groups.setdefault(f"{round(r['면적㎡'])}㎡", []).append(r)
        if 1 < len(groups) <= 25:
            s["면적별"] = [{"면적": k, "건수": len(v), "중위가(만원)": _median([x["거래금액(만원)"] for x in v]),
                          "최근": f"{v[0]['계약일']} {v[0]['거래금액(만원)']:,}"}
                         for k, v in sorted(groups.items(), key=lambda kv: float(kv[0][:-1]))]
        return s
    jeonse = [r for r in rows if r.get("구분") == "전세" and r.get("보증금(만원)")]
    wolse = [r for r in rows if r.get("구분") == "월세"]
    s = {"전세건수": len(jeonse), "월세건수": len(wolse)}
    if jeonse:
        deps = [r["보증금(만원)"] for r in jeonse]
        s.update({"전세중위보증금(만원)": _median(deps), "전세최저": min(deps), "전세최고": max(deps)})
    if wolse:
        s.update({"월세중위보증금(만원)": _median([r["보증금(만원)"] or 0 for r in wolse]),
                  "월세중위월세(만원)": _median([r["월세(만원)"] for r in wolse])})
    return s


def _same_umd(it: dict, loc: dict) -> bool:
    code = loc.get("bjd_code", "")
    if it.get("umdCd") and len(code) == 10 and code[5:] != "00000":
        return it["umdCd"] == code[5:]
    u = (loc.get("umd") or "").strip()
    n = (it.get("umdNm") or "").strip()
    return bool(u and n and (u == n or n.endswith(u) or u.endswith(n)))


def _jibun_match(it: dict, loc: dict) -> bool:
    j = (it.get("jibun") or "").strip()
    if not j or "*" in j or loc.get("bun") is None:
        return False
    if loc.get("ji"):
        return j == f"{loc['bun']}-{loc['ji']}"
    return j in (str(loc["bun"]), f"{loc['bun']}-0")


_SIDO_SHORT = sorted({k for k in SIDO_ABBR if len(k) == 2} | {"세종"}, key=len, reverse=True)


def _strip_region_words(s: str, umd: str | None) -> str:
    if umd:
        u = norm_name(umd)
        s = s.replace(u, "")
        if len(u) > 2:
            s = s.replace(u[:-1], "")
    for w in _SIDO_SHORT:
        if s.startswith(w) and len(s) - len(w) >= 3:
            s = s[len(w):]
    return s


def _nums(s: str) -> set[str]:
    return set(re.findall(r"\d+", s or ""))


def _num_conflict(a: str, b: str) -> bool:
    """'이문1차' vs '이문2차'처럼 둘 다 번호가 있는데 다르면 다른 단지."""
    na, nb = _nums(a), _nums(b)
    return bool(na and nb and not (na & nb))


def _name_match(it: dict, names: list[str], umd: str | None) -> bool:
    n = norm_name(it.get("aptNm") or it.get("offiNm") or it.get("mhouseNm"))
    if len(n) < 2:
        return False
    n2 = _strip_region_words(n, umd)
    for raw in names:
        q = _strip_region_words(norm_name(raw), umd)
        if _num_conflict(q, n):
            continue
        if len(q) >= 2 and (q in n or n in q or (len(n2) >= 2 and (q in n2 or n2 in q))):
            return True
        if len(q) >= 4 and max(fuzzy_ratio(q, n), fuzzy_ratio(q, n2) if len(n2) >= 2 else 0) >= 0.8:
            return True
    return False


async def real_transactions(location: str, property_type: str = "아파트매매", months: int = 12,
                            name: str | None = None, scope: str = "auto", area_min: float | None = None,
                            area_max: float | None = None, end_month: str | None = None,
                            limit: int = 30) -> dict:
    ttype = tx_type(property_type)
    kind = TX_TYPES[ttype][1]
    loc = await resolve(location)
    lawd = loc["lawd_cd"]
    yms = months_back(min(max(months, 1), 60), end_month)
    batches = await asyncio.gather(*[fetch_tx(ttype, lawd, ym) for ym in yms], return_exceptions=True)
    items: list[dict] = []
    errors = []
    for ym, b in zip(yms, batches):
        if isinstance(b, Exception):
            errors.append(f"{ym}: {b}")
        else:
            items.extend(b)

    names = [x for x in [name, loc.get("building_name"), (loc.get("complex") or {}).get("kaptName")] if x]
    if not names and loc.get("source") in ("K-apt 단지명", "브이월드 장소검색"):
        names.append(loc["query"])
    filt_desc = "시군구 전체"
    sel = items
    dong_fallback: list[dict] | None = None
    if name:
        sel = [it for it in items if _name_match(it, [name], loc.get("umd"))]
        if loc.get("level") in ("parcel", "dong") and scope != "sigungu":
            narrowed = [it for it in sel if _same_umd(it, loc)]
            sel = narrowed or sel
        filt_desc = f"명칭 '{name}' 포함"
    elif loc["level"] == "parcel" and scope in ("auto", "building"):
        same = [it for it in items if _same_umd(it, loc)]
        hit = [it for it in same if _jibun_match(it, loc) or _name_match(it, names, loc.get("umd"))]
        if hit:
            sel, filt_desc = hit, "해당 지번/단지 거래"
        else:
            sel, filt_desc = [], "해당 지번/단지 거래 없음"
            dong_fallback = same
    elif loc["level"] in ("parcel", "dong") and scope != "sigungu":
        sel = [it for it in items if _same_umd(it, loc)]
        filt_desc = f"법정동({loc.get('umd') or loc.get('address')}) 전체"

    rows = dedupe_rows([tx_row(it, kind) for it in sel])
    if area_min is not None:
        rows = [r for r in rows if (r.get("면적㎡") or 0) >= area_min]
    if area_max is not None:
        rows = [r for r in rows if (r.get("면적㎡") or 1e9) <= area_max]
    rows.sort(key=lambda r: r["계약일"], reverse=True)
    out = {
        "조회대상": loc_summary(loc),
        "유형": ttype,
        "기간": f"{yms[-1][:4]}-{yms[-1][4:]} ~ {yms[0][:4]}-{yms[0][4:]} (계약월 기준, 신고기한 30일이라 최근 1~2개월은 덜 집계됨)",
        "필터": filt_desc,
        "건수": len(rows),
        "요약": tx_stats(rows, kind),
        "거래목록": rows[:max(1, limit)],
    }
    if len(rows) > limit:
        out["참고"] = f"최근 {limit}건만 표시 (전체 {len(rows)}건). limit을 늘리거나 면적 조건으로 좁힐 수 있음"
    if dong_fallback is not None:
        drows = [tx_row(it, kind) for it in dong_fallback]
        drows.sort(key=lambda r: r["계약일"], reverse=True)
        names_cnt: dict[str, int] = {}
        for r in drows:
            if r.get("명칭"):
                names_cnt[r["명칭"]] = names_cnt.get(r["명칭"], 0) + 1
        out["참고"] = ("이 기간 해당 건물/단지의 신고 거래가 없습니다. 기간(months)을 늘리거나, 단지명이 실거래 "
                     "자료와 다르면 name으로 지정하세요. 아래는 같은 법정동 전체 요약입니다.")
        out["법정동전체"] = {"건수": len(drows), "요약": tx_stats(drows, kind),
                          "거래많은명칭": sorted(names_cnt.items(), key=lambda kv: -kv[1])[:10]}
    if errors:
        out["조회오류"] = errors[:5]
    if loc.get("complex") and len(rows) < 3:
        try:
            b = await kapt_basis(loc["complex"]["kaptCode"])
            if "임대" in str(b.get("codeSaleNm")):
                out["단지분양형태"] = b.get("codeSaleNm")
                out["참고"] = (out.get("참고", "") + " " if out.get("참고") else "") + \
                    "임대 단지라 매매 실거래가 거의 없을 수 있습니다 (전월세는 임대주택 특성상 신고 대상이 아닐 수 있음)."
        except ApiError:
            pass
    return out


async def market_summary(location: str, property_type: str = "아파트매매", months: int = 24,
                         dongs: str | None = None, min_build_year: int | None = None,
                         area_min: float | None = None, area_max: float | None = None, top: int = 30) -> dict:
    """법정동(또는 지정한 여러 동) 단지별 시세 비교 + 월별 추이."""
    ttype = tx_type(property_type)
    kind = TX_TYPES[ttype][1]
    loc = await resolve(location)
    yms = months_back(min(max(months, 1), 60))
    batches = await asyncio.gather(*[fetch_tx(ttype, loc["lawd_cd"], ym) for ym in yms], return_exceptions=True)
    items = [it for b in batches if not isinstance(b, Exception) for it in b]
    if dongs:
        want = [d.strip() for d in re.split(r"[,\s]+", dongs) if d.strip()]
        items = [it for it in items if any((it.get("umdNm") or "").strip() == w or (it.get("umdNm") or "").startswith(w)
                                           for w in want)]
        area_desc = ", ".join(want)
    elif loc["level"] in ("parcel", "dong"):
        items = [it for it in items if _same_umd(it, loc)]
        area_desc = loc.get("umd") or loc.get("address")
    else:
        area_desc = loc.get("address") or loc["lawd_cd"]
    target_names = [x for x in [loc.get("building_name"), (loc.get("complex") or {}).get("kaptName")] if x]
    rows = []
    for it in items:
        r = tx_row(it, kind)
        if min_build_year and int(num(r.get("건축년도")) or 0) < min_build_year:
            continue
        if area_min is not None and (r.get("면적㎡") or 0) < area_min:
            continue
        if area_max is not None and (r.get("면적㎡") or 1e9) > area_max:
            continue
        r["_target"] = loc["level"] == "parcel" and (_jibun_match(it, loc) and _same_umd(it, loc)
                                                     or _name_match(it, target_names, loc.get("umd")))
        rows.append(r)
    rows = dedupe_rows(rows)
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((r.get("법정동", ""), r.get("명칭") or f"(지번 {r.get('지번', '?')})"), []).append(r)
    table = []
    for (umd, nm), rs in groups.items():
        rs.sort(key=lambda r: r["계약일"], reverse=True)
        areas: dict[int, int] = {}
        for r in rs:
            if r.get("면적㎡"):
                areas[round(r["면적㎡"])] = areas.get(round(r["면적㎡"]), 0) + 1
        row: dict[str, Any] = {"법정동": umd, "명칭": nm, "건축년도": rs[0].get("건축년도"), "건수": len(rs),
                               "주력면적㎡": max(areas, key=areas.get) if areas else None}
        if any(r["_target"] for r in rs):
            row["대상"] = "★"
        if kind == "trade":
            v = [r for r in rs if "해제" not in r and r.get("거래금액(만원)")]
            if not v:
                continue
            row.update({"중위가(만원)": _median([r["거래금액(만원)"] for r in v]),
                        "중위평당가(만원/3.3㎡)": _median([r["평당가(만원/3.3㎡)"] for r in v if r.get("평당가(만원/3.3㎡)")]),
                        "최근": f"{v[0]['계약일']} {v[0].get('면적㎡', 0):.0f}㎡ {v[0]['거래금액(만원)']:,}"})
            row["건수"] = len(v)
        else:
            j = [r["보증금(만원)"] for r in rs if r.get("구분") == "전세" and r.get("보증금(만원)")]
            w = [r for r in rs if r.get("구분") == "월세"]
            row.update({"전세건수": len(j), "전세중위(만원)": _median(j), "월세건수": len(w),
                        "월세중위(보증금/월세)": f"{_median([r['보증금(만원)'] or 0 for r in w])}/{_median([r['월세(만원)'] for r in w])}" if w else None})
        table.append({k: v for k, v in row.items() if v is not None})
    table.sort(key=lambda r: (r.get("대상") != "★", -r["건수"]))
    trend: dict[str, list[dict]] = {}
    for r in rows:
        trend.setdefault(r["계약일"][:7], []).append(r)
    trend_rows = []
    for ym in sorted(trend):
        rs = trend[ym]
        if kind == "trade":
            v = [r for r in rs if "해제" not in r and r.get("평당가(만원/3.3㎡)")]
            trend_rows.append({"월": ym, "건수": len(v), "중위평당가": _median([r["평당가(만원/3.3㎡)"] for r in v]),
                               "중위가": _median([r["거래금액(만원)"] for r in v])})
        else:
            j = [r["보증금(만원)"] for r in rs if r.get("구분") == "전세" and r.get("보증금(만원)")]
            trend_rows.append({"월": ym, "건수": len(rs), "전세건수": len(j), "전세중위": _median(j)})
    out = {"조회대상": loc_summary(loc), "유형": ttype, "지역": area_desc,
           "기간": f"{yms[-1][:4]}-{yms[-1][4:]} ~ {yms[0][:4]}-{yms[0][4:]}", "전체건수": len(rows),
           "전체요약": tx_stats(rows, kind), "단지별": table[:top], "월별추이": trend_rows}
    if len(table) > top:
        out["참고"] = f"거래 많은 순 {top}개 단지만 표시 (전체 {len(table)}개)"
    if min_build_year:
        out["조건"] = f"{min_build_year}년 이후 준공"
    return out


# ---------------------------------------------------------------------------
# 건축물대장 / 인허가
# ---------------------------------------------------------------------------
BR_SECTIONS = {
    "표제부": "getBrTitleInfo", "총괄표제부": "getBrRecapTitleInfo", "기본개요": "getBrBasisOulnInfo",
    "층별개요": "getBrFlrOulnInfo", "전유부": "getBrExposInfo", "전유공용면적": "getBrExposPubuseAreaInfo",
    "주택가격": "getBrHsprcInfo", "지역지구구역": "getBrJijiguInfo", "부속지번": "getBrAtchJibunInfo",
    "오수정화시설": "getBrWclfInfo",
}
TITLE_FIELDS = [
    ("bldNm", "건물명"), ("dongNm", "동명칭"), ("platPlc", "지번주소"), ("newPlatPlc", "도로명주소"),
    ("regstrKindCdNm", "대장종류"), ("mainAtchGbCdNm", "주/부속"), ("mainPurpsCdNm", "주용도"),
    ("etcPurps", "기타용도"), ("strctCdNm", "구조"), ("platArea", "대지면적㎡"), ("archArea", "건축면적㎡"),
    ("bcRat", "건폐율%"), ("totArea", "연면적㎡"), ("vlRatEstmTotArea", "용적률산정연면적㎡"), ("vlRat", "용적률%"),
    ("grndFlrCnt", "지상층수"), ("ugrndFlrCnt", "지하층수"), ("heit", "높이m"), ("hhldCnt", "세대수"),
    ("hoCnt", "호수"), ("fmlyCnt", "가구수"), ("mainBldCnt", "주건축물수"), ("atchBldCnt", "부속건축물수"),
    ("rideUseElvtCnt", "승용승강기"), ("emgenUseElvtCnt", "비상승강기"), ("totPkngCnt", "총주차수"),
    ("pmsDay", "허가일"), ("stcnsDay", "착공일"), ("useAprDay", "사용승인일"), ("engrGrade", "에너지효율등급"),
]


def _title_row(it: dict) -> dict:
    r = {}
    for k, label in TITLE_FIELDS:
        v = it.get(k)
        if v is None or v in ("", " ", 0, "0", 0.0):
            continue
        r[label] = v.strip() if isinstance(v, str) else v
    pk = sum(int(num(it.get(k)) or 0) for k in ("indrMechUtcnt", "oudrMechUtcnt", "indrAutoUtcnt", "oudrAutoUtcnt"))
    if pk and "총주차수" not in r:
        r["주차대수"] = pk
    return r


async def building_register(location: str, section: str = "표제부", dong: str | None = None,
                            ho: str | None = None, limit: int = 50) -> dict:
    loc = await resolve(location)
    if loc["level"] != "parcel":
        raise ApiError("건축물대장은 번지(필지) 단위로 조회됩니다. 지번까지 포함한 주소나 단지명을 입력하세요.")
    op = BR_SECTIONS.get(section.replace(" ", ""), None)
    if not op:
        raise ApiError(f"section은 {list(BR_SECTIONS)} 중 하나")
    params = {"sigunguCd": loc["bjd_code"][:5], "bjdongCd": loc["bjd_code"][5:], "platGbCd": "1" if loc["san"] else "0",
              "bun": f"{loc['bun']:04d}", "ji": f"{loc['ji']:04d}", "_type": "json", "numOfRows": 100}
    if dong:
        dong = dong.strip()
        dong = f"{dong}동" if dong.isdigit() else dong
        if op not in ("getBrTitleInfo", "getBrRecapTitleInfo", "getBrBasisOulnInfo"):
            params["dongNm"] = dong
    if ho:
        ho = ho.strip().removesuffix("호")
        params["hoNm"] = ho
    rows: list[dict] = []
    total = 0
    for page in range(1, 11):
        d = await dg_json(f"1613000/BldRgstHubService/{op}", {**params, "pageNo": page})
        items, total = _json_items(d)
        rows.extend(items)
        if len(rows) >= total or not items:
            break
    alt_note = None
    if not rows and (loc.get("building_name") or loc.get("complex")) and not loc.get("_alt_tried"):
        # 재개발·합필 단지: 대장이 실거래 자료의 다른 지번으로 등록된 경우
        for alt in await _alt_pnus_from_trades(loc):
            p2 = {**params, "bjdongCd": alt[5:10], "platGbCd": "1" if alt[10] == "2" else "0",
                  "bun": alt[11:15], "ji": alt[15:19]}
            d = await dg_json(f"1613000/BldRgstHubService/{op}", {**p2, "pageNo": 1})
            items, total = _json_items(d)
            if items:
                rows, alt_note = items, f"대장이 다른 지번(PNU {alt})으로 등록되어 있어 그 자료를 표시"
                break
    if dong:
        dn = dong.removesuffix("동")
        rows = [r for r in rows if str(r.get("dongNm", "")).strip().removesuffix("동") == dn
                or dn in str(r.get("dongNm", ""))] or rows
    if ho:
        rows = [r for r in rows if not r.get("hoNm") or str(r.get("hoNm")).strip().removesuffix("호") == ho] or rows
    if op in ("getBrTitleInfo", "getBrRecapTitleInfo"):
        rows.sort(key=lambda r: (r.get("mainAtchGbCdNm") != "주건축물", -(num(r.get("totArea")) or 0)))
        out_rows = [_title_row(r) for r in rows]
    else:
        out_rows = [clean(r) for r in rows]
    res = {"조회대상": loc_summary(loc), "구분": section, "건수": total, "항목": out_rows[:limit]}
    if alt_note:
        res["참고"] = alt_note
    if not rows:
        res["참고"] = "해당 지번에 대장이 없습니다. 집합건물은 대표 지번(단지 주소)으로, 또는 '총괄표제부'로 시도해 보세요."
    if len(out_rows) > limit:
        res["참고"] = f"{limit}건만 표시. dong/ho로 좁히세요."
    return res


async def building_permits(location: str, kind: str = "건축", limit: int = 20) -> dict:
    loc = await resolve(location)
    if loc["level"] == "sigungu" or loc["level"] == "sido":
        raise ApiError("인허가는 법정동 또는 번지 단위로 조회하세요.")
    house = "주택" in kind
    svc, op = ("HsPmsHubService", "getHpBasisOulnInfo") if house else ("ArchPmsHubService", "getApBasisOulnInfo")
    params = {"sigunguCd": loc["bjd_code"][:5], "bjdongCd": loc["bjd_code"][5:], "_type": "json", "numOfRows": 100}
    if loc["level"] == "parcel":
        params.update({"platGbCd": "1" if loc["san"] else "0", "bun": f"{loc['bun']:04d}", "ji": f"{loc['ji']:04d}"})
    rows: list[dict] = []
    total = 0
    for page in range(1, 6):
        d = await dg_json(f"1613000/{svc}/{op}", {**params, "pageNo": page})
        items, total = _json_items(d)
        rows.extend(items)
        if len(rows) >= total or not items:
            break
    datek = "apprvDay" if house else "archPmsDay"
    rows.sort(key=lambda r: str(r.get(datek) or r.get("crtnDay") or ""), reverse=True)
    return {"조회대상": loc_summary(loc), "구분": "주택인허가" if house else "건축인허가", "전체건수": total,
            "표시": f"허가일 최신순 {min(limit, len(rows))}건 (최대 500건 중 정렬)",
            "항목": [clean(r) for r in rows[:limit]]}


# ---------------------------------------------------------------------------
# 단지정보
# ---------------------------------------------------------------------------
KAPT_LABELS = [
    ("kaptName", "단지명"), ("kaptAddr", "지번주소"), ("doroJuso", "도로명주소"), ("codeAptNm", "유형"),
    ("codeSaleNm", "분양형태"), ("codeHeatNm", "난방"), ("hoCnt", "세대수"), ("kaptDongCnt", "동수"),
    ("kaptTopFloor", "최고층"), ("kaptBaseFloor", "지하층"), ("kaptUsedate", "사용승인일"),
    ("kaptBcompany", "시공사"), ("kaptAcompany", "시행사"), ("codeHallNm", "복도유형"), ("codeMgrNm", "관리방식"),
    ("kaptTarea", "연면적㎡"), ("kaptMarea", "관리비부과면적㎡"), ("privArea", "주거전용면적합㎡"),
    ("kaptMparea60", "전용60이하세대"), ("kaptMparea85", "전용60~85세대"), ("kaptMparea135", "전용85~135세대"),
    ("kaptMparea136", "전용135초과세대"), ("kaptTel", "관리사무소"),
    ("codeStr", "구조"), ("kaptdPcnt", "지상주차"), ("kaptdPcntu", "지하주차"), ("kaptdEcnt", "승강기"),
    ("kaptdCccnt", "CCTV"), ("subwayLine", "지하철노선"), ("subwayStation", "지하철역"),
    ("kaptdWtimesub", "역까지거리"), ("kaptdWtimebus", "버스정류장까지"), ("welfareFacility", "복리시설"),
    ("convenientFacility", "편의시설"), ("educationFacility", "교육시설"),
    ("groundElChargerCnt", "지상전기차충전기"), ("undergroundElChargerCnt", "지하전기차충전기"),
    ("kaptCcompany", "관리업체"),
]


async def complex_info(location: str) -> dict:
    loc = await resolve(location)
    cx = loc.get("complex")
    candidates = []
    if not cx:
        if loc["level"] not in ("parcel", "dong"):
            raise ApiError("단지명이나 단지 주소를 입력하세요.")
        d = await dg_json("1613000/AptListService4/getLegaldongAptList4",
                          {"bjdCode": loc["bjd_code"], "pageNo": 1, "numOfRows": 500})
        items, _ = _json_items(d)
        names = [x for x in [loc.get("building_name"), location] if x]
        best = None
        for n in names:
            sc = match_kapt(n, items)
            if sc:
                best = sc[0][1]
                break
        if not best and loc["level"] == "parcel":
            # 주소로 들어온 경우: 단지별 지번주소와 비교
            for it in items[:80]:
                b = await kapt_basis(it["kaptCode"])
                umd, bun, ji, _ = _parse_parcel_text(b.get("kaptAddr", ""))
                if bun == loc.get("bun") and (ji == loc.get("ji") or not loc.get("ji")):
                    best = it
                    break
        if not best:
            return {"조회대상": loc_summary(loc), "결과": "단지를 특정하지 못했습니다",
                    "같은 법정동 단지": [f"{it['kaptName']} ({it['kaptCode']})" for it in items[:40]]}
        cx = {"kaptCode": best["kaptCode"], "kaptName": best["kaptName"]}
    basis, detail = await asyncio.gather(kapt_basis(cx["kaptCode"]), kapt_detail(cx["kaptCode"]))
    merged = {**detail, **basis}
    info = {}
    for k, label in KAPT_LABELS:
        v = merged.get(k)
        if v in (None, "", " ", 0, 0.0, "0"):
            continue
        info[label] = v.strip() if isinstance(v, str) else v
    if "지상주차" in info or "지하주차" in info:
        info["총주차"] = int(num(info.get("지상주차")) or 0) + int(num(info.get("지하주차")) or 0)
    res = {"조회대상": loc_summary(loc), "단지코드": cx["kaptCode"], "단지정보": info}
    if candidates:
        res["후보"] = candidates
    return res


# ---------------------------------------------------------------------------
# 토지 (브이월드)
# ---------------------------------------------------------------------------
async def land_info(location: str, years: int = 10) -> dict:
    _need_vw_key()
    loc = await resolve(location)
    if loc["level"] != "parcel":
        raise ApiError("토지정보는 번지(필지) 단위로 조회됩니다.")
    pnu = loc["pnu"]
    price_r, char_r, use_r, own_r = await asyncio.gather(
        ned("getIndvdLandPriceAttr", {"pnu": pnu}, rows=100),
        ned("getLandCharacteristics", {"pnu": pnu}, rows=100),
        ned("getLandUseAttr", {"pnu": pnu}, rows=200),
        ned("getPossessionAttr", {"pnu": pnu}, rows=200),
        return_exceptions=True)
    out: dict[str, Any] = {"조회대상": loc_summary(loc)}
    area = None
    if isinstance(char_r, list) and char_r:
        c = max(char_r, key=lambda r: str(r.get("stdrYear")))
        area = num(c.get("lndpclAr"))
        out["토지특성"] = {k: v for k, v in {
            "기준년도": c.get("stdrYear"), "지목": c.get("lndcgrCodeNm"), "면적㎡": c.get("lndpclAr"),
            "면적평": round(area / 3.3058, 1) if area else None, "용도지역1": c.get("prposArea1Nm"),
            "용도지역2": c.get("prposArea2Nm"), "이용상황": c.get("ladUseSittnNm"), "지형높이": c.get("tpgrphHgCodeNm"),
            "지형형상": c.get("tpgrphFrmCodeNm"), "도로접면": c.get("roadSideCodeNm"),
        }.items() if v not in (None, "", "지정되지않음")}
    elif isinstance(char_r, Exception):
        out["토지특성_오류"] = str(char_r)
    if isinstance(price_r, list) and price_r:
        hist = sorted(price_r, key=lambda r: (str(r.get("stdrYear")), str(r.get("stdrMt"))))
        latest = hist[-1]
        p = num(latest.get("pblntfPclnd"))
        lp = {"기준년도": latest.get("stdrYear"), "공시일": latest.get("pblntfDe"), "원/㎡": won(p),
              "원/평": won(p * 3.3058) if p else None, "표준지여부": latest.get("stdLandAt")}
        if p and area:
            lp["토지가액(원)"] = won(p * area)
        if len(hist) >= 2:
            prev = num(hist[-2].get("pblntfPclnd"))
            if prev and p:
                lp["전년대비%"] = round((p / prev - 1) * 100, 2)
        out["개별공시지가"] = lp
        by_year = {h.get("stdrYear"): h for h in hist}  # 같은 연도 중복 행은 마지막 것만
        out["공시지가추이"] = [{"연도": y, "원/㎡": won(num(h.get("pblntfPclnd")))}
                          for y, h in sorted(by_year.items())[-years:]]
    elif isinstance(price_r, Exception):
        out["공시지가_오류"] = str(price_r)
    if isinstance(use_r, list) and use_r:
        seen, plans = set(), []
        for u in use_r:
            key = (u.get("prposAreaDstrcCodeNm"), u.get("cnflcAtNm"))
            if key in seen or not key[0]:
                continue
            seen.add(key)
            plans.append(f"{key[0]}({key[1]})" if key[1] else key[0])
        out["토지이용계획"] = plans
    elif isinstance(use_r, Exception):
        out["토지이용계획_오류"] = str(use_r)
    if isinstance(own_r, list) and own_r:
        cnt: dict[str, int] = {}
        for o in own_r:
            k = o.get("posesnSeCodeNm") or "미상"
            cnt[k] = cnt.get(k, 0) + 1
        o0 = max(own_r, key=lambda r: str(r.get("ownshipChgDe") or ""))
        out["토지소유"] = {k: v for k, v in {
            "소유구분별건수": cnt, "공유인수": o0.get("cnrsPsnCo"), "최근소유권변동원인": o0.get("ownshipChgCauseCodeNm"),
            "최근소유권변동일": o0.get("ownshipChgDe"), "거주지구분": o0.get("resdncSeCodeNm")}.items() if v not in (None, "")}
        if len(own_r) >= 200:
            out["토지소유"]["참고"] = "집합건물은 소유자 행이 많아 앞쪽 200건만 집계한 표본입니다."
    return out


# ---------------------------------------------------------------------------
# 주택 공시가격 (브이월드 + 건축물대장)
# ---------------------------------------------------------------------------
async def _alt_pnus_from_trades(loc: dict) -> list[str]:
    """같은 단지명이 실거래 자료에서 쓰는 지번 → 다른 PNU 후보."""
    names = [x for x in [loc.get("building_name"), (loc.get("complex") or {}).get("kaptName"), loc.get("query")] if x]
    if not names or not loc.get("bjd_code"):
        return []
    batches = await asyncio.gather(*[fetch_tx("아파트매매", loc["lawd_cd"], ym) for ym in months_back(12)],
                                   return_exceptions=True)
    cnt: dict[str, int] = {}
    for b in batches:
        if isinstance(b, Exception):
            continue
        for it in b:
            j = (it.get("jibun") or "").strip()
            if j and "*" not in j and _same_umd(it, loc) and _name_match(it, names, loc.get("umd")):
                cnt[j] = cnt.get(j, 0) + 1
    out = []
    for j, _ in sorted(cnt.items(), key=lambda kv: -kv[1])[:3]:
        m = re.match(r"(산)?(\d+)(?:-(\d+))?$", j)
        if m and loc["bjd_code"][5:] != "00000":
            p = make_pnu(loc["bjd_code"], int(m.group(2)), int(m.group(3) or 0), bool(m.group(1)))
            if p != loc.get("pnu"):
                out.append(p)
    return out


async def housing_price(location: str, dong: str | None = None, ho: str | None = None,
                        year: str | None = None) -> dict:
    loc = await resolve(location)
    if loc["level"] != "parcel":
        raise ApiError("공시가격은 번지(단지 주소) 단위로 조회됩니다.")
    out: dict[str, Any] = {"조회대상": loc_summary(loc)}
    if VW_KEY:
        this_year = dt.date.today().year
        years = [year] if year else [str(this_year), str(this_year - 1)]

        async def apt_prices(pnu: str) -> tuple[list[dict], str | None]:
            for y in years:
                params = {"pnu": pnu, "stdrYear": y}
                if dong:
                    params["dongNm"] = dong.replace("동", "") if dong.endswith("동") and dong[:-1].isdigit() else dong
                if ho:
                    params["hoNm"] = ho.replace("호", "")
                try:
                    rows = await ned("getApartHousingPriceAttr", params, rows=1000, max_pages=10)
                except ApiError as e:
                    out["공동주택가격_오류"] = str(e)
                    rows = []
                if rows:
                    return rows, y
            return [], None

        apt, used_year = await apt_prices(loc["pnu"])
        if not apt and (loc.get("building_name") or loc.get("complex")):
            # 합필·지번변경 단지: 공시가격이 실거래 자료의 옛 지번으로 남아있는 경우
            for alt in await _alt_pnus_from_trades(loc):
                apt, used_year = await apt_prices(alt)
                if apt:
                    out["참고"] = f"공시가격은 다른 지번(PNU {alt})으로 등록되어 있어 그 자료를 표시"
                    break
        if apt:
            if dong:
                dn = dong.replace("동", "")
                apt = [a for a in apt if dn in str(a.get("dongNm"))] or apt
            if ho:
                hn = ho.replace("호", "")
                apt = [a for a in apt if str(a.get("hoNm")) == hn] or apt
            if dong and ho and len(apt) <= 5:
                out["공동주택가격"] = [{"기준년도": a.get("stdrYear"), "동": a.get("dongNm"), "호": a.get("hoNm"),
                                   "전용㎡": a.get("prvuseAr"), "공시가격(원)": won(num(a.get("pblntfPc")))} for a in apt]
            else:
                groups: dict[str, list[float]] = {}
                for a in apt:
                    groups.setdefault(str(a.get("prvuseAr")), []).append(num(a.get("pblntfPc")) or 0)
                out["공동주택가격"] = {"기준년도": used_year, "호수": len(apt), "전용면적별": [
                    {"전용㎡": k, "호수": len(v), "최저(원)": won(min(v)), "중위(원)": won(statistics.median(v)),
                     "최고(원)": won(max(v))} for k, v in sorted(groups.items(), key=lambda kv: num(kv[0]) or 0)]}
        else:
            try:
                ind = await ned("getIndvdHousingPriceAttr", {"pnu": loc["pnu"]}, rows=100)
            except ApiError as e:
                ind = []
                out["개별주택가격_오류"] = str(e)
            if ind:
                hist = sorted(ind, key=lambda r: str(r.get("stdrYear")))
                last = hist[-1]
                out["개별주택가격"] = {"최근": clean(last),
                                   "추이": [{"연도": h.get("stdrYear"), "가격(원)": won(num(h.get("housePc")))} for h in hist[-10:]]}
    if "공동주택가격" not in out and "개별주택가격" not in out:
        try:
            r = await building_register(location, "주택가격", dong, ho, limit=30)
            if r.get("항목"):
                out["건축물대장_주택가격"] = r["항목"]
        except ApiError as e:
            out["건축물대장_오류"] = str(e)
    if len(out) == 1:
        out["결과"] = "공시가격 자료를 찾지 못했습니다 (주택이 아니거나 올해 공시 전일 수 있음)."
    return out


# ---------------------------------------------------------------------------
# 청약홈
# ---------------------------------------------------------------------------
PRESALE_KINDS = {
    "APT": ("getAPTLttotPblancDetail", "getAPTLttotPblancMdl", "getAPTLttotPblancCmpet"),
    "오피스텔": ("getUrbtyOfctlLttotPblancDetail", "getUrbtyOfctlLttotPblancMdl", "getUrbtyOfctlLttotPblancCmpet"),
    "잔여세대": ("getRemndrLttotPblancDetail", "getRemndrLttotPblancMdl", "getRemndrLttotPblancCmpet"),
    "공공지원민간임대": ("getPblPvtRentLttotPblancDetail", "getPblPvtRentLttotPblancMdl", "getPblPvtRentLttotPblancCmpet"),
    "임의공급": ("getOPTLttotPblancDetail", "getOPTLttotPblancMdl", "getOPTLttotPblancCmpet"),
}
PRESALE_FIELDS = [
    ("HOUSE_MANAGE_NO", "주택관리번호"), ("PBLANC_NO", "공고번호"), ("HOUSE_NM", "주택명"),
    ("HOUSE_SECD_NM", "주택구분"), ("HOUSE_DTL_SECD_NM", "상세구분"), ("HSSPLY_ADRES", "공급위치"),
    ("TOT_SUPLY_HSHLDCO", "공급세대수"), ("RCRIT_PBLANC_DE", "모집공고일"), ("SUBSCRPT_RCEPT_BGNDE", "청약접수시작"),
    ("SUBSCRPT_RCEPT_ENDDE", "청약접수종료"), ("SPSPLY_RCEPT_BGNDE", "특별공급접수"),
    ("GNRL_RNK1_CRSPAREA_RCPTDE", "1순위(해당지역)"), ("PRZWNER_PRESNATN_DE", "당첨자발표"),
    ("CNTRCT_CNCLS_BGNDE", "계약시작"), ("CNTRCT_CNCLS_ENDDE", "계약종료"), ("MVN_PREARNGE_YM", "입주예정월"),
    ("SPECLT_RDN_EARTH_AT", "투기과열지구"), ("MDAT_TRGET_AREA_SECD", "조정대상지역"),
    ("PARCPRC_ULS_AT", "분양가상한제"), ("BSNS_MBY_NM", "사업주체"), ("CNSTRCT_ENTRPS_NM", "시공사"),
    ("HMPG_ADRES", "홈페이지"), ("PBLANC_URL", "공고URL"),
]


def _presale_kind(kind: str | None) -> str:
    k = (kind or "APT").replace(" ", "")
    for key in PRESALE_KINDS:
        if key.lower() in k.lower():
            return key
    if any(w in k for w in ("도시형", "생활숙박", "민간임대")) and "공공" not in k:
        return "오피스텔"
    if "무순위" in k or "줍줍" in k:
        return "잔여세대"
    return "APT"


async def presale_search(keyword: str | None = None, region: str | None = None, kind: str = "APT",
                         since: str | None = None, limit: int = 20) -> dict:
    k = _presale_kind(kind)
    since = since or (dt.date.today() - dt.timedelta(days=730)).isoformat()
    params: dict[str, Any] = {"page": 1, "perPage": 300, "cond[RCRIT_PBLANC_DE::GTE]": since}
    if keyword:
        params["cond[HOUSE_NM::LIKE]"] = keyword
    if region:
        params["cond[HSSPLY_ADRES::LIKE]"] = region
    d = await od_get(f"ApplyhomeInfoDetailSvc/v1/{PRESALE_KINDS[k][0]}", params)
    data = d.get("data", []) or []
    data.sort(key=lambda r: str(r.get("RCRIT_PBLANC_DE") or ""), reverse=True)
    rows = []
    for r in data[:limit]:
        row = {label: r.get(key) for key, label in PRESALE_FIELDS if r.get(key) not in (None, "")}
        if not row.get("주택명"):
            row = clean(r)
        rows.append(row)
    return {"구분": k, "조건": {"키워드": keyword, "지역": region, "공고일이후": since},
            "전체매칭": d.get("matchCount"), "목록": rows}


async def presale_detail(house_manage_no: str, kind: str = "APT", pblanc_no: str | None = None) -> dict:
    k = _presale_kind(kind)
    det, mdl, cmp_ = PRESALE_KINDS[k]
    cond = {"cond[HOUSE_MANAGE_NO::EQ]": house_manage_no, "page": 1, "perPage": 200}
    if pblanc_no:
        cond["cond[PBLANC_NO::EQ]"] = pblanc_no
    calls = [od_get(f"ApplyhomeInfoDetailSvc/v1/{det}", cond), od_get(f"ApplyhomeInfoDetailSvc/v1/{mdl}", cond),
             od_get(f"ApplyhomeInfoCmpetRtSvc/v1/{cmp_}", cond)]
    if k == "APT":
        calls += [od_get("ApplyhomeInfoCmpetRtSvc/v1/getAptLttotPblancScore", cond),
                  od_get("ApplyhomeInfoCmpetRtSvc/v1/getAPTSpsplyReqstStus", cond)]
    res = await asyncio.gather(*calls, return_exceptions=True)
    labels = ["공고정보", "주택형별", "경쟁률", "당첨가점", "특별공급신청"]
    out: dict[str, Any] = {"구분": k, "주택관리번호": house_manage_no}
    for label, r in zip(labels, res):
        if isinstance(r, Exception):
            out[label + "_오류"] = str(r)
            continue
        data = r.get("data", []) or []
        if label == "공고정보" and data:
            out[label] = {lab: data[0].get(key) for key, lab in PRESALE_FIELDS if data[0].get(key) not in (None, "")}
        else:
            out[label] = [clean(x) for x in data]
    return out


SUB_STATS = {
    "지역별신청자": "getAPTReqstAreaStat", "연령별신청자": "getAPTReqstAgeStat",
    "지역별당첨자": "getAPTPrzwnerAreaStat", "연령별당첨자": "getAPTPrzwnerAgeStat",
    "지역별경쟁률": "getAPTCmpetrtAreaStat", "가점제당첨자": "getAPTApsPrzwnerStat",
}


async def subscription_stats(stat: str = "지역별경쟁률", from_month: str | None = None,
                             to_month: str | None = None, area_code: str | None = None) -> dict:
    op = SUB_STATS.get(stat.replace(" ", ""))
    if not op:
        raise ApiError(f"stat은 {list(SUB_STATS)} 중 하나")
    params: dict[str, Any] = {"page": 1, "perPage": 500}
    if from_month:
        params["cond[STAT_DE::GTE]"] = from_month
    if to_month:
        params["cond[STAT_DE::LTE]"] = to_month
    if area_code and "AreaStat" in op or (area_code and op == "getAPTApsPrzwnerStat"):
        params["cond[SUBSCRPT_AREA_CODE::EQ]"] = area_code
    d = await od_get(f"ApplyhomeStatSvc/v1/{op}", params)
    data = d.get("data", []) or []
    data.sort(key=lambda r: str(r.get("STAT_DE") or ""), reverse=True)
    return {"통계": stat, "건수": len(data), "자료": [clean(x) for x in data[:200]]}


# ---------------------------------------------------------------------------
# 지도 이미지 (브이월드)
# ---------------------------------------------------------------------------
BASEMAPS = {"항공사진": "PHOTO", "위성": "PHOTO", "사진": "PHOTO", "하이브리드": "PHOTO_HYBRID",
            "일반지도": "GRAPHIC", "지도": "GRAPHIC", "백지도": "GRAPHIC_WHITE", "야간": "GRAPHIC_NIGHT"}
OVERLAYS = {"지적도": "lp_pa_cbnd_bubun", "용도지역": "lt_c_uq111", "도시계획도로": "lt_c_upisuq151"}


def _merc(lon: float, lat: float) -> tuple[float, float]:
    x = lon * 20037508.342789244 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * 20037508.342789244 / math.pi
    return x, y


async def map_image(location: str, map_type: str = "항공사진", overlay: str = "지적도", zoom: int = 18,
                    size: int = 800, save: bool = True) -> tuple[dict, bytes]:
    _need_vw_key()
    loc = await ensure_point(await resolve(location))
    zoom = max(6, min(19, int(zoom)))
    size = max(256, min(1024, int(size)))
    bm = next((v for k, v in BASEMAPS.items() if k in (map_type or "")), "PHOTO")
    base = await _get(f"{VW}/req/image", {
        "service": "image", "request": "getmap", "key": VW_KEY, "domain": VW_DOMAIN, "format": "png",
        "basemap": bm, "center": f"{loc['lon']},{loc['lat']}", "crs": "epsg:4326", "zoom": zoom,
        "size": f"{size},{size}"})
    if "image" not in base.headers.get("content-type", ""):
        raise ApiError(f"지도 이미지 오류: {base.text[:300]}")
    png = base.content
    layers = [OVERLAYS[k] for k in OVERLAYS if overlay and k in overlay]
    if layers and PILImage is not None:
        x, y = _merc(loc["lon"], loc["lat"])
        res = 156543.03392804097 / (2 ** zoom)
        half = size / 2 * res
        img = PILImage.open(io.BytesIO(png)).convert("RGBA")
        for layer in layers:
            r = await _get(f"{VW}/req/wms", {
                "service": "WMS", "request": "GetMap", "version": "1.3.0", "key": VW_KEY, "domain": VW_DOMAIN,
                "layers": layer, "styles": layer, "crs": "EPSG:3857",
                "bbox": f"{x - half},{y - half},{x + half},{y + half}", "width": size, "height": size,
                "format": "image/png", "transparent": "true"})
            if "image" in r.headers.get("content-type", ""):
                ov = PILImage.open(io.BytesIO(r.content)).convert("RGBA").resize(img.size)
                img = PILImage.alpha_composite(img, ov)
        draw = ImageDraw.Draw(img)
        c = size / 2
        draw.ellipse([c - 7, c - 7, c + 7, c + 7], outline=(255, 0, 0, 255), width=3)
        draw.line([c - 12, c, c + 12, c], fill=(255, 0, 0, 255), width=1)
        draw.line([c, c - 12, c, c + 12], fill=(255, 0, 0, 255), width=1)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", optimize=True)
        png = buf.getvalue()
    meta = {"조회대상": loc_summary(loc), "지도": map_type, "오버레이": overlay if layers else "없음", "줌": zoom,
            "축척": f"1px≈{156543.03 * math.cos(math.radians(loc['lat'])) / 2 ** zoom:.2f}m"}
    if "lp_pa_cbnd_bubun" in layers and zoom < 18:
        meta["참고"] = "지적도는 브이월드 정책상 줌 18 이상에서만 그려집니다. 필지 경계가 필요하면 zoom=18 이상으로 다시 요청하세요."
    if save:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        fn = re.sub(r"[\\/:*?\"<>|\s]+", "_", f"{loc.get('building_name') or loc.get('address') or loc['query']}_{map_type}_{overlay}_{zoom}")[:80]
        p = IMAGE_DIR / f"{fn}.png"
        p.write_bytes(png)
        meta["저장위치"] = str(p)
    return meta, png


# ---------------------------------------------------------------------------
# 종합 리포트
# ---------------------------------------------------------------------------
def _guess_tx_type(title: dict | None, has_complex: bool) -> list[str]:
    if has_complex:
        return ["아파트매매", "아파트전월세"]
    if not title:
        return ["토지매매"]
    p = f"{title.get('주용도', '')} {title.get('기타용도', '')}"
    if "오피스텔" in p:
        return ["오피스텔매매", "오피스텔전월세"]
    if "아파트" in p:
        return ["아파트매매", "아파트전월세"]
    if any(w in p for w in ("다세대", "연립", "공동주택")):
        return ["연립다세대매매", "연립다세대전월세"]
    if any(w in p for w in ("단독", "다가구", "다중주택")):
        return ["단독다가구매매", "단독다가구전월세"]
    if any(w in p for w in ("공장", "창고")):
        return ["공장창고매매"]
    return ["상업업무용매매"]


async def report_core(location: str, months: int = 12) -> dict:
    loc = await resolve(location)
    out: dict[str, Any] = {"조회대상": loc_summary(loc)}
    q = loc.get("pnu") or location
    if loc["level"] != "parcel":
        tx = await real_transactions(location, "아파트매매", months=months, limit=15)
        out["아파트매매(법정동)"] = {k: tx[k] for k in ("기간", "필터", "건수", "요약", "거래목록")}
        return out
    async def bld() -> dict:
        r = await building_register(q, "총괄표제부", limit=3)
        if r.get("항목"):
            main = await building_register(q, "표제부", limit=200)
            mains = [x for x in main.get("항목", []) if x.get("주/부속") == "주건축물"]
            r["동별요약"] = {"주건축물수": len(mains),
                         "최고층": max((int(num(x.get("지상층수")) or 0) for x in mains), default=None),
                         "동목록": [x.get("동명칭") for x in mains][:60]}
            return r
        return await building_register(q, "표제부", limit=5)

    tasks = {"토지": land_info(q) if VW_KEY else None, "건축물": bld()}
    res = await asyncio.gather(*[t for t in tasks.values() if t], return_exceptions=True)
    it = iter(res)
    for k, t in tasks.items():
        if t is None:
            continue
        r = next(it)
        if isinstance(r, Exception):
            out[k + "_오류"] = str(r)
        else:
            r.pop("조회대상", None)
            out[k] = r
    items = (out.get("건축물") or {}).get("항목") or []
    title = items[0] if items else None
    has_cx = bool(loc.get("complex"))
    looks_apt = bool(title) and ("공동주택" in str(title.get("주용도", "")) and "아파트" in
                                 f"{title.get('기타용도', '')}{title.get('건물명', '')}")
    if has_cx or looks_apt:
        try:
            ci = await complex_info(location)
            if ci.get("단지정보"):
                out["단지"] = ci["단지정보"]
                has_cx = True
        except ApiError as e:
            if has_cx:
                out["단지_오류"] = str(e)
    types = _guess_tx_type(title, has_cx)
    txs = await asyncio.gather(*[real_transactions(location, t, months=months, limit=10) for t in types],
                               return_exceptions=True)
    for t, r in zip(types, txs):
        if isinstance(r, Exception):
            out[t + "_오류"] = str(r)
        else:
            out[t] = {k: r[k] for k in ("기간", "필터", "건수", "요약", "거래목록") if k in r}
    if any(w in " ".join(types) for w in ("아파트", "연립", "단독", "오피스텔")):
        try:
            hp = await housing_price(q)
            hp.pop("조회대상", None)
            out["공시가격"] = hp
        except ApiError as e:
            out["공시가격_오류"] = str(e)
    return out


# ---------------------------------------------------------------------------
# MCP 서버 / 도구 정의
# ---------------------------------------------------------------------------
INSTRUCTIONS = """한국 부동산 공공데이터 조회 도구입니다.
- location에는 지번주소, 도로명주소, 아파트 단지명, 법정동명, PNU(19자리)를 그대로 넣으면 됩니다.
- 처음에는 property_report로 한 번에 훑고, 필요한 항목만 개별 도구로 깊게 조회하세요.
- 금액 단위: 실거래가는 만원, 공시지가·공시가격은 원.
- 실거래가는 계약일 기준이며 신고기한(30일) 때문에 최근 1~2개월은 덜 집계됩니다.
- 조회대상의 other_candidates가 있으면 사용자가 의도한 곳이 맞는지 확인하세요.
- IM·투자보고서의 시장/입지 파트가 필요하면 create_market_report로 PPT·PDF를 만들고, 돌려받은 문장초안을 다듬어 bullets로 다시 호출하세요.
"""

mcp = _Server("realestate", instructions=INSTRUCTIONS)


def tool(**kw):
    try:
        return mcp.tool(structured_output=False, **kw)
    except TypeError:
        return mcp.tool(**kw)


Loc = Annotated[str, Field(description="지번주소/도로명주소/아파트명/법정동명/PNU. 예: '서울 동대문구 휘경동 172', '답십리로 130', '래미안위브', '휘경동'")]


async def _run(coro) -> str:
    try:
        return dumps(await coro)
    except ApiError as e:
        return dumps({"오류": str(e)})
    except Exception as e:  # noqa: BLE001
        log.exception("tool error")
        return dumps({"오류": f"{type(e).__name__}: {e}"})


@tool()
async def property_report(location: Loc,
                          months: Annotated[int, Field(description="실거래 조회 개월 수", ge=1, le=60)] = 12) -> str:
    """주소/단지명 하나로 종합 조회: 위치해석 + 공시지가·토지특성·토지이용계획 + 건축물대장 표제부 + 단지정보 + 최근 실거래(매매·전월세) + 공시가격."""
    return await _run(report_core(location, months))


@tool()
async def resolve_location(location: Loc) -> str:
    """입력한 주소/단지명이 어느 필지·법정동으로 해석되는지 확인 (PNU, 법정동코드, 좌표, 후보)."""
    return await _run(_resolve_summary(location))


async def _resolve_summary(location: str) -> dict:
    return loc_summary(await resolve(location))


@tool()
async def get_real_transactions(
    location: Loc,
    property_type: Annotated[str, Field(description="아파트매매/아파트전월세/분양권/오피스텔매매/오피스텔전월세/연립다세대매매/연립다세대전월세/단독다가구매매/단독다가구전월세/상업업무용매매/토지매매/공장창고매매 (빌라·전세·상가 등 자연어도 가능)")] = "아파트매매",
    months: Annotated[int, Field(description="최근 몇 개월", ge=1, le=60)] = 12,
    name: Annotated[Optional[str], Field(description="단지/건물명으로 필터 (선택)")] = None,
    scope: Annotated[str, Field(description="auto(건물 우선)/building/dong/sigungu")] = "auto",
    area_min: Annotated[Optional[float], Field(description="최소 면적㎡ (전용 등)")] = None,
    area_max: Annotated[Optional[float], Field(description="최대 면적㎡")] = None,
    end_month: Annotated[Optional[str], Field(description="마지막 계약월 YYYYMM (기본: 이번 달)")] = None,
    limit: Annotated[int, Field(description="표시할 최대 거래 수", ge=1, le=500)] = 30,
) -> str:
    """국토부 실거래가 조회. 건물/단지 주소나 이름이면 그 건물 거래, 동 이름이면 법정동 전체, 구 이름이면 시군구 전체. 요약통계(중위가·평당가·면적별) 포함."""
    return await _run(real_transactions(location, property_type, months, name, scope, area_min, area_max,
                                        end_month, limit))


@tool()
async def get_market_summary(
    location: Loc,
    property_type: Annotated[str, Field(description="아파트매매/아파트전월세/오피스텔매매/연립다세대매매 등")] = "아파트매매",
    months: Annotated[int, Field(description="최근 몇 개월", ge=1, le=60)] = 24,
    dongs: Annotated[Optional[str], Field(description="비교할 법정동들 (예: '신평동,하단동,장림동'). 비우면 대상이 속한 동")] = None,
    min_build_year: Annotated[Optional[int], Field(description="이 연도 이후 준공 단지만 (신축 비교용)")] = None,
    area_min: Annotated[Optional[float], Field(description="최소 면적㎡")] = None,
    area_max: Annotated[Optional[float], Field(description="최대 면적㎡")] = None,
    top: Annotated[int, Field(ge=1, le=100)] = 30,
) -> str:
    """인근 시세조사: 같은 동(또는 지정한 동들)의 단지별 거래건수·중위가·평당가·최근거래와 월별 추이. 대상 단지는 ★ 표시."""
    return await _run(market_summary(location, property_type, months, dongs, min_build_year, area_min, area_max, top))


@tool()
async def get_land_info(location: Loc,
                        years: Annotated[int, Field(description="공시지가 추이 연수", ge=1, le=40)] = 10) -> str:
    """토지 정보: 개별공시지가(최신·추이·토지가액), 토지특성(지목·면적·용도지역·도로접면 등), 토지이용계획(용도지역지구), 토지소유 요약."""
    return await _run(land_info(location, years))


@tool()
async def get_official_housing_price(
    location: Loc,
    dong: Annotated[Optional[str], Field(description="동 (예: 101)")] = None,
    ho: Annotated[Optional[str], Field(description="호 (예: 1502)")] = None,
    year: Annotated[Optional[str], Field(description="기준년도 YYYY (기본: 올해→작년)")] = None,
) -> str:
    """주택 공시가격: 공동주택가격(동·호 지정 시 해당 호, 아니면 전용면적별 요약) 또는 개별주택가격."""
    return await _run(housing_price(location, dong, ho, year))


@tool()
async def get_building_register(
    location: Loc,
    section: Annotated[str, Field(description="표제부/총괄표제부/기본개요/층별개요/전유부/전유공용면적/주택가격/지역지구구역/부속지번/오수정화시설")] = "표제부",
    dong: Annotated[Optional[str], Field(description="동명칭 (전유부 등에서 필터)")] = None,
    ho: Annotated[Optional[str], Field(description="호명칭")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> str:
    """건축물대장(건축HUB) 조회: 용도·구조·면적·건폐율·용적률·층수·세대수·사용승인일 등."""
    return await _run(building_register(location, section, dong, ho, limit))


@tool()
async def get_building_permits(
    location: Loc,
    kind: Annotated[str, Field(description="건축(건축인허가) 또는 주택(주택인허가)")] = "건축",
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> str:
    """건축/주택 인허가 이력 (번지 또는 법정동 단위, 허가일 최신순)."""
    return await _run(building_permits(location, kind, limit))


@tool()
async def get_apartment_complex(location: Loc) -> str:
    """공동주택(K-apt) 단지 정보: 세대수·동수·층수·사용승인일·시공사·난방·주차·승강기·지하철·편의/교육시설 등."""
    return await _run(complex_info(location))


@tool()
async def search_presale(
    keyword: Annotated[Optional[str], Field(description="주택명 키워드")] = None,
    region: Annotated[Optional[str], Field(description="공급위치 키워드 (예: 동대문구, 경기 화성)")] = None,
    kind: Annotated[str, Field(description="APT/오피스텔/잔여세대/공공지원민간임대/임의공급")] = "APT",
    since: Annotated[Optional[str], Field(description="모집공고일 이후 YYYY-MM-DD (기본 1년 전)")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> str:
    """청약홈 분양공고 검색 (모집공고일 최신순). 결과의 주택관리번호로 get_presale_detail 조회."""
    return await _run(presale_search(keyword, region, kind, since, limit))


@tool()
async def get_presale_detail(
    house_manage_no: Annotated[str, Field(description="주택관리번호")],
    kind: Annotated[str, Field(description="APT/오피스텔/잔여세대/공공지원민간임대/임의공급")] = "APT",
    pblanc_no: Annotated[Optional[str], Field(description="공고번호 (선택)")] = None,
) -> str:
    """분양 상세: 공고정보, 주택형별 공급·분양가, 청약 경쟁률, 당첨가점, 특별공급 신청현황."""
    return await _run(presale_detail(house_manage_no, kind, pblanc_no))


@tool()
async def get_subscription_stats(
    stat: Annotated[str, Field(description="지역별신청자/연령별신청자/지역별당첨자/연령별당첨자/지역별경쟁률/가점제당첨자")] = "지역별경쟁률",
    from_month: Annotated[Optional[str], Field(description="시작 YYYYMM")] = None,
    to_month: Annotated[Optional[str], Field(description="종료 YYYYMM")] = None,
    area_code: Annotated[Optional[str], Field(description="청약지역코드 (선택)")] = None,
) -> str:
    """청약홈 청약 신청·당첨 통계."""
    return await _run(subscription_stats(stat, from_month, to_month, area_code))


@tool()
async def get_map_image(
    location: Loc,
    map_type: Annotated[str, Field(description="항공사진/하이브리드/일반지도/백지도/야간")] = "항공사진",
    overlay: Annotated[str, Field(description="지적도/용도지역/도시계획도로/없음 (여러 개면 '지적도,용도지역')")] = "지적도",
    zoom: Annotated[int, Field(description="줌 6~19 (18≈건물, 16≈동네)", ge=6, le=19)] = 18,
    size: Annotated[int, Field(description="이미지 크기(px)", ge=256, le=1024)] = 800,
):
    """위치의 지도/항공사진 이미지를 반환 (지적도·용도지역 겹쳐보기, 가운데 빨간 표시가 대상 위치). 이미지는 images 폴더에도 저장."""
    try:
        meta, png = await map_image(location, map_type, overlay, zoom, size)
        return [dumps(meta), Image(data=png, format="png")]
    except ApiError as e:
        return dumps({"오류": str(e)})
    except Exception as e:  # noqa: BLE001
        log.exception("map error")
        return dumps({"오류": f"{type(e).__name__}: {e}"})


@tool()
async def call_public_api(
    path: Annotated[str, Field(description="apis.data.go.kr 이후 경로. 예: 1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent")],
    params: Annotated[dict, Field(description="쿼리 파라미터 (serviceKey 제외)")],
) -> str:
    """고급: 활용신청한 공공데이터포털 API를 직접 호출 (전용 도구에 없는 오퍼레이션용)."""
    async def go():
        _need_data_key()
        r = await _get(f"{DG}/{path.lstrip('/')}", {"serviceKey": DATA_KEY, **params}, cache=False)
        try:
            return r.json()
        except ValueError:
            if r.content.lstrip().startswith(b"<"):
                items, total = _xml_items(r.content)
                return {"totalCount": total, "items": items[:300]}
            return {"raw": r.text[:5000]}
    return await _run(go())


@tool()
async def call_vworld_api(
    path: Annotated[str, Field(description="브이월드 경로. 예: /ned/data/getLandUseAttr, /req/data, /req/search")],
    params: Annotated[dict, Field(description="쿼리 파라미터 (key/domain 제외)")],
) -> str:
    """고급: 브이월드 API 직접 호출 (국가중점데이터, 2D데이터, 검색, 지오코더)."""
    return await _run(vw_json(path if path.startswith("/") else "/" + path, params))


# ---------------------------------------------------------------------------
# 시장·입지 보고서 (market.py + report_pptx.py)
# ---------------------------------------------------------------------------
_UPD = {"last": 0.0, "note": None}


def _hot_update(force: bool = False) -> str | None:
    """GitHub 자동 업데이트(런처로 실행될 때만). 보고서 모듈(market·report_pptx)은 바로 교체,
    server_core(도구 목록) 변경은 Claude 재시작 후 적용."""
    try:
        import updater  # 확장 폴더의 고정 파일
    except ImportError:
        return None
    if not force and time.time() - _UPD["last"] < 180:
        return None
    _UPD["last"] = time.time()
    if updater._env("REALESTATE_AUTO_UPDATE", "true").lower() in ("false", "0", "no", "off") and not force:
        return None
    new_dir, changed = updater.update(force=force)
    if not changed:
        return None
    old_dir = os.environ.get("REALESTATE_CODE_DIR", "")
    if old_dir in sys.path:
        sys.path.remove(old_dir)
    sys.path.insert(0, new_dir)
    os.environ["REALESTATE_CODE_DIR"] = new_dir
    for m in ("market", "report_pptx"):
        sys.modules.pop(m, None)
    try:
        core_changed = Path(new_dir, "server_core.py").read_bytes() != Path(__file__).read_bytes()
    except OSError:
        core_changed = False
    sha = (updater.current() or {}).get("sha", "")[:7]
    note = f"GitHub 최신 코드({sha})로 보고서 모듈을 업데이트했습니다."
    if core_changed:
        note += " 도구 목록·조회 기능 변경분은 Claude를 다시 시작하면 적용됩니다."
    _UPD["note"] = note
    return note


def _market():
    try:
        _hot_update()
    except Exception as e:  # 업데이트 실패는 무시하고 현재 코드로
        print(f"[update] {e}", file=sys.stderr)
    import market  # 같은 폴더(또는 업데이트된 폴더)
    market.init(sys.modules[__name__])
    return market


_jobs: dict[str, dict] = {}
REPORT_WAIT = 50  # 초: 이 안에 끝나면 바로 결과, 아니면 job_id로 이어서 확인


def _job_result(jid: str, job: dict) -> dict:
    t: asyncio.Task = job["task"]
    if not t.done():
        return {"상태": "작성 중", "job_id": jid, "경과초": int(time.time() - job["start"]),
                "안내": "20~30초 뒤 check_market_report(job_id)로 다시 확인하세요."}
    exc = t.exception()
    if exc:
        return {"상태": "실패", "job_id": jid, "오류": f"{type(exc).__name__}: {exc}"}
    res = t.result()
    out = {"상태": "완료", "job_id": jid, "소요초": int(time.time() - job["start"])}
    if _UPD.get("note"):
        out["업데이트"] = _UPD["note"]
    if job.get("compact"):
        keys = ("자산명", "PPT", "PDF", "슬라이드", "경고", "진단", "시세추정", "최종반경km", "비교면적")
        out.update({k: res.get(k) for k in keys})
        cx = res.get("비교단지") or []
        out["요약"] = {
            "광역시설": [f"{r['구분']}:{r['시설']}({r['거리km']}km)" for r in res.get("광역시설", [])],
            "생활시설": [f"{r['구분']}:{r['시설']}({r['거리km']}km)" for r in res.get("생활시설", [])],
            "비교단지": [f"{r['no']}.{r['단지명']}|{r.get('준공')}|{r.get('세대수')}|매{r.get('매매공급평단가')}({r.get('매매건수')})"
                       f"|전{r.get('전세공급평단가')}({r.get('전세건수')})|{r.get('거리km')}km" for r in cx],
            "매매분기건수": [q["건수"] for q in (res.get("거래추이") or {}).get("매매") or []],
            "미분양": {k: (res.get("미분양") or {}).get(k) for k in ("시군구", "준공후", "참고")},
            "분양정보": {k: (res.get("분양정보") or {}).get(k) for k in ("주택명", "전용㎡", "공급비율", "분양가최고(만원)")},
        }
        return out
    out.update(res)
    return out


@tool()
async def create_market_report(
    location: Loc,
    radius_km: Annotated[float, Field(description="비교단지·생활시설 반경(km)", ge=0.5, le=5)] = 1.5,
    area_min: Annotated[Optional[float], Field(description="비교 전용면적 하한㎡. 비우면 본 건 주력 평형으로 자동(84형→80~86, 59형→57~64)")] = None,
    area_max: Annotated[Optional[float], Field(description="비교 전용면적 상한㎡. 비우면 자동")] = None,
    target_unsold_units: Annotated[Optional[int], Field(description="본 건 미분양 세대수. 넣으면 '본 건 제외' 미분양 차트·비중 문장 생성")] = None,
    target_sale_price: Annotated[Optional[float], Field(description="본 건 예정 매각가(만원, 비교 면적 기준 1세대). 없으면 청약홈 분양가로 비교")] = None,
    target_jeonse: Annotated[Optional[float], Field(description="본 건 예정 전세가(만원)")] = None,
    target_area: Annotated[Optional[float], Field(description="예정 매각가·전세가가 가리키는 전용면적㎡ (예: 84.89). 비교 면적 구간도 이에 맞춤")] = None,
    comparable_names: Annotated[Optional[str], Field(description="비교단지를 직접 고를 때 단지명 쉼표 구분 (비우면 반경 안 전체)")] = None,
    bullets: Annotated[Optional[dict], Field(description="슬라이드별 문장 교체: {'overview':[..],'wide':[..],'near':[..],'unsold':[..],'trend':[..],'prices':[..]}. **굵게** 지원")] = None,
    sections: Annotated[Optional[str], Field(description="만들 슬라이드(쉼표): overview,wide,near,unsold,trend,prices (기본 전체)")] = None,
    asset_name: Annotated[Optional[str], Field(description="자산명 표기(기본: 건축물대장 건물명)")] = None,
    start_page: Annotated[int, Field(description="첫 슬라이드 쪽번호", ge=1)] = 1,
    make_pdf: Annotated[bool, Field(description="PDF도 만들기 (Windows PowerPoint 또는 LibreOffice 필요)")] = True,
    brand: Annotated[str, Field(description="헤더 오른쪽 문구")] = "JB Asset Management",
    output_dir: Annotated[Optional[str], Field(description="저장 폴더 (기본: 이미지 폴더 옆 '시장입지보고서')")] = None,
    photos: Annotated[Optional[str], Field(description="현장사진 파일 또는 폴더 경로(쉼표 구분, 최대 6장). 파일명이 캡션이 됨. 1~2장은 자산개요에, 3장 이상은 '현장사진' 슬라이드 추가")] = None,
    compact: Annotated[bool, Field(description="결과를 짧게(파일 경로·경고·핵심 수치만). 여러 건 연속 작성·점검용")] = False,
) -> str:
    """IM의 시장·입지 파트(자산개요·광역입지·협역입지·준공후 미분양·매매/전세 거래추이·주변단지 시세)를 PPT와 PDF로 만듭니다.
    데이터: 건축물대장·청약홈·브이월드(지도·주변시설)·K-apt·국토부 실거래가·KOSIS 미분양. 사업구조·자금조달은 다루지 않습니다.
    결과의 '문장초안(bullets)'을 다듬어 bullets로 다시 호출하면 문장만 바꾼 파일을 다시 만듭니다(데이터는 캐시 사용).
    처음 실행은 1~2분 걸릴 수 있어 50초 안에 안 끝나면 job_id를 돌려줍니다 → check_market_report로 확인."""
    kwargs = dict(location=location, radius_km=radius_km, area_min=area_min, area_max=area_max,
                  target_unsold_units=target_unsold_units, target_sale_price=target_sale_price,
                  target_jeonse=target_jeonse, target_area=target_area,
                  comparable_names=[x.strip() for x in comparable_names.split(",") if x.strip()] if comparable_names else None,
                  bullets=bullets or None,
                  sections=[x.strip() for x in sections.split(",") if x.strip()] if sections else None,
                  asset_name=asset_name, start_page=start_page, out_dir=output_dir, make_pdf=make_pdf, brand=brand,
                  photos=[x.strip() for x in photos.split(",") if x.strip()] if photos else None)

    async def go():
        try:
            m = _market()
        except ImportError as e:
            raise ApiError(f"보고서 모듈을 불러오지 못했습니다(python-pptx 설치 확인): {e}")
        jid = f"{dt.datetime.now():%H%M%S}{len(_jobs)}"
        task = asyncio.create_task(m.market_report(**kwargs))
        _jobs[jid] = {"task": task, "start": time.time(), "location": location, "compact": compact}
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=REPORT_WAIT)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        return _job_result(jid, _jobs[jid])
    return await _run(go())


@tool()
async def check_market_report(job_id: Annotated[str, Field(description="create_market_report가 돌려준 job_id")]) -> str:
    """작성 중인 시장·입지 보고서의 진행 상태/결과 확인."""
    async def go():
        job = _jobs.get(job_id)
        if not job:
            return {"오류": "해당 job_id가 없습니다(서버가 다시 시작됐을 수 있음). create_market_report를 다시 실행하세요."}
        t: asyncio.Task = job["task"]
        if not t.done():
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=40)
            except (asyncio.TimeoutError, Exception):
                pass
        return _job_result(job_id, job)
    return await _run(go())


@tool()
async def get_unsold_housing(
    location: Loc,
    months: Annotated[int, Field(description="최근 몇 개월", ge=3, le=60)] = 12,
    target_units: Annotated[Optional[int], Field(description="본 건 미분양 세대수(본 건 제외 추정치 계산)")] = None,
) -> str:
    """시군구 미분양(전체·준공 후) 월별 추이 — 국토교통부 미분양주택현황보고(KOSIS, 국가데이터처 MCP 경유, 키 불필요)."""
    async def go():
        m = _market()
        loc = await resolve(location)
        addr = loc.get("address") or location
        if loc.get("level") != "parcel" and not loc.get("address"):
            addr = norm_sido(location)
        return await m.unsold(addr, loc.get("lawd_cd"), months, target_units)
    return await _run(go())


@tool()
async def get_location_facilities(
    location: Loc,
    radius_km: Annotated[float, Field(description="생활시설 반경(km)", ge=0.3, le=5)] = 1.5,
) -> str:
    """입지 분석용 주변시설 직선거리: 광역(산업단지·IC·철도역·KTX·관공서·대도시 거점) + 협역(초중고·마트·행정복지센터·병원·공원·역)."""
    async def go():
        m = _market()
        loc = await ensure_point(await resolve(location))
        parts = m.split_address(loc.get("address") or "")
        wide, near = await asyncio.gather(m.wide_facilities(loc["lat"], loc["lon"], parts["sgg"]),
                                          m.near_facilities(loc["lat"], loc["lon"], radius_km))
        strip = lambda rows: [{k: r[k] for k in ("구분", "시설", "거리km")} for r in rows]
        return {"조회대상": loc_summary(loc), "광역": strip(wide), "협역": strip(near),
                "기준": "브이월드 좌표 기준 직선거리 (IC는 요금소 위치)"}
    return await _run(go())


@tool()
async def agent_update(force: Annotated[bool, Field(description="지금 바로 GitHub 최신 코드 확인·적용")] = True) -> str:
    """에이전트 자동 업데이트 상태 확인·즉시 업데이트 (GitHub 비공개 저장소 → 확장 프로그램)."""
    async def go():
        try:
            import updater
        except ImportError:
            return {"상태": "자동 업데이트 없는 설치본입니다(1.x). 2.0 이상 확장을 설치하세요."}
        note = await asyncio.to_thread(_hot_update, force) if force else None
        st = dict(updater.status())
        if st.get("ok"):  # 성공했으면 이전 실패 기록(github_message·http)은 숨김
            st.pop("github_message", None)
            st.pop("http", None)
        cur = updater.current() or {}
        return {"실행중_코드": os.environ.get("REALESTATE_CODE_DIR", str(BASE_DIR)), "최신_받은_커밋": cur.get("sha", "")[:12],
                "받은_시각": cur.get("at"), "확인결과": st, "이번_업데이트": note or "변경 없음",
                "저장소": updater.REPO, "토큰": "설정됨" if updater.TOKEN else "없음(확장 설정에서 GitHub 읽기 토큰 입력)",
                "토큰_점검": updater.token_info() if hasattr(updater, "token_info") else None,
                "확장_시작시각": getattr(updater, "STARTED", None)}
    return await _run(go())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _selftest(addr: str) -> None:
    print(f"DATA_GO_KR_API_KEY: {'설정됨' if DATA_KEY else '없음'} / VWORLD_API_KEY: {'설정됨' if VW_KEY else '없음'}")
    loc = await resolve(addr)
    print("위치:", json.dumps(loc_summary(loc), ensure_ascii=False))
    tx = await real_transactions(addr, "아파트매매", months=3, limit=3)
    print("실거래(3개월):", tx["건수"], "건", json.dumps(tx["요약"], ensure_ascii=False)[:300])
    if loc["level"] == "parcel":
        br = await building_register(addr, "표제부", limit=1)
        print("건축물대장:", json.dumps(br["항목"][:1], ensure_ascii=False)[:300])
        if VW_KEY:
            li = await land_info(addr, 3)
            print("공시지가:", json.dumps(li.get("개별공시지가"), ensure_ascii=False))
    print("OK")


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--init"]:
        n = len(asyncio.run(kapt_list(force=True)))
        print(f"전국 공동주택 단지목록 {n:,}건 저장: {CACHE_DIR / 'kapt_list.json'}")
        return
    if args[:1] == ["--selftest"]:
        asyncio.run(_selftest(" ".join(args[1:]) or "서울특별시 동대문구 휘경동 172"))
        return
    mcp.run()


if __name__ == "__main__":
    main()
