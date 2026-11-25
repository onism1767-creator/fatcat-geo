# FatCat GEO Full Toolkit (fatcat-geo)

English · [中文](README.zh.md)

One entry point over the three parts of FatCat GEO: measure how AI systems answer questions about your
brand, diagnose where you drop out, improve your site, and measure again. One list of measured AI models,
one guided flow, one long report.

It runs on your own computer with your own AI keys. Nothing is sent to a FatCat server.

| Looking for | Where |
|---|---|
| Why the parts are joined this way | [docs/整合方案.md](docs/整合方案.md) (Chinese) |
| What is done and what is next | [docs/待办.md](docs/待办.md) (Chinese) |
| Problems found along the way | [docs/错误日志.md](docs/错误日志.md) (Chinese) |
| The measured AI models (the only list) | [catalog/models.json](catalog/models.json) |

## Status

The three parts still live in their own folders next to this one; `modules.toml` says where. This project
reads their results and drives them. Merging them into this repository is in progress.

## Install and run

You need Python 3.12 and [uv](https://docs.astral.sh/uv/).

```
uv sync
.venv\Scripts\python -m fatcat serve             open the entry page at http://127.0.0.1:8500/ (this computer only)
.venv\Scripts\python -m fatcat sources           list results that can go into a report
.venv\Scripts\python -m fatcat report --toast-job <id> --butter-run <folder> --weblite <bundle.zip> --lang en --out report.html
.venv\Scripts\python -m fatcat models sync       read the measured models from the measurement tool into catalog/models.json
.venv\Scripts\python -m fatcat models check      check the model list; exit code 1 when it does not match
.venv\Scripts\python -m fatcat evidence check    check that the copied evidence table still matches the original
.venv\Scripts\python scripts\check.py            pre-release checks: lint, tests, both checks, secret scan
.venv\Scripts\python -m pytest -q                tests; offline, free
```

## The six steps on the entry page

1. Quick Check (optional): add the evidence bundle downloaded from the website.
2. Brand details: filled in once.
3. Measure and diagnose: press "Start the measurement tool" (the local database it needs starts with it),
   then "Open the measurement tool". It shows a full cost estimate before any measuring starts.
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
- Nothing here promises an effect. Whether a change helped is only known after measuring again.

## License

MIT, see [LICENSE](LICENSE). Changes are listed in [CHANGELOG.md](CHANGELOG.md).
