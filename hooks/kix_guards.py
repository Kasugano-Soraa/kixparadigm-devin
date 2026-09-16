#!/usr/bin/env python3
"""kix-guards — kixparadigm 机械门禁的 Devin 移植版（hook 命令形态）。

从 DSH 进程内 JS 插件（tools/pre-execute 监听器）改写为 Devin lifecycle hook
命令：每个事件一次进程调用，stdin 收 JSON，stdout 出 JSON 控制结果。

用法（由 hooks.v1.json / config.json hooks 段挂载）：
    kix_guards.py pre       # PreToolUse   —— 门禁判定（block / 放行+pending 提醒）
    kix_guards.py post      # PostToolUse  —— 投递 pending 提醒 + spec gate + 记账
    kix_guards.py stop      # Stop         —— 有实现编辑无测试 → 一次提醒
    kix_guards.py session   # SessionStart —— 成员档/命令可见性注入

门禁集（对齐 kix-guards.js v15 定价：硬 deny 仅不可逆破坏，预算线=结算 steer）：
    exec PreToolUse:
      - 灾难命令（rm -rf /、mkfs、dd→/dev、fork bomb、chmod -R / 等）→ block
      - 终端 DB 客户端破坏性 SQL（DROP/TRUNCATE/ALTER、DELETE/UPDATE 无 WHERE）→ block
      - git push --force/-f/--mirror/+ref → block
      - git push 目标 main/master/refs/heads/* → block
      - git commit 在 main/master 分支 → block
      - commit 预算：reflog 1h 窗口 commits ≥ budget → 放行 + 结算提醒（一次/会话）；
        churn（含 amend）≥ 10 → block（失控 fuse）
      - gh 远端破坏性删除（repo delete / api -X DELETE / release delete）→ block
      - 控制平面写（~/.config/devin、~/.devin、~/.agents、插件目录）→ 放行 + 一次提醒
      - 同操作已被 deny → memo 直拒（禁止重复尝试）
    write/edit PreToolUse:
      - 目标路径在控制平面 → 放行 + 一次提醒
    mcp__github__* PreToolUse:
      - create_or_update_file / delete_file / push_files 无 branch 或 branch=main/master → block
    PostToolUse:
      - exec 成功且有 pending → 投递 additionalContext
      - exec 成功且命中测试命令 → 记 test-ran
      - write/edit 成功且是实现文件 → 记 impl-edited；无 spec 契约 → 一次提醒
      - run_subagent 携带 sprint 语境但缺 current_sprint → 一次提醒
    Stop:
      - impl-edited 且无 test-ran → 第一次 block 提醒「交付前验证三问」，之后放行

状态目录：$XDG_STATE_HOME/kix-hooks/<session_id>/（无则 $TMPDIR/kix-hooks-<uid>/<sid>/）。
所有异常 fail-open（exit 0），门禁脚本本身永远不让会话挂掉。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ── 常量（与 kix-guards.js 同源）───────────────────────────────────────────
COMMIT_HARD_CAP = 10
COMMIT_BUDGET_DEFAULT = 3

DB_CLIENTS = {'psql', 'mysql', 'mariadb', 'sqlite3', 'sqlcmd', 'clickhouse-client', 'duckdb'}
SQL_PAYLOAD_FLAGS = {'-c', '--command', '-e', '--execute', '-q', '--query'}

DANGEROUS_GIT = {
    'push', 'commit', 'reset', 'rebase', 'merge', 'cherry-pick', 'revert', 'clean',
    'checkout', 'restore', 'stash', 'branch', 'rm', 'mv', 'gc', 'prune', 'reflog',
    'update-ref', 'symbolic-ref', 'commit-tree', 'fast-import', 'hash-object',
    'replace', 'am', 'apply', 'pull',
}
GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    '-C', '-c', '--config-env', '--exec-path', '--git-dir', '--work-tree',
    '--namespace', '--super-prefix', '--attr-source',
}
GIT_PUSH_VALUE_FLAGS = {
    '-o', '--push-option', '--repo', '--receive-pack', '--exec', '--recurse-submodules',
}

GH_MUTATION_ACTIONS = {
    'pr': {'create', 'merge', 'close', 'reopen', 'ready', 'review', 'edit', 'delete', 'comment'},
    'issue': {'create', 'close', 'reopen', 'edit', 'delete', 'comment', 'pin', 'unpin', 'lock', 'unlock', 'transfer'},
    'repo': {'create', 'fork', 'transfer', 'rename', 'edit', 'archive', 'unarchive', 'delete'},
    'release': {'create', 'edit', 'delete'},
    'branch': {'-d', '-D', 'delete'},
    'run': {'rerun', 'cancel', 'delete'},
    'secret': {'set', 'delete'},
    'variable': {'set', 'delete'},
    'gist': {'create', 'edit', 'delete'},
    'workflow': {'run', 'enable', 'disable'},
}

# Devin 控制平面（等价 DSH 的 ~/.dsh / .agent-presets）
def control_plane_roots():
    home = str(Path.home())
    return [
        f'{home}/.config/devin', f'{home}/.devin', f'{home}/.agents',
        f'{home}/.codeium', f'{home}/.claude', f'{home}/.windsurf',
        '~/.config/devin', '~/.devin', '$home/.config/devin', '$home/.devin',
    ]

CONTROL_PLANE_MODIFY_ANY = {
    'rm', 'del', 'erase', 'rd', 'rmdir', 'remove-item', 'ri',
    'mv', 'move', 'move-item', 'mi', 'ren', 'rename', 'rename-item',
    'touch', 'mkdir', 'md', 'new-item', 'ni', 'chmod', 'chown', 'icacls',
    'attrib', 'set-content', 'sc', 'add-content', 'ac', 'clear-content', 'clc',
    'out-file', 'set-item', 'si', 'tee', 'sed', 'perl',
}
CONTROL_PLANE_DEST_LAST = {
    'cp', 'copy', 'copy-item', 'cpi', 'robocopy', 'install',
    'ln', 'link', 'wget', 'curl', 'iwr', 'invoke-webrequest',
}
DOWNLOAD_OUTPUT_FLAGS = {'-o', '--output', '--output-document', '-outfile', '--outfile'}

TEST_RUN_RE = re.compile(
    r'\b(cargo\s+(test|nextest)|npm\s+(test|run\s+test)|pnpm\s+(test|run\s+test)|yarn\s+test|'
    r'bun\s+test|vitest|jest|pytest|py\.test|go\s+test|make\s+test|ctest|dotnet\s+test|'
    r'mvn\s+(test|verify)|gradle\w*\s+test|tox|nosetests|python\S*\s+-m\s+(pytest|unittest))\b'
)

IMPL_EXT = {
    '.rs', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.py', '.go', '.java',
    '.c', '.cc', '.cpp', '.h', '.hpp', '.rb', '.php', '.swift', '.kt', '.kts',
    '.scala', '.cs', '.lua', '.sh', '.bash', '.sql', '.vue', '.svelte',
}
NON_IMPL_PATH = re.compile(
    r'(^|/)(test|tests|testing|e2e|cypress|docs?|\.github|examples?|fixtures?|__tests__)(/|$)'
    r'|(_test\.|\.test\.|\.spec\.|\.stories\.|\.md$|\.mdx$)', re.I)

# ── 基础工具 ────────────────────────────────────────────────────────────────

def state_dir(sid: str) -> Path:
    base = os.environ.get('XDG_STATE_HOME')
    if base:
        root = Path(base) / 'kix-hooks'
    else:
        root = Path(tempfile.gettempdir()) / f'kix-hooks-{os.getuid()}'
    d = root / re.sub(r'[^\w.-]', '_', sid or 'unknown')
    d.mkdir(parents=True, exist_ok=True)
    return d


def mark(sid: str, name: str, content: str = '1') -> None:
    try:
        (state_dir(sid) / name).write_text(content)
    except OSError:
        pass


def marked(sid: str, name: str) -> bool:
    try:
        return (state_dir(sid) / name).exists()
    except OSError:
        return False


def queue_pending(sid: str, key: str, text: str) -> None:
    try:
        p = state_dir(sid) / 'pending'
        p.mkdir(exist_ok=True)
        (p / key).write_text(text)
    except OSError:
        pass


def drain_pending(sid: str, key: str | None = None):
    """取出 pending 提醒。key=None 取全部（用于 exec 无 key 匹配时的兜底）。"""
    p = state_dir(sid) / 'pending'
    out = []
    try:
        files = [p / key] if key else sorted(p.iterdir())
        for f in files:
            if f.is_file():
                out.append(f.read_text())
                f.unlink()
    except OSError:
        pass
    return out


def deny_memo_get(sid: str, key: str):
    try:
        f = state_dir(sid) / 'deny-memo.json'
        return json.loads(f.read_text()).get(key) if f.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


def deny_memo_set(sid: str, key: str, reason: str) -> None:
    try:
        f = state_dir(sid) / 'deny-memo.json'
        data = json.loads(f.read_text()) if f.exists() else {}
        data[key] = reason
        f.write_text(json.dumps(data, ensure_ascii=False))
    except (OSError, json.JSONDecodeError):
        pass


def out_block(reason: str) -> int:
    print(json.dumps({'decision': 'block', 'reason': reason}, ensure_ascii=False))
    return 2


def out_context(event: str, text: str) -> int:
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': event, 'additionalContext': text}}, ensure_ascii=False))
    return 0


# ── shell 解析（移植 splitShellSegments/shellTokens/leadingCommand）──────────

def split_shell_segments(text: str):
    parts, cur, pending_sep = [], '', None
    quote, escaped = None, False
    heredocs = []
    s = text or ''

    def flush():
        nonlocal cur, pending_sep
        v = cur.strip()
        if v:
            parts.append({'text': v, 'sep': pending_sep})
        cur, pending_sep = '', None

    def consume_heredoc(frm, tag, strip_tabs):
        i = frm
        while i <= len(s):
            ls = i
            while i < len(s) and s[i] not in '\n\r':
                i += 1
            line = s[ls:i]
            if strip_tabs:
                line = line.lstrip('\t')
            if line == tag:
                if s[i:i + 2] == '\r\n':
                    return i + 2
                return i + 1 if i < len(s) else i
            if i >= len(s):
                return i
            i += 2 if s[i:i + 2] == '\r\n' else 1
        return i

    i = 0
    while i < len(s):
        ch = s[i]
        if quote:
            cur += ch
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            cur += ch
            i += 1
            continue
        if ch == '#' and (cur == '' or i == 0 or s[i - 1].isspace()):
            while i < len(s) and s[i] not in '\n\r':
                i += 1
            continue
        if ch == '\\' and i + 1 < len(s):
            cur += ch + s[i + 1]
            i += 2
            continue
        if ch == '<' and s[i + 1:i + 2] == '<' and s[i + 2:i + 3] != '<':
            cur += '<<'
            i += 2
            strip_tabs = False
            if s[i:i + 1] == '-':
                strip_tabs = True
                cur += '-'
                i += 1
            while i < len(s) and s[i] in ' \t':
                cur += s[i]
                i += 1
            tag = ''
            if i < len(s) and s[i] in "'\"":
                q = s[i]
                cur += q
                i += 1
                while i < len(s) and s[i] != q:
                    tag += s[i]
                    cur += s[i]
                    i += 1
                if i < len(s):
                    cur += q
                    i += 1
            else:
                while i < len(s) and not s[i].isspace() and s[i] not in ';&|':
                    tag += s[i]
                    cur += s[i]
                    i += 1
            if tag:
                heredocs.append((tag, strip_tabs))
            continue
        if ch in ';\n\r':
            flush()
            pending_sep = ';'
            if ch in '\n\r' and heredocs:
                if s[i:i + 2] == '\r\n':
                    i += 1
                pos = i + 1
                while heredocs:
                    tag, st = heredocs.pop(0)
                    pos = consume_heredoc(pos, tag, st)
                i = pos - 1
            i += 1
            continue
        if ch == '&' and s[i + 1:i + 2] == '&':
            flush()
            pending_sep = '&&'
            i += 2
            continue
        if ch == '|' and s[i + 1:i + 2] == '|':
            flush()
            pending_sep = '||'
            i += 2
            continue
        if ch == '|':
            flush()
            pending_sep = '|'
            i += 1
            continue
        cur += ch
        i += 1
    flush()
    return parts


def shell_tokens(segment: str):
    tokens, cur, quote, escaped = [], '', None, False
    s = segment or ''
    i = 0
    while i < len(s):
        ch = s[i]
        if quote:
            if escaped:
                cur += ch
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == quote:
                quote = None
            else:
                cur += ch
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == '#' and (i == 0 or s[i - 1].isspace()):
            break
        if ch == '\\' and i + 1 < len(s) and re.match(r'[\s\'"\\|&;<>#*?(){}\[\]$`!]', s[i + 1]):
            cur += s[i + 1]
            i += 2
            continue
        if ch.isspace():
            if cur:
                tokens.append(cur)
                cur = ''
            i += 1
            continue
        cur += ch
        i += 1
    if cur:
        tokens.append(cur)
    return tokens


def command_basename(token: str) -> str:
    v = (token or '').strip('\'"')
    v = re.sub(r'\.exe$', '', v, flags=re.I)
    return re.split(r'[\\/]', v)[-1].lower()


def leading_command(tokens):
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', t) or re.match(r'^(sudo|doas|env|command|time|nice|ionice|noglob|builtin)$', t, re.I):
            i += 1
            continue
        break
    while i < len(tokens) and tokens[i].startswith('-'):
        i += 2 if (i + 1 < len(tokens) and not tokens[i + 1].startswith('-')) else 1
    if i >= len(tokens):
        return None
    return {'name': command_basename(tokens[i]), 'args': tokens[i + 1:]}


# ── SQL 判定 ────────────────────────────────────────────────────────────────

def strip_sql_noise(text: str) -> str:
    t = re.sub(r"'(?:''|[^'])*'", ' ', text or '')
    t = re.sub(r'"(?:""|[^"])*"', ' ', t)
    t = re.sub(r'/\*[\s\S]*?\*/', ' ', t)
    t = re.sub(r'(?:--|#)[^\r\n]*', ' ', t)
    return t


def is_destructive_sql(text: str) -> bool:
    for stmt in strip_sql_noise(text).split(';'):
        if re.search(r'\b(?:drop|truncate|alter)\b', stmt, re.I):
            return True
        if (re.search(r'\bdelete\b[^;]*?\bfrom\b', stmt, re.I)
                or re.search(r'\bupdate\b[^;]*?\bset\b', stmt, re.I)) \
                and not re.search(r'\bwhere\b', stmt, re.I):
            return True
    return False


def extract_sql_payload(tokens):
    for i, raw in enumerate(tokens):
        m = re.match(r'^(--[a-z-]+)=(.*)$', raw, re.I)
        if m and m.group(1).lower() in SQL_PAYLOAD_FLAGS:
            return m.group(2)
        if raw.lower() in SQL_PAYLOAD_FLAGS:
            if i + 1 < len(tokens) and not tokens[i + 1].startswith('-'):
                return tokens[i + 1]
    return None


def is_terminal_destructive_sql(text: str) -> bool:
    parts = split_shell_segments(text)
    for i, part in enumerate(parts):
        tokens = shell_tokens(part['text'])
        cmd = leading_command(tokens)
        if not cmd or cmd['name'] not in DB_CLIENTS:
            continue
        payload = extract_sql_payload(tokens[1:])
        if payload is not None:
            if is_destructive_sql(payload):
                return True
            continue
        if part['sep'] == '|':
            prev_tokens = shell_tokens(parts[i - 1]['text'])
            prev = leading_command(prev_tokens)
            if prev and re.match(r'^(echo|printf|cat|head|tail)$', prev['name']):
                # echo/printf 的参数即 SQL payload；不能剥引号，直接对原始 token 判定
                if any(is_destructive_sql(t) for t in prev_tokens[1:]):
                    return True
    return False


# ── git 判定 ────────────────────────────────────────────────────────────────

def git_invocations(text: str):
    out = []
    for part in split_shell_segments(text):
        cmd = leading_command(shell_tokens(part['text']))
        if not cmd or cmd['name'] != 'git':
            continue
        tokens = cmd['args']
        for i, t in enumerate(tokens):
            if t == '--':
                continue
            if t in GIT_GLOBAL_OPTIONS_WITH_VALUE:
                if i + 1 < len(tokens):
                    i += 1
                continue
            if re.match(r'^-(?:C|c).+', t) or t.startswith('--') or t.startswith('-'):
                continue
            out.append({'sub': t, 'args': tokens[i + 1:]})
            break
    return out


def git_subcommands(text: str):
    return {inv['sub'] for inv in git_invocations(text)}


def is_git_write(text: str) -> bool:
    if not re.search(r'\bgit(?:\.exe)?\b', text):
        return False
    return bool(git_subcommands(text) & DANGEROUS_GIT)


def is_destructive_git_local(text: str) -> str | None:
    """丢弃未提交工作或引用对象的本地破坏性 git 操作（不可逆）。"""
    for inv in git_invocations(text):
        sub, args = inv['sub'], inv['args']
        argstr = ' '.join(args)
        if sub == 'reset' and '--hard' in args:
            return 'git reset --hard 丢弃全部未提交修改'
        if sub == 'clean' and any(a.startswith('-') and ('f' in a or 'd' in a or 'x' in a)
                                  for a in args):
            return 'git clean -f/-d 删除未跟踪文件'
        if sub == 'checkout' and ('--' in args or '.' in args):
            return 'git checkout -- 丢弃工作区修改'
        if sub == 'restore' and any(a in ('.', '--worktree', '-W') for a in args):
            return 'git restore 丢弃工作区修改'
        if sub == 'branch' and '-D' in args:
            return 'git branch -D 强删分支'
        if sub == 'stash' and args and args[0] in ('drop', 'clear'):
            return 'git stash drop/clear 丢弃暂存'
        if sub == 'reflog' and 'expire' in args:
            return 'git reflog expire 清除引用日志'
        if sub == 'push' and ('--delete' in args or '-d' in args):
            return 'git push --delete 删除远端分支'
        if sub == 'worktree' and args and args[0] == 'remove' and '--force' in args:
            return 'git worktree remove --force'
    return None


def foreach_git_push_arg(args, visit):
    for i, t in enumerate(args):
        t = str(t)
        base = t.split('=', 1)[0] if '=' in t else t
        if t in GIT_PUSH_VALUE_FLAGS or base in GIT_PUSH_VALUE_FLAGS \
                or base in ('--force-with-lease', '--signed'):
            continue
        if visit(t):
            return True
    return False


def is_force_push(text: str) -> bool:
    def hit(t):
        if t in ('--force', '--force=true', '--force=1', '--mirror'):
            return True
        if re.match(r'^-[a-zA-Z0-9]+$', t) and 'f' in t and t != '--follow-tags':
            return True
        return t.startswith('+') and len(t) > 1 and not re.search(r'\s', t)
    for inv in git_invocations(text):
        if str(inv['sub']).lower() == 'push' and foreach_git_push_arg(inv['args'], hit):
            return True
    return False


def push_targets_protected_ref(text: str) -> bool:
    def hit(t):
        if t == '--all':
            return True
        if re.search(r'\s', t):
            return False
        if re.match(r'^refs/heads/(?:main|master)$', t):
            return True
        return bool(re.search(r'(?:^|:)(?:refs/heads/)?(?:main|master)$', t)) and not t.startswith('-')
    for inv in git_invocations(text):
        if str(inv['sub']).lower() == 'push' and foreach_git_push_arg(inv['args'], hit):
            return True
    return False


# ── gh CLI 判定 ─────────────────────────────────────────────────────────────

def gh_invocations(text: str):
    out = []
    for part in split_shell_segments(text):
        cmd = leading_command(shell_tokens(part['text']))
        if cmd and cmd['name'] == 'gh':
            out.append(cmd['args'])
    return out


def gh_entity_action(args):
    i = 0
    while i < len(args):
        t = args[i]
        if t.startswith('-'):
            if t.startswith('--') and '=' in t:
                i += 1
            elif i + 1 < len(args) and not str(args[i + 1]).startswith('-'):
                i += 2
            else:
                i += 1
            continue
        entity = str(t).lower()
        action = str(args[i + 1]).lower() if i + 1 < len(args) and not str(args[i + 1]).startswith('-') else None
        return {'entity': entity, 'action': action}
    return None


def gh_api_write_method(args):
    hit = gh_entity_action(args)
    if not hit or hit['entity'] != 'api':
        return None
    for i, t in enumerate(args):
        if t in ('-X', '--method') and i + 1 < len(args):
            return str(args[i + 1]).upper()
        m = re.match(r'^--method=(.+)$', str(t), re.I)
        if m:
            return m.group(1).upper()
    return None


def is_gh_destructive(text: str) -> bool:
    for args in gh_invocations(text):
        hit = gh_entity_action(args)
        if hit and hit['action'] == 'delete' and hit['entity'] in ('repo', 'release'):
            return True
        if gh_api_write_method(args) == 'DELETE':
            return True
    return False


# ── 灾难命令（硬 deny，仅限不可逆系统级破坏；0% 误报纪律）─────────────────────

CATASTROPHIC_LITERAL = re.compile(r':\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:')
BLOCK_DEVICES = re.compile(r'/dev/(sd[a-z]+|nvme\w+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|disk\d+)')


def is_catastrophic(text: str) -> str | None:
    if CATASTROPHIC_LITERAL.search(text):
        return 'fork bomb'
    for part in split_shell_segments(text):
        tokens = shell_tokens(part['text'])
        cmd = leading_command(tokens)
        if not cmd:
            continue
        name, args = cmd['name'], cmd['args']
        joined = ' '.join(tokens)
        if name.startswith('mkfs') or name in ('fdisk', 'wipefs', 'shred'):
            return f'磁盘级命令 {name}'
        if name == 'dd' and any(re.match(r'^of=', a) and BLOCK_DEVICES.search(a) for a in args):
            return 'dd 写块设备'
        if name == 'rm':
            flags = ''.join(a for a in args if a.startswith('-'))
            if 'r' in flags or 'R' in flags or '--recursive' in args or '--no-preserve-root' in args:
                for a in args:
                    if a.startswith('-'):
                        continue
                    clean = a.rstrip('/') or '/'
                    if clean in ('/', '/*', '~', '~/*', '$HOME', '$HOME/*', '/home',
                                 '/etc', '/usr', '/var', '/bin', '/sbin', '/lib', '/boot',
                                 '/root', '*') or clean.endswith('/*') and clean[:-2] in ('', '~', '$HOME', '/home', '/root'):
                        return f'rm -rf 系统/用户根目录 {a}'
        if name in ('chmod', 'chown') and any(a == '/' or a == '/*' for a in args) \
                and any('R' in a for a in args if a.startswith('-')):
            return f'{name} -R /'
        if name == 'mv' and args and args[-1] == '/dev/null':
            return 'mv → /dev/null'
    if re.search(r'>\s*' + BLOCK_DEVICES.pattern, text):
        return '重定向写块设备'
    return None


# ── 控制平面判定 ────────────────────────────────────────────────────────────

def targets_control_plane(path: str) -> bool:
    low = (path or '').lower().replace('\\', '/')
    return any(root.lower() in low for root in control_plane_roots())


def redirect_targets_control_plane(text: str) -> bool:
    for m in re.finditer(r'(?:[12]?>>?|&>)\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s;&|]+))', text):
        target = m.group(1) or m.group(2) or m.group(3)
        if target and targets_control_plane(target):
            return True
    return False


def download_output_target(args):
    for i, raw in enumerate(args):
        if raw.lower() in DOWNLOAD_OUTPUT_FLAGS and i + 1 < len(args):
            return args[i + 1]
        m = re.match(r'^(--output|--output-document|--outfile)=(.*)$', raw, re.I)
        if m:
            return m.group(2)
    return None


def last_non_flag_arg(args):
    for a in reversed(args):
        if not a.startswith('-'):
            return a
    return None


def is_terminal_control_plane_write(text: str) -> bool:
    if redirect_targets_control_plane(text):
        return True
    for part in split_shell_segments(text):
        cmd = leading_command(shell_tokens(part['text']))
        if not cmd:
            continue
        name, args = cmd['name'], cmd['args']
        if name in CONTROL_PLANE_MODIFY_ANY:
            if any(targets_control_plane(a) for a in args):
                return True
            continue
        if name in ('wget', 'curl', 'iwr', 'invoke-webrequest'):
            out = download_output_target(args)
            if out and targets_control_plane(out):
                return True
            continue
        if name in CONTROL_PLANE_DEST_LAST:
            dest = last_non_flag_arg(args)
            if dest and targets_control_plane(dest):
                return True
            if name in ('mv', 'move', 'move-item', 'mi') \
                    and any(targets_control_plane(a) for a in args):
                return True
            continue
        if name == 'git' and args and args[0].lower() == 'clone':
            dest = last_non_flag_arg(args[1:])
            if dest and targets_control_plane(dest):
                return True
    return False


# ── commit 预算（移植 resolveCommitBudget / reflog 计数 / sprint 上下文）──────

def count_reflog_commits(reflog_text: str):
    commits = churn = 0
    for s in (reflog_text or '').split('\n'):
        s = s.strip()
        if not s:
            continue
        if s.startswith('commit'):
            churn += 1
        if s.startswith('commit:') or s.startswith('commit (initial):'):
            commits += 1
    return commits, churn


def repo_root_from_text(text: str):
    for part in split_shell_segments(text):
        cmd = leading_command(shell_tokens(part['text']))
        if not cmd:
            continue
        if cmd['name'] == 'git':
            tokens = cmd['args']
            i = 0
            while i < len(tokens):
                t = tokens[i]
                if t == '--':
                    break
                if t == '-C' and i + 1 < len(tokens):
                    return tokens[i + 1]
                if t.startswith('-C') and len(t) > 2:
                    return t[2:]
                if t in GIT_GLOBAL_OPTIONS_WITH_VALUE:
                    i += 2
                    continue
                if t.startswith('-'):
                    i += 1
                    continue
                break
            continue
        if cmd['name'] == 'cd' and cmd['args']:
            return cmd['args'][0]
    return None


def resolve_repo_root(text: str):
    return repo_root_from_text(text) or os.environ.get('DEVIN_PROJECT_DIR') or os.getcwd()


def git_read(repo_root: str, args):
    try:
        r = subprocess.run(['git', *args], cwd=repo_root, timeout=5,
                           capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def active_sprint_dir(docs_root: Path, current_sprint: int):
    try:
        sprints = sorted(
            ((e.name, int(e.name[7:])) for e in docs_root.iterdir()
             if e.is_dir() and re.match(r'^sprint-\d+$', e.name)),
            key=lambda x: -x[1])
    except OSError:
        return None, False
    if not sprints:
        return None, False
    if current_sprint > 0:
        for name, n in sprints:
            if n == current_sprint:
                if (docs_root / name / 'done.md').is_file():
                    break  # marker 指向已完结 sprint → 回退最大编号
                return name, False
                break
    name = sprints[0][0]
    stale = (docs_root / name / 'done.md').is_file()
    return name, stale


def read_sprint_context(repo_root: str):
    docs = Path(repo_root) / 'docs'
    current = 0
    try:
        n = int((docs / '.kixpower-current-sprint').read_text().strip())
        if n > 0:
            current = n
    except (OSError, ValueError):
        pass
    name, stale = active_sprint_dir(docs, current)
    if not name:
        return {}
    out = {'sprintDir': name, 'staleAll': stale}
    for f, key in (('progress.md', 'progressMd'), ('plan.md', 'planMd')):
        try:
            out[key] = (docs / name / f).read_text()
        except OSError:
            pass
    return out


def resolve_commit_budget(progress_md, plan_md):
    if progress_md:
        m = re.search(r'^---[\s\S]*?blast_radius:[\s\S]*?commit_budget:\s*(\d+)', progress_md)
        if m:
            return int(m.group(1)), 'progress.md blast_radius.commit_budget'
    if plan_md:
        m = re.search(r'task_sizing:[\s\S]*?derived_commit_budget:\s*(\d+)', plan_md)
        if m:
            return int(m.group(1)), 'plan.md task_sizing.derived_commit_budget'
        m = re.search(r'blast_radius:[\s\S]*?max_commits:\s*(\d+)', plan_md)
        if m:
            return int(m.group(1)), 'plan.md blast_radius.max_commits'
    return COMMIT_BUDGET_DEFAULT, f'冷启动默认 {COMMIT_BUDGET_DEFAULT}'


def budget_steer_message(commits, budget, source, sprint_dir, stale):
    stale_note = f'；注意：预算基线来自已完结的 {sprint_dir or "sprint"}' if stale else ''
    return (f'BLAST RADIUS 结算提醒（本会话一次）：commit 已放行——最近 1 小时窗口内 '
            f'commits={commits}，预算 {budget}（来源：{sprint_dir + " 的 " if sprint_dir else ""}'
            f'{source}{stale_note}）。请在收尾前对账其一：① 迭代节奏真实变快（如 CI 修复链）→ '
            f'重算 commit_budget 并同步 progress.md frontmatter；② 预算合理而提交超速 → '
            f'收敛提交粒度或拆分 Sprint。硬上限 {COMMIT_HARD_CAP} 次/小时（含 amend）仍直接拦截。')


CONTROL_PLANE_REMIND = (
    'kix-guards: 正在改写 Devin 控制平面（~/.config/devin / ~/.devin / 插件目录）。'
    '自迭代或用户已授权时可继续；改完请用新会话验证加载，勿把安装副本当源仓库提交。')

CONTROL_PLANE_KEY = 'control-plane'
BUDGET_STEER_KEY = 'budget-steer'


def norm_key(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '').strip().lower())


# ── PreToolUse ──────────────────────────────────────────────────────────────

def pre_event(payload) -> int:
    tool = (payload.get('tool_name') or '')
    args = payload.get('tool_input') or {}
    sid = payload.get('session_id') or ''
    tool_low = tool.lower()
    text = args.get('command') or args.get('cmd') or ''
    if not isinstance(text, str):
        text = ' '.join(str(a) for a in args.get('argv', [])) if isinstance(args.get('argv'), list) else ''

    # deny memo：同操作已被拒 → 直拒
    memo_key = None
    if tool == 'exec' and text:
        memo_key = 'term::' + norm_key(text)
    elif tool_low in ('write', 'edit', 'apply_patch', 'notebook_edit'):
        p = args.get('file_path') or args.get('path')
        if isinstance(p, str):
            memo_key = 'edit::' + norm_key(p)
    elif tool.startswith('mcp__github__'):
        memo_key = 'ghub::' + tool + '::' + norm_key(json.dumps(args, sort_keys=True, default=str))
    if memo_key:
        prev = deny_memo_get(sid, memo_key)
        if prev:
            return out_block(f'BLAST RADIUS: 该操作此前已被拒绝（{prev}）。禁止重复尝试；'
                             f'如确需执行，请向用户说明原因并等待其明确指示。')

    def deny(reason):
        if memo_key:
            deny_memo_set(sid, memo_key, reason)
        return out_block(reason)

    # ── exec 终端门禁 ──
    if tool == 'exec' and text:
        cat = is_catastrophic(text)
        if cat:
            return deny(f'BLAST RADIUS: 灾难性命令（{cat}）已拦截——不可逆系统破坏。'
                        f'如确需执行请由用户手动执行。')
        if is_terminal_destructive_sql(text):
            return deny('BLAST RADIUS: 终端数据库客户端中的破坏性 SQL'
                        '（DELETE/UPDATE without WHERE / DROP/TRUNCATE/ALTER）已拦截。'
                        '请改用结构化工具或先在事务/只读副本中验证。')
        dgit = is_destructive_git_local(text)
        if dgit:
            return deny(f'BLAST RADIUS: {dgit}——不可逆丢弃本地修改，已拦截。'
                        f'如确需执行，先 stash/备份或由用户手动执行。')
        if is_git_write(text):
            if is_force_push(text):
                return deny('BLAST RADIUS: git push --force 会重写远端历史。'
                            '需用户明确确认；优先使用 --force-with-lease 或 git revert。')
            if push_targets_protected_ref(text):
                return deny('BLAST RADIUS: 禁止直接 push 到 main/master。'
                            '请推送 feature 分支并通过 PR 合并。')
            if 'commit' in git_subcommands(text):
                rc = check_git_commit(text, sid)
                if rc is not None:
                    return rc
        if is_terminal_control_plane_write(text):
            if not marked(sid, 'cp-reminded'):
                queue_pending(sid, CONTROL_PLANE_KEY, CONTROL_PLANE_REMIND)
        if is_gh_destructive(text):
            return deny('BLAST RADIUS: gh 破坏性操作（repo delete / api DELETE / release delete）'
                        '会删除远程数据，禁止执行。')

    # ── write/edit 控制平面 ──
    if tool_low in ('write', 'edit', 'apply_patch', 'notebook_edit'):
        p = args.get('file_path') or args.get('path') or ''
        if isinstance(p, str) and targets_control_plane(p):
            if not marked(sid, 'cp-reminded'):
                queue_pending(sid, CONTROL_PLANE_KEY, CONTROL_PLANE_REMIND)

    # ── MCP GitHub 远程写保护 ──
    if tool.startswith('mcp__github__'):
        decision = check_github_write(tool, args)
        if decision is not None:
            return decision
    return 0


def check_git_commit(text: str, sid: str):
    """commit 前置检查：分支硬拦 + 预算 steer / hard cap。"""
    repo_root = resolve_repo_root(text)
    if not repo_root:
        return None
    branch = (git_read(repo_root, ['rev-parse', '--abbrev-ref', 'HEAD']) or '').strip()
    if branch in ('main', 'master'):
        return out_block(f'BLAST RADIUS: 禁止在 {branch} 分支直接 commit。'
                         f'先创建 feature 分支并通过 PR/MR 合并。')
    reflog = git_read(repo_root, ['reflog', '--since=1 hour ago', '--format=%gs', 'HEAD'])
    if reflog is None:
        return None
    commits, churn = count_reflog_commits(reflog)
    if churn >= COMMIT_HARD_CAP:
        return out_block(f'BLAST RADIUS HARD CAP: 1 小时窗口内已创建 {churn} 个 commit'
                         f'（含 amend；绝对硬上限 {COMMIT_HARD_CAP}）。立即停止并拆分 Sprint。')
    ctx = read_sprint_context(repo_root)
    budget, source = resolve_commit_budget(ctx.get('progressMd'), ctx.get('planMd'))
    if commits >= budget and not marked(sid, 'budget-steer-reminded'):
        queue_pending(sid, BUDGET_STEER_KEY, budget_steer_message(
            commits, budget, source, ctx.get('sprintDir'), ctx.get('staleAll', False)))
    return None


def check_github_write(name: str, args):
    if re.match(r'^mcp__github__(get|list|search)_', name):
        return None
    if re.match(r'^mcp__github__(create_or_update_file|delete_file|push_files)$', name):
        branch = args.get('branch') or args.get('target_branch') or args.get('ref')
        if not branch:
            return out_block('BLAST RADIUS: GitHub 远程写入未提供目标 branch，'
                             '无法确认不是 main/master。请显式提供 feature branch。')
        if branch in ('main', 'master'):
            return out_block('BLAST RADIUS: 禁止通过 GitHub 工具直接写 main/master。'
                             '写入 feature 分支并通过 PR 合并。')
    return None


# ── PostToolUse ─────────────────────────────────────────────────────────────

def post_event(payload) -> int:
    tool = payload.get('tool_name') or ''
    args = payload.get('tool_input') or {}
    resp = payload.get('tool_response') or {}
    sid = payload.get('session_id') or ''
    if not resp.get('success'):
        return 0

    # pending 提醒投递（预算 steer / 控制平面）：Devin post 无 callId 对账，
    # 按会话串行投递——任意工具成功后取走全部 pending。
    notices = drain_pending(sid)

    if tool == 'exec':
        cmd = args.get('command') or ''
        if isinstance(cmd, str) and TEST_RUN_RE.search(cmd):
            mark(sid, 'test-ran')
    elif tool.lower() in ('write', 'edit', 'apply_patch'):
        p = args.get('file_path') or args.get('path') or ''
        if isinstance(p, str) and is_impl_file(p):
            mark(sid, 'impl-edited')
            if not spec_exists() and not marked(sid, 'spec-reminded'):
                mark(sid, 'spec-reminded')
                notices.append(
                    'kix-discipline: 首次实现编辑且无 spec 契约。若本任务命中需求三检信号'
                    '（目标不明/影响大不可逆/含实现方案词汇），请先在 kix-discipline/spec.md '
                    '写契约（goal/xy/前提/路径/验收）；字面明确低风险可逆任务可忽略本提醒。')
    elif tool == 'run_subagent':
        task = str(args.get('task') or args.get('prompt') or '')
        profile = str(args.get('profile') or '')
        sprint_ctx = bool(re.search(r'sprint|kixpower', task + ' ' + profile, re.I))
        if sprint_ctx and 'current_sprint' not in task and not marked(sid, 'sprint-reminded'):
            mark(sid, 'sprint-reminded')
            notices.append(
                'kix-orchestration: Sprint 编排分派未带 current_sprint 契约行。'
                'Sprint 模式下分派 task 应含 `current_sprint: N`'
                '（N 读 docs/.kixpower-current-sprint），交接对账以此为准。')

    if notices:
        return out_context('PostToolUse', '\n\n'.join(notices))
    return 0


def is_impl_file(path: str) -> bool:
    low = path.lower()
    if NON_IMPL_PATH.search(low):
        return False
    return Path(low).suffix in IMPL_EXT


def spec_exists() -> bool:
    root = Path(os.environ.get('DEVIN_PROJECT_DIR') or os.getcwd())
    return (root / 'kix-discipline' / 'spec.md').is_file() \
        or (root / '.kixpower' / 'spec.md').is_file()


# ── Stop ────────────────────────────────────────────────────────────────────

def stop_event(payload) -> int:
    sid = payload.get('session_id') or ''
    if marked(sid, 'impl-edited') and not marked(sid, 'test-ran') \
            and not marked(sid, 'stop-reminded'):
        mark(sid, 'stop-reminded')
        return out_block(
            'kix-discipline: 本会话有实现编辑但无测试运行记录。交付前请回答验证三问：'
            '① 测试镜像真实链路吗（stub 藏 bug）② 证据维度对吗（调用链≠语义）'
            '③ 关键 claim 独立验证过吗。跑过项目标准 lint/test 或向用户说明豁免理由后即可结束。')
    return 0


# ── SessionStart ────────────────────────────────────────────────────────────

def session_event(payload) -> int:
    return out_context('SessionStart', (
        'kixparadigm 已挂载：成员档 kixpower-dev / kixpower-qa / kixpower-reviewer / '
        'kixpower-reviewer-cross（跨厂商）/ kixpower-producer 经 run_subagent profile 分派；'
        '命令 /kixpower-new /kixpower-import /kixpower-continue /kixpower-review 走 Sprint 编排；'
        '门禁 hooks 生效中（force push / main 直写 / 破坏性 SQL / gh 远端删除硬拦截，'
        'commit 预算为结算提醒）。'))


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    if len(sys.argv) < 2:
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        ev = sys.argv[1]
        if ev == 'pre':
            return pre_event(payload)
        if ev == 'post':
            return post_event(payload)
        if ev == 'stop':
            return stop_event(payload)
        if ev == 'session':
            return session_event(payload)
    except Exception as e:  # fail-open：门禁脚本自身故障不阻塞会话
        print(f'kix-guards hook error (fail-open): {e}', file=sys.stderr)
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
