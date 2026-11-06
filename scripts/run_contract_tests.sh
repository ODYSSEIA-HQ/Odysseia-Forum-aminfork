#!/usr/bin/env bash
# 本地对 /api/v1 跑契约测试（odysseia-hq/api 的 contract-tests）。
#
# 步骤：重建测试库 → alembic upgrade head → 清 Redis 里 site:* 键 → 种子 →
#       FLUXER_FAKE=1 起 uvicorn → npm ci（没装时）→ npm run test:contract。
#
# 用法：
#   scripts/run_contract_tests.sh                       # 全部用例
#   scripts/run_contract_tests.sh tests/01 tests/02     # 只跑部分文件（参数原样传给 vitest）
#   KEEP_SERVER=1 scripts/run_contract_tests.sh         # 跑完不关服务，方便手动调试
#
# 可改的环境变量（括号里是默认值）：
#   API_REPO（/home/user/api）          契约测试仓库根目录（package.json 所在处）
#   CT_DB（odysseia_ct）                 测试库名，每次重建
#   PGHOST_ADMIN（/tmp）、PGUSER_ADMIN（postgres）  建库用的超级用户连接
#   DB_USER（odysseia）、DB_PASSWORD（changeme）、DB_HOST（localhost）、DB_PORT（5432）
#   REDIS_URL（redis://:xunmeng123456%21@localhost:6379/0）
#   PORT（8791）
#   KNOWN_GAPS（docs/site_v1/known-gaps.json）  本路线的已知不一致清单；设为空串表示不用
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_REPO="${API_REPO:-/home/user/api}"
CT_DB="${CT_DB:-odysseia_ct}"
PGHOST_ADMIN="${PGHOST_ADMIN:-/tmp}"
PGUSER_ADMIN="${PGUSER_ADMIN:-postgres}"
DB_USER="${DB_USER:-odysseia}"
DB_PASSWORD="${DB_PASSWORD:-changeme}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
PORT="${PORT:-8791}"
KNOWN_GAPS="${KNOWN_GAPS-$ROOT/docs/site_v1/known-gaps.json}"

export DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${CT_DB}"
export REDIS_URL="${REDIS_URL:-redis://:xunmeng123456%21@localhost:6379/0}"
export FLUXER_FAKE=1
export SITE_REGISTRATION_MODE=open
export SITE_TEST_HOOKS=1
export PYTHONPATH="$ROOT/src"

cd "$ROOT"

echo "==> 重建测试库 $CT_DB"
psql -h "$PGHOST_ADMIN" -U "$PGUSER_ADMIN" -q -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS \"$CT_DB\" WITH (FORCE)" \
  -c "CREATE DATABASE \"$CT_DB\" OWNER \"$DB_USER\""

echo "==> alembic upgrade head"
uv run alembic upgrade head >/dev/null

echo "==> 清 Redis 里 /api/v1 的会话和假令牌（site:*）"
uv run python - <<'PY'
import asyncio, os
from redis.asyncio import from_url

async def main():
    r = from_url(os.environ["REDIS_URL"], decode_responses=True)
    keys = [k async for k in r.scan_iter("site:*")]
    if keys:
        await r.delete(*keys)
    await r.aclose()
    print(f"    删了 {len(keys)} 个键")

asyncio.run(main())
PY

echo "==> 种子"
uv run python scripts/seed_site_v1.py

echo "==> 启动 uvicorn :$PORT（FLUXER_FAKE=1）"
LOG="${TMPDIR:-/tmp}/site_v1_contract_uvicorn.log"
uv run uvicorn api.main:app --host 127.0.0.1 --port "$PORT" --log-level warning >"$LOG" 2>&1 &
SERVER_PID=$!
cleanup() {
  if [ "${KEEP_SERVER:-0}" != "1" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
  else
    echo "服务还在跑：pid $SERVER_PID，日志 $LOG"
  fi
}
trap cleanup EXIT

for _ in $(seq 1 60); do
  if curl -fs "http://127.0.0.1:$PORT/api/v1/site" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "uvicorn 启动失败，日志：$LOG"; cat "$LOG"; exit 1
  fi
  sleep 0.5
done

echo "==> 契约测试（$API_REPO）"
cd "$API_REPO"
if [ ! -d node_modules ]; then npm ci; fi
export BASE_URL="http://127.0.0.1:$PORT/api/v1"
export CONTRACT_HOOKS_URL="http://127.0.0.1:$PORT/__test__"
if [ -n "$KNOWN_GAPS" ]; then export CONTRACT_KNOWN_GAPS="$KNOWN_GAPS"; fi
npm run test:contract -- "$@"
