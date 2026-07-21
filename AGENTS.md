# AGENTS.md

This file provides guidance for coding agents when working with code in this repository.

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
3. Parse selected LaTeX commands with `pylatexenc`, then safely simplify the
   document class and remove `\maketitle`
4. Strip incompatible packages, configuration, layout noise, and annotation helpers
5. Extract paper title and authors (with macro expansion)
6. Convert explicit PDF figures to PNG; when the source contains TikZ figures,
   extract their rendered images from the arXiv PDF while preserving outer and
   subfigure labels
7. Normalize citations, hyperref, tables, figures, theorems, listings, and algorithms
8. (Optional) Translate to Chinese via Qwen3.6-Flash with two-phase strategy: glossary extraction → concurrent paragraph translation
9. Resolve `\input`/`\include` targets and append confirmed `.tex` extensions
10. Run pandoc with `filter.lua` to produce EPUB3, assign source-scoped numbers
    to equations, figures, and tables, and resolve cross-references

Reference: https://info.arxiv.org/help/submit_tex.html and https://info.arxiv.org/help/submit_latex_best_practices.html

## Dependencies

- `uv` — runs the script with automatic dependency management (Python deps declared via PEP 723 inline metadata)
- `curl` — downloads the arXiv source tarball
- `pandoc` — converts LaTeX to EPUB3 (uses `--mathml` for math rendering)
- `pylatexenc>=2.10,<3` — parses LaTeX structure (declared in the PEP 723 metadata)
- `DASHSCOPE_API_KEY` environment variable — required for `--translate` mode (Alibaba Bailian API key)
- `SMTP_PROXY` environment variable — optional SOCKS5 proxy for `--email` mode (format: `socks5://[user:pass@]host:port`)

## Testing

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py -q
```

- All tests live in `test_paper2epub.py`. No network or API calls — tests use `tmp_path` for filesystem operations.
- **Test every pure transform or planner.** If you add a legacy preprocessing
  pass, test its `_*_content` function. For syntax-aware passes, test planned
  edits, safety gates, malformed input, and idempotence.
- Tests are organized by class: `TestFindMatchingBrace`, `TestExtractBraceArg`, etc. — follow this pattern.
- When modifying any content transform, run tests to verify no regressions.
- **After generating an EPUB, sample it against the source PDF.** Compare at
  least the beginning, a later chapter, and the appendix when present. Verify
  figure/table numbering, captions, image identity and completeness, and
  nearby text; report the sampled locations and any discrepancies.

## Code Style & Maintainability

### Architecture

- **Single-file Python application** — orchestration and LaTeX preprocessing
  live in `paper2epub.py`; EPUB AST transforms live in `filter.lua`. Do not
  split the Python application into additional modules.
- **Avoid Python module-level mutable state.** `_algorithm_counter` is a known
  exception — don't add more.
- **Keep preprocessing passes focused.** Each pass handles one concern
  (citations, hyperref, tables, etc.) and is ordered by the preprocessing plan.
- **Use syntax-aware edits for structural LaTeX.** `LatexDocument` wraps
  `pylatexenc`; `LatexNodeRef`/`LatexArgumentRef` expose repository-owned
  source ranges and completeness metadata without leaking parser nodes.
- **Treat incomplete syntax conservatively.** A node inside an incomplete
  group is `opaque`; `Safety.SAFE` passes must leave incomplete or opaque
  ranges unchanged.
- **Plan before writing.** Syntax-aware passes return immutable `Edit` values;
  `EditPlanner` validates file ownership, ranges, and overlap before applying
  edits in reverse source order.
- **Derive numbering scope from the source once.** Python scans the discovered
  include order for a complete, numbered `\chapter` and passes either `global`
  or `chapter` to pandoc as `paper2epub-numbering-scope`. Do not infer a second
  scope independently in Lua.
- **Assign numbered objects with one Lua state machine.** `filter.lua` reads the
  metadata first, then uses one top-down document walk for chapter headers,
  equations, figures, and tables. Equation, figure, and table counters reset at
  each numbered chapter or explicit appendix header. Articles without numbered
  chapters retain global counters.
- **Keep captions and references on the same final number.** The numbering walk
  stores each final number before prefixing captions; later `\ref`, `\autoref`,
  and `\eqref` resolution consumes that stored value. Do not reintroduce
  separate collection and rendering counters.
- **Preserve TikZ label aliases.** TikZ replacement keeps labels found inside
  the figure body. Pandoc exposes them as labeled spans inside the replacement
  figure, and Lua maps them in document order to the parent figure number plus
  alphabetic suffixes (`a`, `b`, ...).
- **Files:** `paper2epub.py` (main script), `filter.lua` (pandoc Lua filter for refs/captions), `epub.css` (EPUB styling), `test_paper2epub.py` (tests).

### Preprocessing function patterns

Legacy text transforms follow this convention:
1. A **pure transform** function named `_<operation>_content(content: str) -> str` — testable with no filesystem access.
2. A **file-level wrapper** named `<operation>(paper_dir: Path)` — a one-liner calling `_transform_tex_files`.

Example: `_strip_noise_content` (pure) → `strip_noise_commands` (file-level wrapper). When adding a new pass, follow this pattern.

Structural transforms should instead use:
1. A pure planner receiving `SourceFile`, `LatexDocument`, and any required
   path context, returning `list[Edit]`.
2. A file-level wrapper that parses once, applies edits through `EditPlanner`,
   and writes only when the content changed.

Example: `plan_simplify_documentclass` → `simplify_documentclass`.
Prefer this syntax-aware pattern for commands, arguments, environments, or
nested structures; do not add new Regex-based structural parsing.

### Reusable utilities — use these instead of writing ad-hoc logic

| Utility | Purpose | Don't reinvent |
|---|---|---|
| `SourceFile.from_path(path)` | Read TeX with `errors="replace"` and retain its path | Direct non-tolerant TeX reads |
| `LatexDocument(source)` | Query parsed commands/environments and stable argument/source ranges | Regex command/environment boundary detection |
| `LatexNodeRef.complete` / `.opaque` | Gate safe rewrites on trustworthy syntax | Per-pass malformed-input guesses |
| `EditPlanner.apply(source, edits)` | Validate and apply non-overlapping source edits | Direct multi-edit string slicing |
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
- **Test syntax-aware passes at the planning layer.** Assert generated edits,
  safety level, idempotence, and preservation of incomplete/opaque input.
- **Use `SCRIPT_DIR`** (`Path(__file__).resolve().parent`) to resolve paths relative to the script (e.g. `epub.css`, `filter.lua`).
- **Translation uses `openai` SDK** against Alibaba Bailian's API (`DASHSCOPE_BASE_URL`). The `_chat` helper wraps single-turn completions.
- **Multi-file TeX documents** — use `get_input_order(main_tex)` to walk `\input`/`\include` trees in document order.
- **Input extension normalization is path-aware.**
  `_normalize_input_extensions_content` resolves targets relative to the
  referencing `.tex` file first, then the paper root.
- **Numbering scope follows LaTeX structure.** A document with numbered
  `\chapter` commands uses chapter-local equation/figure/table counters; an
  explicit appendix prefix uses `A`, `B`, and so on; other documents use global
  counters.

## Repository Workflow

- Never commit AI-generated design, specification, planning, or implementation-plan documents. Keep such artifacts outside the repository or leave them untracked for local use only.
- Before merging any development branch into a trunk branch such as `main` or `master`, squash all branch commits into a single commit. Do not merge a multi-commit branch history directly into the trunk.
- After repository changes are complete and verified, automatically create the squashed local commit, merge it into the local `main` branch, and verify the merged result. This is standing authorization for local commits and local merges; do not ask for confirmation each time.

## Key Details

- Output is EPUB3 with MathML for equations, auto-generated TOC (`--toc`), and numbered sections
- Pandoc resource path includes `.:figures:images` to resolve image references
- `filter.lua` numbers equations, figures, and tables in one document-order walk;
  captions and `\ref`/`\autoref`/`\eqref` links share the stored final numbers
- TikZ figure replacement preserves inner labels, which resolve to subfigure
  suffixes such as `Figure 1.7c`
- `epub.css` styles the generated EPUB (includes `.algorithmdisplay` for algorithm blocks)
- The `paper/` directory and `paper.tar.gz` are ephemeral working artifacts, recreated on each run
- Dependencies are declared inline via PEP 723 metadata at the top of `paper2epub.py` — `uv` resolves them automatically
