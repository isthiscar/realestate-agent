"""
시장·입지 보고서용 데이터 수집
================================
server.py(realestate_mcp.py)의 공용 함수를 그대로 씁니다. init(서버모듈)로 연결합니다.

- 미분양: KOSIS MCP(국가데이터처 시범서비스, 인증키 불필요) → 국토교통부 미분양주택현황보고
- 주변시설: 브이월드 장소검색(bbox) → 산업단지·IC(요금소)·철도역·KTX·관공서 / 학교·마트·행정복지센터·병원·공원
- 비교단지: 반경 내 K-apt 단지 + 실거래 단지(aptSeq) → 좌표(브이월드), 세대수·준공·시공사, 공급면적 비율
- 거래추이: 비교단지 전용면적 구간 매매·전세 분기별 건수·공급 3.3㎡당 평균가
"""
from __future__ import annotations

import asyncio
import io
import datetime as dt
import json
import math
import re
import statistics
import time
from pathlib import Path
from typing import Any

S: Any = None  # 서버 모듈


def init(mod: Any) -> None:
    global S
    S = mod


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------
PY = 3.3058  # ㎡ → 평


def hav(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 좌표의 직선거리(km)."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def offset(lat: float, lon: float, km: float, bearing_deg: float) -> tuple[float, float]:
    b = math.radians(bearing_deg)
    dlat = km * math.cos(b) / 111.0
    dlon = km * math.sin(b) / (111.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


class DiskCache:
    """작은 JSON 디스크 캐시 (좌표·단지기본정보처럼 거의 안 바뀌는 값)."""

    def __init__(self, name: str, ttl_days: float = 90):
        self.name, self.ttl = name, ttl_days * 86400
        self._d: dict | None = None

    @property
    def path(self) -> Path:
        return S.CACHE_DIR / f"{self.name}.json"

    def _load(self) -> dict:
        if self._d is None:
            try:
                self._d = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._d = {}
        return self._d

    def get(self, key: str) -> Any:
        v = self._load().get(key)
        if v and time.time() - v[0] < self.ttl:
            return v[1]
        return None

    def set(self, key: str, val: Any) -> None:
        self._load()[key] = [time.time(), val]

    def save(self) -> None:
        if self._d is None:
            return
        try:
            S.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._d, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


GEO = DiskCache("geo_cache", 365)
BASIS = DiskCache("kapt_basis_cache", 60)
KOSIS_ITM = DiskCache("kosis_itm_cache", 30)


def ym_add(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[4:6])
    t = y * 12 + (m - 1) + k
    return f"{t // 12}{t % 12 + 1:02d}"


def yq(date: str) -> str:
    """'2026-09-30' → '26.3Q'"""
    y, m = int(date[:4]), int(date[5:7])
    return f"{y % 100:02d}.{(m - 1) // 3 + 1}Q"


def full_year(yy: str | int) -> int:
    """'98 → 1998, '16 → 2016"""
    y = int(yy)
    if y >= 100:
        return y
    now = dt.date.today().year % 100
    return 2000 + y if y <= now + 1 else 1900 + y


def ym_label(ym: str) -> str:
    return f"'{ym[2:4]}.{ym[4:6]}"


# ---------------------------------------------------------------------------
# 지역명
# ---------------------------------------------------------------------------
def split_address(addr: str) -> dict:
    """'경상북도 칠곡군 석적읍 중리 647' → 시도/시군구/읍면동 텍스트."""
    toks = (addr or "").split()
    out = {"sido": toks[0] if toks else "", "sgg": "", "umd": ""}
    i = 1
    if len(toks) > 1 and re.search(r"(시|군|구)$", toks[1]):
        out["sgg"] = toks[1]
        i = 2
        # K-apt 주소의 '안양동안구'·'창원마산회원구' → '안양시 동안구' (도 지역만)
        if toks[1].endswith("구") and not re.search(r"(특별시|광역시|특별자치시)$", toks[0]):
            for city in _GU_CITIES:
                if toks[1].startswith(city) and len(toks[1]) > len(city) + 1 and not toks[1].startswith(city + "시"):
                    out["sgg"] = f"{city}시 {toks[1][len(city):]}"
                    break
        if len(toks) > 2 and toks[1].endswith("시") and toks[2].endswith("구"):
            out["sgg"] = f"{toks[1]} {toks[2]}"
            i = 3
    rest = [t for t in toks[i:] if not re.search(r"\d", t)]
    out["umd"] = " ".join(rest[:2]) if rest and re.search(r"(읍|면)$", rest[0]) else (rest[0] if rest else "")
    return out


def sido_short(name: str) -> str:
    n = (name or "").replace(" ", "")
    table = [("전남광주", "전남광주"), ("서울", "서울"), ("부산", "부산"), ("대구", "대구"), ("인천", "인천"),
             ("광주광역", "광주"), ("대전", "대전"), ("울산", "울산"), ("세종", "세종"), ("경기", "경기"),
             ("강원", "강원"), ("충청북", "충북"), ("충북", "충북"), ("충청남", "충남"), ("충남", "충남"),
             ("전라북", "전북"), ("전북", "전북"), ("전라남", "전남"), ("전남", "전남"), ("경상북", "경북"),
             ("경북", "경북"), ("경상남", "경남"), ("경남", "경남"), ("제주", "제주"), ("광주", "광주")]
    for k, v in table:
        if n.startswith(k):
            return v
    return n[:2]


# ---------------------------------------------------------------------------
# KOSIS (국가데이터처 MCP 시범서비스)
# ---------------------------------------------------------------------------
KOSIS_MCP = "https://kosismcp2026.vercel.app/api/mcp"
UNSOLD_TOTAL = ("116", "DT_MLTM_2082")      # 시·군·구별 미분양현황
UNSOLD_DONE = ("116", "DT_MLTM_5328")       # 공사완료후 미분양현황
KOSIS_SRC = "국토교통부 「미분양주택현황보고」(KOSIS)"
_rpc_id = 0


async def kosis_call(name: str, args: dict) -> str:
    global _rpc_id
    ck = "kosis:" + name + json.dumps(args, sort_keys=True, ensure_ascii=False)
    hit = S._cache.get(ck)
    if hit and time.time() - hit[0] < S.CACHE_TTL:
        return hit[1]
    _rpc_id += 1
    body = {"jsonrpc": "2.0", "id": _rpc_id, "method": "tools/call", "params": {"name": name, "arguments": args}}
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = await S._http().post(KOSIS_MCP, json=body, timeout=60.0,
                                     headers={"Accept": "application/json, text/event-stream"})
            if r.status_code >= 500:
                raise S.ApiError(f"KOSIS HTTP {r.status_code}")
            payload = None
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
            d = json.loads(payload or r.text)
            if "error" in d:
                raise S.ApiError(f"KOSIS 오류: {d['error'].get('message')}")
            res = d.get("result", {})
            text = "\n".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
            if res.get("isError"):
                raise S.ApiError(f"KOSIS 오류: {text[:200]}")
            S._cache[ck] = (time.time(), text)
            return text
        except S.ApiError:
            raise
        except Exception as e:  # 네트워크·파싱
            last = e
            await asyncio.sleep(1.0 * (attempt + 1))
    raise S.ApiError(f"KOSIS 호출 실패: {last}")


async def kosis_dims(org: str, tbl: str) -> list[dict]:
    """통계표 분류 차원: [{key:'objL1', label:'구분', items:[(code,name)]}]"""
    ck = f"{org}/{tbl}"
    cached = KOSIS_ITM.get(ck)
    if cached:
        return cached
    text = await kosis_call("kosis_table_info", {"orgId": org, "tblId": tbl, "type": "ITM", "pageSize": 1000})
    dims, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^###\s+(.+?)\s+\(OBJ_ID:\s*(\S+)\s*→\s*(\w+)\)", line)
        if m:
            cur = {"key": m.group(3), "label": m.group(1), "obj": m.group(2), "items": []}
            dims.append(cur)
            continue
        m = re.match(r"^- `([^`]+)`\s+(.+?)\s*$", line)
        if m and cur is not None:
            cur["items"].append([m.group(1), m.group(2)])
    if dims:
        KOSIS_ITM.set(ck, dims)
        KOSIS_ITM.save()
    return dims


def parse_series(text: str) -> dict[str, float]:
    m = re.search(r"^- \*\*값\*\*:\s*(.+?)\s*$", text, re.M)
    const = S.num(m.group(1)) if m else None
    out: dict[str, float] = {}
    for line in text.splitlines():
        mm = re.match(r"^\|\s*(\d{4,8})\s*\|(.*)$", line)
        if not mm:
            continue
        cells = [c.strip() for c in mm.group(2).split("|")]
        cells = [c for c in cells if c != ""]
        v = S.num(cells[-1]) if cells else const
        if v is not None:
            out[mm.group(1)] = v
    return out


def _find(items: list, *names: str) -> str | None:
    for n in names:
        for code, nm in items:
            if nm == n:
                return code
    return None


async def kosis_series(tbl: tuple[str, str], sido: str, sgg: str | None, start: str, end: str) -> dict[str, float]:
    org, tid = tbl
    dims = await kosis_dims(org, tid)
    if not dims:
        return {}
    args: dict[str, Any] = {"orgId": org, "tblId": tid, "itmId": "ALL", "prdSe": "M", "startPrdDe": start, "endPrdDe": end}
    for d in dims:
        key = d["key"]
        if not key.startswith("objL"):
            continue
        lab = d["label"]
        if lab in ("구분", "시도", "지역"):
            code = _find(d["items"], sido)
        elif lab in ("시군구", "시·군·구"):
            code = _find(d["items"], sgg) if sgg else _find(d["items"], "계", "합계")
            if not code and sgg:  # '안양동안구'처럼 붙여 쓴 이름 → '안양시'
                for c_, nm in d["items"]:
                    core = re.sub(r"(시|군)$", "", nm)
                    if len(core) >= 2 and nm.endswith(("시", "군")) and sgg.replace(" ", "").startswith(core):
                        code = c_
                        break
        else:  # 부문·규모 등 → 계
            code = _find(d["items"], "계", "합계", "소계")
        if not code:
            return {}
        args[key] = code
    return parse_series(await kosis_call("kosis_get_data", args))


_GU_CITIES = ("수원", "성남", "안양", "안산", "고양", "용인", "화성", "청주", "천안", "전주", "포항", "창원", "부천")


def kosis_sgg_name(sgg: str, sido: str) -> str:
    if sido == "세종":
        return "세종시"
    if not sgg:
        return ""
    first = sgg.split()[0]
    # K-apt·일부 자료는 '창원마산회원구'처럼 시+일반구를 붙여 씀 → 일반구가 있는 시 목록으로 앞부분만
    if " " not in sgg and first.endswith("구") and sido not in ("서울", "부산", "대구", "인천", "광주", "대전", "울산"):
        for city in _GU_CITIES:
            if first.startswith(city) and len(first) > len(city) + 1:
                return city + "시"
    return first


async def unsold(addr: str, lawd_cd: str | None, months: int = 12, target_units: int | None = None) -> dict:
    """시군구 준공 후 미분양·전체 미분양 월별 추이 (최근 months개월)."""
    parts = split_address(addr)
    sido = sido_short(parts["sido"])
    sgg = kosis_sgg_name(parts["sgg"], sido)
    end = dt.date.today().strftime("%Y%m")
    start = ym_add(end, -(months + 8))
    keys = [(sido, sgg)]
    note = None
    if sido == "전남광주":  # '26.7 통합 이전 자료는 옛 시도로 수록
        legacy = S.legacy_code(lawd_cd) if lawd_cd else None
        old = "광주" if (legacy or "").startswith("29") else "전남"
        keys.insert(0, (old, sgg))
        note = f"'26.6 이전은 {old}, '26.7 이후는 전남광주 기준(같은 통계표)"

    async def merged(tbl, s_g: str | None) -> dict[str, float]:
        out: dict[str, float] = {}
        for sd, sg in keys:
            try:
                out.update(await kosis_series(tbl, sd, s_g if s_g is not None else sg, start, end))
            except S.ApiError:
                continue
        return out

    done, total, sido_done = await asyncio.gather(
        merged(UNSOLD_DONE, None), merged(UNSOLD_TOTAL, None),
        merged(UNSOLD_DONE, ""), return_exceptions=True)
    done = done if isinstance(done, dict) else {}
    total = total if isinstance(total, dict) else {}
    sido_done = sido_done if isinstance(sido_done, dict) else {}
    periods = sorted(done)[-months:]
    res: dict[str, Any] = {"시도": sido, "시군구": sgg, "출처": KOSIS_SRC, "기간": [ym_label(p) for p in periods],
                           "준공후": [done.get(p) for p in periods], "전체": [total.get(p) for p in periods],
                           "분양전": [(total[p] - done[p]) if p in total and p in done else None for p in periods],
                           "시도_준공후": [sido_done.get(p) for p in periods], "periods": periods}
    if note:
        res["참고"] = note
    if target_units and periods:
        res["본건제외"] = [max(0, (v or 0) - target_units) for v in res["준공후"]]
        last = res["준공후"][-1]
        res["본건비중%"] = round(target_units / last * 100) if last else None
    if not periods:
        res["오류"] = f"KOSIS에서 {sido} {sgg} 미분양 자료를 찾지 못했습니다."
    return res


# ---------------------------------------------------------------------------
# 브이월드 장소검색 / 역지오코딩
# ---------------------------------------------------------------------------
async def place_bbox(query: str, lat: float, lon: float, r_km: float, size: int = 200) -> list[dict]:
    dlat = r_km / 111.0
    dlon = r_km / (111.0 * math.cos(math.radians(lat)))
    params = {"service": "search", "request": "search", "version": "2.0", "crs": "EPSG:4326", "type": "place",
              "query": query, "bbox": f"{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}",
              "size": size, "page": 1, "errorformat": "json"}
    try:
        d = await S.vw_json("/req/search", params)
    except S.ApiError:
        return []
    resp = d.get("response", {})
    if resp.get("status") != "OK":
        return []
    out = []
    for it in resp.get("result", {}).get("items", []) or []:
        p = it.get("point", {})
        x, y = S.num(p.get("x")), S.num(p.get("y"))
        if x is None or y is None:
            continue
        dist = hav(lat, lon, y, x)
        if dist <= r_km:
            out.append({"title": (it.get("title") or "").strip(), "category": it.get("category") or "",
                        "road": (it.get("address") or {}).get("road") or "",
                        "parcel": (it.get("address") or {}).get("parcel") or "",
                        "lat": y, "lon": x, "dist": dist})
    out.sort(key=lambda r: r["dist"])
    return out


async def reverse_geocode(lat: float, lon: float) -> dict | None:
    key = f"rg:{lat:.5f},{lon:.5f}"
    hit = GEO.get(key)
    if hit:
        return hit
    try:
        d = await S.vw_json("/req/address", {"service": "address", "request": "getAddress", "version": "2.0",
                                              "crs": "epsg:4326", "point": f"{lon:.6f},{lat:.6f}", "type": "PARCEL",
                                              "zipcode": "false", "simple": "false", "errorformat": "json"})
    except S.ApiError:
        return None
    resp = d.get("response", {})
    if resp.get("status") != "OK" or not resp.get("result"):
        return None
    r0 = resp["result"][0]
    st = r0.get("structure", {})
    text = r0.get("text", "")
    parts = split_address(text)
    out = {"text": text, "bjd": st.get("level4LC") or "", "sido": st.get("level1") or parts["sido"],
           "sgg": parts["sgg"], "umd": parts["umd"]}
    GEO.set(key, out)
    return out


_PNU: dict[tuple, str] = {}


async def geocode_parcel(addr: str) -> tuple[float, float] | None:
    key = "pc:" + " ".join(addr.split())
    hit = GEO.get(key)
    if hit:
        if len(hit) > 2 and hit[2]:
            _PNU[(round(hit[0], 6), round(hit[1], 6))] = hit[2]
        return hit[0], hit[1]
    try:
        items = await S.vw_search(addr, "address", "parcel", size=1)
    except S.ApiError:
        items = []
    if not items:
        return None
    p = items[0].get("point", {})
    x, y = S.num(p.get("x")), S.num(p.get("y"))
    if x is None:
        return None
    pnu = items[0].get("id") if re.fullmatch(r"\d{19}", str(items[0].get("id") or "")) else None
    GEO.set(key, [y, x, pnu])
    if pnu:
        _PNU[(round(y, 6), round(x, 6))] = pnu
    return y, x


async def geocode_road(addr: str) -> tuple[float, float] | None:
    key = "rd:" + " ".join(addr.split())
    hit = GEO.get(key)
    if hit:
        return tuple(hit)  # type: ignore[return-value]
    try:
        items = await S.vw_search(addr, "address", "road", size=1)
    except S.ApiError:
        items = []
    if not items:
        return None
    p = items[0].get("point", {})
    x, y = S.num(p.get("x")), S.num(p.get("y"))
    if x is None:
        return None
    GEO.set(key, [y, x])
    return y, x


# ---------------------------------------------------------------------------
# 주변시설
# ---------------------------------------------------------------------------
# KTX·SRT·KTX-이음 정차역: 후보 선정용 대략 좌표(정확한 좌표는 브이월드로 다시 찾음)
KTX_STATIONS = {
    "서울": (37.5547, 126.9707), "용산": (37.5299, 126.9648), "청량리": (37.5803, 127.0469), "상봉": (37.5965, 127.0855),
    "수서": (37.4873, 127.1015), "광명": (37.4163, 126.8848), "행신": (37.6121, 126.8344), "영등포": (37.5157, 126.9072),
    "수원": (37.2660, 127.0000), "동탄": (37.2003, 127.0965), "평택지제": (37.0186, 127.0702), "천안아산": (36.7945, 127.1046),
    "오송": (36.6201, 127.3273), "공주": (36.3920, 127.0930), "대전": (36.3323, 127.4346), "서대전": (36.3226, 127.4038),
    "계룡": (36.2750, 127.2484), "논산": (36.2017, 127.0925), "익산": (35.9420, 126.9464), "김제": (35.8023, 126.8883),
    "정읍": (35.5760, 126.8460), "장성": (35.3022, 126.7869), "광주송정": (35.1376, 126.7910), "나주": (35.0154, 126.7164),
    "목포": (34.7914, 126.3868), "전주": (35.8496, 127.1617), "남원": (35.4100, 127.3870), "곡성": (35.2800, 127.2950),
    "구례구": (35.1990, 127.4600), "순천": (34.9455, 127.5033), "여천": (34.7700, 127.6640), "여수EXPO": (34.7530, 127.7470),
    "김천구미": (36.1137, 128.1803), "서대구": (35.8840, 128.5530), "동대구": (35.8794, 128.6285), "경산": (35.8195, 128.7390),
    "신경주": (35.7982, 129.1395), "울산": (35.5513, 129.1384), "태화강": (35.5390, 129.3530), "부산": (35.1151, 129.0422),
    "부전": (35.1640, 129.0590), "구포": (35.2050, 128.9990), "밀양": (35.4740, 128.7710), "진영": (35.3030, 128.7300),
    "창원중앙": (35.2380, 128.6950), "창원": (35.2557, 128.6140), "마산": (35.2464, 128.5800), "진주": (35.1500, 128.1180),
    "포항": (36.0718, 129.3424), "영천": (35.9640, 128.9350), "안동": (36.5650, 128.6900), "영주": (36.8110, 128.6250),
    "의성": (36.3530, 128.6970), "단양": (36.9850, 128.3650), "제천": (37.1300, 128.1950), "원주": (37.3190, 127.9200),
    "서원주": (37.3790, 127.8460), "만종": (37.3800, 127.9070), "횡성": (37.4900, 127.9880), "둔내": (37.5110, 128.2160),
    "평창": (37.5730, 128.3860), "진부": (37.6380, 128.5640), "강릉": (37.7642, 128.8993), "정동진": (37.6910, 129.0340),
    "묵호": (37.5470, 129.1160), "동해": (37.4990, 129.1240), "양평": (37.4925, 127.4920), "충주": (36.9840, 127.9070),
}
AIRPORTS = {"제주국제공항": (33.5104, 126.4914), "김포국제공항": (37.5587, 126.7945), "김해국제공항": (35.1731, 128.9464),
            "청주국제공항": (36.7166, 127.4991), "대구국제공항": (35.8941, 128.6589), "광주공항": (35.1264, 126.8089),
            "무안국제공항": (34.9914, 126.3828), "여수공항": (34.8423, 127.6161), "울산공항": (35.5935, 129.3518),
            "포항경주공항": (35.9879, 129.4204), "사천공항": (35.0886, 128.0703), "군산공항": (35.9038, 126.6158),
            "원주공항": (37.4381, 127.9604), "양양국제공항": (38.0613, 128.6692), "인천국제공항": (37.4602, 126.4407)}
METRO_HUBS = {  # (도시 중심 좌표): [(역 이름, 대략 좌표, 표시)]
    (35.1798, 129.0750): [("서면", (35.1578, 129.0597), "도심"), ("센텀시티", (35.1690, 129.1316), "부도심")],
    (35.8714, 128.6014): [("반월당", (35.8656, 128.5934), "도심"), ("범어", (35.8592, 128.6253), "부도심")],
    (36.3504, 127.3845): [("정부청사", (36.3615, 127.3790), "도심(둔산)"), ("중앙로", (36.3290, 127.4280), "원도심")],
    (35.1601, 126.8514): [("상무", (35.1525, 126.8530), "도심(상무)"), ("문화전당", (35.1470, 126.9190), "원도심")],
    (35.5384, 129.3114): [("태화강", (35.5390, 129.3530), "도심")],
}
HUB_STATIONS = {"서울": "서울", "부산": "부산", "동대구": "동대구", "대전": "대전", "광주송정": "광주송정", "울산": "울산"}


def clean_shop(t: str) -> str:
    t = re.sub(r"^(롯데쇼핑|\(주\)|㈜|주식회사|농협중앙회|유한회사)\s*", "", t.strip())
    t = re.sub(r"점주$", "점", t)
    return t


BRANDS = ("이마트", "홈플러스", "롯데마트", "하나로마트", "농협하나로", "코스트코", "트레이더스", "롯데슈퍼", "GS더프레시",
          "노브랜드", "킴스클럽", "메가마트", "탑마트", "하나로클럽", "이마트에브리데이")
_STATION_RE = re.compile(r"^[^\s]+역(\s*\([^)]*\))?$")


def _station_key(t: str) -> str:
    """'굽은다리역(강동구민회관앞)' = '굽은다리(강동구민회관앞)역'"""
    return re.sub(r"\(.*?\)|역|\s", "", t)


def _norm_title(t: str) -> str:
    return re.sub(r"[\s()·]", "", t)


# 주변시설 표에 넣지 않을 상업 POI (빨래방·식당·학원 등)
JUNK_CAT = ("세탁", "빨래", "음식점", "주점", "카페", "제과", "부동산", "중개", "학원", "교습", "충전소", "주차장",
            "편의점", "미용", "숙박", "노래", "PC방", "피시방", "자동차", "정비", "통신판매", "도매", "인쇄", "광고",
            "택배", "복지시설운영업", "급식", "프랜차이즈", "가맹점")
JUNK_TITLE = ("빨래방", "세탁", "코인", "학원", "교습소", "주차장", "충전소", "출입구", "정류장", "편의점", "문화비",
              "부동산", "공인중개", "식당", "카페", "치킨", "피자", "약국", "동물", "펫", "셀프", "렌트", "점)")


def _junk(it: dict, allow_cat: tuple = ()) -> bool:
    cat, title = it["category"], it["title"]
    if any(w in cat for w in JUNK_CAT) and not any(w in cat for w in allow_cat):
        return True
    return any(w in title for w in JUNK_TITLE)


def _pick(items: list[dict], n: int, include: tuple = (), exclude: tuple = (), title_has: tuple = (),
          title_not: tuple = (), dedupe=_norm_title) -> list[dict]:
    out, seen = [], set()
    for it in items:
        cat, title = it["category"], it["title"]
        if _junk(it, include):
            continue
        if include and not any(w in cat for w in include):
            continue
        if exclude and any(w in cat for w in exclude):
            continue
        if title_has and not any(w in title for w in title_has):
            continue
        if title_not and any(w in title for w in title_not):
            continue
        k = dedupe(title)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
        if len(out) >= n:
            break
    return out


async def _nearest(query: str, lat: float, lon: float, radii: tuple, n: int = 1, **flt) -> list[dict]:
    for r in radii:
        got = _pick(await place_bbox(query, lat, lon, r), n, **flt)
        if got:
            return got
    return []


def clean_org(t: str) -> str:
    """'의료법인한국의료재단순천한국병원' → '순천한국병원'"""
    t = re.sub(r"^(의료법인|재단법인|사회복지법인|학교법인)\s*", "", t)
    t = re.sub(r"^\S*?재단\s*(?=\S{2,})", "", t) if "병원" in t or "의원" in t else t
    return t.strip()


def _row(kind: str, it: dict, name: str | None = None) -> dict:
    return {"구분": kind, "시설": name or clean_org(it["title"]), "거리km": round(it["dist"], 1), "lat": it["lat"], "lon": it["lon"]}


async def _station_exact(name: str, approx: tuple[float, float], lat: float, lon: float) -> dict | None:
    items = await place_bbox(f"{name}역", approx[0], approx[1], 12, size=50)
    items = [it for it in items if _STATION_RE.match(it["title"])]
    best = _pick(items, 1, include=("철도",), exclude=("출입구", "진출입", "입구"), title_has=(name[:2],))
    if not best:
        return None
    b = best[0]
    b["dist"] = hav(lat, lon, b["lat"], b["lon"])
    return b


async def wide_facilities(lat: float, lon: float, sgg_full: str = "") -> list[dict]:
    """광역 입지: 산업단지·IC·철도역·KTX·관공서·광역 거점."""
    rows: list[dict] = []

    async def industrial():
        items = await place_bbox("산업단지", lat, lon, 15, size=300)
        items += await place_bbox("농공단지", lat, lon, 15, size=100)
        items.sort(key=lambda r: r["dist"])
        items = [it for it in items if re.search(r"산업단지|농공단지|산단|특구|밸리|테크노|클러스터|국가산업|일반산업|도시첨단|산업\d*지구|사이언스파크|디지털단지", it["title"])]
        return [_row("산업", it) for it in _pick(items, 3, include=("산업단지구역",))]

    async def ic():
        for r in (8, 15, 30):
            items = await place_bbox("요금소", lat, lon, r, size=200)
            got = _pick(items, 2, include=("요금소",), title_not=("주차", "주유"),
                        dedupe=lambda t: re.sub(r"(요금소|영업소|톨게이트|\s)", "", t))
            if got:
                out = []
                for it in got:
                    nm = re.sub(r"(요금소|영업소|톨게이트)$", "", re.sub(r"^한국도로공사\s*", "", it["title"])) + "IC"
                    m = re.search(r"(\S+고속도로)", it.get("road", ""))
                    out.append(_row("교통", it, f"{m.group(1)} {nm}" if m else nm))
                return out
        return []

    async def station():
        for r in (3, 8, 20):
            items = [it for it in await place_bbox("역", lat, lon, r) if _STATION_RE.match(it["title"])]
            got = _pick(items, 2, include=("철도",), exclude=("출입구", "진출입", "입구"), dedupe=_station_key)
            if got:
                return [_row("교통", it) for it in got]
        return []

    async def ktx():
        cand = sorted(KTX_STATIONS.items(), key=lambda kv: hav(lat, lon, *kv[1]))[:3]
        found = [b for b in await asyncio.gather(*[_station_exact(n, c, lat, lon) for n, c in cand]) if b]
        if not found:
            return []
        b = min(found, key=lambda r: r["dist"])
        if b["dist"] > 70:  # 제주 등: 의미 없는 원거리 KTX는 뺌
            return []
        return [_row("교통", b, re.sub(r"^(KTX|SRT)\s*", "", re.sub(r"\s*\(.*?\)", "", b["title"])) + " (KTX)")]

    async def airport():
        cand = sorted(AIRPORTS.items(), key=lambda kv: hav(lat, lon, *kv[1]))[:1]
        out = []
        for n, c in cand:
            d0 = hav(lat, lon, *c)
            if d0 > 45:
                continue
            items = await place_bbox(n, c[0], c[1], 6, size=30)
            items = [it for it in items if it["title"].replace(" ", "").endswith("공항") and "공항" in it["category"] + it["title"]
                     and not _junk(it)]
            if items:
                b = min(items, key=lambda r: abs(len(r["title"]) - len(n)))
                b["dist"] = hav(lat, lon, b["lat"], b["lon"])
                out.append(_row("광역", b))
        return out

    async def gov():
        out = []
        bad = ("출입구", "입구", "역(", "역", "주차", "별관", "앞", "점", "어린이집", "직장", "민원", "충전")
        if sgg_full:
            own = sgg_full.split()[0] + "청"
            items = await place_bbox(own, lat, lon, 30, size=40)
            got = [it for it in items if it["title"].replace(" ", "") == own and "철도" not in it["category"] and not _junk(it)] or \
                  [it for it in items if it["title"].startswith(own) and not any(b in it["title"][len(own):] for b in bad)]
            out += [_row("행정", it) for it in got[:1]]
        items = await place_bbox("시청", lat, lon, 25, size=150)
        got = [it for it in items if re.fullmatch(r"\S{1,8}(특별|광역)?시청", it["title"].replace(" ", ""))
               and ("행정" in it["category"] or "업무" in it["category"] or "공공" in it["category"]) and not _junk(it)]
        own_names = {r["시설"].replace(" ", "") for r in out}
        seen = set()
        for it in got:
            t = it["title"].replace(" ", "")
            if t in own_names or t in seen or any(t.endswith(o) or o.endswith(t) for o in own_names):
                continue
            seen.add(it["title"])
            out.append(_row("행정", it, re.sub(r"^(경기도|강원도|강원특별자치도|충청북도|충청남도|전라북도|전북특별자치도|전라남도|경상북도|경상남도|제주특별자치도)", "", it["title"])))
            break
        return out

    async def cbd():
        """수도권 3대 업무지구(CBD·GBD·YBD), 광역시는 도심·부도심까지 직선거리."""
        if 37.0 <= lat <= 38.0 and 126.4 <= lon <= 127.6:
            hubs = [("광화문", (37.5710, 126.9768), "CBD"), ("강남", (37.4979, 127.0276), "GBD"), ("여의도", (37.5216, 126.9242), "YBD")]
        else:
            hubs = []
            for center, items in METRO_HUBS.items():
                if hav(lat, lon, *center) <= 25:
                    hubs = items
                    break
            if not hubs:
                return []
        found = await asyncio.gather(*[_station_exact(n, c, lat, lon) for n, c, _ in hubs])
        out = []
        for (n, _, tag), b in zip(hubs, found):
            if b:
                out.append(_row("업무", b, f"{tag} {re.sub(r'\s*\(.*?\)', '', b['title'])}"))
        return out

    parts = await asyncio.gather(industrial(), ic(), station(), ktx(), gov(), cbd(), airport(), return_exceptions=True)
    for p in parts:
        if isinstance(p, list):
            rows.extend(p)
    # KTX역과 같은 일반역 중복 제거 (예: '대전역 (KTX)'와 '대전역')
    ktx_names = {re.sub(r"\s*\(.*?\)", "", r["시설"]).strip() for r in rows if r["시설"].endswith("(KTX)")}
    rows = [r for r in rows if r["시설"].endswith("(KTX)") or re.sub(r"\s*\(.*?\)", "", r["시설"]).strip() not in ktx_names]
    # 광역 거점(대도시 중심역)
    try:
        hubs = sorted(HUB_STATIONS, key=lambda n: hav(lat, lon, *KTX_STATIONS[n]))
        h = hubs[0]
        if hav(lat, lon, *KTX_STATIONS[h]) < 80 and not any(h in r["시설"] for r in rows):
            b = await _station_exact(h, KTX_STATIONS[h], lat, lon)
            if b:
                rows.append(_row("광역", b, re.sub(r"\s*\(.*?\)", "", b["title"])))
    except Exception:
        pass
    order = {"업무": 0, "산업": 1, "교통": 2, "행정": 3, "광역": 4}
    rows.sort(key=lambda r: (order.get(r["구분"], 9), r["거리km"]))
    return rows


async def near_facilities(lat: float, lon: float, radius: float = 1.5) -> list[dict]:
    """협역 입지: 학교·생활편의·행정·의료·공원·교통."""
    r2 = max(2.0, radius * 1.4)
    specs = [
        ("교육", "초등학교", (r2,), 1, dict(include=("초등학교",), title_has=("초등학교",), title_not=("병설", "유치원"))),
        ("교육", "중학교", (r2, 4), 1, dict(include=("중학교",), title_has=("중학교",))),
        ("교육", "고등학교", (r2, 5), 1, dict(include=("고등학교",), title_has=("고등학교",))),
        ("생활", "행정복지센터", (r2, 4), 1, dict(title_has=("행정복지센터", "주민센터"),
                                                 title_not=("주차", "앞", "입구", "출입구", "분소", "경로당"))),
        ("의료", "병원", (3, 6, 10), 1, dict(include=("종합병원",), title_has=("병원",),
                                             title_not=("동물", "한의", "한방", "치과", "요양", "재활", "주차", "장례"))),
        ("자연", "근린공원", (r2,), 2, dict(include=("공원",), title_has=("공원",), title_not=("주차", "화장실", "묘", "입구"))),
        ("교통", "역", (r2,), 1, dict(include=("철도",), exclude=("출입구", "진출입", "입구"), title_has=("역",),
                                     title_not=("앞", "출입구", "주차", "광장", "번출"))),
    ]

    async def one(kind, q, radii, n, flt):
        got = await _nearest(q, lat, lon, radii, n, **flt)
        numbered = re.compile(r"\d+호|\d+$|\(\d+")
        if kind == "자연":
            got = [g for g in got if not numbered.search(g["title"])]
        if kind == "자연" and len(got) < n:  # 근린공원이 없으면 일반 공원(번호만 붙은 소공원 제외)
            more = [it for it in await place_bbox("공원", lat, lon, radii[-1]) if not numbered.search(it["title"])]
            got += [it for it in _pick(more, n + 2, **flt) if it["title"] not in {g["title"] for g in got}][:n - len(got)]
        return [_row(kind, it) for it in got]

    async def marts():
        """대형마트·SSM 브랜드 우선, 없으면 1km 안 일반 마트."""
        qs = ["이마트", "홈플러스", "롯데마트", "하나로마트", "코스트코", "롯데슈퍼", "GS더프레시", "노브랜드", "탑마트", "마트"]
        res = await asyncio.gather(*[place_bbox(q, lat, lon, 2.0, size=60) for q in qs], return_exceptions=True)
        items = [it for r in res if isinstance(r, list) for it in r]
        items.sort(key=lambda r: r["dist"])
        flt = dict(include=("마트", "슈퍼", "할인점", "백화점", "대형", "쇼핑"),
                   title_not=("편의점", "정육", "주차", "주유", "중고", "건재", "철물", "하이마트", "전자", "가구", "자동차", "타이어"))
        for it in items:
            it["title"] = clean_shop(it["title"])
        brand = [it for it in items if any(it["title"].startswith(b) or b in it["title"][:8] for b in BRANDS)]
        got, used_b = [], set()
        for it in _pick(brand, 10, **flt):  # 같은 브랜드는 하나만
            b = next((b for b in BRANDS if b in it["title"][:10]), it["title"][:4])
            if b in used_b:
                continue
            used_b.add(b)
            got.append(it)
            if len(got) >= 2:
                break
        if len(got) < 2:
            got += [it for it in _pick([i for i in items if i["dist"] <= 1.0], 3, **flt) if it not in got][:2 - len(got)]
        return [_row("생활", it) for it in got]

    parts = await asyncio.gather(*[one(*s) for s in specs], marts(), return_exceptions=True)
    rows = []
    for p in parts:
        if isinstance(p, list):
            rows.extend(p)
    if not any(r["구분"] == "의료" for r in rows):  # 종합병원이 멀면 일반 병원
        try:
            rows += await one("의료", "병원", (2, 4), 1, dict(include=("병원",), title_has=("병원",),
                              title_not=("동물", "한의", "한방", "치과", "요양", "재활", "주차", "장례")))
        except Exception:
            pass
    order = {"교육": 0, "생활": 1, "의료": 2, "자연": 3, "교통": 4}
    rows.sort(key=lambda r: order.get(r["구분"], 9))
    return rows


def cap_wide(rows: list[dict], total: int = 10) -> list[dict]:
    """광역 표: 구분별 개수 제한(업무 3·산업 3·교통 4·행정 2·광역 1). 업무지구가 있으면 먼 산업단지는 뺌."""
    biz = any(r["구분"] == "업무" for r in rows)
    caps = {"업무": 3, "산업": 1 if biz else 3, "교통": 4, "행정": 2, "광역": 2}
    out, cnt = [], {}
    for r in rows:
        k = r["구분"]
        if biz and k == "산업" and r["거리km"] > 8:
            continue
        if cnt.get(k, 0) >= caps.get(k, 2):
            continue
        cnt[k] = cnt.get(k, 0) + 1
        out.append(r)
    return out[:total]


def dist_label(km: float) -> str:
    if km <= 0.5:
        return "도보권"
    return f"약 {km:.1f}km"


# ---------------------------------------------------------------------------
# 비교단지
# ---------------------------------------------------------------------------
_BASIS_FAIL: list[str] = []
_KAPT_SEM = asyncio.Semaphore(2)
_KAPT_STATE = {"consec_fail": 0, "open": False}


async def kapt_basis_cached(code: str) -> dict:
    """K-apt 기본정보(디스크 캐시). 서버 오류가 이어지면 이번 실행에서는 더 부르지 않음(차단 방지)."""
    hit = BASIS.get(code)
    if hit is not None:
        return hit
    if _KAPT_STATE["open"]:
        _BASIS_FAIL.append(code)
        return {}
    b = None
    async with _KAPT_SEM:
        for attempt in range(2):
            if _KAPT_STATE["open"]:
                break
            try:
                b = await S.kapt_basis(code)
                _KAPT_STATE["consec_fail"] = 0
                break
            except S.ApiError:
                _KAPT_STATE["consec_fail"] += 1
                if _KAPT_STATE["consec_fail"] >= 6:
                    _KAPT_STATE["open"] = True
                await asyncio.sleep(1.5)
    if not b:
        _BASIS_FAIL.append(code)
        return {}
    keep = {k: b.get(k) for k in ("kaptCode", "kaptName", "kaptAddr", "doroJuso", "kaptdaCnt", "kaptUsedate",
                                   "kaptBcompany", "kaptAcompany", "kaptMarea", "privArea", "kaptTopFloor",
                                   "kaptDongCnt", "codeSaleNm", "bjdCode")}
    BASIS.set(code, keep)
    return keep


def supply_ratio(basis: dict | None) -> tuple[float, bool]:
    """공급/전용 비율. K-apt 관리비부과면적/전용면적합으로 추정, 없으면 1.32."""
    if basis:
        m, p = S.num(basis.get("kaptMarea")), S.num(basis.get("privArea"))
        if m and p:
            r = m / p
            if 1.15 <= r <= 1.5:
                return round(r, 3), True
    return 1.32, False


_REGION_PREFIX = re.compile(r"^(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)")


def _cx_key(name: str, sgg: str | None = "") -> str:
    sgg = sgg or ""
    n = S.norm_name(name or "")
    for w in [sgg.split()[0][:-1] if sgg else "", sgg[:2] if sgg else ""]:
        if w and len(w) >= 2 and n.startswith(w):
            n = n[len(w):]
    n = _REGION_PREFIX.sub("", n)
    return re.sub(r"(아파트|apt)$", "", n)


def _name_sim(a: str, b: str, sgg: str, umd: str = "", ri: str = "") -> float:
    ka, kb = _cx_key(a, sgg), _cx_key(b, sgg)
    for u in (umd, ri):  # 'K-apt 신평현대' ↔ '실거래 현대': 동·리 이름 접두어를 떼고 다시 비교
        stem = re.sub(r"(\d*가|\d+동|동|리|읍|면)$", "", (u or "").replace(" ", ""))
        if len(stem) >= 2:
            if ka.startswith(stem) and len(ka) - len(stem) >= 2:
                ka = ka[len(stem):]
            if kb.startswith(stem) and len(kb) - len(stem) >= 2:
                kb = kb[len(stem):]
    if not ka or not kb:
        return 0.0
    if S._num_conflict(ka, kb):
        return 0.0
    if ka == kb:
        return 1.0 if len(ka) >= 3 else 0.9
    if ka in kb or kb in ka:
        return 1.0 if min(len(ka), len(kb)) >= 3 else 0.7
    return S.fuzzy_ratio(ka, kb)


def _umd_match(rtms_umd: str, kapt_item: dict) -> bool:
    u = (rtms_umd or "").replace(" ", "")
    k = f"{kapt_item.get('as3') or ''}{kapt_item.get('as4') or ''}".replace(" ", "")
    return bool(u and k and (u == k or u.endswith(k) or k.endswith(u)))


def quarters_back(n: int, today: dt.date | None = None) -> list[str]:
    t = today or dt.date.today()
    q = (t.month - 1) // 3
    y = t.year
    out = []
    for _ in range(n):
        out.append(f"{y % 100:02d}.{q + 1}Q")
        q -= 1
        if q < 0:
            q, y = 3, y - 1
    return out[::-1]


_RTMS_FAIL: dict[str, int] = {}
_RTMS_KEEP = ("aptNm", "aptSeq", "umdNm", "umdCd", "jibun", "buildYear", "excluUseAr", "floor", "dealYear", "dealMonth",
              "dealDay", "dealAmount", "cdealType", "cdealDay", "deposit", "monthlyRent", "sggCd",
              "roadNm", "roadNmBonbun", "roadNmBubun")


def _rtms_cache(lawd: str) -> DiskCache:
    return DiskCache(f"rtms2_{lawd}", 400)


async def _rtms_month(kind: str, lawd: str, ym: str, dc: DiskCache) -> list[dict] | None:
    """한 달치 실거래. 지난 달 이전 자료는 디스크에 보관(3개월 지난 달은 30일, 최근은 1일)."""
    key = f"{kind}:{ym}"
    hit = dc._load().get(key)
    this = dt.date.today().strftime("%Y%m")
    age_m = (int(this[:4]) * 12 + int(this[4:])) - (int(ym[:4]) * 12 + int(ym[4:]))
    ttl = 30 * 86400 if age_m >= 3 else 86400
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    for attempt in range(2):
        try:
            items = await S.fetch_tx(kind, lawd, ym)
            slim = [{k: it.get(k) for k in _RTMS_KEEP if it.get(k) not in (None, "")} for it in items]
            dc.set(key, slim)
            return slim
        except S.ApiError:
            await asyncio.sleep(1.5 * (attempt + 1))
    if hit:  # 갱신 실패 시 예전 자료라도
        return hit[1]
    return None


async def fetch_rows(lawd: str, months: list[str]) -> tuple[list[dict], list[dict]]:
    """시군구의 아파트 매매·전월세 원자료 (해제 제외). 실패한 달 수는 _RTMS_FAIL에 기록."""
    dc = _rtms_cache(lawd)
    jobs = [("아파트매매", m) for m in months] + [("아파트전월세", m) for m in months]
    res = await asyncio.gather(*[_rtms_month(k, lawd, m, dc) for k, m in jobs])
    dc.save()
    trades, rents, fails = [], [], 0
    for (k, _), r in zip(jobs, res):
        if r is None:
            fails += 1
            continue
        (trades if k == "아파트매매" else rents).extend(r)
    _RTMS_FAIL[lawd] = fails
    trades = [t for t in trades if not (t.get("cdealType") or "").strip() and not (t.get("cdealDay") or "").strip()]
    return trades, rents


def _date(it: dict) -> str:
    try:
        return f"{it.get('dealYear')}-{int(it.get('dealMonth')):02d}-{int(it.get('dealDay')):02d}"
    except (TypeError, ValueError):
        return ""


def _dedupe_raw(items: list[dict], kind: str) -> list[dict]:
    seen, out = set(), []
    for it in items:
        k = (it.get("aptSeq") or it.get("aptNm"), _date(it), it.get("floor"), it.get("excluUseAr"),
             it.get("dealAmount") if kind == "trade" else (it.get("deposit"), it.get("monthlyRent")))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def district_ratio(rows: tuple[list, list], area_min: float, area_max: float, end: str,
                   target_year: int | None = None) -> dict:
    """시군구 전세가율 = 단지별(같은 단지 안) 전세 ㎡당가 ÷ 매매 ㎡당가 의 중위값.
    단지를 섞어 평균내면 '전세는 신축, 매매는 구축'처럼 구성이 달라 왜곡되므로 같은 단지 안에서 비교.
    매매가 없는 임대 단지는 자연히 빠짐. 신축(본 건 준공 ±7년) 따로."""
    trades, rents = rows

    def band(it):
        a = S.num(it.get("excluUseAr"))
        return a is not None and area_min <= a <= area_max

    def key(it):
        return it.get("aptSeq") or f"{it.get('umdNm')}|{it.get('aptNm')}"

    def calc(months_back: int, cohort: int):
        lo = ym_add(end, -(months_back - 1))

        def ok(it):
            if not band(it) or _date(it).replace("-", "")[:6] < lo:
                return False
            if cohort and target_year:
                y = S.num(it.get("buildYear"))
                return bool(y) and abs(int(y) - target_year) <= cohort
            return True
        sp: dict[str, list] = {}
        jp: dict[str, list] = {}
        for t in trades:
            if ok(t) and S.num(t.get("dealAmount")):
                sp.setdefault(key(t), []).append(S.num(t["dealAmount"]) / S.num(t["excluUseAr"]))
        for r in rents:
            if ok(r) and not S.num(r.get("monthlyRent")) and S.num(r.get("deposit")):
                jp.setdefault(key(r), []).append(S.num(r["deposit"]) / S.num(r["excluUseAr"]))
        ratios, n_s, n_j = [], 0, 0
        for k in set(sp) & set(jp):
            q = statistics.median(jp[k]) / statistics.median(sp[k])
            if 0.2 <= q <= 1.2:
                ratios.append(q)
                n_s += len(sp[k])
                n_j += len(jp[k])
        if len(ratios) >= 3:
            return {"전세가율": round(statistics.median(ratios), 3), "단지수": len(ratios), "매매표본": n_s, "전세표본": n_j,
                    "기간": f"최근 {months_back}개월", "구분": f"신축(본 건 준공 ±{cohort}년)" if cohort else "전체"}
        return None

    def first(cohort):
        for mb in (12, 24, 36):
            r = calc(mb, cohort)
            if r:
                return r
        return None

    out = {}
    for cohort in ((7, 12) if target_year else ()):  # 신축: ±7년 → ±12년
        r = first(cohort)
        if r:
            out["신축"] = r
            break
    r = first(0)
    if r:
        out["전체"] = r
    return out


def _district_trend(rows: tuple[list, list], area_min: float, area_max: float, q_list: list[str],
                    target_year: int | None) -> tuple[dict, dict, int, str]:
    """시군구 전체 같은 면적대의 분기별 공급 3.3㎡당 가격(공급면적 = 전용×1.32 가정).
    비슷한 연식(준공 ±12년)이 6건 이상이면 그것만, 아니면 전 연식. 같은 날 같은 단지 5건 이상(일괄 매각)은 1건으로."""
    trades, rents = rows
    ty = target_year or dt.date.today().year

    def build(cohort: bool):
        tr = {q: [] for q in q_list}
        js = {q: [] for q in q_list}
        cx = set()
        for kind, src, dst, fld in (("trade", trades, tr, "dealAmount"), ("rent", rents, js, "deposit")):
            items = _dedupe_raw(src, kind)
            cnt: dict[tuple, int] = {}
            for it in items:
                k = (it.get("aptSeq") or it.get("aptNm"), _date(it))
                cnt[k] = cnt.get(k, 0) + 1
            seen_bulk: set = set()
            for it in items:
                a = S.num(it.get("excluUseAr"))
                if a is None or not (area_min <= a <= area_max):
                    continue
                if kind == "rent" and S.num(it.get("monthlyRent")):
                    continue
                y = S.num(it.get("buildYear"))
                if cohort and (not y or abs(int(y) - ty) > 12):
                    continue
                v = S.num(it.get(fld))
                q = yq(_date(it))
                if not v or q not in dst:
                    continue
                k = (it.get("aptSeq") or it.get("aptNm"), _date(it))
                if kind == "trade" and cnt[k] >= 5:
                    if k in seen_bulk:
                        continue
                    seen_bulk.add(k)
                dst[q].append(v / (a * 1.32 / PY))
                cx.add(it.get("aptSeq") or it.get("aptNm"))
        return tr, js, len(cx)

    tr, js, n = build(True)
    if sum(len(v) for v in tr.values()) + sum(len(v) for v in js.values()) >= 6:
        now_y = dt.date.today().year
        return tr, js, n, (f"준공 {ty - 12}년 이후" if ty + 12 >= now_y else f"준공 {ty - 12}~{ty + 12}년")
    tr, js, n = build(False)
    return tr, js, n, "전 연식"


def district_price(rows: tuple[list, list], area_min: float, area_max: float, end: str,
                   target_year: int | None = None) -> dict:
    """시군구 시세(전용 ㎡당 만원) = 단지별 중위 → 단지 간 중위. 비교사례가 모자랄 때 본 건 시세 추정에 씀.
    가격 수준은 연식 차이가 커서(군 지역 구축 소형은 신축의 1/3) 비슷한 연식만 씀:
    ① 준공 ±7년·같은 면적 ② ±7년·전용 50~135㎡ ③ ±12년·같은 면적. 각각 12→24→36개월, 단지 3곳 이상.
    연식을 모르면 미분양·신축 기준으로 올해를 씀."""
    trades, rents = rows
    ty = target_year or dt.date.today().year

    def calc(src, kind, months_back, lo_a, hi_a, yr):
        lo = ym_add(end, -(months_back - 1))
        per: dict[str, list] = {}
        for it in src:
            a = S.num(it.get("excluUseAr"))
            if a is None or not (lo_a <= a <= hi_a) or _date(it).replace("-", "")[:6] < lo:
                continue
            y = S.num(it.get("buildYear"))
            if not y or abs(int(y) - ty) > yr:
                continue
            if kind == "trade":
                v = S.num(it.get("dealAmount"))
            else:
                v = None if S.num(it.get("monthlyRent")) else S.num(it.get("deposit"))
            if v:
                per.setdefault(it.get("aptSeq") or f"{it.get('umdNm')}|{it.get('aptNm')}", []).append(v / a)
        if len(per) >= 3:
            meds = [statistics.median(v) for v in per.values()]
            return {"㎡당(만원)": round(statistics.median(meds), 1), "단지수": len(per),
                    "표본": sum(len(v) for v in per.values()), "기간": f"최근 {months_back}개월"}
        return None

    now_y = dt.date.today().year

    def yrs(yr):
        return f"준공 {ty - yr}년 이후" if ty + yr >= now_y else f"준공 {ty - yr}~{ty + yr}년"
    steps = ((area_min, area_max, 7, yrs(7)), (50, 135, 7, yrs(7) + "·전 평형"), (area_min, area_max, 12, yrs(12)))
    out = {}
    for kind, src, label in (("trade", trades, "매매"), ("rent", rents, "전세")):
        for lo_a, hi_a, yr, tag in steps:
            hit = None
            for mb in (12, 24, 36):
                hit = calc(src, kind, mb, lo_a, hi_a, yr)
                if hit:
                    break
            if hit:
                out[label] = {**hit, "구분": tag}
                break
    return out


def estimate_target(tgt: dict, cands: list[dict], ratio_info: dict, supply_py: float,
                    excl_area: float | None = None, target_year: int | None = None) -> dict:
    """본 건 매매·전세 시세 추정(예정가·분양가가 없을 때).
    매매: ① 비교사례 5곳 중위 ② 시군구 시세(전용 ㎡당, 신축 우선) × 본 건 전용.
    전세: ① 비교사례 중위 ② 시군구 전세가율(신축→전체) × 매매 기준가 ③ 비교단지 자체 전세가율
          ④ 시군구 전 평형 전세가율 ⑤ 시군구 전세 시세(㎡당) × 전용."""
    out: dict[str, Any] = {}
    if supply_py <= 0:
        return out
    dprice = ratio_info.get("시세") or {}

    def comparable(r):
        """시세 추정에 쓸 사례: 50세대 이상(모르면 허용)·준공 ±10년(본 건 연식을 알 때)."""
        hh = r.get("세대수")
        if hh is not None and hh < 50:
            return False
        if target_year:
            y = r.get("준공연도")
            return bool(y) and abs(int(y) - target_year) <= 10
        return True

    def from_cases(key):
        vals = [r[key] for r in cands[:7] if r.get(key) and comparable(r)][:5]
        if len(vals) >= 2:
            return statistics.median(vals), len(vals)
        return None, len(vals)

    def pack(v, method, spread=0.07):
        return {"추정가(만원)": round(v), "공급평단가": round(v / supply_py), "방법": method,
                "범위(만원)": [round(v * (1 - spread)), round(v * (1 + spread))]}

    # 매매
    if not tgt.get("매매가") or tgt.get("가격구분") == "분양가(최고)":
        pp, n = from_cases("매매공급평단가")
        if pp:
            out["매매"] = pack(pp * supply_py, f"비교사례 {n}곳 중위값(50세대 이상·비슷한 연식)")
        elif dprice.get("매매") and excl_area:
            d = dprice["매매"]
            out["매매"] = pack(d["㎡당(만원)"] * excl_area,
                             f"비슷한 연식·규모 비교사례 부족({n}곳) → 시군구 시세 전용 ㎡당 {d['㎡당(만원)']:,.0f}만원"
                             f"({d['구분']}, {d['기간']}, {d['단지수']}개 단지 중위) × 전용 {excl_area:g}㎡", 0.10)
    sale_ref = tgt.get("매매가") if tgt.get("가격구분") == "예정 매각가" else None
    sale_ref = sale_ref or (out.get("매매") or {}).get("추정가(만원)") or tgt.get("매매가")
    basis = ("예정 매각가" if tgt.get("가격구분") == "예정 매각가" else
             ("추정 매매가" if out.get("매매") else "분양가"))
    # 전세
    if not tgt.get("전세가"):
        pp, n = from_cases("전세공급평단가")
        if pp:
            out["전세"] = pack(pp * supply_py, f"비교사례 {n}곳 중위값(50세대 이상·비슷한 연식)")
            return out
        r = ratio_info.get("신축") or ratio_info.get("전체")
        if r and sale_ref:
            out["전세"] = pack(sale_ref * r["전세가율"],
                             f"비교 전세사례 부족 → 시군구 전세가율 {r['전세가율'] * 100:.0f}%({r['구분']}, {r['기간']}, "
                             f"{r['단지수']}개 단지 단지별 중위) × {basis}")
            return out
        own = [c["전세공급평단가"] / c["매매공급평단가"] for c in cands[:7] if comparable(c)
               if c.get("전세공급평단가") and c.get("매매공급평단가") and 0.2 <= c["전세공급평단가"] / c["매매공급평단가"] <= 1.2]
        if own and sale_ref:
            q = statistics.median(own)
            out["전세"] = pack(sale_ref * q, f"시군구 전세가율 산정 불가 → 비교단지 {len(own)}곳 자체 전세가율 "
                                             f"{q * 100:.0f}% × {basis}", 0.10)
            return out
        r = ratio_info.get("전평형")
        if r and sale_ref:
            out["전세"] = pack(sale_ref * r["전세가율"],
                             f"같은 면적 전세가율 산정 불가 → 시군구 전 평형 전세가율 {r['전세가율'] * 100:.0f}%"
                             f"({r['기간']}, {r['단지수']}개 단지) × {basis}", 0.10)
            return out
        if dprice.get("전세") and excl_area:
            d = dprice["전세"]
            out["전세"] = pack(d["㎡당(만원)"] * excl_area,
                             f"매매 기준가·전세가율 없음 → 시군구 전세 시세 전용 ㎡당 {d['㎡당(만원)']:,.0f}만원"
                             f"({d['구분']}, {d['기간']}, {d['단지수']}개 단지) × 전용 {excl_area:g}㎡", 0.12)
    return out


async def comparables(loc: dict, radius_km: float = 1.5, quarters: int = 12, area_min: float = 80,
                      area_max: float = 86, max_items: int = 7, exclude_names: list[str] | None = None,
                      include_names: list[str] | None = None, target_year: int | None = None) -> dict:
    lat, lon = loc["lat"], loc["lon"]
    _BASIS_FAIL.clear()
    _KAPT_STATE.update(consec_fail=0, open=False)
    # 1) 반경 안 법정동(리) 수집: 중심 + 반경/절반 반경 링 16점 역지오코딩
    pts = [(lat, lon)] + [offset(lat, lon, radius_km * f, b) for f in (0.5, 1.0) for b in range(0, 360, 45)]
    rg = [r for r in await asyncio.gather(*[reverse_geocode(a, b) for a, b in pts]) if r]
    if not rg:
        raise S.ApiError("주변 법정동을 찾지 못했습니다(브이월드 역지오코딩 실패).")
    from collections import Counter
    tgt_lawd = str(loc.get("lawd_cd") or (rg[0].get("bjd") or "")[:5])
    hits = Counter((r.get("bjd") or "")[:5] for r in rg if r.get("bjd"))
    # 한 점에서만 나온 다른 시도 코드는 역지오코딩 오류로 보고 버림(엉뚱한 지역 실거래 호출 방지)
    rg = [r for r in rg if r.get("bjd") and (r["bjd"][:5] == tgt_lawd or r["bjd"][:2] == tgt_lawd[:2]
                                            or hits[r["bjd"][:5]] >= 2)]
    bjds = {r["bjd"] for r in rg if r.get("bjd")}
    lawds = sorted({b[:5] for b in bjds})
    umd_by_lawd: dict[str, set] = {}
    sgg_by_lawd: dict[str, tuple[str, str]] = {}
    for r in rg:
        if r.get("bjd"):
            umd_by_lawd.setdefault(r["bjd"][:5], set()).add(r["umd"].replace(" ", ""))
            sgg_by_lawd[r["bjd"][:5]] = (r["sido"], r["sgg"])

    # 2) 실거래 원자료 (분기 수 + 여유)
    end = dt.date.today().strftime("%Y%m")
    first_q_month = ym_add(end, -((quarters - 1) * 3 + (int(end[4:]) - 1) % 3))
    n_months = (int(end[:4]) * 12 + int(end[4:])) - (int(first_q_month[:4]) * 12 + int(first_q_month[4:])) + 1
    months = [ym_add(end, -i) for i in range(n_months)]
    raw = await asyncio.gather(*[fetch_rows(lw, months) for lw in lawds])
    rows_t = dict(zip(lawds, raw)).get(tgt_lawd) or (raw[0] if raw else ([], []))
    ratio_info = district_ratio(rows_t, area_min, area_max, end, target_year)
    dprice = district_price(rows_t, area_min, area_max, end, target_year)
    if dprice:
        ratio_info["시세"] = dprice
    if not ratio_info.get("신축") and not ratio_info.get("전체"):
        r_all = district_ratio(rows_t, 0, 1000, end, None).get("전체")
        if r_all:
            ratio_info["전평형"] = {**r_all, "구분": "전 평형"}
    groups: dict[str, dict] = {}
    for lw, (tr, rt) in zip(lawds, raw):
        umds = umd_by_lawd.get(lw, set())
        for kind, items in (("trade", _dedupe_raw(tr, "trade")), ("rent", _dedupe_raw(rt, "rent"))):
            for it in items:
                u = (it.get("umdNm") or "").replace(" ", "")
                if umds and not any(u == x or u.endswith(x) or x.endswith(u) for x in umds if x):
                    continue
                key = it.get("aptSeq") or f"{lw}:{u}:{it.get('aptNm')}"
                g = groups.setdefault(key, {"aptSeq": key, "lawd": lw, "names": {}, "umd": it.get("umdNm", ""),
                                            "jibun": "", "build": it.get("buildYear"), "trade": [], "rent": []})
                g["names"][it.get("aptNm", "")] = g["names"].get(it.get("aptNm", ""), 0) + 1
                if not g.get("road") and (it.get("roadNm") or "").strip() and S.num(it.get("roadNmBonbun")):
                    bb, bu = int(S.num(it["roadNmBonbun"])), int(S.num(it.get("roadNmBubun")) or 0)
                    g["road"] = f"{it['roadNm'].strip()} {bb}" + (f"-{bu}" if bu else "")
                if not g["jibun"] and (it.get("jibun") or "").strip() and "*" not in it.get("jibun", ""):
                    g["jibun"] = it["jibun"].strip()
                g[kind].append(it)
    bulk_dropped = 0
    for g in groups.values():
        g["name"] = max(g["names"], key=g["names"].get) if g["names"] else ""
        # 같은 날 같은 단지 5건 이상 = 일괄 매각(법인·임대사업자) → 1건으로 줄여 추이 왜곡 방지
        from collections import defaultdict as _dd
        byday = _dd(list)
        for t in g["trade"]:
            byday[_date(t)].append(t)
        keep = []
        for d, ts in byday.items():
            if len(ts) >= 5:
                ts = sorted(ts, key=lambda x: S.num(x.get("dealAmount")) or 0)
                keep.append(ts[len(ts) // 2])
                bulk_dropped += len(ts) - 1
            else:
                keep.extend(ts)
        g["trade"] = keep

    # 3) K-apt 단지 (반경 안 법정동)
    kall = await S.kapt_list()
    kitems = [it for it in kall if it.get("bjdCode") in bjds]
    if not kitems:  # 법정동코드가 어긋날 때: 시군구코드+읍면동 이름으로
        names = {(lw, u) for lw, us in umd_by_lawd.items() for u in us}
        kitems = [it for it in kall if (str(it.get("bjdCode", ""))[:5],
                                        f"{it.get('as3') or ''}{it.get('as4') or ''}".replace(" ", "")) in names]
    bases = [BASIS.get(it["kaptCode"]) or {} for it in kitems]  # 캐시만 사용; 새로 부르는 건 반경 안 단지만(아래)
    complexes: list[dict] = []
    used: set[str] = set()
    for it, b in zip(kitems, bases):
        sgg = sgg_by_lawd.get(it["bjdCode"][:5], ("", ""))[1]
        _, bun, ji, san = S._parse_parcel_text(b.get("kaptAddr", "")) if b else (None, None, 0, False)
        jib = S.jibun_str(bun, ji, san) if bun is not None else ""
        best, best_sc = None, 0.0
        for key, g in groups.items():
            if key in used or g["lawd"] != it["bjdCode"][:5] or not _umd_match(g["umd"], it):
                continue
            sc = _name_sim(it["kaptName"], g["name"], sgg, it.get("as3") or "", it.get("as4") or "")
            if jib and g["jibun"] and g["jibun"] == jib and sc > 0:
                sc = max(sc, 0.95)
            elif jib and g["jibun"] and g["jibun"] == jib and not S._num_conflict(it["kaptName"], g["name"]):
                sc = max(sc, 0.85)
            if sc > best_sc:
                best, best_sc = g, sc
        if best is not None and best_sc >= 0.75:
            used.add(best["aptSeq"])
        else:
            best = None
        # K-apt as2는 '창원마산회원구'처럼 붙여 쓰므로 지오코딩용 시도·시군구는 역지오코딩 값을 씀
        sd_rg, sg_rg = sgg_by_lawd.get(it["bjdCode"][:5], ("", ""))
        complexes.append({"name": it["kaptName"], "kaptCode": it["kaptCode"], "basis": b, "group": best,
                          "lawd": it["bjdCode"][:5], "umd": f"{it.get('as3') or ''} {it.get('as4') or ''}".strip(),
                          "jibun": jib, "sido": sd_rg or it.get("as1") or "",
                          "sgg": sg_rg or (S.norm_sido(it.get("as2") or "") if it.get("as2") else "") or ""})
    for key, g in groups.items():  # K-apt에 없는 실거래 단지
        if key in used or len(g["trade"]) + len(g["rent"]) < 5:
            continue
        sd, sg = sgg_by_lawd.get(g["lawd"], ("", ""))
        complexes.append({"name": g["name"], "kaptCode": None, "basis": None, "group": g, "lawd": g["lawd"],
                          "umd": g["umd"], "jibun": g["jibun"], "sido": sd, "sgg": sg})

    # 4) 좌표 → 반경 필터 (비교 면적 거래가 있는 단지만 좌표를 찾음: 호출 수 절감)
    def band_deals(c):
        g = c.get("group") or {}
        n = 0
        for it in (g.get("trade") or []) + (g.get("rent") or []):
            a = S.num(it.get("excluUseAr"))
            if a is not None and area_min <= a <= area_max:
                n += 1
        return n

    n_all = len(complexes)
    complexes = [c for c in complexes if band_deals(c) > 0]

    async def locate(c: dict):
        b = c.get("basis") or {}
        pt = None
        if c["jibun"]:
            pt = await geocode_parcel(f"{c['sido']} {c['sgg']} {c['umd']} {c['jibun']}")
        if not pt and b.get("doroJuso"):
            pt = await geocode_road(b["doroJuso"])
        if not pt and c.get("group") and c["group"].get("jibun") and c["group"]["jibun"] != c["jibun"]:
            pt = await geocode_parcel(f"{c['sido']} {c['sgg']} {c['group']['umd']} {c['group']['jibun']}")
        if not pt and c.get("group") and c["group"].get("road"):  # 택지지구 신축: 실거래 도로명주소
            pt = await geocode_road(f"{c['sido']} {c['sgg']} {c['group']['road']}")
        if not pt:  # 마지막으로 브이월드 아파트단지 POI 이름 검색
            key = _cx_key(c["name"], c.get("sgg", ""))
            if len(key) >= 3:
                cands = [it for it in await place_bbox(key[:12], lat, lon, radius_km + 1.5, size=20)
                         if "아파트" in it["category"] or "공동주택" in it["category"]]
                cands = [it for it in cands if _name_sim(it["title"].split("/")[0], c["name"], c.get("sgg", "")) >= 0.8]
                if cands:
                    pt = (cands[0]["lat"], cands[0]["lon"])
        if pt:
            c["lat"], c["lon"] = pt
            c["dist"] = hav(lat, lon, pt[0], pt[1])

    await asyncio.gather(*[locate(c) for c in complexes])
    GEO.save()
    diag = {"역지오코딩": len(rg), "법정동코드": sorted(bjds), "K-apt후보": len(kitems), "실거래단지": len(groups),
            "면적거래단지": len(complexes), "전체후보": n_all, "일괄매각정리건수": bulk_dropped,
            "K-apt매칭": sum(1 for c in complexes if c.get("kaptCode") and c.get("group")),
            "좌표확보(K-apt)": sum(1 for c in complexes if c.get("kaptCode") and c.get("dist") is not None),
            "좌표확보(실거래만)": sum(1 for c in complexes if not c.get("kaptCode") and c.get("dist") is not None),
            "K-apt목록": len(kall)}
    excl = [S.norm_name(x) for x in (exclude_names or []) if x]
    tgt_jibun = loc.get("jibun")
    inside = []
    for c in complexes:
        if c.get("dist") is None or c["dist"] > radius_km:
            continue
        if c.get("dist", 9) < 0.03 or (tgt_jibun and c["jibun"] == tgt_jibun and c["lawd"] == loc.get("lawd_cd")):
            continue  # 본 건 자체
        if any(e and (e in S.norm_name(c["name"]) or S.norm_name(c["name"]) in e) for e in excl):
            continue
        if any(x and _name_sim(c["name"], x, c.get("sgg", "")) >= 0.85 for x in (exclude_names or [])):
            continue  # 표기만 다른 본 건(예: 중앙하이츠/중앙하이즈)
        inside.append(c)
    inside.sort(key=lambda c: c["dist"])
    need = [c for c in inside if c.get("kaptCode") and not c.get("basis")][:20]
    if need:
        got = await asyncio.gather(*[kapt_basis_cached(c["kaptCode"]) for c in need])
        for c, b in zip(need, got):
            c["basis"] = b or None
        BASIS.save()
    if include_names:  # 사용자가 고른 비교단지만
        inc = [_cx_key(x) for x in include_names if x]
        inside = [c for c in inside if any(_name_sim(c["name"], x, c.get("sgg", "")) >= 0.75 for x in include_names)]

    # 5) 단지별 통계 (전용 area_min~area_max)
    q_list = quarters_back(quarters)
    last12 = ym_add(end, -11)
    last6 = ym_add(end, -5)

    def in_band(it):
        a = S.num(it.get("excluUseAr"))
        return a is not None and area_min <= a <= area_max

    out_rows = []
    for c in inside[:60]:
        ratio, exact = supply_ratio(c.get("basis"))
        g = c.get("group") or {"trade": [], "rent": []}
        tr = [t for t in g["trade"] if in_band(t)]
        js = [r for r in g["rent"] if in_band(r) and not S.num(r.get("monthlyRent"))]

        def pp(it, price_key, ratio=ratio):  # 공급 3.3㎡당 (만원)
            a = S.num(it.get("excluUseAr"))
            p = S.num(it.get(price_key))
            return p / (a * ratio / PY) if a and p else None

        def recent(items, key):
            sel, win = [], None
            for w in (6, 12, 24, 36):
                lo = ym_add(end, -(w - 1))
                sel = [x for x in items if _date(x).replace("-", "")[:6] >= lo]
                if sel:
                    win = w
                    break
            if not sel:
                return None, None, 0, None, None
            sel = sorted(sel, key=_date)[-10:] if win and win > 12 else sel
            prices = [S.num(x.get(key)) for x in sel if S.num(x.get(key))]
            areas = [S.num(x.get("excluUseAr")) for x in sel if S.num(x.get("excluUseAr"))]
            pps = [pp(x, key) for x in sel if pp(x, key)]
            return (statistics.median(prices) if prices else None, statistics.median(areas) if areas else None,
                    len(sel), statistics.median(pps) if pps else None, win)

        sp, sa, sn, spp, sw = recent(tr, "dealAmount")
        jp, ja, jn, jpp, jw = recent(js, "deposit")
        b = c.get("basis") or {}
        use = (b.get("kaptUsedate") or "")
        built = f"'{use[2:4]}.{use[4:6]}" if len(use) >= 6 else (f"'{str(g.get('build'))[2:4]}" if g.get("build") else "-")
        hh = int(S.num(b.get("kaptdaCnt")) or 0) or None
        out_rows.append({
            "no": 0, "단지명": c["name"], "준공": built,
            "준공연도": full_year(built[1:3]) if built.startswith("'") else None, "세대수": hh,
            "전용㎡": round(sa or ja or 0) or None, "매매가(만원)": sp, "매매공급평단가": round(spp) if spp else None,
            "매매건수": sn, "전세가(만원)": jp, "전세공급평단가": round(jpp) if jpp else None, "전세건수": jn,
            "시공사": (b.get("kaptBcompany") or "").replace("(주)", "").replace("주식회사", "").strip() or None,
            "공급비율": ratio, "공급비율근거": "K-apt 관리비부과면적/전용면적" if exact else "추정(1.32)",
            "거리km": round(c["dist"], 2), "lat": c["lat"], "lon": c["lon"], "kaptCode": c.get("kaptCode"),
            "매매기준개월": sw, "전세기준개월": jw, "비교표": bool(sp or jp),
            "_tr": [(yq(_date(t)), pp(t, "dealAmount")) for t in tr], "_js": [(yq(_date(r)), pp(r, "deposit")) for r in js],
            "_deals": len(g["trade"]) + len(g["rent"]),
        })

    # 6) 비교단지 고르기: 실거래가 있는 '단지급'(K-apt 등록 또는 거래 많은 곳) 우선, 가까운 순
    elig = [r for r in out_rows if r["비교표"]]

    def score(r):  # 가까울수록, 준공 연차가 비슷할수록(20년 ≈ 0.8km), 규모가 클수록 우선
        y = r.get("준공연도")
        sc = r["거리km"] + (0.04 * abs(target_year - y) if (target_year and y) else 0.3)
        hh = r.get("세대수") or 0
        sc += 0.6 if hh and hh < 100 else (0.3 if hh and hh < 200 else (0.15 if not hh else 0))
        return sc

    elig.sort(key=score)
    major = [r for r in elig if r["kaptCode"] or r["_deals"] >= 12 or (r["세대수"] or 0) >= 150]
    chosen = major[:max_items]
    if len(chosen) < min(4, max_items):
        chosen += [r for r in elig if r not in chosen][:max_items - len(chosen)]
    chosen.sort(key=lambda r: r["거리km"])
    for i, r in enumerate(chosen, 1):
        r["no"] = i

    async def fill_from_register(r):  # K-apt를 못 받은 단지: 건축물대장(총괄표제부)로 세대수·준공 보완
        pnu = _PNU.get((round(r["lat"], 6), round(r["lon"], 6)))
        if not pnu or (r["세대수"] and r["준공"] not in ("-", None) and len(r["준공"]) > 3):
            return
        try:
            rec = await S.building_register(pnu, "총괄표제부", limit=2)
            it = (rec.get("항목") or [{}])[0]
            if not it:
                rec = await S.building_register(pnu, "표제부", limit=50)
                rows_ = [x for x in rec.get("항목", []) if x.get("주/부속") == "주건축물"]
                it = {"세대수": sum(int(S.num(x.get("세대수")) or 0) for x in rows_),
                      "사용승인일": (rows_[0].get("사용승인일") if rows_ else None)}
        except Exception:
            return
        hh = int(S.num(it.get("세대수")) or 0)
        if hh and not r["세대수"]:
            r["세대수"] = hh
        ua = str(it.get("사용승인일") or "")
        if len(ua) >= 6 and (r["준공"] in ("-", None) or len(r["준공"]) <= 3):
            r["준공"] = f"'{ua[2:4]}.{ua[4:6]}"
            r["준공연도"] = int(ua[:4])

    await asyncio.gather(*[fill_from_register(r) for r in chosen if not r["세대수"] or len(r["준공"]) <= 3])
    trend_src = chosen if len([r for r in chosen if r["_tr"] or r["_js"]]) >= 3 else elig
    trend_tr: dict[str, list[float]] = {q: [] for q in q_list}
    trend_js: dict[str, list[float]] = {q: [] for q in q_list}
    for r in trend_src:
        for q, v in r["_tr"]:
            if q in trend_tr and v:
                trend_tr[q].append(v)
        for q, v in r["_js"]:
            if q in trend_js and v:
                trend_js[q].append(v)
    others = len(inside) - len(chosen)
    for r in out_rows:
        for k in ("_tr", "_js"):
            r.pop(k, None)
    cands = [{k: v for k, v in r.items() if not k.startswith("_")} for r in elig[:20]]
    out_rows = chosen
    diag["반경내단지"] = len(inside)
    diag["비교표후보"] = len(elig)
    diag["추이단지수"] = len(trend_src)

    # 반경 안 거래가 너무 적으면(합계 6건 미만) 추이는 시군구 전체 같은 면적대(비슷한 연식)로 대체
    trend_scope = None
    if sum(len(v) for v in trend_tr.values()) + sum(len(v) for v in trend_js.values()) < 6:
        d_tr, d_js, n_cx_d, tag = _district_trend(rows_t, area_min, area_max, q_list, target_year)
        if sum(len(v) for v in d_tr.values()) + sum(len(v) for v in d_js.values()) >= 6:
            trend_tr, trend_js = d_tr, d_js
            trend_scope = {"구분": "시군구", "단지수": n_cx_d, "연식": tag, "공급비율": 1.32}
            diag["추이단지수"] = n_cx_d

    def summarize(d):
        return [{"분기": q, "건수": len(d[q]), "평균공급평단가": round(statistics.mean(d[q])) if d[q] else None} for q in q_list]

    return {"반경km": radius_km, "면적구간": f"전용 {area_min:g}~{area_max:g}㎡", "단지": out_rows,
            "후보": cands, "시군구전세가율": ratio_info,
            "추이단지수": (trend_scope or {}).get("단지수") or len(trend_src), "추이범위": trend_scope,
            "반경내단지수": len(inside),
            "매매추이": summarize(trend_tr), "전세추이": summarize(trend_js),
            "기준": f"{yq(dt.date.today().isoformat())} 은 {dt.date.today().month}월까지",
            "법정동": sorted({r['umd'] for r in rg if r.get('umd')}), "시군구코드": lawds,
            "자료누락": {"실거래_실패_월수": sum(_RTMS_FAIL.get(lw, 0) for lw in lawds), "단지정보_실패": len(_BASIS_FAIL),
                      "요청_월수": len(months) * 2 * len(lawds)}, "진단": diag}


# ---------------------------------------------------------------------------
# 본 건 개요
# ---------------------------------------------------------------------------
_SIDO_ALIAS = {"전남광주": ("광주", "전라남도", "전남"), "강원": ("강원",), "전북": ("전라북도", "전북"), "제주": ("제주",),
               "세종": ("세종",)}


# 행정구역 개편: 개편 전 공고는 옛 이름으로 수록 (인천 서구 → 서구·검단구, '26.7)
_SGG_ALIAS = {("인천", "검단구"): ("서구",)}


def _region_words(place: dict | None) -> list[str]:
    if not place:
        return []
    ws = []
    for k in ("sido", "sgg", "umd"):
        for tok in (place.get(k) or "").split():
            ws.append(tok)
            core = re.sub(r"(특별자치시|특별자치도|통합특별시|특별시|광역시|도|시|군|구|읍|면|동|리)$", "", tok)
            if len(core) >= 2:
                ws.append(core)
    return sorted(set(ws), key=len, reverse=True)


def _core_name(n: str, words: list[str]) -> str:
    """단지명에서 지역 이름(시·구·동)을 떼어낸 핵심 이름."""
    x = S.norm_name(n)
    for w in words:
        w2 = S.norm_name(w)
        if w2 and x.startswith(w2) and len(x) - len(w2) >= 3 and not x[len(w2)] == "역":
            x = x[len(w2):]
    return x


def _same_place(addr: str, place: dict | None) -> bool:
    """청약홈 공급위치가 본 건 시군구와 같은지."""
    if not place or not addr:
        return True
    a = addr.replace(" ", "")
    sgg = (place.get("sgg") or "").split()
    sgg_core = re.sub(r"(시|군|구)$", "", sgg[0]) if sgg else ""
    sd = sido_short(place.get("sido") or "")
    sido_ok = any(x in a for x in _SIDO_ALIAS.get(sd, (sd,))) if sd else True
    if sgg_core and len(sgg_core) >= 2:
        alts = [sgg_core] + [re.sub(r"(시|군|구)$", "", x) for x in _SGG_ALIAS.get((sd, sgg[0]), ())]
        return any(c in a for c in alts) and (sido_ok or sgg[0].endswith(("시", "군")))
    return sido_ok


async def presale_info(name: str, area: float | None, region: str = "", place: dict | None = None) -> dict | None:
    """청약홈 분양정보에서 해당 전용면적 주택형의 공급면적·분양가.
    매칭: 공급위치가 같은 시군구 + (지역명을 뗀) 단지 핵심 이름이 같거나 포함/유사(0.85) + 단지 번호 충돌 없음."""
    target = S.norm_name(name)
    if len(target) < 3:
        return None
    words = _region_words(place)
    t_core = _core_name(name, words)
    since = (dt.date.today() - dt.timedelta(days=365 * 8)).isoformat()
    cands: list[dict] = []
    seen = set()
    # 검색 순서: 시군구(개편 전 이름 포함) → 단지명 낱말(청약홈 주택명은 띄어쓰기가 있어 원래 낱말로 검색)
    sd = sido_short((place or {}).get("sido") or "")
    if not region and sd == "세종":
        region = "세종"
    regions = [region] + list(_SGG_ALIAS.get((sd, region), ()))
    wset = {S.norm_name(w) for w in words}
    toks = sorted({t for t in re.split(r"[\s()]+", name or "")
                   if len(S.norm_name(t)) >= 3 and S.norm_name(t) not in wset}, key=len, reverse=True)
    plan = [(None, rg) for rg in regions if rg] + [(kw, None) for kw in toks[:2]]
    for kw, rg in plan:
        try:
            found = await S.presale_search(keyword=kw, region=rg or None, since=since, limit=300)
        except Exception:
            continue
        for it in found.get("목록", []):
            if it.get("주택관리번호") in seen:
                continue
            n = S.norm_name(it.get("주택명") or "")
            if not n or S._num_conflict(n, target) or not _same_place(it.get("공급위치") or "", place):
                continue
            n_core = _core_name(it.get("주택명") or "", words)
            n_core = re.sub(r"(임의공급|잔여세대|무순위|추가입주자모집|신혼희망타운|조합원취소분|\d+회차)$", "", n_core)
            ok = (n == target or n_core == t_core
                  or (min(len(n_core), len(t_core)) >= 4 and (n_core in t_core or t_core in n_core))
                  or S.fuzzy_ratio(t_core, n_core) >= 0.85)
            if ok:
                seen.add(it.get("주택관리번호"))
                cands.append(it)
        if cands:
            break
    # 원래 입주자모집공고를 우선(신혼희망타운·추가모집·잔여세대 공고는 일부 주택형만 있음)
    def closeness(it):
        c = _core_name(it.get("주택명") or "", words)
        return 0 if c == t_core else (1 if (c in t_core or t_core in c) else 2)
    cands.sort(key=lambda it: (closeness(it), bool(re.search(r"신혼희망|추가|잔여|임의|무순위|취소|재공급", it.get("주택명") or "")),
                               str(it.get("모집공고일") or "")))
    for it in cands[:3]:
        no = it.get("주택관리번호")
        if not no:
            continue
        try:
            det = await S.presale_detail(str(no))
        except Exception:
            continue
        models = []
        for m in det.get("주택형별", []) or []:
            ty = str(m.get("HOUSE_TY") or "")
            mm = re.match(r"0*(\d+(?:\.\d+)?)", ty)
            ex = float(mm.group(1)) if mm else None
            sup = S.num(m.get("SUPLY_AR"))
            if not ex or not sup or sup <= ex:
                continue
            n = int((S.num(m.get("SUPLY_HSHLDCO")) or 0) + (S.num(m.get("SPSPLY_HSHLDCO")) or 0))
            models.append({"주택형": ty, "전용㎡": ex, "공급㎡": sup, "세대수": n, "분양가최고(만원)": S.num(m.get("LTTOT_TOP_AMOUNT"))})
        if not models:
            continue
        pick = min(models, key=lambda m: abs(m["전용㎡"] - area)) if area else max(models, key=lambda m: m["세대수"])
        if area and abs(pick["전용㎡"] - area) > 3:
            pick = max(models, key=lambda m: m["세대수"])
        info = det.get("공고정보") or {}
        return {"주택명": it.get("주택명"), "모집공고일": it.get("모집공고일"), "시공사": info.get("시공사") or it.get("시공사"),
                "공급세대수": info.get("공급세대수"), "입주예정월": info.get("입주예정월"), "주택형": models, "선택": pick,
                "전용㎡": pick["전용㎡"], "공급㎡": pick["공급㎡"], "공급비율": round(pick["공급㎡"] / pick["전용㎡"], 4),
                "분양가최고(만원)": pick["분양가최고(만원)"]}
    return None


def types_text(agg: dict[int, int]) -> str:
    """주택형 요약. 4개 이하는 모두, 많으면 범위·타입 수·주력 3개(표 칸 넘침 방지)."""
    items = sorted(agg.items())
    if len(items) <= 4:
        return " / ".join(f"전용 {a}㎡ {n:,}세대" if n else f"전용 {a}㎡" for a, n in items)
    top = sorted(items, key=lambda x: -x[1])[:3]
    head = f"전용 {items[0][0]}~{items[-1][0]}㎡ {len(items)}개 타입"
    if not any(n for _, n in top):
        return head
    return head + " (주력 " + " · ".join(f"{a}㎡ {n:,}세대" for a, n in top if n) + ")"


def clean_addr(addr: str) -> str:
    """K-apt 주소 꼬리 정리: '…읍남리 503- 울진역센트럴두산위브' → '…읍남리 503'."""
    toks = [t for t in (addr or "").split() if t != "-"]
    if len(toks) > 1 and toks[1].endswith("구") and not re.search(r"(특별시|광역시|특별자치시)$", toks[0]):
        for city in _GU_CITIES:  # '안양동안구' → '안양시 동안구'
            if toks[1].startswith(city) and len(toks[1]) > len(city) + 1 and not toks[1].startswith(city + "시"):
                toks[1:2] = [city + "시", toks[1][len(city):]]
                break
    keep = 0
    for i, t in enumerate(toks):
        if re.match(r"^(산)?\d+(-\d*)?(번지)?$", t):
            keep = i + 1
            break
        if re.search(r"(시|군|구|읍|면|동|리|가)$", t) or i == 0:
            keep = i + 1
    out = toks[:keep] if keep else toks
    if out:
        out[-1] = out[-1].rstrip("-")
    return " ".join(out)


async def asset_overview(loc: dict) -> dict:
    """자산개요 표: 건축물대장(총괄표제부/표제부) + 토지이용 + K-apt."""
    q = loc.get("pnu") or loc.get("query")
    out: dict[str, Any] = {}
    recap, title, land, cx = await asyncio.gather(
        S.building_register(q, "총괄표제부", limit=3), S.building_register(q, "표제부", limit=200),
        S.land_info(q) if S.VW_KEY and loc.get("level") == "parcel" else asyncio.sleep(0, {}),
        S.complex_info(loc.get("query") or q) if loc.get("complex") else asyncio.sleep(0, {}),
        return_exceptions=True)
    recap_items = recap.get("항목", []) if isinstance(recap, dict) else []
    title_items = title.get("항목", []) if isinstance(title, dict) else []
    mains = [x for x in title_items if x.get("주/부속") == "주건축물"] or title_items
    base = recap_items[0] if recap_items else (mains[0] if mains else {})
    addr = clean_addr(loc.get("address") or base.get("지번주소") or "")
    road = loc.get("road_address") or base.get("도로명주소") or ""
    road_short = road
    for t in addr.split()[:3]:
        if road_short.startswith(t + " "):
            road_short = road_short[len(t) + 1:]
    out["대지위치"] = f"{addr}" + (f" ({road_short})" if road_short else "")
    if isinstance(land, dict):
        ch = land.get("토지특성", {})
        z = [v for v in (ch.get("용도지역1"), ch.get("용도지역2")) if v]
        if z:
            out["용도지역"] = z[0] + (f" 외 {len(z) - 1}" if len(z) > 1 else "")
    for lab, key in (("대지면적", "대지면적㎡"), ("건축면적", "건축면적㎡"), ("연면적", "연면적㎡")):
        v = S.num(base.get(key))
        if v:
            out[lab] = f"{v:,.2f}㎡ / {v / PY:,.1f}평"
    for lab, key in (("건폐율", "건폐율%"), ("용적률", "용적률%")):
        v = S.num(base.get(key))
        if v:
            out[lab] = f"{v:.2f}%"
    hh = sum(int(S.num(x.get("세대수")) or 0) for x in mains)
    if mains:
        top = max(int(S.num(x.get("지상층수")) or 0) for x in mains)
        bot = max(int(S.num(x.get("지하층수")) or 0) for x in mains)
        purp = base.get("주용도") or mains[0].get("주용도") or ""
        out["규모"] = f"{purp} {len(mains)}개동(" + (f"B{bot}~" if bot else "") + f"{top}F)" + (f" {hh:,}세대" if hh else "")
    pk = S.num(base.get("총주차수") or base.get("주차대수"))
    if pk:
        out["주차대수"] = f"{int(pk):,}대" + (f" (세대당 {pk / hh:.2f}대)" if hh else "")
    ua = str(base.get("사용승인일") or (mains[0].get("사용승인일") if mains else "") or "")
    if len(ua) >= 8:
        out["준공일자"] = f"{ua[:4]}.{ua[4:6]}.{ua[6:8]} (사용승인)"
    info = cx.get("단지정보", {}) if isinstance(cx, dict) else {}
    if info:
        for k in ("시공사",):
            if info.get(k):
                out["시공사"] = info[k]
    out["_세대수"] = hh or None
    out["_건물명"] = base.get("건물명") or loc.get("building_name")
    return out


# ---------------------------------------------------------------------------
# 지도 배경
# ---------------------------------------------------------------------------
async def basemap(lat: float, lon: float, zoom: int, w: int, h: int, bm: str = "GRAPHIC") -> dict:
    out = {"lat": lat, "lon": lon, "zoom": zoom, "w": w, "h": h, "png": None}
    if not S.VW_KEY:
        return out
    try:
        r = await S._get(f"{S.VW}/req/image", {
            "service": "image", "request": "getmap", "key": S.VW_KEY, "domain": S.VW_DOMAIN, "format": "png",
            "basemap": bm, "center": f"{lon},{lat}", "crs": "epsg:4326", "zoom": zoom, "size": f"{w},{h}"})
        if "image" in r.headers.get("content-type", ""):
            out["png"] = r.content
    except S.ApiError:
        pass
    return out


async def parcel_polygon(pnu: str | None) -> list[list[tuple[float, float]]]:
    """필지 경계 (lon, lat) 링 목록."""
    if not pnu or not S.VW_KEY:
        return []
    try:
        d = await S.vw_json("/req/data", {"service": "data", "request": "GetFeature", "data": "LP_PA_CBND_BUBUN",
                                          "attrFilter": f"pnu:=:{pnu}", "geometry": "true", "size": 1, "crs": "EPSG:4326"})
        geom = d["response"]["result"]["featureCollection"]["features"][0]["geometry"]
    except (S.ApiError, KeyError, IndexError, TypeError):
        return []
    coords = geom.get("coordinates") or []
    rings: list = []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            return
        if c and isinstance(c[0], list) and c[0] and isinstance(c[0][0], (int, float)):
            rings.append([(float(x), float(y)) for x, y in c])
            return
        for sub in c:
            walk(sub)

    walk(coords)
    return rings


async def aerial_image(loc: dict, w: int, h: int) -> tuple[bytes | None, dict]:
    """슬라이드 칸 비율(w:h)에 맞춘 항공사진 + 지적도(줌 18 이상) + 본 건 필지 빨간 테두리."""
    from report_pptx import merc, draw_aerial
    rings = await parcel_polygon(loc.get("pnu"))
    lat, lon = loc["lat"], loc["lon"]
    zoom = 18
    if rings:
        xs = [x for r in rings for x, _ in r]
        ys = [y for r in rings for _, y in r]
        lon, lat = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        x0, y0 = merc(min(xs), min(ys))
        x1, y1 = merc(max(xs), max(ys))
        for z in (19, 18, 17, 16, 15):
            res = 156543.03392804097 / 2 ** z
            if (x1 - x0) / res <= w * 0.6 and (y1 - y0) / res <= h * 0.6:
                zoom = z
                break
        zoom = min(zoom, 18)
    base = await basemap(lat, lon, zoom, w, h, "PHOTO")
    if not base.get("png"):
        return None, base
    overlay = None
    if zoom >= 18:
        cx, cy = merc(lon, lat)
        res = 156543.03392804097 / 2 ** zoom
        try:
            r = await S._get(f"{S.VW}/req/wms", {
                "service": "WMS", "request": "GetMap", "version": "1.3.0", "key": S.VW_KEY, "domain": S.VW_DOMAIN,
                "layers": "lp_pa_cbnd_bubun", "styles": "lp_pa_cbnd_bubun", "crs": "EPSG:3857",
                "bbox": f"{cx - w / 2 * res},{cy - h / 2 * res},{cx + w / 2 * res},{cy + h / 2 * res}",
                "width": w, "height": h, "format": "image/png", "transparent": "true"})
            if "image" in r.headers.get("content-type", ""):
                overlay = r.content
        except S.ApiError:
            pass
    return draw_aerial(base, overlay, rings), base


def fit_zoom(lat: float, lon: float, pts: list[tuple[float, float]], w: int, h: int, zmin=8, zmax=15, margin=70) -> int:
    from report_pptx import merc
    cx, cy = merc(lon, lat)
    for z in range(zmax, zmin - 1, -1):
        res = 156543.03392804097 / 2 ** z
        ok = True
        for a, b in pts:
            x, y = merc(b, a)
            if abs(x - cx) / res > w / 2 - margin or abs(y - cy) / res > h / 2 - margin:
                ok = False
                break
        if ok:
            return z
    return zmin


def radius_zoom(lat: float, km: float, size: int) -> int:
    for z in range(17, 9, -1):
        res = 156543.03392804097 / 2 ** z
        if km * 1000 / math.cos(math.radians(lat)) / res <= size * 0.44:
            return z
    return 10


# ---------------------------------------------------------------------------
# 문장(초안) 만들기
# ---------------------------------------------------------------------------
def _pct(a: float, b: float) -> float:
    return (b / a - 1) * 100 if a else 0.0


def _trend_word(chg: float) -> str:
    if chg <= -20:
        return "큰 폭으로 감소"
    if chg <= -3:
        return "소폭 감소"
    if chg < 3:
        return "보합 수준을 유지"
    if chg < 20:
        return "소폭 증가"
    return "큰 폭으로 증가"


def short_name(name: str, sgg: str = "") -> str:
    n = re.sub(r"\s*아파트$", "", (name or "").strip())
    head = (sgg.split()[0][:-1] if sgg else "")
    if head and len(head) >= 2 and n.startswith(head) and len(n) > len(head) + 2:
        n = n[len(head):]
    return n.strip()


def _josa(word: str, pair=("와", "과")) -> str:
    if not word:
        return pair[0]
    ch = word[-1]
    if "가" <= ch <= "힣":
        return pair[1] if (ord(ch) - 0xAC00) % 28 else pair[0]
    return pair[0]


def bullets_overview(ov: dict, name: str) -> list[str]:
    addr = (ov.get("대지위치") or "").split(" (")[0]
    scale = ov.get("규모", "")
    m = re.search(r"([\d,]+)세대", scale)
    hh = f" {m.group(1)}세대" if m else ""
    use = "공동주택" if "공동주택" in scale or hh else (scale.split()[0] if scale else "건물")
    done = ov.get("준공일자", "")
    when = f"('{done[2:4]}.{int(done[5:7])}월 준공)" if len(done) >= 7 else ""
    return [f"본 건 자산은 {addr} 소재 **'{name}'** {use}{hh}{when}임"]


def bullets_wide(rows: list[dict]) -> list[str]:
    out = []
    biz = [r for r in rows if r["구분"] == "업무"]
    if biz:
        seoul = any(r["시설"].split()[0] in ("CBD", "GBD", "YBD") for r in biz)
        if seoul:
            s0 = "·".join(f"{r['시설'].split()[0]}({r['시설'].split()[-1]}, 약 {r['거리km']:.1f}km)" for r in sorted(biz, key=lambda r: r["거리km"]))
            out.append(f"서울 주요 업무지구인 {s0}까지 접근 가능한 **수도권 직주근접 입지**임")
        else:
            s0 = "·".join(f"{r['시설'].split()[-1]}({r['시설'].split()[0]}, 약 {r['거리km']:.1f}km)" for r in sorted(biz, key=lambda r: r["거리km"]))
            out.append(f"{s0} 등 **도심 상권·업무지역 접근성**을 갖춘 입지임")
    ind = [r for r in rows if r["구분"] == "산업" and r["거리km"] <= (6 if biz else 12)]
    if ind:
        s = f"{ind[0]['시설']}(직선 약 {ind[0]['거리km']:.1f}km)"
        if len(ind) > 1:
            s += f"·{ind[1]['시설']}(약 {ind[1]['거리km']:.1f}km)"
        out.append(f"본 건 사업지는 {s}{_josa(s.split('(')[0] if len(ind) == 1 else ind[1]['시설'])} 인접하여 "
                   "**산업단지 근로자 중심의 배후 주거수요**가 기대됨")
    tr = [r for r in rows if r["구분"] == "교통"]
    gov = [r for r in rows if r["구분"] in ("행정", "광역")]
    if tr:
        parts = [f"{r['시설']}(약 {r['거리km']:.1f}km)" for r in tr[:3]]
        tail = ""
        if gov:
            g = sorted(gov, key=lambda r: r["거리km"])[0]
            tail = f" {g['시설']}(약 {g['거리km']:.1f}km) 등 도심·광역 접근이 가능한 교통 입지임"
        out.append("·".join(parts) + " 등을 통해" + (tail or " 광역 접근이 가능한 교통 입지임"))
    return out or ["주요 시설까지의 직선거리는 오른쪽 표와 같음"]


def bullets_near(rows: list[dict], radius: float) -> list[str]:
    out = []
    edu = [r for r in rows if r["구분"] == "교육"]
    walk = [r["시설"] for r in edu if r["거리km"] <= 0.5]
    near = [f"{r['시설']}(약 {r['거리km']:.1f}km)" for r in edu if 0.5 < r["거리km"] <= radius]
    if edu:
        s = ""
        if walk:
            s = f"본 건은 {'·'.join(walk)}{'가' if not _josa(walk[-1], ('', '1')) else '이'} 도보권에 있고"
        if near:
            s += (" " if s else "본 건 ") + f"반경 {radius:g}km 내 {'·'.join(near)} 등이 위치하여"
        if s:
            out.append(s + " **교육 인프라 접근성이 양호**함")
    life = [r for r in rows if r["구분"] in ("생활", "의료")]
    nat = [r for r in rows if r["구분"] == "자연"]
    if life or nat:
        s = "·".join(f"{r['시설']}(약 {r['거리km']:.1f}km)" for r in life[:3])
        tail = " 등 생활편의시설"
        if nat:
            tail += f"과 {'·'.join(r['시설'] for r in nat[:2])} 등 자연환경을 함께 갖춘 주거 입지임"
        else:
            tail += "을 이용하기 편리한 주거 입지임"
        out.append((s + tail) if s else tail.strip(" 등"))
    return out or [f"반경 {radius:g}km 내 주요 생활 인프라는 오른쪽 표와 같음"]


def bullets_unsold(u: dict, target_units: int | None) -> list[str]:
    v = u["준공후"]
    lab = [f"'{p[2:4]}.{int(p[4:6])}" for p in u.get("periods", [])] or u["기간"]
    if not v:
        return ["미분양 통계를 불러오지 못했음"]
    out = []
    pre = [x for x in u.get("분양전", []) if x is not None]
    tot = [x for x in u.get("전체", []) if x is not None]
    if not any(v):
        s = f"{u['시군구']} 준공 후 미분양은 {lab[-1]}월 기준 **0세대**로 집계됨"
        if tot and any(tot):
            s += f"(전체 미분양은 {lab[0]}월 {int(tot[0]):,}세대 → {lab[-1]}월 {int(tot[-1]):,}세대)"
        out.append(s)
        if u.get("시도_준공후") and u["시도_준공후"][0] and u["시도_준공후"][-1]:
            a, b = u["시도_준공후"][0], u["시도_준공후"][-1]
            out.append(f"{u['시도']} 전체 준공 후 미분양은 {lab[-1]}월 {int(b):,}세대로 {lab[0]}월 대비 {_pct(a, b):+.0f}%")
        return out
    if not v[0]:
        s = f"{u['시군구']} 준공 후 미분양은 **{lab[0]}월 0세대에서 {lab[-1]}월 {int(v[-1]):,}세대로 증가**"
    else:
        s = (f"{u['시군구']} 준공 후 미분양은 **{lab[0]}월 {int(v[0]):,}세대에서 {lab[-1]}월 {int(v[-1]):,}세대로 "
             f"{_trend_word(_pct(v[0], v[-1]))}**")
    if pre:
        s += ", 분양 전 미분양은 없음" if pre[-1] == 0 else f", 분양 전 미분양은 {int(pre[-1]):,}세대"
    out.append(s)
    if target_units and u.get("본건비중%") is not None:
        out.append(f"본 건 {target_units:,}세대가 **{u['시군구']} 준공 후 미분양의 약 {u['본건비중%']}%**를 차지하는 것으로 "
                   "추정되어, 본 건 해소 시 지역 미분양 물량 감소 효과가 클 것으로 기대됨")
    elif u.get("시도_준공후") and u["시도_준공후"][0] and u["시도_준공후"][-1]:
        a, b = u["시도_준공후"][0], u["시도_준공후"][-1]
        out.append(f"{u['시도']} 전체 준공 후 미분양은 {lab[-1]}월 {int(b):,}세대로 {lab[0]}월 대비 {_pct(a, b):+.0f}%")
    return out


def _range_txt(vals: list[float]) -> str:
    vs = [v for v in vals if v]
    if not vs:
        return "-"
    lo, hi = min(vs), max(vs)
    r = lambda x: int(round(x / 10.0) * 10)
    if r(lo) == r(hi):
        return f"{r(lo):,}만원"
    return f"{r(lo):,}~{r(hi):,}만원"


def bullets_trend(cmp_: dict, scope: str, n_cx: int) -> list[str]:
    out = []
    tr, js = cmp_["매매추이"], cmp_["전세추이"]
    band = cmp_["면적구간"]
    p = [q["평균공급평단가"] for q in tr]
    c = [q["건수"] for q in tr]
    if any(p):
        first = [x for x in p[:4] if x]
        last = [x for x in p[-4:] if x]
        chg = _pct(statistics.mean(first), statistics.mean(last)) if first and last else 0
        word = "상승세" if chg > 5 else ("하락세" if chg < -5 else "보합세")
        cc = [x for x in c if x]
        out.append(f"{scope} 주요 {n_cx}개 단지({band})의 매매가는 '{tr[0]['분기'][:2]}년 이후 "
                   f"**공급 3.3㎡당 {_range_txt(p)} 범위의 {word}**를 보이며, 분기 거래량은 "
                   f"{min(cc) if cc else 0}~{max(cc) if cc else 0}건 수준임")
    jp = [q["평균공급평단가"] for q in js]
    jc = [q["건수"] for q in js]
    if any(jp):
        f0 = next((q for q in js if q["평균공급평단가"]), None)
        recent = [q["평균공급평단가"] for q in js[-4:] if q["평균공급평단가"]]
        s = f"전세가는 '{f0['분기']} 공급 3.3㎡당 {f0['평균공급평단가']:,}만원에서 최근 4개 분기 **{_range_txt(recent)}대**"
        chg = _pct(f0["평균공급평단가"], statistics.mean(recent)) if recent else 0
        s += "로 상승하여 전세 수요가 꾸준함" if chg > 5 else ("로 하락함" if chg < -5 else "로 보합세임")
        if statistics.mean(jc) < 6:
            s += ". 다만 거래량이 적어 개별 거래에 따라 분기별 변동 폭이 큼"
        out.append(s)
    return out or ["비교단지 거래가 적어 추이를 판단하기 어려움"]


def bullets_prices(rows: list[dict], target: dict) -> list[str]:
    out = []
    t = [r for r in rows if r.get("비교표")]
    if not t:
        return ["반경 내 비교 가능한 실거래가 없음"]
    yrs = [full_year(r["준공"][1:3]) for r in t if r.get("준공") and r["준공"].startswith("'")]
    sp = [r["매매공급평단가"] for r in t if r.get("매매공급평단가")]
    jp = [r["전세공급평단가"] for r in t if r.get("전세공급평단가")]
    s = "비교단지는 " + (f"{min(yrs)}~{max(yrs)}년 준공 단지로, " if yrs else "")
    if sp:
        s += f"매매 공급 3.3㎡당 {_range_txt(sp)}(평균 {round(statistics.mean(sp)):,}만원)"
    if jp:
        s += f", 전세 {_range_txt(jp)} 수준"
    out.append(s)
    tp, tj = target.get("매매공급평단가"), target.get("전세공급평단가")
    def yr(r):
        return full_year(r["준공"][1:3]) if r.get("준공", "").startswith("'") else 0
    newest = sorted([r for r in t if r.get("매매공급평단가")], key=yr, reverse=True)
    if newest:
        top_y = yr(newest[0])
        newest = [r for r in newest if yr(r) >= top_y - 2][:3]
    if tp and sp:
        s2 = f"본 건 {target.get('가격구분', '매매가')} **공급 3.3㎡당 {tp:,}만원**은 비교단지 평균 대비 {_pct(statistics.mean(sp), tp):+.0f}%"
        if newest:
            pc = [_pct(r["매매공급평단가"], tp) for r in newest]
            base = re.sub(r"\d+단지$", "", newest[0]["단지명"]) if len(newest) > 1 else newest[0]["단지명"]
            rng = f"{min(pc):+.0f}~{max(pc):+.0f}%" if len(pc) > 1 and round(min(pc)) != round(max(pc)) else f"{pc[0]:+.0f}%"
            s2 += f", 가장 최근 준공된 비교단지({base}, {yr(newest[0])}년) 대비 {rng} 수준임"
        else:
            s2 += " 수준임"
        out.append(s2)
    if target.get("전세추정"):
        e = target["전세추정"]
        lo, hi = e["범위(만원)"]
        out.append(f"본 건 전세가는 **약 {e['추정가(만원)'] / 10000:.2f}억원(공급 3.3㎡당 {e['공급평단가']:,}만원, "
                   f"범위 {lo / 10000:.2f}~{hi / 10000:.2f}억원)**으로 추정됨({e['방법']})")
        tj = None  # 추정치를 다시 비교단지 평균과 비교하지 않음
    if target.get("매매추정") and target.get("가격구분") != "추정 시세" and target.get("매매가"):
        e = target["매매추정"]
        out.append(f"주변 시세로 본 본 건 적정 매매가는 약 {e['추정가(만원)'] / 10000:.2f}억원(공급 3.3㎡당 {e['공급평단가']:,}만원, "
                   f"{e['방법']})으로 {target.get('가격구분', '매매가')} 대비 {_pct(target['매매가'], e['추정가(만원)']):+.0f}% 수준임")
    elif target.get("가격구분") == "추정 시세" and target.get("매매추정"):
        e = target["매매추정"]
        out.append(f"본 건 매매가는 **약 {e['추정가(만원)'] / 10000:.2f}억원(공급 3.3㎡당 {e['공급평단가']:,}만원)**으로 추정됨({e['방법']})")
    if tj and jp:
        s3 = f"본 건 전세가(공급 3.3㎡당 {tj:,}만원)는 비교단지 전세 평균 대비 {_pct(statistics.mean(jp), tj):+.0f}%"
        if target.get("매매가") and target.get("전세가"):
            s3 += f", 매매가 대비 {target['전세가'] / target['매매가'] * 100:.0f}% 수준임"
        else:
            s3 += " 수준임"
        out.append(s3)
    return out


# ---------------------------------------------------------------------------
# 보고서 데이터 조립
# ---------------------------------------------------------------------------
def _fmt_eok(v: float | None, tag: str = "") -> str:
    if not v:
        return "-"
    return f"{v / 10000:.2f}억원" + (f"\n({tag})" if tag else "")


PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic")


def load_photos(paths: list[str], max_n: int = 6) -> tuple[list[tuple[str, bytes]], list[str]]:
    """사용자 사진 파일/폴더 → [(캡션, JPEG bytes)]. 캡션은 파일명(확장자 제외)."""
    from PIL import Image, ImageOps
    files: list[Path] = []
    bad: list[str] = []
    for p in paths:
        q = Path(str(p).strip().strip('"')).expanduser()
        if q.is_dir():
            files += sorted([f for f in q.iterdir() if f.suffix.lower() in PHOTO_EXT])
        elif q.is_file():
            files.append(q)
        elif str(p).strip():
            bad.append(str(p))
    out = []
    for f in files[:max_n]:
        try:
            im = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
            im.thumbnail((1800, 1800))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            cap = re.sub(r"^\d+[_.\-\s]*", "", f.stem) or f.stem
            out.append((cap, buf.getvalue()))
        except Exception:
            bad.append(f.name)
    return out, bad


async def locate_target(location: str, asset_name: str | None) -> dict:
    """주소 → 좌표. 개발로 지번이 없어진 곳(합필·환지)은 자산명·장소검색으로 다시 찾음."""
    errs = []
    tries = [location]
    if asset_name:
        region = " ".join(location.split()[:2])
        tries += [f"{region} {asset_name}", asset_name]
    for q in tries:
        try:
            return await S.ensure_point(await S.resolve(q))
        except S.ApiError as e:
            errs.append(f"{q}: {e}")
    if asset_name:  # 브이월드 장소검색(아파트 단지 POI)
        try:
            items = await S.vw_search(asset_name, "place", size=5)
        except S.ApiError:
            items = []
        region = location.split()[1] if len(location.split()) > 1 else ""
        for it in items:
            addr = (it.get("address") or {}).get("parcel") or ""
            if region and region not in addr:
                continue
            p = it.get("point", {})
            x, y = S.num(p.get("x")), S.num(p.get("y"))
            if x is not None:
                return {"query": location, "level": "place", "address": addr, "lat": y, "lon": x,
                        "building_name": it.get("title"), "source": "브이월드 장소검색(자산명)"}
    raise S.ApiError("위치를 찾지 못했습니다: " + " / ".join(errs[:3]))


def repick_model(presale: dict | None, area_min: float, area_max: float) -> None:
    """비교 면적 구간에 맞는 본 건 주택형(공급면적·분양가)으로 다시 고름."""
    if not presale or not presale.get("주택형"):
        return
    c = (area_min + area_max) / 2
    pick = min(presale["주택형"], key=lambda m: abs(m["전용㎡"] - c))
    if area_min - 1 <= pick["전용㎡"] <= area_max + 1:
        presale.update({"전용㎡": pick["전용㎡"], "공급㎡": pick["공급㎡"], "공급비율": round(pick["공급㎡"] / pick["전용㎡"], 4),
                        "분양가최고(만원)": pick["분양가최고(만원)"], "선택": pick})


def auto_band(models: list[dict] | None) -> tuple[float, float, str]:
    """비교 면적 구간. 거래가 많은 국민평형(59·84)을 우선: 둘 중 하나라도 본 건 비중 20% 이상이면 비중 큰 쪽,
    둘 다 아니면 본 건 주력(세대수 최다) 평형 ±3.5㎡. 59 구간 거래가 부족하면 market_report에서 84로 대체."""
    if not models:
        return 80, 86, "기본(국민평형 84㎡)"
    agg: dict[int, list] = {}
    for x in models:  # 59A·59B는 합쳐서 판단
        k = int(x["전용㎡"])
        agg.setdefault(k, [0, x["전용㎡"]])
        agg[k][0] += int(x.get("세대수") or 0)
    total = sum(v[0] for v in agg.values()) or 1
    share84 = sum(v[0] for k, v in agg.items() if 80 <= k <= 86) / total
    share59 = sum(v[0] for k, v in agg.items() if 57 <= k < 64) / total
    if max(share84, share59) >= 0.2:  # 두 국민평형 중 본 건 비중이 큰 쪽
        if share84 >= share59:
            return 80, 86, f"본 건 84㎡형 비중 {share84 * 100:.0f}%"
        return 57, 64, f"본 건 59㎡형 비중 {share59 * 100:.0f}%"
    a = max(agg.values(), key=lambda v: v[0])[1]
    lo, hi = math.floor(a - 3.5), math.ceil(a + 3.5)
    return lo, hi, f"본 건 주력 {int(a)}㎡"


async def market_report(location: str, radius_km: float = 1.5, area_min: float | None = None, area_max: float | None = None,
                        quarters: int = 12, unsold_months: int = 12, target_unsold_units: int | None = None,
                        target_sale_price: float | None = None, target_jeonse: float | None = None,
                        comparable_names: list[str] | None = None, bullets: dict | None = None,
                        sections: list[str] | None = None, asset_name: str | None = None, start_page: int = 1,
                        out_dir: str | None = None, make_pdf: bool = True, brand: str = "JB Asset Management",
                        photos: list[str] | None = None, target_area: float | None = None) -> dict:
    import report_pptx as R

    warn: list[str] = []
    want = set(sections or ["overview", "wide", "near", "unsold", "trend", "prices"])
    t0 = time.time()
    tm: dict[str, float] = {}

    def lap(k):
        tm[k] = round(time.time() - t0, 1)

    loc = await locate_target(location, asset_name)
    lap("위치")
    lat, lon = loc["lat"], loc["lon"]
    parts = split_address(loc.get("address") or "")
    if not parts["sgg"] or not parts["umd"] or not parts["sido"].endswith(("도", "시")):
        rgc = await reverse_geocode(lat, lon)  # 도로명·단지명 입력이면 좌표로 지번주소를 다시 얻음
        if rgc and rgc.get("text"):
            parts = split_address(rgc["text"])
            if not loc.get("address") or not split_address(loc["address"])["sgg"]:
                loc["address"] = rgc["text"]
            if not loc.get("lawd_cd"):
                loc["lawd_cd"] = (rgc.get("bjd") or "")[:5] or None
    sgg_full = parts["sgg"] or ("세종시" if parts["sido"].startswith("세종") else "")
    band_note = "사용자 지정"
    area_c = 84.9 if (area_min is None or area_max is None or area_min <= 84.9 <= area_max) else (area_min + area_max) / 2

    async def safe(coro, label):
        try:
            return await coro
        except Exception as e:  # 한 부분이 실패해도 나머지는 만듦
            warn.append(f"{label}: {e}")
            return None

    name0 = asset_name or loc.get("building_name") or (loc.get("complex") or {}).get("kaptName") or ""
    ov, wide, near, unsold_d, presale = await asyncio.gather(
        safe(asset_overview(loc), "자산개요") if "overview" in want or "prices" in want else asyncio.sleep(0, None),
        safe(wide_facilities(lat, lon, sgg_full), "광역시설") if "wide" in want else asyncio.sleep(0, None),
        safe(near_facilities(lat, lon, radius_km), "생활시설") if "near" in want else asyncio.sleep(0, None),
        safe(unsold(loc.get("address") or "", loc.get("lawd_cd"), unsold_months, target_unsold_units), "미분양")
        if "unsold" in want else asyncio.sleep(0, None),
        safe(presale_info(name0, area_c, parts["sgg"].split()[0] if parts["sgg"] else "", parts), "분양정보"),
    )
    lap("개요·시설·미분양·분양")
    name = asset_name or (ov or {}).get("_건물명") or (presale or {}).get("주택명") or name0 or "본 건"
    if presale is None and name != name0 and name != "본 건":
        presale = await safe(presale_info(name, area_c, parts["sgg"].split()[0] if parts["sgg"] else "", parts), "분양정보")
    if not asset_name:  # 사용자가 준 자산명은 그대로 둠
        name = short_name(name, sgg_full)
    if area_min is None or area_max is None:
        if target_area:
            lo, hi, band_note = auto_band([{"전용㎡": float(target_area), "세대수": 1}])
            band_note = f"예정가 기준 전용 {target_area:g}㎡"
        elif target_sale_price or target_jeonse:
            lo, hi, band_note = 80, 86, "예정가 기준 84㎡ 가정(target_area 미입력)"
        else:
            lo, hi, band_note = auto_band((presale or {}).get("주택형"))
        area_min = lo if area_min is None else area_min
        area_max = hi if area_max is None else area_max
    area_c = (area_min + area_max) / 2
    repick_model(presale, area_min, area_max)
    cmp_ = None
    if want & {"near", "trend", "prices"}:
        ty = None
        mv = str((presale or {}).get("입주예정월") or "")
        if len(mv) >= 4:
            ty = int(mv[:4])
        elif (ov or {}).get("준공일자"):
            ty = int(str(ov["준공일자"])[:4])
        cmp_ = await safe(comparables(loc, radius_km, quarters, area_min, area_max,
                                      exclude_names=[name, name0, asset_name or ""], include_names=comparable_names,
                                      target_year=ty), "비교단지")
        # 비교단지가 3곳 미만이면 반경을 넓혀 다시(최대 5km) — 지방·택지지구 신축 대비
        for wider in (3.0, 5.0):
            if comparable_names or (cmp_ and len(cmp_.get("단지") or []) >= 3) or wider <= radius_km:
                continue
            c2 = await safe(comparables(loc, wider, quarters, area_min, area_max,
                                        exclude_names=[name, name0, asset_name or ""], target_year=ty), f"비교단지(반경 {wider:g}km)")
            if c2 and len(c2.get("단지") or []) > len((cmp_ or {}).get("단지") or []):
                warn.append(f"반경 {radius_km:g}km 안 비교단지가 부족해 {wider:g}km로 넓혔습니다.")
                cmp_, radius_km = c2, wider
        if (not comparable_names and len((cmp_ or {}).get("단지") or []) < 3 and band_note != "사용자 지정"
                and not (area_min == 80 and area_max == 86)):
            c3 = await safe(comparables(loc, radius_km, quarters, 80, 86, exclude_names=[name, name0, asset_name or ""],
                                        target_year=ty), "비교단지(84㎡)")
            if c3 and len(c3.get("단지") or []) > len((cmp_ or {}).get("단지") or []):
                warn.append(f"전용 {area_min:g}~{area_max:g}㎡ 거래가 적어 거래가 많은 84㎡(80~86㎡) 기준으로 비교했습니다.")
                cmp_, area_min, area_max = c3, 80, 86
                band_note += " → 84㎡로 대체(거래 부족)"
                area_c = 83
                repick_model(presale, 80, 86)
        n_cx = len((cmp_ or {}).get("단지") or [])
        if cmp_ is not None and n_cx < 3 and not comparable_names:
            tried = max([radius_km] + [w for w in (3.0, 5.0) if w > radius_km])
            head = (f"반경 {tried:g}km까지 찾아도 전용 {area_min:g}~{area_max:g}㎡ 거래 단지가 없어 비교표를 비웠습니다. "
                    if n_cx == 0 else
                    f"반경 {tried:g}km까지 찾아도 전용 {area_min:g}~{area_max:g}㎡ 거래 단지가 {n_cx}곳뿐입니다. ")
            warn.append(head + "본 건 시세는 시군구 자료로 보완해 추정했습니다(가격 슬라이드 주석에 근거). "
                               "비교할 단지를 알면 comparable_names로 지정하세요.")
    lap("비교단지")
    cx_rows = (cmp_ or {}).get("단지", [])
    miss = (cmp_ or {}).get("자료누락") or {}
    if miss.get("실거래_실패_월수"):
        warn.append(f"실거래 {miss['요청_월수']}건 중 {miss['실거래_실패_월수']}건을 받지 못했습니다(공공데이터포털 일시 오류). 다시 실행하면 받은 달은 캐시로 건너뜁니다.")
    if miss.get("단지정보_실패"):
        warn.append(f"K-apt 단지정보 {miss['단지정보_실패']}건을 받지 못해 일부 단지의 세대수·시공사가 비어 있을 수 있습니다.")
    for r in cx_rows:
        r["단지명"] = short_name(r["단지명"], sgg_full)

    # 본 건 가격 정보
    ratio_t = (presale or {}).get("공급비율") or 1.32
    t_area = (presale or {}).get("전용㎡") or area_c
    t_supply_py = t_area * ratio_t / PY
    tgt: dict[str, Any] = {"단지명": name}
    if target_sale_price:
        tgt.update({"매매가": target_sale_price, "매매공급평단가": round(target_sale_price / t_supply_py), "가격구분": "예정 매각가",
                    "매매표시": _fmt_eok(target_sale_price, "예정")})
    elif presale and presale.get("분양가최고(만원)"):
        p = presale["분양가최고(만원)"]
        tgt.update({"매매가": p, "매매공급평단가": round(p / t_supply_py), "가격구분": "분양가(최고)",
                    "매매표시": _fmt_eok(p, "분양가")})
    if target_jeonse:
        tgt.update({"전세가": target_jeonse, "전세공급평단가": round(target_jeonse / t_supply_py),
                    "전세표시": _fmt_eok(target_jeonse, "예정")})
    # 시세 추정(예정가가 없을 때): 비교사례 → 시군구 전세가율
    est = estimate_target(tgt, (cmp_ or {}).get("후보") or [], (cmp_ or {}).get("시군구전세가율") or {}, t_supply_py,
                          t_area, ty) if cmp_ else {}
    if est.get("전세") and not tgt.get("전세가"):
        e = est["전세"]
        tgt.update({"전세가": e["추정가(만원)"], "전세공급평단가": e["공급평단가"], "전세표시": _fmt_eok(e["추정가(만원)"], "추정"),
                    "전세추정": e})
    if est.get("매매") and not tgt.get("매매가"):
        e = est["매매"]
        tgt.update({"매매가": e["추정가(만원)"], "매매공급평단가": e["공급평단가"], "가격구분": "추정 시세",
                    "매매표시": _fmt_eok(e["추정가(만원)"], "추정"), "매매추정": e})
    elif est.get("매매"):
        tgt["매매추정"] = est["매매"]  # 분양가가 있어도 시장 추정치는 따로 보여줌
    if cmp_ and not tgt.get("전세가"):
        warn.append("본 건 전세가를 산정할 자료(비교 전세사례·시군구 전세가율·시군구 전세 시세)가 없어 비워 두었습니다. "
                    "target_jeonse로 직접 넣을 수 있습니다.")
    if cmp_ and not tgt.get("매매가"):
        warn.append("본 건 매매가(분양가·예정가·추정 시세)를 정하지 못했습니다. target_sale_price로 직접 넣을 수 있습니다.")

    B = bullets or {}
    sec: dict[str, Any] = {}
    imgs: dict[str, Any] = {}

    # 지도 배경 (동시에)
    map_jobs = {}
    if "near" in want or "prices" in want:
        z = radius_zoom(lat, radius_km, 1024)
        map_jobs["near"] = basemap(lat, lon, z, 1024, 1024)
    if "overview" in want:
        map_jobs["loc"] = basemap(lat, lon, 15, 1024, 514)
    if wide and "wide" in want:
        pts = [(r["lat"], r["lon"]) for r in wide if r["거리km"] <= 35]
        z = fit_zoom(lat, lon, pts + [(lat, lon)], 1024, 868, 8, 15)
        map_jobs["wide"] = basemap(lat, lon, z, 1024, 868)
    got = await asyncio.gather(*map_jobs.values())
    bases = dict(zip(map_jobs.keys(), got))
    if any(b.get("png") is None for b in bases.values()):
        warn.append("일부 배경지도를 불러오지 못해 빈 배경으로 그렸습니다(브이월드 키·네트워크 확인).")
    aerial = None
    if "overview" in want and S.VW_KEY:
        try:
            aerial, _ = await aerial_image(loc, 1024, 544)
        except Exception as e:
            warn.append(f"항공사진: {e}")

    lap("지도")
    target_pt = {"lat": lat, "lon": lon, "name": name}
    sido_short_name = sido_short(parts["sido"])
    # --- 1 자산개요 (건축물대장이 아직 없거나 비면 모집공고로 보완)
    if ov is not None and presale:
        if not ov.get("_세대수") and presale.get("공급세대수"):
            ov["_세대수"] = int(presale["공급세대수"])
            ov.setdefault("규모", f"공동주택 {int(presale['공급세대수']):,}세대")
        mv = str(presale.get("입주예정월") or "")
        if not ov.get("준공일자") and len(mv) >= 6:
            ov["준공일자"] = f"{mv[:4]}.{mv[4:6]} (입주예정)"
    if "overview" in want and ov is not None:
        rows = []
        for k in ("대지위치", "용도지역", "대지면적", "건축면적", "연면적", "건폐율", "용적률", "규모"):
            if ov.get(k):
                rows.append((k, ov[k]))
        if presale and presale.get("주택형"):
            agg: dict[int, int] = {}
            for m in presale["주택형"]:  # 84A·84B…는 하나로 합침
                agg[int(m["전용㎡"])] = agg.get(int(m["전용㎡"]), 0) + int(m.get("세대수") or 0)
            rows.append(("주택형", types_text(agg)))
        for k in ("주차대수", "준공일자"):
            if ov.get(k):
                rows.append((k, ov[k]))
        builder = (presale or {}).get("시공사") or ov.get("시공사")  # 모집공고 우선(K-apt 시공사 칸은 시행·시공이 섞인 경우가 있음)
        if builder:
            rows.append(("시공사", builder))
        sec["overview"] = {
            "title": "01. 투자자산 개요", "subtitle": "1.1 자산개요", "rows": rows,
            "bullets": B.get("overview") or bullets_overview(ov, name),
            "img1": aerial, "img1_title": "항공사진 (지적 경계)",
            "img2": R.draw_plain(bases["loc"], target_pt) if bases.get("loc") else None, "img2_title": "위치도",
            "note": f"*건축물대장(총괄표제부·표제부, {dt.date.today():%y.%m.%d} 조회)" + (" 및 입주자모집공고(청약홈) 기준" if presale else " 기준"),
        }
    # 현장사진(사용자 파일): 개요 슬라이드 1~2칸 + 3장 이상이면 별도 슬라이드
    pics, bad = load_photos(photos or [])
    if bad:
        warn.append("사진을 읽지 못함: " + ", ".join(bad[:5]))
    if pics and "overview" in sec:
        o = sec["overview"]
        o["img2"], o["img2_title"] = o["img1"] or o["img2"], ("항공사진 (지적 경계)" if o["img1"] else o["img2_title"])
        o["img1"], o["img1_title"] = pics[0][1], f"현장사진 – {pics[0][0]}"
        if len(pics) >= 2 and len(pics) < 3:
            o["img2"], o["img2_title"] = pics[1][1], f"현장사진 – {pics[1][0]}"
        if len(pics) >= 3:
            sec["photos"] = {"title": "01. 투자자산 개요", "subtitle": "1.2 현장사진", "photos": pics[:6],
                             "bullets": B.get("photos") or [f"본 건 {name} 현장 사진"]}
    # --- 2 광역
    if "wide" in want and wide is not None:
        wrows = cap_wide(wide)
        sec["wide"] = {
            "title": "02. 입지분석", "subtitle": "2.1 광역 입지분석", "rows": wrows,
            "bullets": B.get("wide") or bullets_wide(wrows),
            "map": R.draw_wide_map(bases["wide"], target_pt, [r for r in wrows]) if bases.get("wide") else None,
            "notes": ["* 브이월드 좌표 기준 직선거리 (IC는 요금소 위치 기준)"],
        }
    # --- 3 협역
    map_near = None
    if bases.get("near") and cmp_ is not None:
        map_near = R.draw_near_map(bases["near"], target_pt, radius_km, cx_rows,
                                   f"기준 단지({name}) 반경 {radius_km:g}km 비교 단지")
    if "near" in want and near is not None:
        nrows = [{"구분": r["구분"], "시설": r["시설"], "거리": dist_label(r["거리km"]), "거리km": r["거리km"]} for r in near]
        sec["near"] = {
            "title": "02. 입지분석", "subtitle": "2.2 협역 입지분석", "rows": nrows,
            "bullets": B.get("near") or bullets_near(nrows, radius_km), "map": map_near,
            "notes": ["* 브이월드 좌표 기준 직선거리 (도보권: 0.5km 이내)",
                      f"* 좌측 지도: 반경 {radius_km:g}km 비교단지 (배경지도 © 브이월드)"],
        }
    # --- 4 미분양
    if "unsold" in want and unsold_d and unsold_d.get("준공후"):
        sg = unsold_d["시군구"]
        mv = str((presale or {}).get("입주예정월") or "")
        last_p = (unsold_d.get("periods") or [""])[-1]
        not_yet = bool(target_unsold_units and len(mv) >= 6 and last_p and mv[:6] > last_p)
        done_last = (unsold_d["준공후"] or [0])[-1] or 0
        if target_unsold_units and (not_yet or done_last < target_unsold_units):
            unsold_d["본건비중%"] = None
        all_zero = not any(unsold_d["준공후"])
        c1 = {"title": f"{sg} 준공 후 미분양 추이", "cats": unsold_d["기간"], "vals": unsold_d["준공후"],
              "source": "출처 : 국토교통부 미분양주택현황보고(KOSIS) 시군구별 미분양 현황"}
        if all_zero and any(unsold_d.get("전체") or []):
            c1 = {"title": f"{sg} 미분양 추이(전체)", "cats": unsold_d["기간"], "vals": unsold_d["전체"],
                  "source": "출처 : 국토교통부 미분양주택현황보고(KOSIS) 시군구별 미분양 현황"}
        if target_unsold_units and unsold_d.get("본건비중%") is not None:
            c2 = {"title": f"{sg} 준공 후 미분양 (본 건 제외, 추정)", "cats": unsold_d["기간"], "vals": unsold_d["본건제외"],
                  "source": f"본 건 {target_unsold_units:,}세대(전 세대 미분양) 제외 추정치"}
        else:
            c2 = {"title": f"{unsold_d['시도']} 준공 후 미분양 추이", "cats": unsold_d["기간"], "vals": unsold_d["시도_준공후"],
                  "source": "출처 : 국토교통부 미분양주택현황보고(KOSIS)"}
        if unsold_d.get("참고"):
            c1["source"] += f" · {unsold_d['참고']}"
        ub = bullets_unsold(unsold_d, target_unsold_units if unsold_d.get("본건비중%") is not None else None)
        if not_yet:
            ub.append(f"본 건({target_unsold_units:,}세대 미분양)은 '{mv[2:4]}.{int(mv[4:6])}월 입주로 KOSIS 통계("
                      f"'{last_p[2:4]}.{int(last_p[4:6])}월 기준)에는 아직 준공 후 미분양으로 반영되지 않았음")
        elif target_unsold_units and done_last < target_unsold_units:
            ub.append(f"본 건 미분양 {target_unsold_units:,}세대는 KOSIS {sg} 준공 후 미분양({int(done_last):,}세대)보다 많아 "
                      "집계 시점·범위 차이를 확인할 필요가 있음")
        sec["unsold"] = {"title": "03. 시장분석", "subtitle": f"3.1 {sg} 미분양 추이" if all_zero else f"3.1 {sg} 준공 후 미분양 추이",
                         "charts": [c1, c2], "bullets": B.get("unsold") or ub}
    # --- 5 거래추이
    n_cx = (cmp_ or {}).get("추이단지수") or len([r for r in cx_rows if r.get("매매건수") or r.get("전세건수")])
    scope = parts["umd"].split()[0] if parts["umd"] else sgg_full
    t_scope = (cmp_ or {}).get("추이범위")
    if "trend" in want and cmp_:
        cats = [q["분기"] for q in cmp_["매매추이"]]
        src = f"출처 : 국토교통부 실거래가 · 반경 {radius_km:g}km {n_cx}개 단지 {cmp_['면적구간']} ({cmp_['기준']})"
        subtitle = f"3.2 {sgg_full} {scope} 매매/전세거래 추이"
        if t_scope:
            scope = sgg_full.split()[-1] if sgg_full else scope
            src = (f"출처 : 국토교통부 실거래가 · {sgg_full} 전체 {n_cx}개 단지 {cmp_['면적구간']}, {t_scope['연식']} "
                   f"(공급면적 = 전용×1.32 가정, {cmp_['기준']})")
            subtitle = f"3.2 {sgg_full} 매매/전세거래 추이(시군구 전체)"
            warn.append(f"반경 안 거래가 적어 매매·전세 추이는 {sgg_full} 전체 같은 면적대({t_scope['연식']}) 거래로 그렸습니다.")
        sec["trend"] = {
            "title": "03. 시장분석", "subtitle": subtitle,
            "bullets": B.get("trend") or bullets_trend(cmp_, scope, n_cx),
            "charts": [
                {"title": f"{scope} 매매거래 추이", "cats": cats, "counts": [q["건수"] for q in cmp_["매매추이"]],
                 "prices": [q["평균공급평단가"] for q in cmp_["매매추이"]], "source": src},
                {"title": f"{scope} 전세거래 추이", "cats": cats, "counts": [q["건수"] for q in cmp_["전세추이"]],
                 "prices": [q["평균공급평단가"] for q in cmp_["전세추이"]], "source": src},
            ]}
    # --- 6 시세
    if "prices" in want and cmp_:
        trows = []
        has_t = bool(tgt.get("매매가") or tgt.get("전세가") or ov)
        if has_t:
            done = (ov or {}).get("준공일자", "")
            trows.append(["", f"{name}\n(본 건)", f"'{done[2:4]}.{done[5:7]}" if len(done) >= 7 else "-",
                          f"{(ov or {}).get('_세대수') or (presale or {}).get('공급세대수') or '-'}",
                          f"{t_area:.0f}㎡", tgt.get("매매표시", "-"),
                          f"{tgt['매매공급평단가']:,}만원" if tgt.get("매매공급평단가") else "-",
                          tgt.get("전세표시", "-"),
                          f"{tgt['전세공급평단가']:,}만원" if tgt.get("전세공급평단가") else "-",
                          ((presale or {}).get("시공사") or (ov or {}).get("시공사") or "-").replace("(주)", "").replace("주식회사", "").strip()])
        sel = [r for r in cx_rows if r.get("비교표")][:7]
        for r in sel:
            trows.append([str(r["no"]), r["단지명"], r["준공"], f"{r['세대수']:,}" if r.get("세대수") else "-",
                          f"{r['전용㎡']}㎡" if r.get("전용㎡") else "-", _fmt_eok(r.get("매매가(만원)")),
                          f"{r['매매공급평단가']:,}만원" if r.get("매매공급평단가") else "-", _fmt_eok(r.get("전세가(만원)")),
                          f"{r['전세공급평단가']:,}만원" if r.get("전세공급평단가") else "-", r.get("시공사") or "-"])
        old = [r for r in sel if (r.get("매매기준개월") or 0) > 12 or (r.get("전세기준개월") or 0) > 12]
        notes = [f"* 실거래 {cmp_['면적구간']} 최근 6개월 중위값(거래 없으면 12→24→36개월로 확대), 공급면적은 K-apt 관리비부과면적/전용면적 비율로 환산"]
        if tgt.get("전세추정"):
            notes.append(f"* 본 건 전세가(추정): {tgt['전세추정']['방법']}")
        if tgt.get("가격구분") == "추정 시세" and tgt.get("매매추정"):
            notes.append(f"* 본 건 매매가(추정): {tgt['매매추정']['방법']}")
        if old:
            notes.append("* 최근 12개월 거래가 없어 기간을 넓힌 단지: " + ", ".join(r["단지명"] for r in old))
        if tgt.get("가격구분") == "분양가(최고)":
            notes.append(f"* 본 건 매매가는 청약홈 분양가(최고가, {presale.get('모집공고일')} 공고) · 공급 {presale.get('공급㎡'):.2f}㎡ 기준")
        sec["prices"] = {"title": "03. 시장분석", "subtitle": "3.3 주변 단지 시세 현황", "label": f"{scope} 주요 단지 시세 현황",
                         "rows": trows, "has_target": has_t, "notes": notes,
                         "bullets": B.get("prices") or bullets_prices(cx_rows, tgt),
                         "map": R.draw_near_map(bases["near"], target_pt, radius_km, cx_rows,
                                                f"기준 단지({name}) 반경 {radius_km:g}km 비교 단지", crop_ratio=0.81)
                         if bases.get("near") else None}

    data = {"asset_name": name, "start_page": start_page, "sections": sec}
    base_dir = Path(out_dir) if out_dir else (S.IMAGE_DIR.parent / "시장입지보고서")
    fn = re.sub(r"[\\/:*?\"<>|\s]+", "_", f"{name}_시장입지분석_{dt.date.today():%y%m%d}")
    pptx_path = base_dir / f"{fn}.pptx"
    R.build_pptx(data, pptx_path, brand)
    lap("PPT")
    pdf_path, how = (None, "요청 안 함")
    if make_pdf:
        pdf_path, how = await asyncio.to_thread(R.to_pdf, pptx_path)
        lap("PDF")
        if not pdf_path:
            warn.append(f"PDF 변환 실패({how}). PPT를 PowerPoint에서 'PDF로 저장'하세요.")

    # 결과 요약 (에이전트가 문장을 다듬어 bullets로 다시 넣을 수 있게)
    used_bullets = {k: v.get("bullets") for k, v in sec.items()}
    return {
        "대상": S.loc_summary(loc), "자산명": name, "PPT": str(pptx_path), "PDF": str(pdf_path) if pdf_path else None,
        "PDF변환": how, "슬라이드": [f"{v['subtitle']}" for v in sec.values()],
        "문장초안(bullets)": used_bullets,
        "광역시설": [{k: r[k] for k in ("구분", "시설", "거리km")} for r in (wide or [])],
        "생활시설": [{k: r[k] for k in ("구분", "시설", "거리km")} for r in (near or [])],
        "비교단지": [{k: r.get(k) for k in ("no", "단지명", "준공", "세대수", "전용㎡", "매매가(만원)", "매매공급평단가", "매매건수",
                                          "전세가(만원)", "전세공급평단가", "전세건수", "시공사", "공급비율", "거리km")} for r in cx_rows],
        "거래추이": {"매매": (cmp_ or {}).get("매매추이"), "전세": (cmp_ or {}).get("전세추이")},
        "미분양": {k: (unsold_d or {}).get(k) for k in ("시도", "시군구", "기간", "준공후", "전체", "본건제외", "본건비중%", "참고")},
        "분양정보": {k: v for k, v in (presale or {}).items() if k != "선택"},
        "비교면적": {"구간": f"{area_min:g}~{area_max:g}㎡", "근거": band_note},
        "시세추정": {"매매": tgt.get("매매추정"), "전세": tgt.get("전세추정"),
                   "시군구전세가율": (cmp_ or {}).get("시군구전세가율")},
        "최종반경km": radius_km,
        "진단": {"비교단지": (cmp_ or {}).get("진단"), "자료누락": (cmp_ or {}).get("자료누락"), "누적초": tm},
        "경고": warn,
    }
