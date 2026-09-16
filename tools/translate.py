#!/usr/bin/env python3
"""kixparadigm DSH/Copilot -> Devin CLI/Desktop 文本移植器。

对插件树内所有 .md 做两遍处理：
1. frontmatter：Copilot/DSH 字段 -> Devin 字段
   - user-invocable: false  -> triggers: [model]
   - user-invocable: true   -> 删除（Devin 默认 user+model 都可触发）
   - agent:/tools:/agents:/hooks:/disable-model-invocation: -> 删除（Devin 无对应；
     hooks 由全局 hooks.v1.json/config.json 承担，角色边界由 allowed-tools 承担）
2. 正文：工具名与机制名映射（长名优先，避免子串误伤）

用法: translate.py <root>   —— 就地改写 root 下全部 .md
"""
import re
import sys
from pathlib import Path

# ── 正文替换表：(pattern, repl, note)。按序执行，长模式在前。─────────────────
BODY_RULES = [
    # DSH 专属机制 -> Devin 说明
    (r'kix_capability_search[^\n`]*', 'skill（Devin 技能目录）'),
    (r'kix_capability_call[^\n`]*', '直接调用对应工具'),
    (r'kix_tool_activate[^\n`]*', '直接使用对应工具'),
    (r'kix_discipline_spec[^\n`]*', '在工作区写 kix-discipline/spec.md 契约文件'),
    (r'subagent_cross', 'run_subagent（profile="kixpower-reviewer-cross"，跨厂商模型）'),
    (r'subagent_vision', 'read（Devin 原生识图，直接读图片文件）'),
    (r'subagent_fork', 'run_subagent（background/ resume 续跑）'),
    (r'subagent_lite', 'run_subagent（profile="subagent_explore"）'),
    (r'subagent_thinker', 'run_subagent（profile 内 model: 钉深思考模型）'),
    (r'read_image', 'read（图片路径直接读）'),
    (r'runSubagent', 'run_subagent'),
    (r'agentName:\s*"', 'profile: "'),
    (r"agentName:\s*'", "profile: '"),
    (r'run_in_terminal', 'exec'),
    (r'get_terminal_output', 'get_output'),
    (r'run_in_background:\s*true', '后台运行（exec timeout:0 / run_subagent is_background）'),
    (r'job_output', 'get_output'),
    (r'job_list', 'jobs（shell_id 列表）'),
    (r'job_kill', 'kill_shell'),
    (r'replace_string_in_file', 'edit'),
    (r'create_or_update_file', 'write'),
    (r'create_file', 'write'),
    (r'apply_patch', 'edit'),
    (r'read_file', 'read'),
    (r'grep_search', 'grep'),
    (r'file_search', 'glob'),
    (r'list_dir', 'exec ls'),
    (r'vscode_askQuestions', 'ask_user_question'),
    (r'manage_todo_list', 'todo_write'),
    (r'fetch_webpage', 'webfetch'),
    (r'run_notebook_cell', 'exec'),
    (r'semantic_search', 'grep / code_search'),
    (r'codegraphy_\w+', 'grep+read（Devin 无 CodeGraphy）'),
    (r'CodeGraphy MCP[^\n]*', '无 CodeGraphy；依赖/影响面分析用 grep + read。'),
    (r'CodeGraphy', 'grep+read'),
    (r'run_code', 'exec（脚本文件执行）'),
    # 只替换反引号包裹的 `workflow`（DSH 工具名）；裸英文 workflow 是普通名词（CI workflow 等）不碰
    (r'`workflow`', '`run_subagent` 编排（Devin 无 workflow 工具：todo_write + 成员档分派）'),
    (r'create_goal|update_goal|get_goal', 'todo_write / docs 落盘（Devin 无 goal 工具）'),
    (r'list_agents', 'subagent panel / read_subagent'),
    (r'send_message', 'report（终报=最终回复）'),
    (r'\bpwsh\b', 'bash'),
    (r'pwsh -NoProfile -File ', 'bash '),
    # Copilot/DSH 语境词
    (r'DeepSeek Harness|DSH(?![-\w])', 'Devin CLI/Desktop'),
    (r'\bdsh web\b', 'devin'),
    (r'VS Code Copilot|Copilot', 'Devin'),
    (r'~/.dsh', '~/.config/devin'),
    (r'\.agent-presets', '.config/devin 插件目录'),
    (r'agent\.cordis\.yml', 'config.json / AGENTS.md'),
]

# frontmatter 中删除的键（整行连同其 yaml 块列表/嵌套块都删）
FM_DROP_KEYS = re.compile(
    r'^(agent|agents|tools|user-invocable|disable-model-invocation|hooks|mode):'
)


def fix_frontmatter(text: str) -> str:
    if not text.startswith('---'):
        return text
    end = text.find('\n---', 3)
    if end == -1:
        return text
    fm, body = text[:end], text[end:]
    lines = fm.split('\n')
    out = []
    skip_indent = None
    for ln in lines:
        if skip_indent is not None:
            # 跳过被删键的 yaml 续行（缩进或 - 列表项）
            if ln.strip() == '' or ln.startswith((' ', '\t')) or ln.lstrip().startswith('- '):
                continue
            skip_indent = None
        m = re.match(r'^(\w[\w-]*):', ln)
        if m and FM_DROP_KEYS.match(ln):
            val = ln.split(':', 1)[1].strip()
            if m.group(1) == 'user-invocable' and val == 'false':
                out.append('triggers: [model]')
                continue
            if val == '' or val in ('|', '>', '|-', '>-'):
                skip_indent = True  # 后续是块内容
            continue
        out.append(ln)
    return '\n'.join(out) + body


def translate_body(text: str) -> str:
    for pat, repl in BODY_RULES:
        text = re.sub(pat, repl, text)
    return text


def main() -> None:
    root = Path(sys.argv[1])
    n = 0
    for p in sorted(root.rglob('*.md')):
        old = p.read_text(encoding='utf-8')
        new = translate_body(fix_frontmatter(old))
        if new != old:
            p.write_text(new, encoding='utf-8')
            n += 1
            print(f'  translated: {p.relative_to(root)}')
    print(f'done: {n} files updated')


if __name__ == '__main__':
    main()
