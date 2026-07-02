#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pypdfium2",
#     "Pillow",
#     "openai",
#     "PySocks",
#     "httpx[socks]",
# ]
# ///
"""Download an arXiv paper's LaTeX source and convert it to EPUB."""

import argparse
import datetime
import os
import re
import shutil
import smtplib
import socket
import subprocess
import sys
from urllib.parse import urlparse
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.message import EmailMessage
from collections.abc import Callable
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

_ALGO_CMD_NAMES = (
    "Require", "Ensure", "State", "For", "EndFor", "ForAll",
    "If", "ElsIf", "Else", "EndIf",
    "While", "EndWhile", "Repeat", "Until",
    "Loop", "EndLoop", "Return", "Print",
)
ALGO_CMDS = re.compile(
    r"\\(" + "|".join(_ALGO_CMD_NAMES) + r")\b",
    re.IGNORECASE,
)
_ALGO_CANONICAL = {name.lower(): name for name in _ALGO_CMD_NAMES}
NEEDS_BRACE_ARG = {"For", "ForAll", "If", "ElsIf", "While", "Until"}
INDENT_OPEN = {"For", "ForAll", "If", "While", "Loop", "Repeat"}
INDENT_CLOSE_BEFORE = {
    "EndFor",
    "EndIf",
    "EndWhile",
    "EndLoop",
    "Else",
    "ElsIf",
    "Until",
}
INDENT_OPEN_AFTER = {"Else", "ElsIf"}

_algorithm_counter = 0


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------


def collect_macros(tex_dir: Path) -> dict[str, str]:
    macros: dict[str, str] = {}
    pattern = re.compile(
        r"\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand)\s*\{?\\(\w+)\}?"
        r"(?:\[\d+\])?"
        r"\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}"
    )
    decl_math_op_re = re.compile(
        r"\\DeclareMathOperator\*?\s*\{\\(\w+)\}\s*\{([^}]*)\}"
    )
    for f in tex_dir.glob("*.tex"):
        text = f.read_text(errors="replace")
        for m in pattern.finditer(text):
            name, body = m.group(1), m.group(2)
            body = re.sub(r"\\xspace\b", "", body).strip()
            if name not in macros:
                macros[name] = body
        for m in decl_math_op_re.finditer(text):
            name, body = m.group(1), m.group(2)
            if name not in macros:
                macros[name] = f"\\operatorname{{{body}}}"
    return macros


def expand_macros(text: str, macros: dict[str, str], depth: int = 5) -> str:
    for _ in range(depth):
        prev = text
        for name, body in macros.items():
            text = re.sub(rf"\\{name}\b\s*", lambda _: body, text)
        if text == prev:
            break
    return text


def _read_and_strip_comments(tex_path: Path) -> str:
    return strip_tex_comments(tex_path.read_text(errors="replace"))


def extract_title(main_tex: Path, macros: dict[str, str], _content: str | None = None) -> str | None:
    content = _content or _read_and_strip_comments(main_tex)

    m = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*\{", content)
    if not m:
        return None

    brace_start = m.end() - 1
    brace_end = find_matching_brace(content, brace_start)
    if brace_end is None:
        return None
    raw = content[brace_start + 1 : brace_end]

    raw = re.sub(r"\\\\", " ", raw)
    raw = re.sub(r"\\[a-zA-Z]+\{([^{}]*)\}", r"\1", raw)
    raw = expand_macros(raw, macros)
    raw = re.sub(r"[{}]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def extract_authors(main_tex: Path, macros: dict[str, str], _content: str | None = None) -> list[str]:
    content = _content or _read_and_strip_comments(main_tex)

    m = re.search(r"\\author\s*\{", content)
    if not m:
        return []

    brace_start = m.end() - 1
    brace_end = find_matching_brace(content, brace_start)
    if brace_end is None:
        return []
    raw = content[brace_start + 1 : brace_end]

    # Take only the first line (before \\) — the rest are affiliations
    first_line = re.split(r"\\\\", raw)[0]

    # Remove superscript affiliations: $^{1}$, $^{1,2}$, etc.
    first_line = re.sub(r"\$\^?\{[^}]*\}\$", "", first_line)
    first_line = re.sub(r"\$\^\{[^}]*\}\$", "", first_line)
    # Remove LaTeX commands: \rm, \Envelope, \;, \textsuperscript{...}, etc.
    first_line = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", "", first_line)
    first_line = re.sub(r"\\[a-zA-Z]+\b", "", first_line)
    first_line = re.sub(r"\\;", "", first_line)
    # Remove remaining braces and dollar signs
    first_line = re.sub(r"[{}$]", "", first_line)

    first_line = expand_macros(first_line, macros)

    authors = []
    for name in re.split(r"\s*,\s*", first_line):
        name = re.sub(r"\s+", " ", name).strip()
        if name and re.search(r"[a-zA-Z]", name):
            authors.append(name)
    return authors


# ---------------------------------------------------------------------------
# TeX file utilities
# ---------------------------------------------------------------------------


def _resolve_tex_include(current_tex: Path, name: str) -> Path | None:
    candidates = [name] if name.endswith(".tex") else [name, f"{name}.tex"]
    for candidate in candidates:
        path = (current_tex.parent / candidate).resolve()
        if path.exists():
            return path
    return None


def get_input_order(main_tex: Path) -> list[Path]:
    visited: set[Path] = set()
    result: list[Path] = []

    def _walk(tex: Path) -> None:
        resolved = tex.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        result.append(tex)
        text = strip_tex_comments(tex.read_text(errors="replace"))
        includes = sorted(
            list(iter_latex_command_args(text, "input", optional=False))
            + list(iter_latex_command_args(text, "include", optional=False)),
            key=lambda item: item[0],
        )
        for _, _, name in includes:
            child = _resolve_tex_include(tex, name.strip())
            if child is not None:
                _walk(child)

    _walk(main_tex)
    return result


def _transform_tex_files(
    paper_dir: Path,
    transform,
    label: str,
    *,
    guard: str | None = None,
    glob: str = "**/*.tex",
) -> None:
    for tex in paper_dir.glob(glob):
        content = tex.read_text(errors="replace")
        if guard and guard not in content:
            continue
        updated = transform(content)
        if updated != content:
            tex.write_text(updated)
            print(f"{label}: {tex.name}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Algorithm preprocessing
# ---------------------------------------------------------------------------


def find_matching_brace(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def extract_brace_arg(text, pos):
    while pos < len(text) and text[pos] in " \t\n\r":
        pos += 1
    if pos >= len(text) or text[pos] != "{":
        return None, pos
    end = find_matching_brace(text, pos)
    if end is None:
        return None, pos
    return text[pos + 1 : end], end + 1


def find_matching_bracket(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "[":
        return None
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def skip_latex_options(text: str, pos: int) -> int:
    while True:
        while pos < len(text) and text[pos] in " \t\n\r":
            pos += 1
        if pos >= len(text) or text[pos] != "[":
            return pos
        end = find_matching_bracket(text, pos)
        if end is None:
            return pos
        pos = end + 1


def iter_latex_command_args(content: str, command: str, *, optional: bool = True):
    tag = f"\\{command}"
    i = 0
    while True:
        idx = content.find(tag, i)
        if idx == -1:
            return
        after = idx + len(tag)
        if after < len(content) and content[after].isalpha():
            i = after
            continue
        pos = skip_latex_options(content, after) if optional else after
        arg, end = extract_brace_arg(content, pos)
        if arg is not None:
            yield idx, end, arg
            i = end
        else:
            i = after


def strip_tex_comments(content: str) -> str:
    lines = []
    for line in content.splitlines(keepends=True):
        i = 0
        while True:
            i = line.find("%", i)
            if i == -1:
                lines.append(line)
                break
            backslashes = 0
            j = i - 1
            while j >= 0 and line[j] == "\\":
                backslashes += 1
                j -= 1
            if backslashes % 2 == 0:
                newline = "\n" if line.endswith("\n") else ""
                lines.append(line[:i] + newline)
                break
            i += 1
    return "".join(lines)


def replace_call_in_text(text):
    result = []
    i = 0
    while i < len(text):
        if text[i : i + 5] == "\\Call":
            j = i + 5
            while j < len(text) and text[j] in " \t":
                j += 1
            if j < len(text) and text[j] == "{":
                name, pos = extract_brace_arg(text, j)
                args, pos = extract_brace_arg(text, pos)
                if name is not None:
                    args_str = args if args else ""
                    result.append(f"\\operatorname{{{name}}}({args_str})")
                    i = pos
                    continue
        result.append(text[i])
        i += 1
    return "".join(result)


def format_command(cmd, arg, extra):
    if cmd == "Require":
        return f"\\textbf{{Require:}} {extra}"
    elif cmd == "Ensure":
        return f"\\textbf{{Ensure:}} {extra}"
    elif cmd == "State":
        return extra
    elif cmd == "Return":
        return f"\\textbf{{return}} {extra}"
    elif cmd == "Print":
        return f"\\textbf{{print}} {extra}"
    elif cmd in ("For", "ForAll"):
        kw = "for all" if cmd == "ForAll" else "for"
        return f"\\textbf{{{kw}}} {arg} \\textbf{{do}}"
    elif cmd == "EndFor":
        return "\\textbf{end for}"
    elif cmd == "If":
        return f"\\textbf{{if}} {arg} \\textbf{{then}}"
    elif cmd == "ElsIf":
        return f"\\textbf{{else if}} {arg} \\textbf{{then}}"
    elif cmd == "Else":
        return "\\textbf{else}"
    elif cmd == "EndIf":
        return "\\textbf{end if}"
    elif cmd == "While":
        return f"\\textbf{{while}} {arg} \\textbf{{do}}"
    elif cmd == "EndWhile":
        return "\\textbf{end while}"
    elif cmd == "Repeat":
        return "\\textbf{repeat}"
    elif cmd == "Until":
        return f"\\textbf{{until}} {arg}"
    elif cmd == "Loop":
        return "\\textbf{loop}"
    elif cmd == "EndLoop":
        return "\\textbf{end loop}"
    return extra


def parse_algorithmic(content):
    matches = list(ALGO_CMDS.finditer(content))
    if not matches:
        return []

    lines = []
    indent = 0
    line_num = 1

    for idx, match in enumerate(matches):
        cmd = _ALGO_CANONICAL[match.group(1).lower()]
        pos = match.end()

        if cmd in NEEDS_BRACE_ARG:
            arg, after_arg = extract_brace_arg(content, pos)
            rest_end = (
                matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
            )
            extra = content[after_arg:rest_end].strip()
        else:
            rest_end = (
                matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
            )
            arg = None
            extra = content[pos:rest_end].strip()

        if cmd in INDENT_CLOSE_BEFORE:
            indent = max(0, indent - 1)

        if cmd == "State" and not extra.strip():
            continue

        text = format_command(cmd, arg, extra)
        lines.append((line_num, indent, text))
        line_num += 1

        if cmd in INDENT_OPEN or cmd in INDENT_OPEN_AFTER:
            indent += 1

    return lines


def build_algorithm_output(caption, label, lines):
    global _algorithm_counter
    _algorithm_counter += 1

    indent_unit = "~~~~"
    parts = []
    parts.append("\\begin{algorithmdisplay}\n")

    if caption:
        caption = replace_call_in_text(caption)
        if label:
            parts.append(f"\\hypertarget{{{label}}}{{}}%")
        parts.append(f"\\textbf{{Algorithm {_algorithm_counter}}} {caption}\n")

    rule = "\\begin{center}\\rule{0.8\\textwidth}{0.4pt}\\end{center}"
    parts.append(rule + "\n")

    for line_num, indent, text in lines:
        text = replace_call_in_text(text)
        indent_str = indent_unit * indent
        parts.append(f"{line_num}:{indent_str} {text}\n")

    parts.append(rule + "\n")
    parts.append("\\end{algorithmdisplay}\n")
    return "\n".join(parts)


def find_algorithm_blocks(tex):
    pattern = re.compile(r"\\begin\{algorithm\*?\}(\[[^\]]*\])?\s*\n?", re.DOTALL)
    blocks = []
    for m in pattern.finditer(tex):
        start = m.start()
        starred = "*" in tex[m.start() : m.end()]
        end_tag = "\\end{algorithm*}" if starred else "\\end{algorithm}"
        end_pos = tex.find(end_tag, m.end())
        if end_pos == -1:
            continue
        end_pos += len(end_tag)
        blocks.append((start, end_pos, tex[m.end() : end_pos - len(end_tag)]))
    return blocks


def extract_caption_and_label(block_content):
    caption = None
    label = None

    cap_match = re.search(r"\\caption\{", block_content)
    if cap_match:
        caption, _ = extract_brace_arg(block_content, cap_match.start() + 8)

    lab_match = re.search(r"\\label\{([^}]+)\}", block_content)
    if lab_match:
        label = lab_match.group(1)

    return caption, label


def extract_algorithmic_content(block_content):
    start_match = re.search(r"\\begin\{algorithmic\}(\[\d+\])?\s*\n?", block_content)
    if not start_match:
        return None
    end_match = re.search(r"\\end\{algorithmic\}", block_content)
    if not end_match:
        return block_content[start_match.end() :]
    return block_content[start_match.end() : end_match.start()]


def process_algorithms(tex):
    tex = replace_call_in_text(tex)
    blocks = find_algorithm_blocks(tex)
    if not blocks:
        return tex

    result = []
    last_end = 0
    for start, end, content in blocks:
        result.append(tex[last_end:start])
        caption, label = extract_caption_and_label(content)
        algo_content = extract_algorithmic_content(content)
        if algo_content:
            lines = parse_algorithmic(algo_content)
            output = build_algorithm_output(caption, label, lines)
            result.append(output)
        else:
            result.append(tex[start:end])
        last_end = end

    result.append(tex[last_end:])
    return "".join(result)


# ---------------------------------------------------------------------------
# Translation (Qwen3.6-Flash via Bailian)
# ---------------------------------------------------------------------------

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TRANSLATE_MODEL = "qwen3.6-flash"

SKIP_ENV_NAMES = {
    "figure",
    "figure*",
    "table",
    "table*",
    "equation",
    "equation*",
    "align",
    "align*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "eqnarray",
    "eqnarray*",
    "algorithmdisplay",
    "algorithm",
    "algorithm*",
    "algorithmic",
    "lstlisting",
    "verbatim",
    "minted",
    "listing",
    "thebibliography",
    "tabular",
    "tabular*",
    "tabularx",
    "longtable",
    "wrapfigure",
    "subfigure",
    "tabu",
    "tabulary",
    "tblr",
    "xltabular",
    "ltablex",
    "NiceTabular",
    "NiceTabular*",
    "adjustbox",
    "tikzpicture",
}

PURE_CMD_LINE = re.compile(
    r"^\s*\\(section|subsection|subsubsection|paragraph|label|caption"
    r"|begin|end|centering|includegraphics|bibliographystyle"
    r"|bibliography|maketitle|tableofcontents|newpage|clearpage"
    r"|vspace|hspace|noindent|appendix|input"
    r"|newcommand|renewcommand|providecommand|def|let"
    r"|usepackage|RequirePackage|documentclass"
    r"|DeclareMathOperator|DeclareRobustCommand"
    r"|setlength|setcounter|addtocounter"
    r"|graphicspath|definecolor|captionsetup"
    r"|title|author|date|keywords|fancyhead|fancyfoot"
    r"|renewcommand|pagestyle|thispagestyle)\b"
)

SECTION_HEADING_LINE_RE = re.compile(
    r"^\s*\\(?:section|subsection|subsubsection)\*?(?:\[[^\]]*\])?\{",
    re.MULTILINE,
)


def create_openai_client():
    from openai import OpenAI

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print(
            "Error: DASHSCOPE_API_KEY environment variable is required for --translate",
            file=sys.stderr,
        )
        sys.exit(1)
    return OpenAI(api_key=api_key, base_url=DASHSCOPE_BASE_URL)


def _chat(client, system_prompt: str, user_prompt: str) -> str:
    resp = client.chat.completions.create(
        model=TRANSLATE_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return resp.choices[0].message.content.strip()


def _parse_numbered_response(raw: str, postprocess=None) -> dict[int, str]:
    translations: dict[int, str] = {}
    segments = re.split(r"\[(\d+)\]\s*\n", raw)
    i = 1
    while i < len(segments) - 1:
        try:
            num = int(segments[i])
            text = segments[i + 1].strip()
            if postprocess:
                text = postprocess(text)
            if text:
                translations[num] = text
        except (ValueError, IndexError):
            pass
        i += 2
    return translations


def extract_abstract(main_tex: Path) -> str:
    content = main_tex.read_text(errors="replace")
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", content, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def extract_section_headings(paper_dir: Path) -> list[str]:
    headings = []
    for tex in paper_dir.glob("*.tex"):
        for m in re.finditer(
            r"\\(?:sub)*section\*?\{([^}]+)\}", tex.read_text(errors="replace")
        ):
            headings.append(m.group(1))
    return headings


def extract_glossary(
    client, title: str | None, abstract: str, headings: list[str]
) -> str:
    system_prompt = (
        "你是一位专业的科技论文翻译专家。请根据以下论文信息，提取关键术语并给出中英对照翻译表。\n\n"
        "要求：\n"
        "1. 提取所有重要的技术术语、方法名、概念\n"
        "2. 专有名词（模型名、数据集名、人名）标注为'保留英文'\n"
        "3. 输出格式为每行一个：English term | 中文翻译\n"
        "4. 只输出术语表，不要其他内容"
    )
    user_prompt = (
        f"标题：{title or '未知'}\n摘要：{abstract}\n章节标题：{', '.join(headings)}"
    )
    print("Extracting glossary ...")
    glossary = _chat(client, system_prompt, user_prompt)
    print(f"Glossary extracted ({glossary.count(chr(10)) + 1} terms)")
    return glossary


def _strip_heading_lines(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(l for l in lines if not SECTION_HEADING_LINE_RE.match(l)).strip()


def _is_prose(chunk: str) -> bool:
    if not chunk.strip():
        return False
    lines = chunk.strip().splitlines()
    if all(line.strip().startswith("%") for line in lines if line.strip()):
        return False
    if all(PURE_CMD_LINE.match(line) or not line.strip() for line in lines):
        return False
    stripped = re.sub(r"\\[a-zA-Z]+(\[[^\]]*\])?\{[^}]*\}", "", chunk)
    stripped = re.sub(r"\\[a-zA-Z]+", "", stripped)
    stripped = re.sub(r"\$[^$]*\$", "", stripped)
    stripped = re.sub(r"[{}%\[\]~]", "", stripped)
    ascii_text = re.sub(r"[^a-zA-Z]", "", stripped)
    return len(ascii_text) >= 20


def _split_into_chunks(content: str) -> list[str]:
    return re.split(r"\n\s*\n", content)


def _strip_env_wrappers(text: str) -> str:
    lines = text.strip().splitlines()
    lines = [l for l in lines if not re.match(r"^\s*\\(begin|end)\{[^}]+\}", l)]
    return "\n".join(lines).strip()


def _find_skip_ranges(content: str) -> list[tuple[int, int]]:
    ranges = []
    stack: list[tuple[str, int]] = []
    for m in re.finditer(r"\\(begin|end)\{([^}]+)\}", content):
        cmd, env_name = m.group(1), m.group(2)
        if cmd == "begin" and env_name in SKIP_ENV_NAMES:
            stack.append((env_name, m.start()))
        elif cmd == "end" and stack and stack[-1][0] == env_name:
            _, start = stack.pop()
            ranges.append((start, m.end()))
    return ranges


def _chunk_in_skip_range(
    chunk_start: int, chunk_end: int, skip_ranges: list[tuple[int, int]]
) -> bool:
    for rs, re_ in skip_ranges:
        if chunk_start >= rs and chunk_end <= re_:
            return True
        if chunk_start < re_ and chunk_end > rs:
            return True
    return False


def _fix_braces(text: str) -> str:
    depth = 0
    result = []
    for ch in text:
        if ch == "{":
            depth += 1
            result.append(ch)
        elif ch == "}":
            if depth > 0:
                depth -= 1
                result.append(ch)
        else:
            result.append(ch)
    return "".join(result)


def _batch_translate(
    client, glossary: str, numbered_paragraphs: dict[int, str]
) -> dict[int, str]:
    if not numbered_paragraphs:
        return {}

    system_prompt = (
        "你是一位专业的科技论文翻译专家。请将以下编号的英文学术论文段落逐段翻译为中文。\n\n"
        f"术语对照表（请严格遵守）：\n{glossary}\n\n"
        "要求：\n"
        "1. 保持学术论文的专业性和准确性\n"
        "2. 严格按照术语对照表翻译术语，标注'保留英文'的术语保留英文原文\n"
        "3. 数学公式和LaTeX命令保持不变\n"
        "4. 引用标记保持不变\n"
        "5. 译文流畅自然，符合中文科技论文的表达习惯\n"
        "6. 每段译文前标注对应编号，格式为 [编号]，然后换行输出译文\n"
        "7. 只输出翻译结果，不要添加任何解释"
    )

    parts = []
    for idx in sorted(numbered_paragraphs):
        parts.append(f"[{idx}]\n{numbered_paragraphs[idx]}")
    user_prompt = "\n\n".join(parts)

    for attempt in range(3):
        try:
            raw = _chat(client, system_prompt, user_prompt)
            break
        except Exception as e:
            if attempt < 2:
                import time

                time.sleep(2**attempt)
                print(f"  Retry {attempt + 1} ...", file=sys.stderr)
            else:
                print(f"  Warning: batch translation failed: {e}", file=sys.stderr)
                return {}

    return _parse_numbered_response(raw, postprocess=_fix_braces)


def _translate_heading_texts(client, glossary: str, texts: list[str]) -> dict[int, str]:
    if not texts:
        return {}
    system_prompt = (
        "你是一位专业的科技论文翻译专家。请将以下编号的英文章节标题翻译为中文。\n\n"
        f"术语对照表（请严格遵守）：\n{glossary}\n\n"
        "要求：\n"
        "1. 标注'保留英文'的术语保留英文原文\n"
        "2. 每个译文前标注对应编号，格式为 [编号]，然后换行输出译文\n"
        "3. 只输出纯文本翻译，不要添加任何LaTeX命令\n"
        "4. 只输出翻译结果，不要添加任何解释"
    )
    parts = [f"[{i}]\n{text}" for i, text in enumerate(texts)]
    user_prompt = "\n\n".join(parts)
    raw = _chat(client, system_prompt, user_prompt)
    return _parse_numbered_response(raw)


def _translate_headings(client, glossary: str, content: str) -> str:
    heading_re = re.compile(
        r"\\(?:section|subsection|subsubsection)\*?(?:\[[^\]]*\])?\{"
    )
    headings = []
    for m in heading_re.finditer(content):
        brace_start = m.end() - 1
        brace_end = find_matching_brace(content, brace_start)
        if brace_end is None:
            continue
        title_text = content[brace_start + 1 : brace_end]
        insert_pos = brace_end + 1
        rest = content[insert_pos:]
        label_m = re.match(r"\s*\\label\{[^}]*\}", rest)
        if label_m:
            insert_pos += label_m.end()
        headings.append((insert_pos, title_text))

    if not headings:
        return content

    unique_titles = list(dict.fromkeys(h[1] for h in headings))
    print(f"Translating {len(unique_titles)} section headings ...")
    translations = _translate_heading_texts(client, glossary, unique_titles)
    title_to_zh = {}
    for i, title in enumerate(unique_titles):
        if i in translations:
            title_to_zh[title] = translations[i]

    for insert_pos, title_text in reversed(headings):
        zh = title_to_zh.get(title_text)
        if not zh:
            continue
        content = content[:insert_pos] + f"\n\n{zh}\n" + content[insert_pos:]

    return content


def translate_file_content(client, glossary: str, content: str) -> str:
    skip_ranges = _find_skip_ranges(content)
    chunks = _split_into_chunks(content)

    # Map each chunk to its character position in the original content
    chunk_positions: list[tuple[int, int]] = []
    pos = 0
    for i, chunk in enumerate(chunks):
        idx = content.find(chunk, pos)
        if idx == -1:
            idx = pos
        chunk_positions.append((idx, idx + len(chunk)))
        pos = idx + len(chunk)

    numbered: dict[int, str] = {}
    for i, chunk in enumerate(chunks):
        if _chunk_in_skip_range(
            chunk_positions[i][0], chunk_positions[i][1], skip_ranges
        ):
            continue
        stripped = _strip_env_wrappers(chunk)
        stripped = _strip_heading_lines(stripped)
        if stripped and _is_prose(stripped):
            numbered[i] = stripped

    if not numbered:
        return _translate_headings(client, glossary, content)

    translations = _batch_translate(client, glossary, numbered)

    result = []
    for i, chunk in enumerate(chunks):
        result.append(chunk)
        if i in translations:
            result.append(translations[i])

    assembled = "\n\n".join(result)
    return _translate_headings(client, glossary, assembled)


def translate_tex_files(
    paper_dir: Path, main_tex: Path, client, title: str | None
) -> None:
    abstract = extract_abstract(main_tex)
    headings = extract_section_headings(paper_dir)
    glossary = extract_glossary(client, title, abstract, headings)

    input_files = get_input_order(main_tex)
    has_input_files = len(input_files) > 1

    files_to_translate = input_files[1:] if has_input_files else [main_tex]

    def translate_one(tex_file: Path) -> str:
        content = tex_file.read_text(errors="replace")
        doc_begin = re.search(r"\\begin\{document\}", content)
        if doc_begin:
            preamble = content[: doc_begin.end()]
            body = content[doc_begin.end() :]
        else:
            preamble = ""
            body = content

        translated_body = translate_file_content(client, glossary, body)
        result = preamble + translated_body
        tex_file.write_text(result)
        return tex_file.name

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(translate_one, f): f for f in files_to_translate}
        for future in as_completed(futures):
            name = future.result()
            print(f"Translated: {name}")

    print("Translation complete.")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def find_main_tex(paper_dir: Path) -> Path:
    for tex in paper_dir.rglob("*.tex"):
        content = strip_tex_comments(tex.read_text(errors="replace"))
        if re.search(r"\\documentclass|\\begin\{document\}", content):
            return tex

    texfiles = list(paper_dir.rglob("*.tex"))
    if texfiles:
        return texfiles[0]

    print("Error: no .tex file found in paper/", file=sys.stderr)
    sys.exit(1)


def simplify_documentclass(tex_path: Path) -> None:
    content = tex_path.read_text()
    updated = content
    for start, end, _ in reversed(list(iter_latex_command_args(content, "documentclass"))):
        updated = updated[:start] + r"\documentclass{article}" + updated[end:]
    updated = re.sub(r"^\s*\\maketitle\s*$", "", updated, flags=re.MULTILINE)
    if updated != content:
        tex_path.write_text(updated)


# ---------------------------------------------------------------------------
# Preamble & noise cleanup
# ---------------------------------------------------------------------------

STRIP_PACKAGES = {
    "geometry", "hyperref", "cleveref", "natbib", "biblatex",
    "xcolor", "color", "microtype", "fontspec", "inputenc", "fontenc",
    "graphicx", "float", "placeins", "stfloats",
    "titlesec", "fancyhdr", "setspace", "parskip",
    "caption", "subcaption", "lineno", "enumitem",
    "booktabs", "multirow", "makecell", "colortbl", "array",
    "diagbox", "rotating", "ulem", "soul",
    "tcolorbox", "forest", "tikz", "pgfplots",
    "axessibility", "savetrees", "comment",
    "wrapfig", "pifont", "fontawesome", "fontawesome5",
    "lipsum", "blindtext", "nicematrix",
    "babel", "etoolbox", "bm",
    "adjustbox", "changepage", "pdflscape", "afterpage",
    "fancyvrb", "minted",
}

_USEPACKAGE_RE = re.compile(
    r"^\s*\\(?:usepackage|RequirePackage)"
    r"(?:\[[^\]]*\])?"
    r"\{([^}]+)\}\s*$",
    re.MULTILINE,
)

_CONFIG_CMDS = [
    "hypersetup", "captionsetup", "definecolor",
    "lstset", "lstdefinestyle", "pagestyle", "fancyhf", "fancyhead",
    "fancyfoot", "tcbset", "usetikzlibrary", "pgfplotsset",
    "sisetup", "DeclareSIUnit", "linespread",
]

_CONFIG_CMD_RE = re.compile(
    r"^\s*\\(?:" + "|".join(_CONFIG_CMDS) + r")(?:\*?)(?:\[[^\]]*\])?\s*\{",
    re.MULTILINE,
)


def _filter_usepackage(m: re.Match) -> str:
    pkgs = [p.strip() for p in m.group(1).split(",")]
    remaining = [p for p in pkgs if p not in STRIP_PACKAGES]
    if not remaining:
        return ""
    if len(remaining) == len(pkgs):
        return m.group(0)
    return m.group(0).replace(m.group(1), ", ".join(remaining))


def _strip_problematic_packages_content(content: str) -> str:
    content = _USEPACKAGE_RE.sub(_filter_usepackage, content)

    for cm in reversed(list(_CONFIG_CMD_RE.finditer(content))):
        brace_pos = cm.group(0).rindex("{") + cm.start()
        end = find_matching_brace(content, brace_pos)
        if end is not None:
            content = content[: cm.start()] + content[end + 1 :]

    content = re.sub(
        r"^\s*\\(?:makeatletter|makeatother)\s*$", "", content, flags=re.MULTILINE
    )
    return content


def strip_problematic_packages(paper_dir: Path) -> None:
    _transform_tex_files(paper_dir, _strip_problematic_packages_content, "Stripped packages/config")


_NOISE_NO_ARG_RE = re.compile(
    r"\\(?:sloppy|raggedright|raggedbottom|noindent"
    r"|smallskip|medskip|bigskip|vfill|hfill"
    r"|allowbreak|linebreak|pagebreak"
    r"|newpage|clearpage|cleardoublepage"
    r"|centering|tableofcontents|FloatBarrier"
    r"|maketitle|notag)\b\s*"
)

_CCSDESC_RE = re.compile(r"\\ccsdesc\s*(?:\[[^\]]*\])?\s*\{[^}]*\}\s*")

_NOISE_ONE_ARG = [
    "vspace", "hspace", "enlargethispage",
    "phantom", "vphantom", "hphantom",
    "todo", "fixme", "marginpar",
    "stepcounter",
]

_NOISE_TWO_ARG = [
    "setlength", "addtolength", "setcounter", "addtocounter",
    "csgdef",
]

_STRIP_ENVS = {"tikzpicture", "comment", "CCSXML"}

_STRIP_ENV_RE = re.compile(
    r"\\begin\{(" + "|".join(_STRIP_ENVS) + r")\}.*?\\end\{\1\}",
    re.DOTALL,
)


def _strip_noise_content(content: str) -> str:
    content = _NOISE_NO_ARG_RE.sub("", content)
    content = _CCSDESC_RE.sub("", content)
    content = content.replace(r"\today", datetime.date.today().strftime("%B %d, %Y"))

    for cmd in _NOISE_ONE_ARG:
        tag = f"\\{cmd}"
        while tag in content:
            idx = content.index(tag)
            pos = idx + len(tag)
            if pos < len(content) and content[pos] == "*":
                pos += 1
            arg, pos = extract_brace_arg(content, pos)
            if arg is not None:
                content = content[:idx] + content[pos:]
            else:
                break

    for cmd in _NOISE_TWO_ARG:
        tag = f"\\{cmd}"
        while tag in content:
            idx = content.index(tag)
            pos = idx + len(tag)
            _, pos = extract_brace_arg(content, pos)
            _, pos = extract_brace_arg(content, pos)
            content = content[:idx] + content[pos:]

    return _STRIP_ENV_RE.sub("", content)


def strip_noise_commands(paper_dir: Path) -> None:
    _transform_tex_files(paper_dir, _strip_noise_content, "Stripped noise commands")


# ---------------------------------------------------------------------------
# Annotation system stripping (e.g. \atran, \aeq, \annotate)
# ---------------------------------------------------------------------------

_ATRAN_TAG = r"\newcommand{\atran}"

_ANNOTATE_DEF_RE = re.compile(
    r"\\(?:newcommand|renewcommand)\{"
    r"\\(?:annotate(?:hypertarget|initused|getlabels|printlabels)?)\b"
)

_ANNOTATE_COUNTER_RE = re.compile(
    r"\\newcounter\{annotate\w*\}\s*",
)


def _strip_newcommand_block(content: str, start: int) -> tuple[int, int]:
    """Return (start_of_block, end_of_block) for a \\newcommand at *start*."""
    i = start
    while i < len(content) and content[i] != "{":
        i += 1
    end = find_matching_brace(content, i)
    if end is None:
        return start, start
    i = end + 1
    while i < len(content) and content[i] in " \t":
        i += 1
    if i < len(content) and content[i] == "[":
        while i < len(content) and content[i] != "]":
            i += 1
        i += 1
    while i < len(content) and content[i] in " \t":
        i += 1
    if i < len(content) and content[i] == "{":
        end = find_matching_brace(content, i)
        if end is not None:
            return start, end + 1
    return start, start


def _strip_annotation_system(content: str) -> str:
    tag = _ATRAN_TAG
    idx = content.find(tag)
    if idx != -1:
        _, block_end = _strip_newcommand_block(content, idx)
        if block_end > idx:
            content = content[:idx] + r"\newcommand{\atran}[2]{#1}" + content[block_end:]

    for m in reversed(list(_ANNOTATE_DEF_RE.finditer(content))):
        _, block_end = _strip_newcommand_block(content, m.start())
        if block_end > m.start():
            content = content[:m.start()] + content[block_end:]

    content = _ANNOTATE_COUNTER_RE.sub("", content)
    return content


def strip_annotation_system(paper_dir: Path) -> None:
    _transform_tex_files(
        paper_dir, _strip_annotation_system, "Stripped annotation system",
        guard="\\atran",
    )


# ---------------------------------------------------------------------------
# Citation normalization
# ---------------------------------------------------------------------------

_CITE_ALIASES = [
    "parencite", "textcite", "autocite", "fullcite", "footcite",
    "citeauthor", "citetitle",
    "citep", "citet", "Citet", "Citep",
    "citealt", "citealp", "citenum", "citeyear",
]

_CITE_RE = re.compile(
    r"\\(?:" + "|".join(_CITE_ALIASES) + r")"
    r"\*?"
    r"(?:\[[^\]]*\])?"
    r"(?:\[[^\]]*\])?"
    r"\{([^}]+)\}"
)


def normalize_citations(paper_dir: Path) -> None:
    _transform_tex_files(
        paper_dir, lambda c: _CITE_RE.sub(r"\\cite{\1}", c), "Normalized citations"
    )


# ---------------------------------------------------------------------------
# Hyperref preprocessing
# ---------------------------------------------------------------------------


def _unwrap_latex_cmd(content: str, tag: str, num_args: int, keep: int = -1, *, star: bool = False) -> str:
    result: list[str] = []
    i = 0
    if keep < 0:
        keep = num_args + keep
    while i < len(content):
        if content[i:i + len(tag)] == tag and (
            i + len(tag) >= len(content) or not content[i + len(tag)].isalpha()
        ):
            pos = i + len(tag)
            if star and pos < len(content) and content[pos] == "*":
                pos += 1
            kept = None
            for n in range(num_args):
                arg, pos = extract_brace_arg(content, pos)
                if n == keep:
                    kept = arg
            if kept is not None:
                result.append(kept)
                i = pos
                continue
        result.append(content[i])
        i += 1
    return "".join(result)




def _unwrap_hyperref_content(content: str) -> str:
    result: list[str] = []
    pos = 0
    tag = r"\hyperref"
    while True:
        idx = content.find(tag, pos)
        if idx == -1:
            result.append(content[pos:])
            return "".join(result)
        after = idx + len(tag)
        if after < len(content) and content[after].isalpha():
            result.append(content[pos:after])
            pos = after
            continue
        opt_start = after
        while opt_start < len(content) and content[opt_start] in " \t\n\r":
            opt_start += 1
        if opt_start >= len(content) or content[opt_start] != "[":
            result.append(content[pos:after])
            pos = after
            continue
        opt_end = find_matching_bracket(content, opt_start)
        if opt_end is None:
            result.append(content[pos:after])
            pos = after
            continue
        arg, end = extract_brace_arg(content, opt_end + 1)
        if arg is None:
            result.append(content[pos:after])
            pos = after
            continue
        result.append(content[pos:idx])
        result.append(arg)
        pos = end


def _preprocess_hyperref_content(content: str) -> str:
    if "\\texorpdfstring" in content:
        content = _unwrap_latex_cmd(content, "\\texorpdfstring", 2, keep=0)
    if "\\Hy@raisedlink" in content:
        content = _unwrap_latex_cmd(content, "\\Hy@raisedlink", 1, keep=0)
    if "\\hypertarget" in content:
        content = _unwrap_latex_cmd(content, "\\hypertarget", 2, keep=1)
    if "\\hyperlink" in content:
        content = _unwrap_latex_cmd(content, "\\hyperlink", 2, keep=1)
    if "\\hyperref" in content:
        content = _unwrap_hyperref_content(content)
    return content


def preprocess_hyperref(paper_dir: Path) -> None:
    _transform_tex_files(paper_dir, _preprocess_hyperref_content, "Preprocessed hyperref")


# ---------------------------------------------------------------------------
# Table environment normalization
# ---------------------------------------------------------------------------

_TABLE_SIMPLE_RENAME = {
    "tabu": "tabular", "ltablex": "longtable",
    "NiceTabular": "tabular", "NiceTabular*": "tabular",
}

_TABLE_STRIP_WIDTH = {
    "tabularx": "tabular",
    "tabulary": "tabular",
    "xltabular": "longtable",
}

_TABLE_STRIP_WIDTH_RES = {
    src: (re.compile(r"\\begin\{" + re.escape(src) + r"\}\s*(?:\[[^\]]*\])?\s*\{[^}]*\}"), dst)
    for src, dst in _TABLE_STRIP_WIDTH.items()
}


def _normalize_table_envs_content(content: str) -> str:
    for src, dst in _TABLE_SIMPLE_RENAME.items():
        content = content.replace(f"\\begin{{{src}}}", f"\\begin{{{dst}}}")
        content = content.replace(f"\\end{{{src}}}", f"\\end{{{dst}}}")

    for src, (pat, dst) in _TABLE_STRIP_WIDTH_RES.items():
        content = pat.sub(f"\\\\begin{{{dst}}}", content)
        content = content.replace(f"\\end{{{src}}}", f"\\end{{{dst}}}")

    if "\\begin{tblr}" in content:
        content = re.sub(
            r"\\begin\{tblr\}\s*(?:\[[^\]]*\])?\s*\{[^}]*\}",
            r"\\begin{tabular}{l}",
            content,
        )
        content = content.replace("\\end{tblr}", "\\end{tabular}")
    return content


def normalize_table_envs(paper_dir: Path) -> None:
    _transform_tex_files(paper_dir, _normalize_table_envs_content, "Normalized table envs")


def _unwrap_makecell_content(content: str) -> str:
    result: list[str] = []
    i = 0
    tag = "\\makecell"
    while i < len(content):
        if content[i : i + len(tag)] == tag and (
            i + len(tag) >= len(content) or not content[i + len(tag)].isalpha()
        ):
            pos = i + len(tag)
            if pos < len(content) and content[pos] == "*":
                pos += 1
            while pos < len(content) and content[pos] in " \t\n\r":
                pos += 1
            if pos < len(content) and content[pos] == "[":
                bracket_end = content.find("]", pos)
                if bracket_end != -1:
                    pos = bracket_end + 1
            arg, pos = extract_brace_arg(content, pos)
            if arg is not None:
                flat = re.sub(r"\s*\\\\(?:\s*\[[^\]]*\])?\s*", " ", arg)
                flat = re.sub(r"\s*\\newline\s*", " ", flat)
                result.append(flat.strip())
                i = pos
                continue
        result.append(content[i])
        i += 1
    return "".join(result)


def unwrap_makecell(paper_dir: Path) -> None:
    _transform_tex_files(paper_dir, _unwrap_makecell_content, "Unwrapped makecell", guard="\\makecell")


_MINIPAGE_BEGIN_RE = re.compile(
    r"\\begin\{minipage\}(?:\s*\[[^\]]*\])*\s*\{[^}]*\}"
)


def _strip_minipage_in_tables_content(content: str) -> str:
    for m in reversed(list(re.finditer(r"\\begin\{tabular[*x]?\}", content))):
        tab_start = m.start()
        tab_body_start = m.end()
        brace_pos = content.find("{", tab_body_start)
        if brace_pos == -1:
            continue
        brace_end = find_matching_brace(content, brace_pos)
        if brace_end is None:
            continue
        tab_end_tag = content.find("\\end{" + m.group()[7:], brace_end)
        if tab_end_tag == -1:
            continue
        region = content[tab_body_start:tab_end_tag]
        new_region = _MINIPAGE_BEGIN_RE.sub("", region)
        new_region = new_region.replace("\\end{minipage}", "")
        if new_region != region:
            content = content[:tab_body_start] + new_region + content[tab_end_tag:]
    return content


def strip_minipage_in_tables(paper_dir: Path) -> None:
    _transform_tex_files(paper_dir, _strip_minipage_in_tables_content, "Stripped minipage in tables", guard="\\begin{minipage}")


# ---------------------------------------------------------------------------
# wrapfigure conversion
# ---------------------------------------------------------------------------

_WRAPFIG_RE = re.compile(
    r"\\begin\{wrapfigure\}(?:\[[^\]]*\])?(?:\{[^}]*\}){1,2}"
)


def convert_wrapfigure(paper_dir: Path) -> None:
    def _convert(c: str) -> str:
        return _WRAPFIG_RE.sub(r"\\begin{figure}[H]", c).replace(
            "\\end{wrapfigure}", "\\end{figure}"
        )
    _transform_tex_files(paper_dir, _convert, "Converted wrapfigure", guard="\\begin{wrapfigure}")


# ---------------------------------------------------------------------------
# Subfigure unwrapping
# ---------------------------------------------------------------------------

_SUBFIG_BEGIN_RE = re.compile(
    r"\\begin\{subfigure\}(?:\[[^\]]*\])?\{[^}]*\}"
)


def unwrap_subfigures(paper_dir: Path) -> None:
    def _unwrap(c: str) -> str:
        return _SUBFIG_BEGIN_RE.sub("", c).replace("\\end{subfigure}", "")
    _transform_tex_files(paper_dir, _unwrap, "Unwrapped subfigures", guard="\\begin{subfigure}")


# ---------------------------------------------------------------------------
# adjustbox stripping
# ---------------------------------------------------------------------------


def _remove_adjustbox_cmd(content: str) -> str:
    return _unwrap_latex_cmd(content, "\\adjustbox", 2)


_ADJUSTBOX_ENV_BEGIN_RE = re.compile(r"\\begin\{adjustbox\}\{[^}]*\}")


def strip_adjustbox(paper_dir: Path) -> None:
    def _strip(c: str) -> str:
        if "\\adjustbox{" in c:
            c = _remove_adjustbox_cmd(c)
        c = _ADJUSTBOX_ENV_BEGIN_RE.sub("", c)
        return c.replace("\\end{adjustbox}", "")
    _transform_tex_files(paper_dir, _strip, "Stripped adjustbox", guard="adjustbox")


# ---------------------------------------------------------------------------
# Theorem preprocessing
# ---------------------------------------------------------------------------

_NEWTHEOREM_RE = re.compile(
    r"\\newtheorem\*?\{(\w+)\}"
    r"(?:\[[^\]]*\])?"
    r"\{([^}]+)\}"
    r"(?:\[[^\]]*\])?"
)

_BUILTIN_THEOREMS = {
    "theorem": "Theorem",
    "lemma": "Lemma",
    "proposition": "Proposition",
    "corollary": "Corollary",
    "definition": "Definition",
    "example": "Example",
    "remark": "Remark",
    "assumption": "Assumption",
    "claim": "Claim",
    "conjecture": "Conjecture",
    "observation": "Observation",
    "note": "Note",
    "fact": "Fact",
    "property": "Property",
    "condition": "Condition",
    "hypothesis": "Hypothesis",
}


def preprocess_theorems(paper_dir: Path) -> None:
    declared: dict[str, str] = {}

    for tex in paper_dir.rglob("*.tex"):
        content = tex.read_text(errors="replace")
        for m in _NEWTHEOREM_RE.finditer(content):
            env_name, label = m.group(1), m.group(2)
            if env_name not in declared:
                declared[env_name] = label

    for name, label in _BUILTIN_THEOREMS.items():
        if name not in declared:
            declared[name] = label

    if not declared:
        return

    counters: dict[str, int] = {}

    for tex in paper_dir.rglob("*.tex"):
        content = tex.read_text(errors="replace")
        updated = content

        for env_name, label in declared.items():
            begin_tag = f"\\begin{{{env_name}}}"
            end_tag = f"\\end{{{env_name}}}"
            if begin_tag not in updated:
                continue

            if env_name not in counters:
                counters[env_name] = 0

            result: list[str] = []
            i = 0
            while i < len(updated):
                if updated[i:i + len(begin_tag)] == begin_tag:
                    pos = i + len(begin_tag)
                    opt_name = ""
                    while pos < len(updated) and updated[pos] in " \t\n\r":
                        pos += 1
                    if pos < len(updated) and updated[pos] == "[":
                        bracket_end = updated.index("]", pos)
                        opt_name = updated[pos + 1:bracket_end].strip()
                        pos = bracket_end + 1

                    end_pos = updated.find(end_tag, pos)
                    if end_pos == -1:
                        result.append(updated[i:])
                        break

                    body = updated[pos:end_pos].strip()
                    counters[env_name] += 1
                    n = counters[env_name]

                    header = f"\\textbf{{{label} {n}}}"
                    if opt_name:
                        header += f" \\textit{{({opt_name})}}"
                    header += "\\textbf{.}"

                    result.append(f"\n\\begin{{quote}}\n{header} {body}\n\\end{{quote}}\n")
                    i = end_pos + len(end_tag)
                else:
                    result.append(updated[i])
                    i += 1
            updated = "".join(result)

        if "\\begin{proof}" in updated:
            begin_tag = "\\begin{proof}"
            end_tag = "\\end{proof}"
            result = []
            i = 0
            while i < len(updated):
                if updated[i:i + len(begin_tag)] == begin_tag:
                    pos = i + len(begin_tag)
                    while pos < len(updated) and updated[pos] in " \t\n\r":
                        pos += 1
                    end_pos = updated.find(end_tag, pos)
                    if end_pos == -1:
                        result.append(updated[i:])
                        break
                    body = updated[pos:end_pos].strip()
                    result.append(
                        f"\n\\begin{{quote}}\n\\textit{{Proof.}} {body} \\hfill$\\square$\n\\end{{quote}}\n"
                    )
                    i = end_pos + len(end_tag)
                else:
                    result.append(updated[i])
                    i += 1
            updated = "".join(result)

        if updated != content:
            tex.write_text(updated)
            print(f"Preprocessed theorems: {tex.name}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Code listing normalization
# ---------------------------------------------------------------------------


def _normalize_code_listings_content(content: str) -> str:
    content = re.sub(
        r"\\begin\{minted\}(?:\[[^\]]*\])?\{[^}]*\}",
        r"\\begin{verbatim}",
        content,
    )
    content = content.replace("\\end{minted}", "\\end{verbatim}")

    tag = "\\mintinline"
    while tag in content:
        idx = content.index(tag)
        pos = idx + len(tag)
        if pos < len(content) and content[pos] == "[":
            bracket_end = content.index("]", pos)
            pos = bracket_end + 1
        _, pos = extract_brace_arg(content, pos)
        code, pos = extract_brace_arg(content, pos)
        if code is not None:
            content = content[:idx] + f"\\texttt{{{code}}}" + content[pos:]
        else:
            break

    content = re.sub(
        r"\\begin\{lstlisting\}\s*\[[^\]]*\]",
        r"\\begin{lstlisting}",
        content,
    )
    return content


def normalize_code_listings(paper_dir: Path) -> None:
    _transform_tex_files(
        paper_dir, _normalize_code_listings_content, "Normalized code listings",
    )


def convert_pdf_images(paper_dir: Path) -> None:
    import pypdfium2 as pdfium

    for pdf_path in paper_dir.rglob("*.pdf"):
        png_path = pdf_path.with_suffix(".png")
        try:
            doc = pdfium.PdfDocument(str(pdf_path))
            page = doc[0]
            bitmap = page.render(scale=4)
            bitmap.to_pil().save(str(png_path))
            print(f"Converted: {pdf_path} -> {png_path}")
        except Exception as e:
            print(f"Warning: could not convert {pdf_path}: {e}", file=sys.stderr)




def _rewrite_pdf_image_refs_content(content: str) -> str:
    result: list[str] = []
    pos = 0
    for start, end, path in iter_latex_command_args(content, "includegraphics"):
        result.append(content[pos:start])
        if path.lower().endswith(".pdf"):
            rewritten = content[start:end - len(path) - 1] + path[:-4] + ".png}"
            result.append(rewritten)
        else:
            result.append(content[start:end])
        pos = end
    result.append(content[pos:])
    return "".join(result)


def rewrite_pdf_image_refs(paper_dir: Path) -> None:
    _transform_tex_files(
        paper_dir, _rewrite_pdf_image_refs_content, "Rewrote PDF image refs",
        guard="\\includegraphics",
    )


def _transform_col_specs(content: str, pattern: str, transform: Callable[[str], str]) -> str:
    for m in reversed(list(re.finditer(pattern, content))):
        brace_start = m.end()
        brace_end = find_matching_brace(content, brace_start)
        if brace_end is None:
            continue
        spec = content[brace_start + 1 : brace_end]
        new_spec = transform(spec)
        if new_spec != spec:
            content = content[: brace_start + 1] + new_spec + content[brace_end:]
    return content


_AT_COL_SPEC_RE = re.compile(r"@\{[^}]*\}")
_TABULAR_RE = r"\\begin\{tabular[*x]?\}\s*"
_MULTICOLUMN_RE = r"\\multicolumn\{[^}]*\}\s*"


def _strip_at_col_specs_content(content: str) -> str:
    strip = _AT_COL_SPEC_RE.sub
    content = _transform_col_specs(content, _TABULAR_RE, lambda s: strip("", s))
    content = _transform_col_specs(content, _MULTICOLUMN_RE, lambda s: strip("", s))
    return content


def strip_at_col_specs(paper_dir: Path) -> None:
    _transform_tex_files(paper_dir, _strip_at_col_specs_content, "Stripped @{} column specs", guard="@{")


_SIUNITX_COL_RE = re.compile(r"S\s*(?:\[[^\]]*\])?")


def _normalize_siunitx_content(content: str) -> str:
    return _transform_col_specs(content, _TABULAR_RE, lambda s: _SIUNITX_COL_RE.sub("r", s))


def normalize_siunitx_columns(paper_dir: Path) -> None:
    _transform_tex_files(paper_dir, _normalize_siunitx_content, "Normalized siunitx columns")


def strip_resizebox(paper_dir: Path) -> None:
    _transform_tex_files(paper_dir, _remove_resizebox, "Stripped resizebox", guard="\\resizebox")


def _remove_resizebox(content: str) -> str:
    return _unwrap_latex_cmd(content, "\\resizebox", 3, star=True)


def rewrite_captionof(paper_dir: Path) -> None:
    _transform_tex_files(paper_dir, _replace_captionof_blocks, "Rewrote captionof", guard="\\captionof{")


def _replace_captionof_blocks(content: str) -> str:
    result = []
    pos = 0
    while True:
        idx = content.find("\\captionof{", pos)
        if idx == -1:
            result.append(content[pos:])
            break

        env_arg_end = content.find("}", idx + 11)
        if env_arg_end == -1:
            result.append(content[pos:])
            break
        env_type = content[idx + 11 : env_arg_end]

        mp_start = content.rfind("\\begin{minipage}", pos, idx)
        if mp_start == -1:
            result.append(content[pos : env_arg_end + 1])
            pos = env_arg_end + 1
            continue

        mp_end_tag = "\\end{minipage}"
        mp_end = content.find(mp_end_tag, idx)
        if mp_end == -1:
            result.append(content[pos : env_arg_end + 1])
            pos = env_arg_end + 1
            continue

        result.append(content[pos:mp_start])

        inner = content[mp_start : mp_end + len(mp_end_tag)]
        inner = re.sub(
            r"\\begin\{minipage\}(?:\[[^\]]*\])?\{[^}]*\}",
            f"\\\\begin{{{env_type}}}[H]",
            inner,
            count=1,
        )
        inner = inner.replace(mp_end_tag, f"\\end{{{env_type}}}", 1)
        inner = re.sub(r"\\captionof\{[^}]*\}", r"\\caption", inner, count=1)
        result.append(inner)

        pos = mp_end + len(mp_end_tag)

    return "".join(result)


def destar_floats(paper_dir: Path) -> None:
    def _destar(c: str) -> str:
        for env in ("table", "figure"):
            c = c.replace(f"\\begin{{{env}*}}", f"\\begin{{{env}}}")
            c = c.replace(f"\\end{{{env}*}}", f"\\end{{{env}}}")
        return c
    _transform_tex_files(paper_dir, _destar, "De-starred floats")


DING_MAP = {
    "33": "!",
    "34": '"',
    "35": "#",
    "36": "$",
    "37": "%",
    "38": "&",
    "39": "'",
    "40": "(",
    "41": "✉",  # envelope
    "42": "*",
    "43": "+",
    "44": ",",
    "45": "-",
    "46": ".",
    "47": "/",
    "51": "✓",  # check mark ✓
    "52": "✗",  # ballot x ✗
    "53": "✗",  # alternate x
    "54": "✔",  # heavy check
    "55": "✘",  # heavy ballot x ✘
    "56": "✠",  # Maltese cross
    "72": "★",  # black star ★
    "73": "☆",  # white star
    "108": "▶",  # right triangle
    "110": "▼",  # down triangle
    "115": "●",  # black circle ●
    "164": "♦",  # diamond
    "168": "♣",  # club
    "170": "♥",  # heart
    "171": "♠",  # spade
    "172": "←",  # left arrow
    "173": "↑",  # up arrow
    "174": "→",  # right arrow
    "175": "↓",  # down arrow
    "228": "✉",  # envelope
}


_DING_RE = re.compile(r"\\ding\{(\d+)\}")


def replace_ding_commands(paper_dir: Path) -> None:
    _transform_tex_files(
        paper_dir,
        lambda c: _DING_RE.sub(lambda m: DING_MAP.get(m.group(1), m.group(0)), c),
        "Replaced \\ding commands",
        guard="\\ding{",
    )


_TEXTCIRCLED_MAP = {str(i): chr(0x2460 + i - 1) for i in range(1, 21)}  # ①-⑳
_TEXTCIRCLED_RE = re.compile(r"\$?\\textcircled\{(\d+)\}\$?")


def _replace_textcircled(content: str) -> str:
    return _TEXTCIRCLED_RE.sub(
        lambda m: _TEXTCIRCLED_MAP.get(m.group(1), m.group(0)), content
    )


def replace_textcircled(paper_dir: Path) -> None:
    _transform_tex_files(
        paper_dir, _replace_textcircled, "Replaced \\textcircled",
        guard="\\textcircled{",
    )


_TEXTSC_RE = re.compile(r"\\textsc\b")


def normalize_textsc(paper_dir: Path) -> None:
    _transform_tex_files(
        paper_dir,
        lambda c: _TEXTSC_RE.sub(r"\\text", c),
        "Replaced \\textsc with \\text",
        guard="\\textsc",
    )


def preprocess_algorithms(paper_dir: Path) -> None:
    _transform_tex_files(
        paper_dir, process_algorithms, "Preprocessed algorithms",
        guard="\\begin{algorithm",
    )


def download(url: str, dest: Path) -> None:
    subprocess.run(["curl", "-L", url, "-o", str(dest)], check=True)



def _iter_graphicspath_dirs(content: str) -> list[str]:
    r"""Extract directories from LaTeX \graphicspath{{...}} declarations."""
    dirs: list[str] = []
    for _, _, arg in iter_latex_command_args(content, "graphicspath"):
        pos = 0
        while pos < len(arg):
            path, pos = extract_brace_arg(arg, pos)
            if path is None:
                pos += 1
                continue
            path = path.strip()
            if path and path not in dirs:
                dirs.append(path.rstrip("/"))
    return dirs


def collect_graphicspath_dirs(paper_dir: Path) -> list[str]:
    r"""Collect \graphicspath directories from TeX files for pandoc resources."""
    dirs: list[str] = []
    for tex in paper_dir.glob("**/*.tex"):
        for path in _iter_graphicspath_dirs(tex.read_text(errors="replace")):
            if path not in dirs:
                dirs.append(path)
    return dirs


def _pandoc_resource_paths(cwd: Path) -> list[str]:
    paths = [".", "figures", "images"]
    for path in collect_graphicspath_dirs(cwd):
        if path not in paths:
            paths.append(path)
    return paths

def run_pandoc(
    main_tex: Path,
    output: Path,
    title: str | None,
    authors: list[str] | None = None,
    *,
    workdir: Path | None = None,
) -> None:
    cwd = workdir or main_tex.parent
    input_path = os.path.relpath(main_tex, cwd)
    args = [
        "pandoc",
        input_path,
        "--mathml",
        "--from",
        "latex",
        "--to",
        "epub3",
        "--standalone",
        "--toc",
        "--number-sections",
        f"--resource-path={':'.join(_pandoc_resource_paths(cwd))}",
        f"--css={SCRIPT_DIR / 'epub.css'}",
        f"--lua-filter={SCRIPT_DIR / 'filter.lua'}",
    ]
    if title:
        args += ["--metadata", f"title={title}"]
    if authors:
        for author in authors:
            args += ["--metadata", f"author={author}"]
    args += ["-o", str(output)]

    subprocess.run(args, cwd=cwd, check=True)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def send_email(epub_path: Path, title: str | None, arxiv_id: str) -> None:
    missing = [
        v for v in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_PASSWORD") if not os.environ.get(v)
    ]
    if missing:
        print(
            f"Error: missing environment variables for --email: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(1)

    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]
    email_password = os.environ["EMAIL_PASSWORD"]
    smtp_host = os.environ.get("SMTP_SSL_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_SSL_PORT", "465"))

    msg = EmailMessage()
    msg["Subject"] = f"[paper2epub] {title or arxiv_id} ({arxiv_id})"
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(f"EPUB for arXiv paper {arxiv_id} is attached.")
    msg.add_attachment(
        epub_path.read_bytes(),
        maintype="application",
        subtype="epub+zip",
        filename=epub_path.name,
    )

    proxy_url = os.environ.get("SMTP_PROXY")
    orig_socket = socket.socket
    if proxy_url:
        import socks

        parsed = urlparse(proxy_url)
        proxy_host = parsed.hostname
        proxy_port = parsed.port or 1080
        proxy_user = parsed.username
        proxy_pass = parsed.password
        socks.set_default_proxy(
            socks.SOCKS5, proxy_host, proxy_port,
            username=proxy_user, password=proxy_pass,
        )
        socket.socket = socks.socksocket
        print(f"Sending {epub_path.name} to {email_to} via {smtp_host}:{smtp_port} (proxy {proxy_host}:{proxy_port}) ...")
    else:
        print(f"Sending {epub_path.name} to {email_to} via {smtp_host}:{smtp_port} ...")

    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(email_from, email_password)
            server.send_message(msg)
    finally:
        if proxy_url:
            socket.socket = orig_socket
    print("Email sent.")


def main():
    parser = argparse.ArgumentParser(description="Convert an arXiv paper to EPUB")
    parser.add_argument("arxiv_id", help="arXiv paper ID (e.g. 2402.08954)")
    parser.add_argument(
        "--translate",
        action="store_true",
        help="Translate to Chinese using Qwen3.6-Flash (requires DASHSCOPE_API_KEY)",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help="Send the EPUB via email (requires EMAIL_PASSWORD, EMAIL_FROM, EMAIL_TO; optional SMTP_PROXY for SOCKS5)",
    )
    args = parser.parse_args()

    if args.translate and not os.environ.get("DASHSCOPE_API_KEY"):
        print(
            "Error: DASHSCOPE_API_KEY environment variable is required for --translate",
            file=sys.stderr,
        )
        sys.exit(1)

    arxiv_id = args.arxiv_id
    paper_dir = Path("paper")
    tarball = Path("paper.tar.gz")

    if paper_dir.exists():
        shutil.rmtree(paper_dir)
    tarball.unlink(missing_ok=True)

    url = f"https://arxiv.org/src/{arxiv_id}"
    print(f"Downloading {url} ...")
    download(url, tarball)

    paper_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball) as tar:
        tar.extractall(path=paper_dir, filter="data")

    main_tex = find_main_tex(paper_dir)
    print(f"Using TeX file: {main_tex}")

    simplify_documentclass(main_tex)
    strip_problematic_packages(paper_dir)
    strip_noise_commands(paper_dir)
    strip_annotation_system(paper_dir)
    normalize_textsc(paper_dir)

    macros = collect_macros(paper_dir)
    main_content = _read_and_strip_comments(main_tex)
    title = extract_title(main_tex, macros, main_content)
    if title:
        print(f"Paper title: {title}")
    authors = extract_authors(main_tex, macros, main_content)
    if authors:
        print(f"Authors: {', '.join(authors)}")

    convert_pdf_images(paper_dir)
    rewrite_pdf_image_refs(paper_dir)
    normalize_citations(paper_dir)
    preprocess_hyperref(paper_dir)
    normalize_table_envs(paper_dir)
    unwrap_makecell(paper_dir)
    strip_minipage_in_tables(paper_dir)
    strip_at_col_specs(paper_dir)
    normalize_siunitx_columns(paper_dir)
    strip_resizebox(paper_dir)
    strip_adjustbox(paper_dir)
    convert_wrapfigure(paper_dir)
    unwrap_subfigures(paper_dir)
    rewrite_captionof(paper_dir)
    destar_floats(paper_dir)
    replace_ding_commands(paper_dir)
    replace_textcircled(paper_dir)
    preprocess_theorems(paper_dir)
    normalize_code_listings(paper_dir)
    preprocess_algorithms(paper_dir)

    if args.translate:
        client = create_openai_client()
        translate_tex_files(paper_dir, main_tex, client, title)

    suffix = "-zh" if args.translate else ""
    output = Path.cwd() / f"{arxiv_id}{suffix}.epub"
    run_pandoc(main_tex, output, title, authors, workdir=paper_dir)
    print(f"Generated: {output}")

    if args.email:
        send_email(output, title, arxiv_id)


if __name__ == "__main__":
    main()
