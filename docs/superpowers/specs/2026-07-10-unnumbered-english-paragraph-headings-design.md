# Unnumbered English Paragraph Headings Design

## Goal

Render every English LaTeX `\paragraph` heading without a section number in both
translated and untranslated EPUB output. Chinese paragraph titles remain unnumbered
plain text.

## Design

Add a syntax-aware preprocessing pass that converts complete, non-opaque unstarred
paragraph commands from `\paragraph{...}` to `\paragraph*{...}`. The pass inserts
only the star after the command token and preserves optional arguments, labels,
multiline titles, same-line prose, and title contents.

The pass runs across all TeX files after optional translation and before Pandoc.
This placement applies the rule to both CLI modes while allowing the translation
pipeline to continue collecting and translating the original paragraph titles.

## Safety

- Already starred `\paragraph*` commands are unchanged.
- Incomplete or opaque paragraph commands are unchanged.
- Commands whose required title argument is absent or incomplete are unchanged.
- Reapplying the pass produces no additional edits.
- Section, subsection, and subsubsection numbering is unchanged.

## Interfaces

- `plan_unnumber_paragraphs(source: SourceFile, document: LatexDocument) -> list[Edit]`
  returns SAFE insertion edits for eligible paragraph commands.
- `unnumber_paragraph_headings(paper_dir: Path) -> None` parses every TeX file,
  applies validated edits, and writes only changed files.

## Alternatives Rejected

- A Lua filter acting on every level-four Pandoc header is broader than the source
  command requested and could affect unrelated headings.
- Removing `--number-sections` would also remove section and subsection numbering.

## Testing

- An ordinary paragraph command receives exactly one star.
- An already starred paragraph is unchanged.
- Optional arguments, nested titles, multiline titles, labels, and same-line prose
  are preserved byte-for-byte apart from the inserted star.
- Incomplete and opaque commands produce no SAFE edit.
- The planner is idempotent.
- The file wrapper processes nested TeX files.
- A Pandoc integration check confirms starred paragraph headings have no generated
  section number while ordinary section headings remain numbered.
- The complete offline test suite remains green.
