#!/usr/bin/env bash
# kixparadigm → Devin 安装器
# 用法: ./install.sh [--dir <kixparadigm-devin 目录>]
# 安装目标: ~/.config/devin/ （全局；对所有 Devin 会话生效）
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
if [[ "${1:-}" == "--dir" ]]; then SRC="$(cd "$2" && pwd)"; fi
DEVIN_DIR="${HOME}/.config/devin"
HOOKS_DIR="$DEVIN_DIR/hooks"

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "缺少依赖: $1" >&2; exit 1; }; }
need_cmd python3

mkdir -p "$DEVIN_DIR/skills" "$DEVIN_DIR/agents" "$HOOKS_DIR"

# 1) 认知层全局规则（同名文件会被覆盖；先备份）
if [[ -f "$DEVIN_DIR/AGENTS.md" ]]; then
  cp "$DEVIN_DIR/AGENTS.md" "$DEVIN_DIR/AGENTS.md.bak.$(date +%Y%m%d%H%M%S)"
fi
cp "$SRC/AGENTS.md" "$DEVIN_DIR/AGENTS.md"

# 2) skills
rsync -a --delete "$SRC/skills/" "$DEVIN_DIR/skills/" 2>/dev/null \
  || { rm -rf "$DEVIN_DIR/skills"; cp -r "$SRC/skills" "$DEVIN_DIR/skills"; }

# 3) subagent profiles
cp "$SRC"/agents/*.md "$DEVIN_DIR/agents/"

# 4) 守卫 hook 脚本
cp "$SRC/hooks/kix_guards.py" "$HOOKS_DIR/kix_guards.py"
chmod +x "$HOOKS_DIR/kix_guards.py"

# 5) hooks 注册：合并进 config.json（幂等；保留既有配置）
python3 - "$DEVIN_DIR/config.json" "$HOOKS_DIR" <<'PYEOF'
import json
import sys
from pathlib import Path

cfg_path, hooks_dir = Path(sys.argv[1]), sys.argv[2]
cfg = {}
if cfg_path.is_file():
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception:
        cfg = {}

hooks = cfg.setdefault("hooks", {})

def upsert(event, matcher, command):
    entries = hooks.setdefault(event, [])
    # 去掉旧的 kixparadigm 条目（幂等）
    entries[:] = [e for e in entries
                  if not any("kix_guards.py" in h.get("command", "") for h in e.get("hooks", []))]
    entries.append({
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    })

upsert("PreToolUse", "exec|edit|write|mcp__github__.*", f"python3 {hooks_dir}/kix_guards.py pre")
upsert("PostToolUse", "exec|edit|write", f"python3 {hooks_dir}/kix_guards.py post")
upsert("Stop", "*", f"python3 {hooks_dir}/kix_guards.py stop")
upsert("SessionStart", "*", f"python3 {hooks_dir}/kix_guards.py session")

cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
PYEOF

echo ""
echo "✅ kixparadigm 已安装到 $DEVIN_DIR"
echo "   规则:     AGENTS.md"
echo "   skills:   $(ls "$SRC/skills" | wc -l) 个（含 /kixpower-new /kixpower-import /kixpower-continue /kixpower-review）"
echo "   agents:   $(ls "$SRC/agents" | wc -l) 个 subagent profile"
echo "   hooks:    kix_guards.py（PreToolUse/PostToolUse/Stop）"
echo ""
echo "验证: devin 会话内输入 /kixpower-new 应出现 skill；运行 git push --force 测试应被硬拦。"
