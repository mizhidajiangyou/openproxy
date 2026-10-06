#!/usr/bin/env bash
# 站外被拒的根因验证（2026-10-04 实测）
#
# 结论：403 FreeTierError 与 UA / Referer / Origin 无关，只与「有没有 Zen API key」有关。
# 带任何 Authorization 头都会换成401 AuthError（说明服务端确实在查凭证）；
# 不带凭证时，服务端按「是不是 opencode 自己的客户端」放行 space-bunny-free，
# 其余 9 个免费模型一律回 FreeTierError。
#
# 所以 openproxy 的处境是：**它拿到的是访客身份，而访客只能用 space-bunny-free。**
# 这不是能靠改header 绕过的（header 层已逐一实测，见下）。

set -uo pipefail
BASE="https://opencode.ai/zen/v1/chat/completions"
BODY='{"model":"MODEL","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'

# 用哪个解释器取清单：优先项目 venv，其次 python3。
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  for cand in "$(dirname "$0")/../.venv314/bin/python" "$(dirname "$0")/../.venv/bin/python" python3; do
    if command -v "$cand" >/dev/null 2>&1; then PYTHON_BIN="$cand"; break; fi
  done
fi

probe() {
  # $1=说明$2=模型 $3..=额外 curl 参数
  local label="$1" model="$2"; shift 2
  local body="${BODY/MODEL/$model}"
  local out code
  out=$(curl -s -w '\n%{http_code}' -X POST "$BASE" \
        -H 'Content-Type: application/json' "$@" -d "$body" 2>&1)
  code=$(printf '%s' "$out" | tail -n1)
  printf '%-34s HTTP %s  %s\n' "$label" "$code" \
    "$(printf '%s' "$out" | head -n1 | cut -c1-110)"
}

echo "=== A. 换 header 有没有用（模型 big-pickle） ==="
probe "裸请求"                       big-pickle
probe "UA: opencode/1.0"             big-pickle -H 'User-Agent: opencode/1.0'
probe "UA: OpenCode/1.0"             big-pickle -H 'User-Agent: OpenCode/1.0'
probe "Referer: opencode.ai"         big-pickle -H 'Referer: https://opencode.ai'
probe "Origin: opencode.ai"          big-pickle -H 'Origin: https://opencode.ai'
probe "X-OpenCode-Client: 1"         big-pickle -H 'X-OpenCode-Client: 1'
probe "以上全带（看起来最像官方客户端）" big-pickle \
  -H 'User-Agent: OpenCode/1.0' -H 'Referer: https://opencode.ai' \
  -H 'Origin: https://opencode.ai'   -H 'X-OpenCode-Client: 1'

echo
echo "=== B. 带凭证会怎样（说明服务端确实在查凭证） ==="
probe "Authorization: Bearer sk-fake" big-pickle -H 'Authorization: Bearer sk-fake'

echo
echo "=== C. 逐模型裸请求（哪些站外真的能调） ==="
# 模型名**从清单里取**，不要手写：手写的名字容易过期（我第一版就写了 5 个
# 上游早已不存在的模型，回的是 401 ModelError「not supported」，
# 会被误读成「站外被拒」—— 那是两种完全不同的结论）。
MODELS=$(cd "$(dirname "$0")/.." && "$PYTHON_BIN" -c '
import sys; sys.path.insert(0, "src")
from openproxy.domain import FREE_MODELS
print(" ".join(m.model_id for m in FREE_MODELS))
')
for m in $MODELS; do
  probe "$m" "$m"
done