# Simple Translation Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce repeated translation requests and retries while guaranteeing that ordinary paper-sized translations are complete.

**Architecture:** Preserve the existing glossary-first, one-body-request-per-file workflow and five-worker file pool. Add a small response validation/retry layer, translate globally deduplicated headings once, and include the main TeX body in the existing file translation pass.

**Tech Stack:** Python 3.12, OpenAI-compatible client, pytest, pathlib, concurrent.futures

## Global Constraints

- Keep all production logic in `paper2epub.py`.
- Do not add token schedulers, paragraph worker queues, persistent caches, or placeholder systems.
- Keep one body request per TeX file and `ThreadPoolExecutor(max_workers=5)`.
- Read TeX files with `errors="replace"`.
- All tests must avoid network and API calls.

---

### Task 1: Validate batch responses and retry only affected paragraphs

**Files:**
- Modify: `paper2epub.py:938-1114`
- Test: `test_paper2epub.py:322-370`

**Interfaces:**
- Produces: `_has_balanced_braces(text: str) -> bool`
- Produces: `_request_numbered_translation(client, system_prompt: str, numbered_paragraphs: dict[int, str]) -> dict[int, str]`
- Changes: `_batch_translate(...) -> dict[int, str]` returns every requested ID or raises `RuntimeError`

- [ ] **Step 1: Replace destructive brace-fix tests with failing validation tests**

```python
class TestHasBalancedBraces:
    def test_balanced(self):
        assert p._has_balanced_braces(r"中文 \\textbf{术语}")

    def test_extra_closing_is_invalid(self):
        assert not p._has_balanced_braces("中文}")

    def test_missing_closing_is_invalid(self):
        assert not p._has_balanced_braces(r"中文 \\textbf{术语")


class TestBatchTranslate:
    def test_retries_only_missing_ids(self, monkeypatch):
        calls = []
        responses = iter(["[0]\n译文零", "[1]\n译文一"])

        def fake_chat(client, system_prompt, user_prompt):
            calls.append(user_prompt)
            return next(responses)

        monkeypatch.setattr(p, "_chat", fake_chat)
        result = p._batch_translate(object(), "term | 术语", {0: "zero", 1: "one"})

        assert result == {0: "译文零", 1: "译文一"}
        assert "[0]" in calls[0] and "[1]" in calls[0]
        assert "[0]" not in calls[1] and "[1]" in calls[1]

    def test_retries_invalid_braces(self, monkeypatch):
        responses = iter(["[0]\n错误}", "[0]\n正确"])
        monkeypatch.setattr(p, "_chat", lambda *args: next(responses))
        assert p._batch_translate(object(), "", {0: "source"}) == {0: "正确"}

    def test_raises_when_retry_is_still_incomplete(self, monkeypatch):
        monkeypatch.setattr(p, "_chat", lambda *args: "")
        with pytest.raises(RuntimeError, match="0"):
            p._batch_translate(object(), "", {0: "source"})
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py::TestHasBalancedBraces test_paper2epub.py::TestBatchTranslate -q
```

Expected: failures because `_has_balanced_braces` is undefined and `_batch_translate` does not retry partial responses.

- [ ] **Step 3: Implement minimal validation and targeted retry**

```python
def _has_balanced_braces(text: str) -> bool:
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _request_numbered_translation(client, system_prompt, numbered_paragraphs):
    user_prompt = "\n\n".join(
        f"[{idx}]\n{numbered_paragraphs[idx]}" for idx in sorted(numbered_paragraphs)
    )
    for attempt in range(3):
        try:
            return _parse_numbered_response(_chat(client, system_prompt, user_prompt))
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")
```

Update `_batch_translate` to request all paragraphs once, compute IDs that are missing or have unbalanced braces, request that subset once, merge valid retry results, and raise `RuntimeError` listing IDs that remain invalid.

- [ ] **Step 4: Run focused and full tests and verify GREEN**

Run:

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py::TestHasBalancedBraces test_paper2epub.py::TestBatchTranslate -q
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py -q
```

Expected: all selected tests and the full suite pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add paper2epub.py test_paper2epub.py
git commit -m "fix: validate translation batches"
```

### Task 2: Translate headings globally and include the main document body

**Files:**
- Modify: `paper2epub.py:975-1002,1117-1246`
- Test: `test_paper2epub.py:872-957`

**Interfaces:**
- Changes: `extract_section_headings(tex_files: list[Path]) -> list[str]`
- Produces: `_build_heading_translations(client, glossary: str, headings: list[str]) -> dict[str, str]`
- Changes: `_translate_headings(heading_translations: dict[str, str], content: str) -> str`
- Changes: `translate_file_content(client, glossary: str, heading_translations: dict[str, str], content: str) -> str`

- [ ] **Step 1: Write failing orchestration tests**

```python
class TestTranslateTexFiles:
    def test_translates_main_body_and_deduplicates_headings(self, tmp_path, monkeypatch):
        main = tmp_path / "main.tex"
        child = tmp_path / "child.tex"
        main.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\section{Intro}\nMain body with enough prose.\n"
            "\\input{child}\n\\end{document}\n"
        )
        child.write_text("\\section{Intro}\nChild body with enough prose.\n")
        heading_calls = []
        translated_bodies = []

        monkeypatch.setattr(p, "extract_glossary", lambda *args: "term | 术语")

        def fake_heading_texts(client, glossary, texts):
            heading_calls.append(texts)
            return {0: "引言"}

        def fake_translate(client, glossary, heading_map, content):
            translated_bodies.append(content)
            assert heading_map == {"Intro": "引言"}
            return content + "\n译文"

        monkeypatch.setattr(p, "_translate_heading_texts", fake_heading_texts)
        monkeypatch.setattr(p, "translate_file_content", fake_translate)

        p.translate_tex_files(tmp_path, main, object(), "Title")

        assert heading_calls == [["Intro"]]
        assert len(translated_bodies) == 2
        assert "Main body" in main.read_text()
        assert main.read_text().count("译文") == 1
        assert child.read_text().count("译文") == 1
```

Add a focused assertion that `extract_glossary`'s system prompt contains an explicit maximum of 50 terms by monkeypatching `_chat` and inspecting the captured prompt.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py::TestTranslateTexFiles -q
```

Expected: failure because the current implementation excludes `main.tex`, translates headings per file, and uses the old `translate_file_content` signature.

- [ ] **Step 3: Implement the minimal global-heading orchestration**

Move `get_input_order(main_tex)` before glossary extraction, collect headings only from those files, deduplicate with `list(dict.fromkeys(headings))`, call `_translate_heading_texts` once, and build a title-to-Chinese mapping. Pass the immutable mapping to each `translate_one` call and set `files_to_translate = input_files`.

Change `_translate_headings` so it only scans insertion positions and applies the supplied mapping. Change `translate_file_content` to call this pure write-back helper. Add `5. 最多提取 50 个最重要的术语` to the glossary system prompt.

- [ ] **Step 4: Run focused and full tests and verify GREEN**

Run:

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py::TestTranslateTexFiles -q
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py -q
```

Expected: all selected tests and the full suite pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add paper2epub.py test_paper2epub.py
git commit -m "perf: reuse global heading translations"
```

### Task 3: Final regression and scope verification

**Files:**
- Verify: `paper2epub.py`
- Verify: `test_paper2epub.py`
- Verify: `docs/superpowers/specs/2026-07-10-simple-translation-optimization-design.md`

**Interfaces:**
- Consumes all interfaces produced by Tasks 1 and 2.
- Produces a verified implementation with no additional runtime dependencies.

- [ ] **Step 1: Run syntax compilation**

```bash
uv run python -m py_compile paper2epub.py test_paper2epub.py
```

Expected: exit code 0 with no output.

- [ ] **Step 2: Run the complete test suite**

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 3: Inspect final scope and whitespace**

```bash
git diff --check
git status --short
git diff --stat HEAD~2..HEAD
```

Expected: no whitespace errors; only translation implementation, tests, and approved design/plan documentation are changed.
