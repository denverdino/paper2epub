# Simple Translation Optimization Design

## Goal

Reduce repeated translation tokens and API latency while preserving translation
completeness and terminology consistency for ordinary research-paper-sized input.

## Scope

Keep the current two-phase workflow and file-level concurrency. Do not add token
budget scheduling, paragraph-level worker queues, persistent caches, placeholder
systems, or infrastructure intended for unusually large documents.

## Design

1. Extract one glossary before translation and ask for at most 50 important terms.
2. Discover section headings across the complete input tree, deduplicate them, and
   translate them in one request. Reuse the resulting title-to-Chinese mapping while
   translating every file.
3. Translate the document body of every file returned by `get_input_order`, including
   the main file. Preserve the main-file preamble by translating only content after
   `\begin{document}`.
4. Keep one body request per TeX file and retain the existing five-worker file pool.
5. After a successful body response, compare returned paragraph IDs with requested
   IDs. Retry only missing paragraphs once. If any remain missing, raise an error so
   the command cannot report a silently incomplete translation.
6. Stop deleting unmatched closing braces from model output. Reject translations
   whose brace balance is invalid, include them in the one targeted retry, and fail
   explicitly if the retry is still invalid.

## Interfaces

- `_batch_translate(client, glossary, numbered_paragraphs)` continues to return a
  `dict[int, str]`, but guarantees that every requested ID has a structurally valid
  translation or raises `RuntimeError`.
- `_translate_headings` becomes a pure write-back helper that consumes a precomputed
  `dict[str, str]` instead of calling the model itself.
- `translate_file_content` consumes the precomputed heading map.
- `translate_tex_files` owns glossary extraction, global heading translation, file
  selection, and file-level concurrency.

## Error Handling

Transport/API exceptions keep the existing bounded retry behavior. A successful but
partial or brace-invalid response triggers one request containing only the affected
paragraphs. Failure after that retry aborts translation with the affected paragraph
IDs in the error message.

## Testing

Offline tests with fake clients will cover:

- missing paragraph IDs are retried without resending successful paragraphs;
- invalid brace balance is retried and never silently repaired;
- persistent missing/invalid results raise `RuntimeError`;
- headings are translated once after global deduplication;
- a main file containing `\input` still has its own document body translated;
- the existing full test suite remains green.
