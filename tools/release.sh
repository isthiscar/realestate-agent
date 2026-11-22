#!/usr/bin/env bash
# 배포: 검증 → (껍데기 바뀌었으면) .mcpb 빌드 → GitHub main에 push
#   bash tools/release.sh "변경 요약"
set -euo pipefail
cd "$(dirname "$0")/.."
msg="${1:-update}"
python3 tests/verify.py || { echo "검증 실패 — 배포 중단"; exit 1; }
python3 tools/build.py --kapt-cache agent/cache/kapt_list.json >/dev/null
git add -A
git commit -m "$msg" || true
if git remote get-url origin >/dev/null 2>&1; then
  git push origin main
else
  echo "(원격 저장소 미설정: push 생략)"
fi
