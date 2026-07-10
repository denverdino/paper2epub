# Unnumbered Translated Paragraph Headings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the English `\paragraph` heading number while rendering its Chinese translation as unnumbered plain text.

**Architecture:** Extend the existing syntax-aware global heading pipeline to include `paragraph`. Remove paragraph heading lines before body translation, then reuse the existing plain-text heading write-back so no second LaTeX heading command is generated.

**Tech Stack:** Python 3.12, pylatexenc, pytest, pandoc-compatible LaTeX preprocessing

## Global Constraints

- Keep all production logic in `paper2epub.py`.
- Preserve the English `\paragraph` command and its Pandoc numbering.
- Never generate a Chinese `\paragraph` command.
- Leave incomplete or opaque paragraph syntax unchanged.
- Do not add dependencies or API calls to tests.

---

### Task 1: Route paragraph headings through the global heading translator

**Files:**
- Modify: `paper2epub.py:74-91,919-922,979-1001,1181-1220`
- Test: `test_paper2epub.py:930-1030`

**Interfaces:**
- Changes parser metadata so `LatexDocument.commands("paragraph")` exposes the title as argument 0.
- Changes `extract_section_headings(tex_files: list[Path]) -> list[str]` to include paragraph titles in source order.
- Changes `_strip_heading_lines(text: str) -> str` to remove paragraph command lines.
- Changes `_translate_headings(heading_translations: dict[str, str], content: str) -> str` to insert translated paragraph titles as plain text.

- [ ] **Step 1: Write failing regression tests**

```python
class TestExtractSectionHeadings:
    def test_includes_paragraph_with_nested_title(self, tmp_path):
        tex = tmp_path / "section.tex"
        tex.write_text(r"\paragraph[Short]{About \textbf{Method}}")

        assert p.extract_section_headings([tex]) == [r"About \textbf{Method}"]


class TestStripHeadingLines:
    def test_strips_paragraph(self):
        text = r"\paragraph{Contributions.}" + "\nBody text."
        result = p._strip_heading_lines(text)

        assert "Contributions" not in result
        assert result == "Body text."


class TestTranslateHeadings:
    def test_paragraph_translation_is_plain_text(self):
        content = r"\paragraph{Contributions.}\label{para:contrib}" + "\nBody."
        result = p._translate_headings({"Contributions.": "贡献。"}, content)

        assert result.count(r"\paragraph") == 1
        assert r"\paragraph{贡献。}" not in result
        assert r"\label{para:contrib}" + "\n\n贡献。\n" in result


class TestTranslateFileContent:
    def test_paragraph_heading_is_not_duplicated(self, monkeypatch):
        content = r"\paragraph{Contributions.}" + "\nBody text with enough prose."
        monkeypatch.setattr(
            p, "_batch_translate", lambda client, glossary, numbered: {0: "正文。"}
        )

        result = p.translate_file_content(
            object(), "", {"Contributions.": "贡献。"}, content
        )

        assert result.count(r"\paragraph") == 1
        assert r"\paragraph{贡献。}" not in result
        assert "贡献。" in result
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py::TestExtractSectionHeadings::test_includes_paragraph_with_nested_title test_paper2epub.py::TestStripHeadingLines::test_strips_paragraph test_paper2epub.py::TestTranslateHeadings test_paper2epub.py::TestTranslateFileContent::test_paragraph_heading_is_not_duplicated -q
```

Expected: paragraph extraction and stripping fail because the command is not registered; `TestTranslateHeadings` fails because write-back ignores paragraph commands.

- [ ] **Step 3: Implement the minimal production change**

Add `"paragraph": "*[{"` to `_PARSER_MACRO_ARGS` and `"paragraph": (2,)` to `_PARSER_ARGUMENT_INDEXES`.

Change the command tuple in `extract_section_headings` to:

```python
("section", "subsection", "subsubsection", "paragraph")
```

Change `SECTION_HEADING_LINE_RE` to recognize `paragraph`:

```python
r"^\s*\\(?:section|subsection|subsubsection|paragraph)\*?(?:\[[^\]]*\])?\{"
```

Change `_translate_headings`' command regex to the same four-command set. Keep the existing insertion code unchanged so the Chinese title is inserted as plain text after the original command and optional label.

- [ ] **Step 4: Run focused and complete tests and verify GREEN**

Run:

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py::TestExtractSectionHeadings::test_includes_paragraph_with_nested_title test_paper2epub.py::TestStripHeadingLines::test_strips_paragraph test_paper2epub.py::TestTranslateHeadings test_paper2epub.py::TestTranslateFileContent::test_paragraph_heading_is_not_duplicated -q
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py -q
```

Expected: focused tests pass and the complete suite has zero failures.

- [ ] **Step 5: Commit**

```bash
git add paper2epub.py test_paper2epub.py
git commit -m "fix: keep translated paragraph headings unnumbered"
```

### Task 2: Final verification

**Files:**
- Verify: `paper2epub.py`
- Verify: `test_paper2epub.py`

**Interfaces:**
- Consumes the Task 1 heading pipeline changes.
- Produces a clean, tested branch with no generated EPUB or downloaded paper files committed.

- [ ] **Step 1: Compile and run the complete suite**

```bash
uv run python -m py_compile paper2epub.py test_paper2epub.py
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py -q
```

Expected: compilation succeeds and all tests pass.

- [ ] **Step 2: Inspect scope**

```bash
git diff --check
git status --short
git diff --stat HEAD~1..HEAD
```

Expected: only `paper2epub.py` and `test_paper2epub.py` changed in the fix commit; no paper sources or EPUB artifacts are tracked.
