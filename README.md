# realestate-kr (부동산 공공데이터 에이전트)

Claude 데스크톱 확장 프로그램(MCP 서버). 주소·아파트명으로 실거래가·공시지가·건축물대장·청약·미분양·지도를 조회하고,
IM의 시장·입지 파트(자산개요·광역/협역 입지·준공 후 미분양·매매/전세 추이·주변 단지 시세)를 PPT/PDF로 만듭니다.

## 폴더
| 경로 | 내용 |
|---|---|
| `agent/` | 실제 기능 코드. **여기를 고쳐 main에 올리면 PC 확장이 자동으로 받아 씀** |
| `agent/server_core.py` | 공공데이터 조회·도구 정의 (바뀌면 Claude 재시작 시 적용) |
| `agent/market.py` | 보고서 데이터(KOSIS 미분양·주변시설·비교단지·거래추이) — 다음 보고서 작성 때 바로 적용 |
| `agent/report_pptx.py` | PPT 렌더러·지도 그리기·PDF 변환 — 다음 보고서 작성 때 바로 적용 |
| `extension/` | 확장 껍데기: `launcher.py`(진입점), `updater.py`(GitHub 업데이트), manifest, pyproject |
| `tools/build.py` | `.mcpb` 묶기 (껍데기가 바뀔 때만 다시 설치 필요) |
| `tests/` | 샌드박스 테스트(브이월드 목업), 시도별 시뮬레이션 메모 |

## 자동 업데이트 동작
1. 확장(2.0 이상)이 켜질 때 `main` 최신 커밋을 확인 → 바뀌었으면 `agent/`만 내려받아 `live/<커밋>/`에서 실행
2. 보고서 작성(`create_market_report`) 때도 3분 간격으로 확인 → `market.py`·`report_pptx.py`는 즉시 교체
3. `agent_update` 도구로 상태 확인·즉시 업데이트
4. 네트워크·토큰 문제 시 마지막으로 받은 코드 → 설치본 순으로 실행

비공개 저장소이므로 확장 설정의 **GitHub 읽기 토큰**(fine-grained, 이 저장소 Contents: Read-only)이 필요합니다.
API 인증키는 저장소에 올리지 않고 확장 설정에만 둡니다(`extension/local_defaults.json`은 .gitignore).

## 새 의존 패키지가 필요할 때
`extension/pyproject.toml`에 추가 → `python tools/build.py` → 새 `.mcpb`를 한 번 설치.
