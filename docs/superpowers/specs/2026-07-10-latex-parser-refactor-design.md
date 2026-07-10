# LaTeX Parser Refactor Design

## Goal

Reduce arXiv-to-EPUB conversion failures by replacing structure-changing regular expressions with syntax-aware LaTeX processing, while preserving the current single-file Python architecture, command-line interface, output naming, translation mode, and email mode.

The refactor will add `pylatexenc` as a lightweight parser dependency. It will not implement a complete TeX engine. Dynamic constructs that cannot be understood safely will be preserved during normal conversion and changed only by an explicit compatibility fallback.

## Design Principles

- Keep all Python logic in `paper2epub.py`.
- Parse command, argument, environment, and nesting boundaries through `pylatexenc` rather than regular expressions.
- Treat original extracted sources as immutable. Every conversion attempt operates on an independent working copy.
- Separate syntax parsing, semantic indexing, rewrite planning, resource conversion, and Pandoc execution.
- Prefer conservative continuation over destructive preprocessing.
- Move operations that are naturally expressed over Pandoc's AST into `filter.lua`.
- Retain regular expressions only for leaf text and external diagnostic messages, not for structural LaTeX matching.

## Considered Approaches

### 1. `pylatexenc` syntax tree with source-range edits

This is the selected approach. It supports incremental migration, works within the single-file constraint, and retains Pandoc as the EPUB backend. It is not a full TeX interpreter, so macro-dependent regions require conservative handling.

### 2. Pandoc AST as the only structured representation

This would simplify figure, table, reference, caption, and metadata processing. However, Pandoc must successfully read the LaTeX before an AST exists, so it cannot address many of the current input-stage failures by itself.

### 3. LaTeXML as the conversion backend

This offers stronger TeX semantics and mature recovery behavior, but adds a large Perl/LaTeXML system dependency and changes the current lightweight `uv + pandoc` distribution and output pipeline.

The final design combines approach 1 with selective use of approach 2.

## Architecture

The single Python file is organized into five internal layers.

### Source Workspace

`PaperWorkspace` owns:

- immutable extracted source files;
- per-attempt working copies;
- main-file candidates and their scores;
- path resolution relative to both the referencing file and document root;
- diagnostic artifacts for failed attempts.

Every TeX file is read with `errors="replace"`. A pass never writes directly to the original extracted source.

### LaTeX Syntax Layer

`LatexDocument` wraps `pylatexenc` and prevents parser-specific node types from leaking into passes. Its stable query API includes operations equivalent to:

```python
document.commands("includegraphics")
document.environments("table")
document.command("documentclass")
document.argument_text(node, index)
```

Returned references contain the command or environment name, source file, source range, arguments, parent environment, and whether the parser considers the region opaque or incomplete.

### Semantic Index

`DocumentIndex` collects cross-file information required by conversion:

- the `\input` and `\include` dependency graph;
- macro definitions and statically identifiable uses;
- packages and environments;
- `\graphicspath` declarations and image references;
- labels, references, citations, and bibliography declarations;
- scored main-file candidates.

This is a deliberately limited analogue of LaTeXML's state. It records static facts but does not execute arbitrary TeX, expand dynamic definitions, or emulate catcodes. Regions depending on dynamic conditionals, catcode changes, or unresolvable macro expansion are marked opaque and preserved in safe attempts.

### Rewrite Planner

Passes return immutable edits instead of modifying strings or files:

```python
@dataclass(frozen=True)
class Edit:
    file: Path
    start: int
    end: int
    replacement: str
    pass_name: str
    safety: Safety
```

`EditPlanner` validates ranges, detects overlapping edits, rejects conflicts, and applies accepted edits in descending source order. Each pass is a pure planning function:

```python
def normalize_inputs(
    source: SourceFile,
    document: LatexDocument,
    index: DocumentIndex,
) -> list[Edit]:
    ...
```

Passes have three safety levels:

- `SAFE`: syntax-preserving or based on a verified resource, such as adding a confirmed `.tex` extension;
- `LOSSY`: may discard presentation details, such as flattening a subfigure;
- `FALLBACK_ONLY`: intentionally removes or replaces unsupported content, such as rasterizing or omitting a complex TikZ block.

### Conversion Attempts

Pandoc conversion is performed through isolated attempts. Every attempt records its main-file candidate, enabled passes, applied edits, Pandoc diagnostics, validation result, and final status.

```python
@dataclass
class ConversionAttempt:
    main_tex: Path
    enabled_passes: tuple[str, ...]
    stderr: str = ""
    succeeded: bool = False
```

## Component Responsibilities

### Python TeX stage

The Python syntax layer performs only changes needed to make the source readable by Pandoc:

- document class compatibility;
- package and configuration handling based on parsed command signatures;
- input/include resolution;
- resource resolution and verified image-reference rewriting;
- unsupported environment conversion when selected by an attempt;
- translation paragraph selection after structural parsing.

### Pandoc and Lua stage

Operations that do not need to mutate the input TeX move to Pandoc AST or `filter.lua`:

- figure, table, and equation numbering;
- label, ref, autoref, and eqref links;
- caption prefixes;
- EPUB metadata;
- theorem and algorithm presentation when the Pandoc AST can represent it safely.

This avoids parsing the same structural concept separately with Python regular expressions and Lua heuristics.

## Data Flow

1. Download the source archive with HTTP failure detection and extract it safely.
2. Build an immutable `PaperWorkspace`.
3. Parse all TeX sources into `LatexDocument` instances.
4. Build `DocumentIndex` and rank main-file candidates.
5. Resolve graphic resources and plan required conversions.
6. Select a conversion attempt and its passes.
7. Generate, validate, and apply edits to an isolated working copy.
8. Run Pandoc on the selected main file.
9. Apply Lua/Pandoc AST postprocessing.
10. Validate the generated EPUB.

## Main-File Selection

All complete-document candidates are scored. Useful positive signals include:

- `\documentclass`, `\begin{document}`, and `\end{document}` occurring together;
- a title and meaningful body content;
- filenames containing `main`, `paper`, or the arXiv identifier;
- a larger successfully resolved include graph.

Files included by another candidate, examples, responses, appendices, and supplements receive lower scores. If all attempts for the highest-scoring candidate fail, conversion proceeds to the next candidate.

## Resource Resolution

Image references are resolved through one component that combines:

- the referencing TeX file's directory;
- the document root;
- active `\graphicspath` entries;
- explicit or inferred extensions;
- a deterministic extension preference list.

The resolver supports at least PDF, EPS, PS, SVG, PNG, JPEG, and GIF. A reference is rewritten only after the destination resource was generated successfully. Generated resources preserve a mapping to their original file for diagnostics.

## Failure Recovery

Each main-file candidate can be processed using three attempt levels.

### Minimal

Only safe changes are enabled: encoding tolerance, verified input extensions, verified resource rewrites, and minimal document-class compatibility.

### Targeted

Pandoc stderr is classified by diagnostic rules and enables only the passes associated with the observed problem. Regular expressions are acceptable here because diagnostics are plain external text and are not used to identify or mutate LaTeX structure.

Examples include missing resources, unsupported environments, or malformed table diagnostics.

### Compatibility

Explicitly lossy table, algorithm, TikZ, listing, theorem, and custom-environment fallbacks are enabled. Every lossy edit records its reason and source range.

If all three levels fail, the next main-file candidate is tried.

## Edit Validation and Rollback

Before invoking Pandoc, each edited working copy is checked for:

- valid, non-overlapping edit ranges;
- balanced braces and environments in parsed regions;
- no newly broken include edges;
- successful reparsing of changed regions;
- preservation of the document body;
- no accidental deletion of an entire source document.

If enabling a pass changes validation from success to failure, that pass is rejected for the attempt. Since attempts use independent working copies, rollback does not require reversing string mutations.

## Diagnostics

Failures raise a domain-specific `ConversionFailed` exception rather than exposing only `CalledProcessError`. The exception contains every candidate and attempt, enabled passes, relevant edits, and Pandoc stderr.

A concise terminal summary identifies the likely file and pass. A detailed machine-readable report is retained with the failed working copy. `--keep-workdir` may be added while preserving current default cleanup behavior for successful conversions.

## EPUB Validation

A Pandoc zero exit status is necessary but not sufficient. The generated EPUB must also pass lightweight checks:

- valid ZIP container and EPUB mimetype;
- manifest entries refer to present resources;
- at least one readable content document exists;
- referenced images are present;
- extracted body text exceeds a small minimum threshold.

## Testing Strategy

### Parser contract tests

Test the stable `LatexDocument` wrapper for nested arguments, optional arguments, comments, environments, incomplete input, source ranges, and opaque regions. Tests must not depend directly on `pylatexenc` node classes.

### Pass planning tests

Every pass is tested as a pure function by applying its planned edits to source text. Each pass includes cases for:

- normal syntax;
- nested braces and options;
- comments and line breaks;
- incomplete commands;
- opaque regions that must remain unchanged;
- idempotence.

### Workspace integration tests

`tmp_path` fixtures cover nested includes, cyclic includes, multiple main-file candidates, relative resources, `\graphicspath`, and colliding filenames without network access.

### Pandoc contract tests

Small repository fixtures invoke Pandoc to verify attempt selection, stderr classification, fallback behavior, and EPUB validation. These tests explicitly skip when Pandoc is unavailable.

### Regression corpus

Each real conversion failure is reduced to the smallest representative TeX fixture. The fixture records the expected main candidate, attempt level, enabled passes, and output invariant. Full copyrighted papers are not added to the repository.

The existing 208 tests remain passing throughout the migration.

## Migration Plan

### Phase 1: Parser and edit infrastructure

- Add the `pylatexenc` inline dependency.
- Add `SourceFile`, `LatexDocument`, `LatexNodeRef`, `Edit`, `EditPlanner`, and parser contract tests.
- Keep existing preprocessing passes operational.

### Phase 2: High-failure paths

- Migrate main-file selection.
- Migrate package and configuration command handling.
- Migrate input/include resolution with current-file context.
- Introduce the unified graphic resource resolver and converters.

### Phase 3: Structured environments

- Migrate tables, figures, theorems, listings, and algorithms.
- Move numbering, references, caption prefixes, metadata, and representable presentation logic to Pandoc AST/Lua.

### Phase 4: Remove structural regular expressions

- Remove obsolete structural helpers such as environment-level matching and ad hoc command unwrapping after their replacements have regression coverage.
- Keep only leaf-text and diagnostic regular expressions.
- Document the limited responsibility of every remaining regular expression.
- Migrate translation paragraph selection last, after the conversion core is stable.

Each phase is independently reviewable and must preserve the CLI, output filenames, translation behavior, email behavior, and passing test baseline.

## Out of Scope

- Implementing full TeX expansion or catcode emulation.
- Replacing Pandoc as the EPUB writer.
- Splitting Python logic into multiple modules.
- Redesigning translation prompts or email delivery.
- Guaranteeing faithful visual reproduction of arbitrary TikZ or custom class layouts.

## Success Criteria

- Structural LaTeX mutations no longer rely on regular expressions.
- The original extracted source remains unchanged across attempts.
- Nested include and resource paths resolve relative to the correct source file.
- Common PDF/EPS/PS/SVG graphics reach EPUB-compatible resources.
- A failed safe attempt can recover through targeted passes or another main-file candidate.
- Conversion errors identify the candidate, file, pass, and diagnostic involved.
- Existing CLI behavior and the full existing test suite remain compatible.
