# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Python script (`paper2epub.py`) that downloads an arXiv paper's LaTeX source and converts it to EPUB using pandoc.

## Usage

```bash
uv run paper2epub.py <arxiv-id>
# Example: uv run paper2epub.py 2402.08954

# With Chinese translation (bilingual immersive format)
DASHSCOPE_API_KEY=your-key uv run paper2epub.py 2402.08954 --translate
```

The script downloads the LaTeX source tarball from arxiv.org, extracts it into `paper/`, finds the main `.tex` file, and runs pandoc to produce `<arxiv-id>.epub`.

With `--translate`, it uses Alibaba Bailian's Qwen3.6-Flash model to translate the paper to Chinese, producing a bilingual EPUB (`<arxiv-id>-zh.epub`) with English and Chinese paragraphs interleaved.

## Pipeline Steps

1. Download and extract arXiv source tarball
2. Find the main `.tex` file (looks for `\documentclass` or `\begin{document}`)
3. Simplify document class and strip problematic packages/noise
4. Run TeX preprocessing passes (citations, hyperref, tables, algorithms, theorems, etc.)
5. Extract paper title and authors (with macro expansion)
6. Convert PDF figures to PNG via pypdfium2, rewrite image references
7. (Optional) Translate to Chinese via Qwen3.6-Flash with two-phase strategy: glossary extraction → concurrent paragraph translation
8. Run pandoc to produce EPUB3

Reference: https://info.arxiv.org/help/submit_tex.html and https://info.arxiv.org/help/submit_latex_best_practices.html

## Dependencies

- `uv` — runs the script with automatic dependency management (Python deps declared via PEP 723 inline metadata)
- `curl` — downloads the arXiv source tarball
- `pandoc` — converts LaTeX to EPUB3 (uses `--mathml` for math rendering)
- `DASHSCOPE_API_KEY` environment variable — required for `--translate` mode (Alibaba Bailian API key)
- `SMTP_PROXY` environment variable — optional SOCKS5 proxy for `--email` mode (format: `socks5://[user:pass@]host:port`)

## Testing

```bash
python3 -m pytest test_paper2epub.py -v
```

- All tests live in `test_paper2epub.py`. No network or API calls — tests use `tmp_path` for filesystem operations.
- **Test every `_content` pure function.** If you add a new preprocessing pass, add tests for its `_*_content` function.
- Tests are organized by class: `TestFindMatchingBrace`, `TestExtractBraceArg`, etc. — follow this pattern.
- When modifying any content transform, run tests to verify no regressions.

## Code Style & Maintainability

### Architecture

- **Single-file script** — all logic lives in `paper2epub.py`. Keep it that way.
- **Avoid module-level mutable state.** `_algorithm_counter` is a known exception — don't add more.
- **Keep preprocessing passes independent.** Each pass handles one concern (citations, hyperref, tables, etc.) and can run in any order.
- **Files:** `paper2epub.py` (main script), `filter.lua` (pandoc Lua filter for refs/captions), `epub.css` (EPUB styling), `test_paper2epub.py` (tests).

### Two-tier function pattern

Every TeX preprocessing pass follows this convention:
1. A **pure transform** function named `_<operation>_content(content: str) -> str` — testable with no filesystem access.
2. A **file-level wrapper** named `<operation>(paper_dir: Path)` — a one-liner calling `_transform_tex_files`.

Example: `_strip_noise_content` (pure) → `strip_noise_commands` (file-level wrapper). When adding a new pass, follow this pattern.

### Reusable utilities — use these instead of writing ad-hoc logic

| Utility | Purpose | Don't reinvent |
|---|---|---|
| `_transform_tex_files(paper_dir, transform, label, *, guard, glob)` | Iterate/read/transform/write/print boilerplate for all TeX files | File-walking + write-if-changed loops |
| `find_matching_brace(text, start)` | Find closing `}` for an opening `{` at `start` | Inline brace-depth counting loops |
| `find_matching_bracket(text, start)` | Same for `[…]` pairs | Inline bracket-depth loops |
| `extract_brace_arg(text, pos)` | Skip whitespace, extract `{…}` content, return `(arg, end_pos)` | Manual whitespace-skip + brace matching |
| `skip_latex_options(text, pos)` | Skip zero or more `[…]` optional arguments | Manual bracket skipping |
| `iter_latex_command_args(content, cmd)` | Iterate over all `\cmd[…]{arg}` occurrences, yielding `(start, end, arg)` | Manual regex + brace extraction |
| `_unwrap_latex_cmd(content, tag, num_args, keep)` | Strip a LaTeX command keeping one of its N brace arguments (e.g. `\resizebox{w}{h}{content}` → `content`) | Manual character-by-character unwrapping |
| `_transform_col_specs(content, pattern, transform)` | Transform column specs in `\begin{tabular}{…}` or `\multicolumn{…}{…}` | Pattern-match + brace extraction for column specs |
| `strip_tex_comments(content)` | Remove `%`-comments respecting `\%` escapes | Naive line-based comment stripping |
| `_parse_numbered_response(raw, postprocess)` | Parse `[N]\ntext` formatted LLM responses into `dict[int, str]` | Custom response parsing |

### `guard` parameter

Pass `guard="\\commandname"` to `_transform_tex_files` to skip files that don't contain the guard string. This avoids unnecessary reads/writes and should be used whenever the transform targets a specific LaTeX command or environment.

### Conventions

- **Read TeX files with `errors="replace"`** — arXiv sources may contain non-UTF-8 bytes.
- **Use `SCRIPT_DIR`** (`Path(__file__).resolve().parent`) to resolve paths relative to the script (e.g. `epub.css`, `filter.lua`).
- **Translation uses `openai` SDK** against Alibaba Bailian's API (`DASHSCOPE_BASE_URL`). The `_chat` helper wraps single-turn completions.
- **Multi-file TeX documents** — use `get_input_order(main_tex)` to walk `\input`/`\include` trees in document order.

## Key Details

- Output is EPUB3 with MathML for equations, auto-generated TOC (`--toc`), and numbered sections
- Pandoc resource path includes `.:figures:images` to resolve image references
- `filter.lua` handles figure/table numbering in captions and resolves `\ref`/`\autoref`/`\eqref` links
- `epub.css` styles the generated EPUB (includes `.algorithmdisplay` for algorithm blocks)
- The `paper/` directory and `paper.tar.gz` are ephemeral working artifacts, recreated on each run
- Dependencies are declared inline via PEP 723 metadata at the top of `paper2epub.py` — `uv` resolves them automatically
