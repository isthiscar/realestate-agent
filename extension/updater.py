"""
GitHub 자동 업데이트 (확장 프로그램 폴더에 고정으로 두는 파일 — 자주 바꾸지 않음)

- 저장소(기본 isthiscar/realestate-agent)의 main 브랜치 최신 커밋을 확인하고,
  바뀌었으면 agent/ 폴더(server_core.py·market.py·report_pptx.py)만 내려받아 live/<커밋>/에 풂.
- 비공개 저장소는 확장 설정의 'GitHub 읽기 토큰'으로 접근 (Contents: Read-only).
- 네트워크·토큰 문제가 있으면 마지막으로 받은 코드, 그것도 없으면 설치본(agent/)으로 실행.
- MCP는 표준출력을 쓰므로 여기서는 print 대신 stderr와 live/status.json에만 기록.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

EXT = Path(__file__).resolve().parent
LIVE = EXT / "live"
BUNDLED = EXT / "agent"
REQUIRED = ("server_core.py", "market.py", "report_pptx.py")


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default).strip()
    return "" if v.startswith("${") else v


REPO = _env("REALESTATE_REPO", "isthiscar/realestate-agent") or "isthiscar/realestate-agent"
BRANCH = _env("REALESTATE_BRANCH", "main") or "main"
TOKEN = _env("GITHUB_TOKEN_REALESTATE")


def log(msg: object) -> None:
    print(f"[realestate-updater] {msg}", file=sys.stderr, flush=True)


def _status(**kw) -> None:
    try:
        LIVE.mkdir(parents=True, exist_ok=True)
        p = LIVE / "status.json"
        cur = {}
        if p.exists():
            cur = json.loads(p.read_text(encoding="utf-8"))
        cur.update(kw, checked_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def status() -> dict:
    try:
        return json.loads((LIVE / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _get(url: str, accept: str = "application/vnd.github+json", timeout: float = 8.0) -> bytes:
    headers = {"Accept": accept, "User-Agent": "realestate-kr-updater", "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def current() -> dict | None:
    try:
        cur = json.loads((LIVE / "current.json").read_text(encoding="utf-8"))
        d = Path(cur["dir"])
        if all((d / f).exists() for f in REQUIRED):
            return cur
    except (OSError, ValueError, KeyError):
        pass
    return None


def code_dir() -> str:
    cur = current()
    return cur["dir"] if cur else str(BUNDLED)


def latest_sha() -> str:
    d = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))
    return d["sha"]


def fetch(sha: str) -> str:
    raw = _get(f"https://api.github.com/repos/{REPO}/zipball/{sha}", accept="application/vnd.github+json", timeout=30)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    dest = LIVE / sha[:12]
    tmp = LIVE / f".tmp-{sha[:12]}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    n = 0
    for name in zf.namelist():
        parts = name.split("/", 1)  # '<owner>-<repo>-<sha>/agent/xxx.py'
        if len(parts) < 2 or not parts[1].startswith("agent/") or name.endswith("/"):
            continue
        rel = parts[1][len("agent/"):]
        if ".." in rel or rel.startswith("/"):
            continue
        out = tmp / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(zf.read(name))
        n += 1
    if not all((tmp / f).exists() for f in REQUIRED):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("저장소 agent/ 폴더에 필요한 파일이 없습니다")
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    (LIVE / "current.json").write_text(json.dumps({"sha": sha, "dir": str(dest), "files": n,
                                                   "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
                                       encoding="utf-8")
    # 오래된 버전 정리(최근 3개만 남김)
    vers = sorted([p for p in LIVE.iterdir() if p.is_dir() and not p.name.startswith(".")], key=lambda p: p.stat().st_mtime)
    for p in vers[:-3]:
        shutil.rmtree(p, ignore_errors=True)
    return str(dest)


def update(force: bool = False) -> tuple[str, bool]:
    """(코드 폴더, 이번에 바뀌었는지). 실패하면 기존 코드 폴더를 돌려줌."""
    cur = current()
    if not TOKEN:
        _status(ok=False, error="GitHub 읽기 토큰이 없습니다(확장 설정). 설치본/마지막 코드로 실행합니다.")
        return code_dir(), False
    try:
        sha = latest_sha()
        if cur and cur.get("sha") == sha and not force:
            _status(ok=True, sha=sha, error=None)
            return cur["dir"], False
        d = fetch(sha)
        _status(ok=True, sha=sha, error=None, updated_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        log(f"업데이트: {sha[:12]}")
        return d, True
    except urllib.error.HTTPError as e:
        msg = {401: "토큰이 잘못됐거나 만료됐습니다", 403: "토큰 권한 부족 또는 호출 제한",
               404: "저장소를 찾을 수 없습니다(토큰에 이 저장소 읽기 권한이 있는지 확인)"}.get(e.code, f"HTTP {e.code}")
        _status(ok=False, error=msg)
        log(msg)
    except Exception as e:  # 네트워크 등
        _status(ok=False, error=f"{type(e).__name__}: {e}")
        log(e)
    return code_dir(), False
