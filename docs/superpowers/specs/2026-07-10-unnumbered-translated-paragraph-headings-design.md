# Unnumbered Translated Paragraph Headings Design

## Problem

Translation currently treats `\paragraph{...}` as body prose. The model preserves
the LaTeX command and produces a second translated `\paragraph{...}`. Pandoc sees
both commands as level-four headings and assigns a new number to the Chinese copy,
for example `1.0.0.2 贡献。`.

## Goal

Keep the original English `\paragraph` heading and its existing Pandoc numbering,
while rendering the Chinese title immediately after it as unnumbered plain text.

## Design

1. Register `paragraph` with the same parser argument shape as `section`,
   `subsection`, and `subsubsection`.
2. Include complete, non-opaque `paragraph` commands when collecting headings for
   the single global heading-translation request.
3. Recognize `paragraph` in the heading-line removal pass so body translation never
   receives or reproduces that command.
4. Recognize `paragraph` in the heading write-back pass and insert its Chinese title
   after the original command (and an immediately following `\label`, if present)
   as plain text, not another LaTeX heading command.
5. Preserve existing behavior for `section`, `subsection`, and `subsubsection`.

## Alternatives Rejected

- Hiding Chinese heading numbers with CSS leaves duplicate structural headings in
  the EPUB table of contents and accessibility tree.
- Removing translated `\paragraph` commands after model output is fragile and still
  spends tokens translating heading commands in the body request.

## Error Handling

Incomplete or opaque `\paragraph` syntax is not rewritten or globally translated,
matching the conservative parser safety rules used by the existing heading path.

## Testing

- Heading extraction returns nested and optional-argument `\paragraph` titles.
- Body paragraph selection removes a `\paragraph` line before calling the model.
- Heading write-back inserts Chinese plain text and never creates a second
  `\paragraph` command.
- A source fragment containing `\paragraph{Contributions.}` produces exactly one
  paragraph command after bilingual assembly.
- The complete offline test suite remains green.
