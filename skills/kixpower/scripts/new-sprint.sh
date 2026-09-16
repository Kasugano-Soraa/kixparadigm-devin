#!/usr/bin/env bash
# new-sprint.sh — Kixpower Orchestration：创建新 Sprint 文档骨架（ps1 → bash 移植）
# 用法: new-sprint.sh <PROJECT_ROOT> <SPRINT_NUMBER> <SPRINT_NAME>
set -euo pipefail

PROJECT_ROOT="${1:?usage: new-sprint.sh <PROJECT_ROOT> <SPRINT_NUMBER> <SPRINT_NAME>}"
SPRINT_NUMBER="${2:?missing sprint number}"
SPRINT_NAME="${3:?missing sprint name}"

PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd)"
SPRINT_DIR="$PROJECT_ROOT/docs/sprint-$SPRINT_NUMBER"

if [[ -d "$SPRINT_DIR" ]]; then
  echo "Sprint $SPRINT_NUMBER 目录已存在：$SPRINT_DIR。为保护规划与历史证据，脚本不会覆盖；请使用显式恢复流程。" >&2
  exit 2
fi

if [[ "$SPRINT_NUMBER" -gt 1 ]]; then
  PREV_DONE="$PROJECT_ROOT/docs/sprint-$((SPRINT_NUMBER - 1))/done.md"
  if [[ ! -f "$PREV_DONE" ]]; then
    echo "Sprint $SPRINT_NUMBER 需要前一 Sprint 的最终 done.md：$PREV_DONE" >&2
    exit 2
  fi
  if ! grep -Pzq '(?s)^---\s*\n.*?^status:\s*done\s*$.*?\n---' "$PREV_DONE" 2>/dev/null \
     && ! grep -Pzq '^---.*^status:\s*done\s*$.*?^---' "$PREV_DONE" 2>/dev/null; then
    echo "Sprint $SPRINT_NUMBER 的前一 Sprint done.md 没有 status: done 的最终证据。" >&2
    exit 2
  fi
fi

DOCS_DIR="$PROJECT_ROOT/docs"
mkdir -p "$SPRINT_DIR" "$PROJECT_ROOT/.kixpower/memory/repo"
for f in harness-backlog.md lessons-learned.md; do
  [[ -f "$PROJECT_ROOT/.kixpower/memory/repo/$f" ]] || printf '# %s\n' "$f" > "$PROJECT_ROOT/.kixpower/memory/repo/$f"
done

MARKER="$DOCS_DIR/.kixpower-current-sprint"
HEAD="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || true)"
[[ "$HEAD" =~ ^[0-9a-fA-F]{40}$ ]] || HEAD="null"
TODAY="$(date +%F)"

printf '%s' "$SPRINT_NUMBER" > "$MARKER"

GITIGNORE="$PROJECT_ROOT/.gitignore"
for entry in docs/.kixpower-current-sprint docs/.kixpower-qa-reverify.json docs/.kixpower-qa-session.json; do
  if [[ -f "$GITIGNORE" ]]; then
    grep -qxF "$entry" "$GITIGNORE" || printf '%s\n' "$entry" >> "$GITIGNORE"
  else
    printf '%s\n' "$entry" > "$GITIGNORE"
  fi
done

cat > "$SPRINT_DIR/plan.md" <<EOF
---
schema_version: 5.7
sprint: $SPRINT_NUMBER
title: "$SPRINT_NAME"
status: planning
baseline_commit: $HEAD
---

# Sprint $SPRINT_NUMBER — $SPRINT_NAME

> Producer 必须在交接前填充任务、DAG、target_rules、task_sizing 和 verifiable_gates。
> 空骨架不能进入 Dev/QA，也不会预生成 done.md。

## Sprint Goal

[一句话描述本次交付]

## Prioritized Task List

<!-- Producer 用唯一 task id 替换此占位内容。 -->

## Task DAG

\`\`\`yaml
task_dag:
  nodes: []
  properties:
    max_antichain_width: 0
    critical_path_depth: 0
    coupling_density: 0
    recommended_topology: sequential
    layers: []
\`\`\`

## Task Sizing

\`\`\`yaml
task_sizing:
  derived_commit_budget: 0
  bug_reserve: 1
  hard_cap: 10
  warn_threshold: 0
  max_parallelism: 0
  source: producer-derived
\`\`\`

## Verifiable gates

\`\`\`yaml
verifiable_gates: []
\`\`\`

## What's NOT in This Sprint

| Feature | Reason |
|---|---|
| [cut feature] | [why] |
EOF

cat > "$SPRINT_DIR/progress.md" <<EOF
---
schema_version: 5.7
sprint: $SPRINT_NUMBER
status: planning
last_updated: $TODAY
completed_tasks: 0
total_tasks: 0
blocked_tasks: 0
open_issues: {P0: 0, P1: 0, P2: 0}
artifacts_changed_since_last_observe: []
observe_fingerprint: null
sprint_baseline_sha: $HEAD
dev_self_tests_passed: []
l2_verification_status: pending
l2_verification_passed: []
l2_verified_sha: null
l2_gate_manifest_sha256: null
l2_stash_refs: []
qa_started_sha: null
qa_verified_sha: null
qa_gate_manifest_sha256: null
qa_test_changes: []
ci_pending: false
topology_used: sequential
blast_radius:
  commit_budget: 3
  branch_required: true
  block_force_push: true
  block_destructive_sql: true
---

# Sprint $SPRINT_NUMBER Progress

## Task Status

<!-- Producer 填充任务；Dev 只更新执行状态。 -->

## Blockers

- 无。

## Trace Log

\`\`\`yaml
[]
\`\`\`
EOF

cat > "$SPRINT_DIR/drift-check.md" <<EOF
# Sprint $SPRINT_NUMBER Drift Check

\`\`\`yaml
verification_fidelity: pending
baseline_sha: $HEAD
baseline_source: scaffold
\`\`\`

Producer 在 planning 阶段补充 context drift、error propagation、tech debt 与 verification fidelity。
EOF

echo "✅ Sprint $SPRINT_NUMBER ($SPRINT_NAME) 文档骨架已创建："
echo "   📄 $SPRINT_DIR/plan.md"
echo "   📄 $SPRINT_DIR/progress.md"
echo "   📄 $SPRINT_DIR/drift-check.md"
echo "   📍 active marker: $MARKER"
