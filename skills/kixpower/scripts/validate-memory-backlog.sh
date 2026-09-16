#!/usr/bin/env bash
# validate-memory-backlog.sh — harness-backlog.md 生命周期校验（ps1 → python 移植）
# 用法: validate-memory-backlog.sh <PROJECT_ROOT>
exec python3 - "$1" <<'PYEOF'
import hashlib
import re
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
if not root:
    print('memory_backlog: missing ProjectRoot arg')
    sys.exit(2)

backlog = root / '.kixpower' / 'memory' / 'repo' / 'harness-backlog.md'
if not backlog.is_file():
    print('memory_backlog: missing')
    sys.exit(2)

text = backlog.read_text()
lines = text.split('\n')
records = []
i = 0
while i < len(lines):
    m = re.match(r'^\s*-\s+id:\s*(\S+)', lines[i])
    if not m:
        i += 1
        continue
    start = i
    end = len(lines)
    for j in range(i + 1, len(lines)):
        if re.match(r'^\s*-\s+id:\s*\S+', lines[j]) or re.match(r'^##\s+', lines[j]):
            end = j
            break
    records.append({'id': m.group(1), 'text': '\n'.join(lines[start:end])})
    i = end

errors = []
seen = set()
improvement_hashes = {}
for rec in records:
    rid, body = rec['id'], rec['text']
    if rid in seen:
        errors.append(f'duplicate id: {rid}')
        continue
    seen.add(rid)
    m = re.search(r'(?ms)^\s*improvement:\s*(.*?)(?=^\s*[A-Za-z_][\w-]*:|\Z)', body)
    if m:
        val = re.sub(r'\s+', ' ', m.group(1)).strip().strip('"').strip("'")
        if val:
            h = hashlib.sha256(val.encode()).hexdigest()
            if h in improvement_hashes:
                errors.append(f'{rid}: duplicate improvement semantics with {improvement_hashes[h]}')
            else:
                improvement_hashes[h] = rid
    sm = re.search(r'(?m)^\s*status:\s*(candidate|validated|archived)\s*$', body)
    if not sm:
        errors.append(f'{rid}: missing or invalid status')
        continue
    status = sm.group(1)
    for field in ('type', 'problem', 'improvement', 'source', 'evidence', 'eval'):
        if not re.search(r'(?m)^\s*' + re.escape(field) + r':\s*', body):
            errors.append(f'{rid}: missing {field}')
    if status == 'archived' and not re.search(r'(?m)^\s*archive_reason:\s*(rejected|superseded|stale)\s*$', body):
        errors.append(f'{rid}: archived record needs archive_reason')
    if status == 'validated' and not (re.search(r'(?m)kind:\s*trial', body) and re.search(r'(?m)result:\s*pass', body)):
        errors.append(f'{rid}: validated record needs a trial/pass evidence')
    if status == 'candidate' and not re.search(r'(?m)kind:\s*origin', body):
        errors.append(f'{rid}: candidate record needs origin evidence')

print('memory_backlog: valid')
print(f'record_count: {len(records)}')
print(f'legacy_unstructured_records: {len(re.findall(r"(?m)^\s*-\s+\[[^]]+\]", text))}')
if errors:
    print('errors:')
    for e in errors:
        print(f'  - {e}')
    sys.exit(2)
PYEOF
