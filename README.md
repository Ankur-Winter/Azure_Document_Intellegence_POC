# Azure AI Document Intelligence POC

A small Python command-line project for comparing Azure AI Document Intelligence results across selected documents and models. It records service latency, extracted text, and optional checks against a human-reviewed ground truth and expected document structure. A local dashboard makes saved runs easier to review.

This project sends a document to Azure only when you run `analyze` without `--dry-run`. The `check` command verifies the configured endpoint and key without uploading a document. The dashboard reads local result files and does not call Azure.

## What belongs in Git

The repository contains the code, an example environment file, and a test-matrix template/list. Document inputs, ground-truth transcripts, real `.env` files, and generated `results/` are local files and are ignored by Git. After cloning, add your own test documents and verified reference files before running an analysis. The current matrix includes the POC's T01–T14 case metadata; those input files and their reference transcripts are intentionally not included.

## Requirements and setup

- Python 3.9 or newer
- An Azure AI Document Intelligence resource endpoint and one of its keys
- Network access from the machine to that Azure resource

Open the project folder in VS Code and use its integrated terminal, or open a terminal in the project root. Create an isolated Python environment and install the project:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

On Windows PowerShell, use:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

Copy `.env.example` to `.env` in the project root. Replace the placeholders in `.env` with the endpoint and key provided for your resource. `AZURE_DI_REGION` is an optional label used in reports; the endpoint determines the resource. Never put real credentials in `.env.example`, source code, the test matrix, or a commit. `.env` is ignored by Git.

```bash
cp .env.example .env                         # macOS/Linux
# PowerShell: Copy-Item .env.example .env
```

Check that this machine can authenticate to the resource:

```bash
docintel-poc check
```

The check calls the Azure resource-info operation. A successful check confirms that the endpoint/key pair works from this environment; it does not validate document analysis permissions, supported formats, or quota. The command does not upload a document.

## Add and run one document

The following workflow creates a scored, named test case that appears in the dashboard.

### 1. Put the input in the documents folder

Copy the file into `data/documents/`. The CLI currently accepts PDF, JPG/JPEG, PNG, BMP, TIF/TIFF, HEIF, DOCX, XLSX, and PPTX files. Use a simple filename and keep it unchanged between the matrix and this folder.

Example:

```text
data/documents/T15_sample_contract.pdf
```

The document itself is not committed to Git. Each person running the project needs to obtain the document through an approved source and place their local copy here.

### 2. Prepare a ground-truth transcript

Create a UTF-8 text file in `data/ground_truth/` containing the text you expect a model to extract. For a matrix case, the filename must match the `ground_truth` value in its row. A useful convention is `<document filename>.txt`:

```text
data/ground_truth/T15_sample_contract.pdf.txt
```

Use a careful human-verified transcription of the document's readable text, in reading order. Include meaningful headings, labels, numbers, and table cell text. Use ordinary spaces and line breaks; the scorer collapses whitespace and ignores case, while punctuation and character/word changes can affect CER and WER. Do not write a description of the document in place of its text. Review the reference against the source before relying on its score.

Text accuracy is only calculated when the reference file exists. If there is no reliable transcript yet, leave the `ground_truth` cell empty; the run can still report latency, page units, and configured structural checks, while text accuracy remains blank. A transcript is an evaluation reference, not an Azure-provided score.

For table-structure scoring, add a separate JSON file in `data/ground_truth/` and set `table_reference` to its filename. The JSON shape is:

```json
{
  "tables": [
    [
      ["Item", "Quantity", "Price"],
      ["Consulting", "2", "$150.00"]
    ]
  ]
}
```

Each item in `tables` is one table, represented as rows of cell strings. The order and dimensions must match the source table. The evaluator compares detected table count, row/column shapes, and normalized cell text. It does not determine whether the table's meaning is correct.

### 3. Add a row to `data/test_matrix.csv`

Open the CSV in a text editor or spreadsheet application. Keep the existing header row and add one row for the new document. Every `filename` must be unique, and every `case_id` must be unique. Quote a CSV value if it contains a comma; spreadsheet applications usually do this automatically.

The columns are:

| Column | What to enter |
| --- | --- |
| `case_id` | A unique short ID, for example `T15`. This is the value used by `--case`. |
| `filename` | Exact name of the file inside `data/documents/`. |
| `input_type` | A readable description, such as `PDF with selectable text` or `Scanned PNG`. |
| `validation` | What you intend to evaluate, such as text extraction, table structure, or OCR robustness. This is descriptive. |
| `ground_truth` | Transcript filename in `data/ground_truth/`, or blank if there is no reviewed transcript. |
| `expected_page_units` | Expected number of pages/units returned by the model. For Office documents, an Azure unit may be a slide, worksheet, or character-based page; confirm what applies to the format. |
| `expected_table_count` | Expected number of tables, if a table count check is meaningful. Leave blank when unknown or not applicable. |
| `table_reference` | Optional table JSON filename in `data/ground_truth/` for shape and cell checks. |
| `expected_figure_count` | Expected number of figures, when the figure check is applicable. |
| `figure_model` | Model ID for the figure check, usually `prebuilt-layout`. Figure output is requested only for this model. |
| `expected_paragraph_count` | Expected paragraph count, when this check is applicable. |
| `paragraph_model` | Model ID to use for the paragraph count check, usually `prebuilt-layout`. |
| `embedded_image_marker` | Optional phrase expected to be absent from extracted text, for example words visible only inside an embedded Word image. The run reports whether it was found. |
| `notes` | Optional context about the document or evaluation assumptions. |

For example, this row describes a text PDF with one page and a reference transcript; the remaining optional structure checks are blank:

```csv
T15,T15_sample_contract.pdf,PDF with selectable text,Text extraction accuracy,T15_sample_contract.pdf.txt,1,,,,,,,,
```

Expected page units are checked for each selected model. Paragraph and figure checks are restricted to the corresponding `paragraph_model` and `figure_model`. Table checks run when their expected values/reference are supplied. These checks compare explicit counts or cells; they do not assess semantic understanding or image classification. In particular, the embedded-image marker check looks for a phrase in extracted text; it does not detect or describe the image itself.

The included matrix rows document the POC's existing cases. If your local copy does not have their input documents, run only the case you added with `--case T15` rather than running the full matrix.

### 4. Preview the selection

Preview the case and model selection before sending an analysis request:

```bash
docintel-poc analyze --dry-run --case T15 --models prebuilt-read prebuilt-layout
```

This lists the selected input and request count without calling Azure. If it reports a missing input or an unexpected selection, fix the filename/matrix row before continuing.

### 5. Call the model

Run a single case with one worker first:

```bash
docintel-poc analyze --case T15 --models prebuilt-read prebuilt-layout --workers 1
```

`prebuilt-read` and `prebuilt-layout` are the default comparison. To run one model, name only that model. `prebuilt-invoice` is also available for invoice documents:

```bash
docintel-poc analyze --case T15 --models prebuilt-layout --workers 1
docintel-poc analyze --case T12 --models prebuilt-read prebuilt-layout prebuilt-invoice --workers 1
```

The actual model call uploads the selected document to the configured Azure resource and may incur Azure charges. Do not include sensitive documents unless your organization has approved their use with that resource. Avoid running the full matrix accidentally: without `--case` or `--file`, `analyze` selects every row in the matrix.

For a one-off file that you do not want to add to the matrix, put it in `data/documents/` and use `--file`. Its reference, if present, should be named `<filename>.txt` under `data/ground_truth/` (for example, `draft.pdf.txt`):

```bash
docintel-poc analyze --dry-run --file draft.pdf
docintel-poc analyze --file draft.pdf --models prebuilt-read prebuilt-layout --workers 1
```

A one-off file is identified as `CUSTOM` and does not have the matrix's expected page/table/figure/paragraph checks. Add a matrix row if you want named case-specific expectations and a case selector for the dashboard.

## Accuracy and performance measures

- **Text accuracy:** the project compares Azure's extracted content with the supplied transcript using character error rate (CER) and word error rate (WER). It normalizes case and collapses whitespace; punctuation differences still count. Character/word accuracy is `max(0, 1 - error rate)`. These are text similarity measures, not semantic correctness or field-level invoice validation.
- **Structure checks:** page-unit, paragraph, figure, table-count, table-shape, and table-cell results compare the returned response with explicit values in the matrix and optional table JSON. They are only as reliable as those expected values and references.
- **Confidence:** Azure may return confidence for text elements/fields. Where present in the saved response, the dashboard displays available confidence data; a confidence value is not itself an accuracy score.
- **Latency:** per-document latency covers submission through service completion, including polling. It excludes local file reading and client setup. Aggregate throughput uses the total run wall time, so use per-document latency distributions when comparing models.

These results describe the selected sample documents and configured references. They should not be presented as a production-wide accuracy guarantee without a representative, reviewed test set.

## Where results are saved

Each analysis run creates a timestamped folder under `results/`:

- `documents.csv`: one row per document/model with status, latency, text, and structure metrics.
- `summary.json`: per-model counts, latency statistics, text scores, and configured checks.
- `documents/<model>/<filename>.json`: raw Azure response for that input/model.
- `documents/<model>/<filename>.txt`: extracted text.

The CLI also appends cumulative metrics to `results/history/document_metrics.csv` and `results/history/run_metrics.csv`. Generated result files can contain extracted document content and are ignored by Git.

If you edit a matrix expectation or ground truth after an Azure run, recalculate the selected case against saved responses without calling Azure:

```bash
docintel-poc rescore --case T15
```

Re-scoring creates a separate offline snapshot under `results/`, preserves the original API run/history, and carries the original latency as context. It does not refresh the Azure response or measure a new service call.

## Run the dashboard

From the project root, start the local dashboard:

```bash
docintel-poc dashboard
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in a browser. The dashboard reads `data/test_matrix.csv` and local files under `results/`; it does not load `.env` or call Azure. Select a test case/model to review available metrics, extracted text, confidence details, run history, and raw response. It can only show cases for which results have been saved. Stop it with `Ctrl+C` in the terminal. If port 8765 is occupied, use `docintel-poc dashboard --port 8766`.

## Azure model notes

The POC currently allows PDF, JPG/JPEG, PNG, BMP, TIF/TIFF, HEIF, DOCX, XLSX, and PPTX input extensions. Always confirm current service/model requirements for the resource and API version you use. Read/Layout does not analyze images embedded in Office files. XLSX worksheet text can be evaluated, but the service's Layout table extraction behavior for Excel differs from PDF table extraction. A photograph of a polar bear is not described as a polar bear by this OCR/layout POC; it extracts text and document structure.

For the service's current input requirements and model behavior, see Microsoft's [supported formats and input requirements](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/model-overview?view=doc-intel-4.0.0), [Read model](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept-read?view=doc-intel-4.0.0), and [Layout model](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/layout?view=doc-intel-4.0.0) documentation.
