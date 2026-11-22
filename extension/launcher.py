"""
확장 프로그램 진입점: GitHub에서 최신 코드를 받아(가능하면) 그 코드로 MCP 서버를 실행합니다.
이 파일과 updater.py는 거의 바뀌지 않고, 실제 기능 코드는 agent/(또는 live/<커밋>/)에 있습니다.
"""
import os
import sys
from pathlib import Path

EXT = Path(__file__).resolve().parent
sys.path.insert(0, str(EXT))
os.environ.setdefault("REALESTATE_CACHE_DIR", str(EXT / "cache"))

import updater  # noqa: E402

auto = updater._env("REALESTATE_AUTO_UPDATE", "true").lower() not in ("false", "0", "no", "off")
code = updater.code_dir()
if auto:
    try:
        code, _ = updater.update()
    except Exception as e:  # 업데이트 실패해도 서버는 뜨게
        updater.log(e)
sys.path.insert(0, code)
os.environ["REALESTATE_CODE_DIR"] = code

import server_core  # noqa: E402

server_core.main()
