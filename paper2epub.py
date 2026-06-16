#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pypdfium2",
#     "openai",
#     "PySocks",
#     "httpx[socks]",
# ]
# ///
"""Download an arXiv paper's LaTeX source and convert it to EPUB."""

import argparse
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
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

ALGO_CMDS = re.compile(
    r"\\(Require|Ensure|State|For|EndFor|ForAll|"
    r"If|ElsIf|Else|EndIf|"
    r"While|EndWhile|"
    r"Repeat|Until|"
    r"Loop|EndLoop|"
    r"Return|Print)\b"
)
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
        r"\\(?:newcommand|renewcommand|def)\s*\{?\\(\w+)\}?"
        r"(?:\[\d+\])?"
        r"\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}"
    )
    for f in tex_dir.glob("*.tex"):
        for m in pattern.finditer(f.read_text(errors="replace")):
            name, body = m.group(1), m.group(2)
            body = re.sub(r"\\xspace\b", "", body).strip()
            if name not in macros:
                macros[name] = body
    return macros


def expand_macros(text: str, macros: dict[str, str], depth: int = 5) -> str:
    for _ in range(depth):
        prev = text
        for name, body in macros.items():
            text = re.sub(rf"\\{name}\b\s*", lambda _: body, text)
        if text == prev:
            break
    return text


def extract_title(main_tex: Path, macros: dict[str, str]) -> str | None:
    content = main_tex.read_text(errors="replace")
    content = re.sub(r"%.*", "", content)

    m = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*\{", content)
    if not m:
        return None

    start = m.end()
    depth = 1
    i = start
    while i < len(content) and depth > 0:
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
        i += 1
    raw = content[start : i - 1]

    raw = re.sub(r"\\\\", " ", raw)
    raw = re.sub(r"\\[a-zA-Z]+\{([^{}]*)\}", r"\1", raw)
    raw = expand_macros(raw, macros)
    raw = re.sub(r"[{}]", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def extract_authors(main_tex: Path, macros: dict[str, str]) -> list[str]:
    content = main_tex.read_text(errors="replace")
    content = re.sub(r"%.*", "", content)

    m = re.search(r"\\author\s*\{", content)
    if not m:
        return []

    start = m.end()
    depth = 1
    i = start
    while i < len(content) and depth > 0:
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
        i += 1
    raw = content[start : i - 1]

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


def get_input_order(main_tex: Path) -> list[Path]:
    root = main_tex.parent
    visited: set[Path] = set()
    result: list[Path] = []

    def _walk(tex: Path) -> None:
        resolved = tex.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        result.append(tex)
        text = tex.read_text(errors="replace")
        for m in re.finditer(r"\\input\{([^}]+)\}", text):
            name = m.group(1)
            if not name.endswith(".tex"):
                name += ".tex"
            p = root / name
            if p.exists():
                _walk(p)

    _walk(main_tex)
    return result


# ---------------------------------------------------------------------------
# Algorithm preprocessing
# ---------------------------------------------------------------------------


def find_matching_brace(text, start):
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
    return len(text) - 1


def extract_brace_arg(text, pos):
    while pos < len(text) and text[pos] in " \t\n\r":
        pos += 1
    if pos >= len(text) or text[pos] != "{":
        return None, pos
    end = find_matching_brace(text, pos)
    return text[pos + 1 : end], end + 1


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
                    prefix = "".join(result)
                    dollars = sum(
                        1
                        for k, c in enumerate(prefix)
                        if c == "$" and (k == 0 or prefix[k - 1] != "\\")
                    )
                    in_math = dollars % 2 == 1
                    args_str = args if args else ""
                    if in_math:
                        result.append(f"\\operatorname{{{name}}}({args_str})")
                    else:
                        result.append(f"\\textsc{{{name}}}({args_str})")
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
        cmd = match.group(1)
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

    translations: dict[int, str] = {}
    segments = re.split(r"\[(\d+)\]\s*\n", raw)
    i = 1
    while i < len(segments) - 1:
        try:
            num = int(segments[i])
            text = _fix_braces(segments[i + 1].strip())
            if text:
                translations[num] = text
        except (ValueError, IndexError):
            pass
        i += 2

    return translations


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
    translations: dict[int, str] = {}
    segments = re.split(r"\[(\d+)\]\s*\n", raw)
    j = 1
    while j < len(segments) - 1:
        try:
            num = int(segments[j])
            t = segments[j + 1].strip()
            if t:
                translations[num] = t
        except (ValueError, IndexError):
            pass
        j += 2
    return translations


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
    for tex in paper_dir.glob("*.tex"):
        content = tex.read_text(errors="replace")
        if re.search(r"\\documentclass|\\begin\{document\}", content):
            return tex

    texfiles = list(paper_dir.glob("*.tex"))
    if texfiles:
        return texfiles[0]

    print("Error: no .tex file found in paper/", file=sys.stderr)
    sys.exit(1)


def simplify_documentclass(tex_path: Path) -> None:
    content = tex_path.read_text()
    updated = re.sub(
        r"\\documentclass\[[^\]]*\]\{[^}]*\}",
        r"\\documentclass{article}",
        content,
    )
    updated = re.sub(r"^\s*\\maketitle\s*$", "", updated, flags=re.MULTILINE)
    if updated != content:
        tex_path.write_text(updated)


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


def _find_brace_block(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def normalize_siunitx_columns(paper_dir: Path) -> None:
    for tex in paper_dir.glob("**/*.tex"):
        content = tex.read_text()
        updated = content
        for m in reversed(list(re.finditer(r"\\begin\{tabular[*x]?\}\s*", updated))):
            brace_start = m.end()
            brace_end = _find_brace_block(updated, brace_start)
            if brace_end is None:
                continue
            spec = updated[brace_start + 1 : brace_end]
            new_spec = re.sub(r"S\s*(?:\[[^\]]*\])?", "r", spec)
            if new_spec != spec:
                updated = updated[: brace_start + 1] + new_spec + updated[brace_end:]
        if updated != content:
            tex.write_text(updated)
            print(f"Normalized siunitx columns: {tex}", file=sys.stderr)


def strip_resizebox(paper_dir: Path) -> None:
    for tex in paper_dir.rglob("*.tex"):
        content = tex.read_text(errors="replace")
        if "\\resizebox" not in content:
            continue
        updated = _remove_resizebox(content)
        if updated != content:
            tex.write_text(updated)
            print(f"Stripped resizebox: {tex.name}", file=sys.stderr)


def _remove_resizebox(content: str) -> str:
    result = []
    i = 0
    tag = "\\resizebox"
    while i < len(content):
        if content[i : i + len(tag)] == tag:
            pos = i + len(tag)
            if pos < len(content) and content[pos] == "*":
                pos += 1
            _, pos = extract_brace_arg(content, pos)
            _, pos = extract_brace_arg(content, pos)
            inner, pos = extract_brace_arg(content, pos)
            if inner is not None:
                result.append(inner)
                i = pos
                continue
        result.append(content[i])
        i += 1
    return "".join(result)


def rewrite_captionof(paper_dir: Path) -> None:
    for tex in paper_dir.rglob("*.tex"):
        content = tex.read_text(errors="replace")
        if "\\captionof{" not in content:
            continue
        updated = _replace_captionof_blocks(content)
        if updated != content:
            tex.write_text(updated)
            print(f"Rewrote captionof: {tex.name}", file=sys.stderr)


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
    for tex in paper_dir.rglob("*.tex"):
        content = tex.read_text(errors="replace")
        updated = content
        for env in ("table", "figure"):
            updated = updated.replace(f"\\begin{{{env}*}}", f"\\begin{{{env}}}")
            updated = updated.replace(f"\\end{{{env}*}}", f"\\end{{{env}}}")
        if updated != content:
            tex.write_text(updated)
            print(f"De-starred floats: {tex.name}", file=sys.stderr)


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


def replace_ding_commands(paper_dir: Path) -> None:
    pat = re.compile(r"\\ding\{(\d+)\}")
    for tex in paper_dir.rglob("*.tex"):
        content = tex.read_text(errors="replace")
        if "\\ding{" not in content:
            continue

        def _repl(m):
            return DING_MAP.get(m.group(1), m.group(0))

        updated = pat.sub(_repl, content)
        if updated != content:
            tex.write_text(updated)
            print(f"Replaced \\ding commands: {tex.name}", file=sys.stderr)


def preprocess_algorithms(paper_dir: Path) -> None:
    for tex in paper_dir.glob("*.tex"):
        content = tex.read_text()
        if "\\begin{algorithm" not in content:
            continue
        processed = process_algorithms(content)
        if processed != content:
            tex.write_text(processed)
            print(f"Preprocessed algorithms: {tex}")


def download(url: str, dest: Path) -> None:
    subprocess.run(["curl", "-L", url, "-o", str(dest)], check=True)


def run_pandoc(
    main_tex: Path, output: Path, title: str | None, authors: list[str] | None = None
) -> None:
    args = [
        "pandoc",
        str(main_tex.name),
        "--mathml",
        "--from",
        "latex",
        "--to",
        "epub3",
        "--standalone",
        "--toc",
        "--number-sections",
        "--resource-path=.:figures:images",
        f"--css={SCRIPT_DIR / 'epub.css'}",
        f"--lua-filter={SCRIPT_DIR / 'filter.lua'}",
    ]
    if title:
        args += ["--metadata", f"title={title}"]
    if authors:
        for author in authors:
            args += ["--metadata", f"author={author}"]
    args += ["-o", str(output)]

    subprocess.run(args, cwd=main_tex.parent, check=True)


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

    macros = collect_macros(paper_dir)
    title = extract_title(main_tex, macros)
    if title:
        print(f"Paper title: {title}")
    authors = extract_authors(main_tex, macros)
    if authors:
        print(f"Authors: {', '.join(authors)}")

    convert_pdf_images(paper_dir)
    normalize_siunitx_columns(paper_dir)
    strip_resizebox(paper_dir)
    rewrite_captionof(paper_dir)
    destar_floats(paper_dir)
    replace_ding_commands(paper_dir)
    preprocess_algorithms(paper_dir)

    if args.translate:
        client = create_openai_client()
        translate_tex_files(paper_dir, main_tex, client, title)

    suffix = "-zh" if args.translate else ""
    output = Path.cwd() / f"{arxiv_id}{suffix}.epub"
    run_pandoc(main_tex, output, title, authors)
    print(f"Generated: {output}")

    if args.email:
        send_email(output, title, arxiv_id)


if __name__ == "__main__":
    main()
