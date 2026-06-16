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

The script downloads the LaTeX source tarball from arxiv.org, extracts it into `paper/`, finds the main `.tex` file (by looking for `\documentclass` or `\begin{document}`), and runs pandoc to produce `<arxiv-id>.epub`.

With `--translate`, it uses Alibaba Bailian's Qwen3.6-Flash model to translate the paper to Chinese, producing a bilingual EPUB (`<arxiv-id>-zh.epub`) with English and Chinese paragraphs interleaved.

## Pipeline Steps

1. Download and extract arXiv source tarball
2. Find the main `.tex` file
3. Simplify `\documentclass` for pandoc compatibility
4. Extract paper title (with macro expansion)
5. Convert PDF figures to PNG via pypdfium2
6. Rewrite `.pdf` image references to `.png` in tex files
7. Resolve LaTeX cross-references (`\ref`, `\autoref`, `\cref`, `\eqref`)
8. Preprocess algorithm/algorithmic environments into pandoc-friendly LaTeX
9. (Optional) Translate to Chinese via Qwen3.6-Flash with two-phase strategy: glossary extraction → context-aware paragraph translation
10. Run pandoc to produce EPUB3

## Dependencies

- `uv` — runs the script with automatic dependency management (Python deps declared via PEP 723 inline metadata)
- `curl` — downloads the arXiv source tarball
- `pandoc` — converts LaTeX to EPUB3 (uses `--mathml` for math rendering)
- `DASHSCOPE_API_KEY` environment variable — required for `--translate` mode (Alibaba Bailian API key)

## Key Details

- Output format is EPUB3 with MathML for equations and auto-generated table of contents (`--toc`)
- Pandoc resource path includes `.:figures:images` to resolve image references
- `epub.css` provides styling for the generated EPUB
- The `paper/` directory and `paper.tar.gz` are ephemeral working artifacts, recreated on each run
