---
name: paper2epub
description: Use when the user wants to convert an arXiv paper to EPUB ebook format, mentions an arXiv ID or URL and wants a readable ebook, or asks to make a paper available on an e-reader. Triggers on phrases like "convert paper to epub", "make this paper readable", "arXiv to ebook", or any arXiv ID (e.g., 2402.08954) combined with ebook/epub intent.
---

# Convert arXiv Paper to EPUB

Convert an arXiv paper's LaTeX source into an EPUB3 ebook.

## Extract the arXiv ID

Determine the arXiv ID from the user's input. Accept any of these formats:
- Bare ID: `2402.08954`
- Full URL: `https://arxiv.org/abs/2402.08954` or `https://arxiv.org/pdf/2402.08954`
- With version suffix: `2402.08954v2` (strip the version — use `2402.08954`)

If ambiguous, ask the user to confirm.

## Check Dependencies

**uv:**
```bash
which uv
```
If missing: `brew install uv` (macOS) or `curl -LsSf https://astral.sh/uv/install.sh | sh`.

**curl:**
```bash
which curl
```
Pre-installed on macOS. If missing: `brew install curl`.

**pandoc:**
```bash
which pandoc
```
If missing: `brew install pandoc`.

Stop and report to the user if any dependency cannot be installed.

## Determine Translation Mode

Check if the user wants a Chinese translation. Look for keywords like "翻译", "中文", "translate", "bilingual", or "双语".

- If translation is requested, ensure `DASHSCOPE_API_KEY` is set. If not, ask the user for it.
- If not mentioned, default to English-only.

## Run the Conversion

Execute from the project directory:
```bash
uv run paper2epub.py <arxiv-id>
```

For bilingual (English + Chinese) output:
```bash
DASHSCOPE_API_KEY=<key> uv run paper2epub.py <arxiv-id> --translate
```

The script downloads LaTeX source from arxiv.org, converts PDF figures to PNG, resolves cross-references, preprocesses algorithm environments, and runs pandoc to produce EPUB3 with MathML math rendering. With `--translate`, it uses Qwen3.6-Flash to produce a bilingual EPUB with English and Chinese paragraphs interleaved.

## Verify Output

Check that the EPUB file was created:
```bash
ls -la <arxiv-id>.epub
# or for translated version:
ls -la <arxiv-id>-zh.epub
```

Report the file path and size to the user. A typical paper produces a 0.5–2 MB EPUB.

## Error Handling

- **Download failure** (curl error / HTTP 404): Confirm the arXiv ID with the user and check that `https://arxiv.org/abs/<arxiv-id>` exists.
- **No .tex file found**: The source may be PDF-only. This tool requires LaTeX source — inform the user.
- **pandoc failure**: Check stderr for the specific error and report it.
- **pypdfium2 warning**: Non-fatal — some PDF figures may fail to convert but the EPUB will still generate.
- **Translation failure**: Individual paragraph translation failures are non-fatal — untranslated paragraphs will remain English-only. Report any warnings to the user.
- **Missing DASHSCOPE_API_KEY**: If `--translate` is used without setting the environment variable, the script will exit with an error. Ask the user for the key.
