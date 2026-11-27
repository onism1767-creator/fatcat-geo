# FatCat GEO Full Toolkit

English · [中文](README.zh.md)

Measure how AI systems answer questions about your brand, see where you drop out, improve your site, and
measure again. One guided flow, one list of measured AI models, one long report.

It runs on your own computer with your own AI keys. Nothing is sent to a FatCat server.

## What is in this repository

| Folder | What it does |
|---|---|
| `fatcat/` | The entry page: fill in your brand once and follow six steps; builds the long report |
| `measure/` | Asks the AIs your questions, judges the answers and diagnoses where you drop out |
| `improve/` | Reads your site, checks its content, prepares a change package, compares before and after |
| `catalog/models.json` | The measured AI models (the only list) |

## The quick way: portable package for Windows

Download `FatCat-GEO-<version>-windows-x64.zip` from the
[Releases page](https://github.com/onism1767-creator/fatcat-geo/releases), unzip it and double-click
`FatCat-GEO.bat`. Python and the database are inside the folder; there is nothing to install.
The package has no headless browser, so a few cited pages that only show their content after running
scripts are recorded as could not be read. Everything else is the same.

The rest of this page is for installing from source.

## Before you install

- Windows 10 or 11.
- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- PostgreSQL, installed with the Windows installer from [postgresql.org](https://www.postgresql.org/download/windows/)
  using the default options. The measurement results are stored in it, on your computer. The entry page
  creates and starts the database it needs the first time you start the measurement tool.
- An API key for at least one of: Anthropic, OpenAI, Google Gemini, xAI, Perplexity.

## Install and run

```
uv sync --all-packages
uv run python -m playwright install chromium      once; used to read pages that only render in a browser
uv run python -m fatcat serve                     open http://127.0.0.1:8500/ (this computer only)
```

Other commands:

```
uv run python -m fatcat sources                   list results that can go into a report
uv run python -m fatcat report --measure-job <id> --improve-run <folder> --weblite <bundle.zip> --lang en --out report.html
uv run python -m fatcat models check              check the model list; exit code 1 when it does not match
uv run python -m fatcat evidence check            check that the copied evidence table still matches the original
uv run python scripts/check.py                    lint, tests, both checks, secret scan
uv run python scripts/build_portable.py           build the portable package (Windows; needs PostgreSQL installed to copy its programs from)
```

## The six steps on the entry page

1. Quick Check (optional): add the evidence bundle downloaded from the FatCat GEO website.
2. Brand details: filled in once.
3. Measure and diagnose: press "Start the measurement tool", then "Open the measurement tool". It shows a
   full cost estimate before any measuring starts.
4. Improve: reads your site (no AI, free) and prepares the facts it found, content checks and a change package.
5. Put the changes live, then measure again: record the day the changes went live and press "Measure again
   now". **This starts spending at once** (it repeats the earlier run; there is no confirmation page); the
   button states roughly how much. Then press "Compare with this one".
6. Report: one long report from everything done so far. Steps not done say so.

## Cost and keys

- Only two steps cost money: measuring (step 3) and measuring again (step 5). They use your own AI keys and
  you pay the AI providers directly.
- Enter keys once, at the bottom of the entry page. Keys typed there stay in memory only. To keep them, put
  them in a `.env` file in this folder, using the variable names shown on the page.
- Keys are never written to logs, reports or result files. `.env` and `runs/` are not committed.
- The measurement tool runs independently of the entry page: closing the entry page does not stop a
  measurement. Stop the tool from step 3; it refuses while a measurement is running.

## Limits you should know

- Answers come from each provider's API with web search on. They are not the same as what you see in the
  providers' chat products.
- Before/after numbers are listed without a verdict: how much these models move on their own has not been
  measured for this method yet.
- `improve/` also has pages of its own with an older before/after measurement. That one needs a separate
  sampling tool which is not part of this repository; use step 5 on the entry page instead.
- Nothing here promises an effect. Whether a change helped is only known after measuring again.

## License

MIT, see [LICENSE](LICENSE). Changes are listed in [CHANGELOG.md](CHANGELOG.md).
