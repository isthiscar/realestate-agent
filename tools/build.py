"""확장 프로그램(.mcpb) 묶기.

사용: python tools/build.py [--kapt-cache 경로] [--out dist]
- extension/ 의 런처·업데이터·manifest + agent/ 코드(설치본 = 오프라인 대비)를 build/에 모아 mcpb로 묶음
- extension/local_defaults.json(저장소에 올리지 않는 파일)이 있으면 인증키 기본값을 manifest에 넣음
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kapt-cache", default=str(ROOT / "cache" / "kapt_list.json"))
    ap.add_argument("--out", default=str(ROOT / "dist"))
    a = ap.parse_args()

    b = ROOT / "build"
    if b.exists():
        shutil.rmtree(b)
    (b / "agent").mkdir(parents=True)
    (b / "cache").mkdir()
    for f in ("launcher.py", "updater.py", "pyproject.toml", "icon.png", ".mcpbignore"):
        shutil.copy2(ROOT / "extension" / f, b / f)
    for f in (ROOT / "agent").glob("*.py"):
        shutil.copy2(f, b / "agent" / f.name)
    kc = Path(a.kapt_cache)
    if kc.exists():
        shutil.copy2(kc, b / "cache" / "kapt_list.json")
    m = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    ld = ROOT / "extension" / "local_defaults.json"
    if ld.exists():
        for k, v in json.loads(ld.read_text(encoding="utf-8")).items():
            if v and k in m["user_config"]:
                m["user_config"][k]["default"] = v
    (b / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"realestate-kr-{m['version']}.mcpb"
    subprocess.run(["mcpb", "validate", str(b / "manifest.json")], check=True)
    subprocess.run(["mcpb", "pack", str(b), str(target)], check=True)
    print(target)


if __name__ == "__main__":
    main()
