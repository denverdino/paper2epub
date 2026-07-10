# Final Review Fix Report

## Scope and commits

- Branch: `codex/latex-parser-infrastructure`
- Worktree: `/private/tmp/paper2epub-latex-parser`
- Implementation commit: `96a8b73 fix: make LaTeX parser rewrites conservative`
- Scope stayed within parser/edit infrastructure, document-class planning,
  input-extension normalization, tests, and test documentation. No resource
  resolver, retry system, workspace abstraction, or later-phase pass migration
  was added.

## Finding-to-fix mapping

1. **Incomplete groups were truncated**
   - Added immutable repository-owned `LatexArgumentRef` with source range,
     text, delimiters, `complete`, and `opaque`.
   - Complete groups exclude verified delimiters from `text`; incomplete
     groups retain all content after the opening delimiter, so a nonexistent
     closing delimiter is never stripped.
   - Completeness is checked with the repository brace/bracket matchers rather
     than trusting a tolerant parser node that happens to end in `}` or `]`.
   - Added complete required/optional and incomplete required/optional contract
     tests.

2. **Malformed `documentclass` could erase the body**
   - `plan_simplify_documentclass` now requires a complete, non-opaque command
     and a complete `{...}` class argument before emitting a `SAFE` edit.
   - Added malformed required argument, malformed optional argument, missing
     argument, and exact body-preservation assertions.

3. **Raw pylatexenc nodes leaked into production passes**
   - Removed `node: Any` from public `LatexNodeRef`.
   - `LatexNodeRef` now exposes repository-owned arguments, command token end,
     post-space end, completeness, and opacity.
   - Raw `nodeargd` and `macro_post_space` access is confined to private
     `LatexDocument` construction. The production planner uses
     `document.argument(...)` and `command_token_end` only.

4. **Nested includes resolved only from the document root**
   - Added pure `_normalize_input_extensions_content(content, tex_path,
     paper_dir)`.
   - Resolution checks the referencing file's directory first, then the
     document root.
   - Added root, nested sibling, existing extension, missing target,
     idempotence, and file-wrapper tests.

5. **Test command did not reproduce the pylatexenc environment**
   - Updated `AGENTS.md` to document the exact command:
     `uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py -q`.
   - Documented the pinned parser dependency in the dependency list.

6. **Weak incomplete-input test**
   - Replaced it with behavior assertions for non-truncated text, stable
     ranges and metadata, parser-node isolation, and conservative planning.

7. **Missing `_strip_at_commands` direct coverage**
   - Added direct pure-transform tests for a complete definition, nested
     replacement text, incomplete definition, non-definition text, and
     idempotence.
   - The package stripping subsystem itself was deliberately not redesigned.

## TDD evidence

### RED

Command:

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest \
  test_paper2epub.py::TestLatexDocument \
  test_paper2epub.py::TestPlanSimplifyDocumentclass \
  test_paper2epub.py::TestNormalizeInputExtensionsContent -q
```

Observed before implementation:

```text
11 failed, 8 passed in 0.23s
```

Failures were the intended missing/incorrect behaviors: no stable `argument`
API, unsafe edits for all three malformed `documentclass` cases, and no pure
path-aware input-normalization function.

The `_strip_at_commands` tests characterize an existing pure transform and did
not require a production behavior change; they were added as direct regression
coverage rather than represented as a false RED cycle.

### GREEN

Final focused command:

```bash
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest \
  test_paper2epub.py::TestLatexDocument \
  test_paper2epub.py::TestPlanSimplifyDocumentclass \
  test_paper2epub.py::TestNormalizeInputExtensionsContent \
  test_paper2epub.py::TestStripAtCommands -q
```

Result:

```text
25 passed in 0.04s
```

## Final validation

```text
uv run --with pytest --with 'pylatexenc>=2.10,<3' python -m pytest test_paper2epub.py -q
241 passed in 0.13s

uv run --with 'pylatexenc>=2.10,<3' python -m py_compile paper2epub.py
exit 0

uv run --with 'pylatexenc>=2.10,<3' paper2epub.py --help
exit 0; help listed arxiv_id, --translate, and --email

git diff --check
exit 0

rg -n "ref\\.node|\\.nodeargd|\\.macro_post_space" paper2epub.py
no production-pass matches
```

## Self-review and concerns

- Incomplete optional groups that pylatexenc leaves outside a macro node are
  conservatively represented from `[` through end-of-file. This intentionally
  makes the entire uncertain tail opaque; it favors body preservation over a
  speculative shorter range.
- Input normalization still uses the existing command-argument iterator. A
  full migration of this pass to `LatexDocument` belongs to the later
  structural-regex migration phase and was not pulled into this review fix.
- `_strip_at_commands` retains its current regex-based semantics, including
  its existing treatment of commented source. This task added regression
  coverage without redesigning that subsystem, as required.
- No known failing tests, compile errors, CLI regressions, whitespace errors,
  or raw parser-node access from production planners remain in this scope.
