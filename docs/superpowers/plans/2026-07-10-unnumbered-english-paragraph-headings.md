# Unnumbered English Paragraph Headings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert every safe English `\paragraph` heading to `\paragraph*` before Pandoc in translated and untranslated modes.

**Architecture:** Add one syntax-aware planner and one file wrapper following the existing `SourceFile`/`LatexDocument`/`EditPlanner` pattern. Invoke the wrapper after optional translation and before input-extension normalization so both CLI modes receive identical final heading semantics.

**Tech Stack:** Python 3.12, pylatexenc, pytest, pandoc

## Global Constraints

- Keep production logic in `paper2epub.py`.
- Insert only `*`; preserve all other source bytes.
- Leave starred, incomplete, opaque, and missing-title commands unchanged.
- Do not change section, subsection, or subsubsection numbering.
- Tests make no network or API calls.

---

### Task 1: Add the syntax-aware paragraph unnumbering pass

**Files:**
- Modify: `paper2epub.py:1380-1460,2590-2605`
- Test: `test_paper2epub.py` near syntax-aware planner tests

**Interfaces:**
- Produces: `plan_unnumber_paragraphs(source: SourceFile, document: LatexDocument) -> list[Edit]`
- Produces: `unnumber_paragraph_headings(paper_dir: Path) -> None`
- Consumes: `LatexNodeRef.command_token_end`, raw star argument at `ref.arguments[0]`, and required title via `document.argument(ref, 0)`.

- [ ] **Step 1: Write failing planner and wrapper tests**

```python
class TestPlanUnnumberParagraphs:
    def plan(self, tmp_path, content):
        source = p.SourceFile(tmp_path / "main.tex", content)
        document = p.LatexDocument(source)
        return source, p.plan_unnumber_paragraphs(source, document)

    def test_inserts_star_after_command_token(self, tmp_path):
        source, edits = self.plan(tmp_path, r"\paragraph{Contributions.} Body")
        assert len(edits) == 1
        assert edits[0].start == len(r"\paragraph")
        assert edits[0].start == edits[0].end
        assert edits[0].replacement == "*"
        assert edits[0].safety is p.Safety.SAFE
        assert p.EditPlanner.apply(source, edits) == r"\paragraph*{Contributions.} Body"

    def test_preserves_optional_nested_multiline_content(self, tmp_path):
        content = "\\paragraph[Short]{About \\textbf{Method}\nDetails}\\label{x} Body"
        source, edits = self.plan(tmp_path, content)
        assert p.EditPlanner.apply(source, edits) == content.replace(
            r"\paragraph", r"\paragraph*", 1
        )

    def test_already_starred_is_unchanged(self, tmp_path):
        _, edits = self.plan(tmp_path, r"\paragraph*{Existing}")
        assert edits == []

    def test_incomplete_title_is_unchanged(self, tmp_path):
        _, edits = self.plan(tmp_path, r"\paragraph{Incomplete")
        assert edits == []

    def test_opaque_paragraph_is_unchanged(self, tmp_path):
        _, edits = self.plan(tmp_path, r"\section{Outer \paragraph{Inner}")
        assert edits == []

    def test_idempotent(self, tmp_path):
        source, edits = self.plan(tmp_path, r"\paragraph{Title}")
        once = p.EditPlanner.apply(source, edits)
        second_source, second_edits = self.plan(tmp_path, once)
        assert p.EditPlanner.apply(second_source, second_edits) == once
        assert second_edits == []


class TestUnnumberParagraphHeadings:
    def test_processes_nested_tex_files(self, tmp_path):
        nested = tmp_path / "sections"
        nested.mkdir()
        tex = nested / "intro.tex"
        tex.write_text(r"\paragraph{Contributions.} Body")

        p.unnumber_paragraph_headings(tmp_path)

        assert tex.read_text() == r"\paragraph*{Contributions.} Body"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py::TestPlanUnnumberParagraphs test_paper2epub.py::TestUnnumberParagraphHeadings -q
```

Expected: tests fail because the planner and wrapper do not exist.

- [ ] **Step 3: Implement the planner and wrapper**

```python
def plan_unnumber_paragraphs(
    source: SourceFile,
    document: LatexDocument,
) -> list[Edit]:
    edits = []
    for ref in document.commands("paragraph"):
        title = document.argument(ref, 0)
        star = ref.arguments[0] if ref.arguments else None
        if (
            not ref.complete
            or ref.opaque
            or title is None
            or not title.complete
            or title.opaque
            or star is not None
        ):
            continue
        edits.append(
            Edit(
                file=source.path,
                start=ref.command_token_end,
                end=ref.command_token_end,
                replacement="*",
                pass_name="unnumber_paragraphs",
                safety=Safety.SAFE,
            )
        )
    return edits


def unnumber_paragraph_headings(paper_dir: Path) -> None:
    for tex in paper_dir.glob("**/*.tex"):
        source = SourceFile.from_path(tex)
        document = LatexDocument(source)
        edits = plan_unnumber_paragraphs(source, document)
        if edits:
            tex.write_text(EditPlanner.apply(source, edits))
            print(f"Unnumbered paragraph headings: {tex.name}", file=sys.stderr)
```

- [ ] **Step 4: Run focused and complete tests and verify GREEN**

Run:

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py::TestPlanUnnumberParagraphs test_paper2epub.py::TestUnnumberParagraphHeadings -q
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py -q
```

Expected: focused tests pass and the complete suite has zero failures.

- [ ] **Step 5: Wire the pass into both CLI modes**

Insert this unconditional call immediately after the optional translation block and before `normalize_input_extensions(paper_dir)`:

```python
unnumber_paragraph_headings(paper_dir)
```

Add a source-level pipeline-order test that reads `paper2epub.py` and asserts the call appears after `translate_tex_files(...)` and before `normalize_input_extensions(...)`.

```python
class TestParagraphUnnumberingPipeline:
    def test_runs_after_translation_and_before_input_normalization(self):
        source = Path(p.__file__).read_text()
        main_source = source[source.index("def main():") :]

        assert (
            main_source.index("translate_tex_files(")
            < main_source.index("unnumber_paragraph_headings(")
            < main_source.index("normalize_input_extensions(")
        )
```

- [ ] **Step 6: Verify Pandoc semantics manually**

Run:

```bash
uv run python -c 'import subprocess; r=subprocess.run(["pandoc","--from=latex","--to=native","--number-sections"],input="\\section{Numbered}\n\\paragraph*{Unnumbered}\nBody\n",text=True,capture_output=True,check=True); print(r.stdout)'
```

Expected: the section `Header` has a `number` attribute and the paragraph `Header`
has the `unnumbered` class without a generated number.

- [ ] **Step 7: Commit**

```bash
git add paper2epub.py test_paper2epub.py
git commit -m "fix: unnumber English paragraph headings"
```

### Task 2: Final verification

**Files:**
- Verify: `paper2epub.py`
- Verify: `test_paper2epub.py`

**Interfaces:**
- Consumes the Task 1 planner, wrapper, and pipeline call.
- Produces a clean tested branch with no generated paper or EPUB artifacts committed.

- [ ] **Step 1: Compile and run all tests**

```bash
uv run python -m py_compile paper2epub.py test_paper2epub.py
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py -q
```

Expected: compilation succeeds and all tests pass.

- [ ] **Step 2: Inspect final scope**

```bash
git diff --check
git status --short
git diff --stat HEAD~1..HEAD
```

Expected: the implementation commit changes only `paper2epub.py` and `test_paper2epub.py`.
