# Ground-truth references

Place human-reviewed references in this folder. These files are inputs to local evaluation and may contain text from their source documents. The public demo includes references for T01–T03; other references are ignored by Git. Only commit reference text that has been cleared for public sharing.

The bundled references are candidate drafts, not guaranteed-perfect gold labels. In particular, review the T02 table grids for merged cells, repeated headers, and footnotes, and verify text transcripts against their source files before relying on accuracy scores.

For a test-matrix case, put the transcript filename in the row's `ground_truth` column. For example, if the matrix lists `contract.pdf`, you might set `ground_truth` to `contract.pdf.txt` and save the transcript as `data/ground_truth/contract.pdf.txt`.

The transcript should contain the document's readable text in reading order. The scorer normalizes case and whitespace but preserves punctuation. Review it against the source before treating any score as meaningful. A missing transcript means the run will not report text CER/WER for that case.

For table-structure comparisons, you can also add a JSON file with a top-level `tables` array. Each table is represented as an array of rows, and each row as an array of cell strings:

```json
{
  "tables": [
    [
      ["Item", "Quantity"],
      ["Consulting", "2"]
    ]
  ]
}
```

Set the JSON filename in the matrix's `table_reference` field. Keep expected table counts/shapes/cells faithful to the source; table scores compare returned structure and normalized cell text, not meaning.
