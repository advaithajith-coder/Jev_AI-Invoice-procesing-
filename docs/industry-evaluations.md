# Industry evaluation suite

The new suite contains **200 synthetic invoices**, separate from the original 100-case
baseline. Each invoice belongs to exactly one industry and one evaluation stage.

| Industry | Extraction | Classification | Tax | Routing | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Technology | 5 | 5 | 5 | 5 | 20 |
| Light manufacturing | 5 | 5 | 5 | 5 | 20 |
| Professional services | 5 | 5 | 5 | 5 | 20 |
| Wholesale distribution | 5 | 5 | 5 | 5 | 20 |
| Retail | 5 | 5 | 5 | 5 | 20 |
| Construction | 5 | 5 | 5 | 5 | 20 |
| Hospitality | 5 | 5 | 5 | 5 | 20 |
| Logistics | 5 | 5 | 5 | 5 | 20 |
| Healthcare | 5 | 5 | 5 | 5 | 20 |
| Property management | 5 | 5 | 5 | 5 | 20 |
| Total | 50 | 50 | 50 | 50 | 200 |

## Files and expected answers

`invoice generator/industry tests/ten-industries-v1/` contains:

- `invoices/`: 200 PDFs across the ten existing invoice layouts.
- `records/`: the source data used to generate each invoice.
- Each stage's `inputs/`, `expected/` and `manifest.json`: isolated stage inputs,
  authored assertions and PDF hashes.
- `suite.json`: industry context, synthetic chart of accounts, assumptions and version.
- `coverage.json`: counts by industry, stage, layout and test split.

Every buyer is **Northwind Analytics Inc**. Industry labels simulate different operating
contexts for the demo; they do not create ten real entities in the application.
Fixed-fee service lines omit quantity and unit price. Goods have three quantified lines.
No invoices, POs or vendors are uploaded into the live ledger by preparation or evaluation.

Expected answers come from authored fixtures, not from JEV responses. The importer freezes
the inputs, assertions, industry context and source document hashes in its dataset ID.
Changing the context produces a new dataset rather than rewriting an old result.

## Development and reserved tests

There are **160 development cases** and **40 reserved cases**: four development and one
reserved case per industry/stage. Reserved scenarios rotate across industries, so the
reserved set does not consist only of freight or exemption examples.

Use development errors to revise account descriptions, prompts, evidence checks or
correction scope. Freeze the candidate before running the reserved set. If reserved
answers are used to tune a candidate, that set becomes development data; obtain a new
independent test set before claiming unseen accuracy. The PDFs share synthetic template
families and are an initial diagnostic sample, not proof of real-world generalization.

The runner defaults to development cases when using its new split selector:

```powershell
uv run python -m jev_invoice.evaluation.pilot --run <run-id> --split development
```

Each invocation processes at most 40 unrun cases, interleaving industries and stages.
Repeat four times for the development set. Reserved cases require an explicit selection:

```powershell
uv run python -m jev_invoice.evaluation.pilot --run <run-id> --split holdout
```

Preparation does not invoke models. To generate a separate version, with the optional
PDF dependency available:

```powershell
uv run --with reportlab python -m jev_invoice.evaluation.industries --output "invoice generator/industry tests/ten-industries-v2"
```

The generator refuses to overwrite a nonempty destination.

## What the comparisons mean

The Evaluations screen shows per-industry, per-stage field accuracy, counts, recorded
cost and duration. Development and reserved results are reported separately. Industry
and test-set selectors filter the case list; summary totals remain the full run.
CSV reports include industry and split so external comparisons retain those dimensions.

Classification distinguishes PO-inherited coding from JEV coding. Two cases per industry
have approved PO values; three require model GL suggestions without a vendor default.
JEV receives the stated industry and account descriptions, but never expected answers.
The new context is available to the evaluation runner; live processing retains its
existing behavior unless an industry context is explicitly supplied.

Tax cases cover ordinary office goods, paid software access, pure professional services,
a deliberate telephone-rate error and a conditional exemption without documentation.
Tax fixtures use the existing Massachusetts DOR source snapshot and its section links.
Manufacturing, resale, healthcare and other exemptions are not inferred from industry
names. Conditional cases should return Finance review, not a guessed rate. Relevant
alternative telephone citations are accepted. Finance should independently validate
these demo assumptions before production use.

Nonempty evidence is a presence check. It does not prove that a citation or explanation
supports the item; manual semantic review remains necessary. Missing answers and answered
differences are counted separately by the backend. Correct stage outputs do not establish
end-to-end invoice approval, and this suite does not measure scans or vision.

Improvement means evaluated changes to prompts, master data and scoped validated
corrections. This feature does not retrain model weights or automatically promote changes.
