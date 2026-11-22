"""샌드박스 테스트용: 브이월드 대신 고정 응답."""
import hashlib, math
import market as M

TARGET = (36.07438, 128.40691)
# 칠곡 석적읍 주요 단지 실제 대략 좌표(IM 지도 기준 상대 위치)
KNOWN = {"171": (36.0761, 128.4038), "1511": (36.0699, 128.4069), "1337": (36.0693, 128.4047),
         "29-1": (36.0693, 128.4047), "1087": (36.0681, 128.4007), "764": (36.0711, 128.3955),
         "710": (36.0742, 128.3940), "593": (36.0751, 128.3969), "141": (36.0790, 128.4095)}

async def reverse_geocode(lat, lon):
    if lat > 36.0745:
        return {"text": "경상북도 칠곡군 석적읍 중리 1", "bjd": "4785025622", "sido": "경상북도", "sgg": "칠곡군", "umd": "석적읍 중리"}
    return {"text": "경상북도 칠곡군 석적읍 남율리 1", "bjd": "4785025623", "sido": "경상북도", "sgg": "칠곡군", "umd": "석적읍 남율리"}

async def geocode_parcel(addr):
    j = addr.split()[-1]
    if j in KNOWN:
        return KNOWN[j]
    h = int(hashlib.md5(addr.encode()).hexdigest()[:8], 16)
    ang = (h % 360) * math.pi / 180; d = ((h >> 9) % 25) / 10.0
    return TARGET[0] + d * math.cos(ang) / 111, TARGET[1] + d * math.sin(ang) / 90

async def geocode_road(addr):
    return None

def install():
    M.reverse_geocode = reverse_geocode
    M.geocode_parcel = geocode_parcel
    M.geocode_road = geocode_road

FAC = {
 "산업단지": [("구미국가산업단지3단지","시설구역경계 > 산업단지구역",36.09396,128.40953),("구미국가산업단지","시설구역경계 > 산업단지구역",36.09969,128.37239),
            ("구미국가산업단지4단지","시설구역경계 > 산업단지구역",36.13883,128.43943),("구미국가산업단지2단지","시설구역경계 > 산업단지구역",36.12216,128.41050)],
 "요금소": [("남구미요금소","도로시설 > 요금소",36.07216,128.37172),("구미요금소","도로시설 > 요금소",36.11366,128.36088)],
 "역": [("구미역","교통시설 > 철도시설 > 철도/지하철 > 일반철도역",36.1283,128.3310),("사곡역","교통시설 > 철도시설 > 철도/지하철 > 일반철도역",36.1086,128.3605)],
 "김천구미역": [("김천구미역","교통시설 > 철도시설 > 철도/지하철 > 일반철도역",36.1137,128.1803)],
 "동대구역": [("동대구역","교통시설 > 철도시설 > 철도/지하철 > 일반철도역",35.8794,128.6285)],
 "칠곡군청": [("칠곡군청","공공기관 > 군청",35.9956,128.4017)],
 "시청": [("구미시청","공공기관 > 시청",36.1196,128.3443)],
 "초등학교": [("대교초등학교","교육 > 학교 > 초등학교",36.0755,128.4031)],
 "중학교": [("장곡중학교","교육 > 학교 > 중학교",36.0721,128.4092)],
 "고등학교": [("석적고등학교","교육 > 학교 > 고등학교",36.0840,128.4085)],
 "마트": [("롯데슈퍼프레시 칠곡중리점","소매 > 슈퍼마켓",36.0770,128.4120),("석적농협 하나로마트","소매 > 슈퍼마켓",36.0840,128.4050)],
 "행정복지센터": [("석적읍행정복지센터","공공기관 > 읍행정복지센터",36.0830,128.4060)],
 "병원": [("구미강동병원","의료 > 병원 > 종합병원",36.1060,128.4180)],
 "공원": [("경마산공원","공원 > 근린공원",36.0710,128.4010)],
}

async def place_bbox(query, lat, lon, r_km, size=200):
    out=[]
    for k,v in FAC.items():
        if k==query or (query=="역" and k=="역"):
            for t,c,y,x in v:
                d=M.hav(lat,lon,y,x)
                if d<=r_km: out.append({"title":t,"category":c,"road":"","parcel":"","lat":y,"lon":x,"dist":d})
    out.sort(key=lambda r:r["dist"]); return out

async def basemap(lat, lon, zoom, w, h, bm="GRAPHIC"):
    return {"lat": lat, "lon": lon, "zoom": zoom, "w": w, "h": h, "png": None}

def install_all(S):
    install()
    M.place_bbox = place_bbox
    M.basemap = basemap
    async def resolve(q):
        return {"level":"parcel","pnu":"4785025622106470000","bjd_code":"4785025622","lawd_cd":"47850","bun":647,"ji":0,"san":False,
                "jibun":"647","address":"경상북도 칠곡군 석적읍 중리 647","road_address":"경상북도 칠곡군 석적읍 남중리2길 14-2",
                "query":q,"umd":"석적읍 중리"}
    async def ensure_point(loc):
        loc["lat"],loc["lon"]=TARGET; return loc
    async def map_image(*a,**k): raise S.ApiError("mock: no vworld")
    async def land_info(*a,**k): return {"토지특성":{"용도지역1":"제3종일반주거지역","용도지역2":"자연녹지지역"}}
    S.resolve=resolve; S.ensure_point=ensure_point; S.map_image=map_image; S.land_info=land_info
