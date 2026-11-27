# FatCat GEO

English · [中文](README.zh.md) · [Install and use](#install-and-use) · [What GEO is](#what-geo-is)

See and improve how often AI mentions your brand. FatCat GEO is open source, runs on your own computer and starts with one click.

Below is a report we measured for Xiaomi. Everything we can measure is in this one image.

![A FatCat GEO report measured for Xiaomi](assets/report-example-en.png)

## Install and use

For Windows 10 and 11. Nothing to install.

1. Open the [Releases page](https://github.com/onism1767-creator/fatcat-geo/releases) and download the file that ends in `windows-x64.zip`.
2. Unzip it.
3. Double-click `FatCat-GEO.bat`. A black window opens and your browser shows the FatCat GEO page. Leave the black window open.
4. Add an AI key at the bottom of the page: Anthropic, OpenAI, Google Gemini, xAI or Perplexity. One is enough. FatCat GEO runs on your own computer, so your key and your results stay there.
5. Enter your brand name, website and category, then follow the six steps on the page.
6. To stop, double-click `Stop-FatCat-GEO.bat`.

The six steps on the page:

| Step | What happens |
|---|---|
| 1. Quick Check | Add the result of a Quick Check from the FatCat GEO website. You can skip it. |
| 2. Brand details | Brand name, website and category. You enter them once. |
| 3. Measure | Press "Start the measurement tool", then "Open the measurement tool". It drafts the questions and shows the estimated cost before it asks the AIs. |
| 4. Improve | FatCat reads your site and lists what to change. Free. |
| 5. Measure again | When your changes are live, press "Measure again now" to ask the same questions again. It starts at once, and the button shows the price. Then press "Compare with this one" to see before and after side by side. |
| 6. Report | One long report of everything above, like the image at the top of this page. |

Steps 3 and 5 use your own AI keys, and you pay the AI providers directly. The Xiaomi report above asked 5 AIs 24 questions, 3 times each, and cost $19.23.

### Run from source

Install Python 3.12, [uv](https://docs.astral.sh/uv/) and [PostgreSQL](https://www.postgresql.org/download/windows/) first.

```
uv sync --all-packages
uv run python -m playwright install chromium
uv run python -m fatcat serve
```

Then open http://127.0.0.1:8500/. The second command installs a browser for reading pages that only show their content after running scripts.

## What GEO is

GEO is short for generative engine optimization. It means getting AI search to find your site and cite it.

When someone asks an AI a question, your brand goes through four steps. We measure each one:

| Step | What the AI does | What we measure |
|---|---|---|
| 1. Read | AI crawlers read your site | Which AI crawlers are allowed in, and what fails on your page |
| 2. Find | The AI searches the web before it answers | How often your site turns up in what it searches |
| 3. Cite | The AI links to sources in its answer | How often answers link to your site, and to other pages about you |
| 4. Describe | The AI names brands and describes them | How often you're named, how you compare with competitors, and what AI says about you |

After measuring you also get:

- A list of changes for your site, each tied to the answers behind it.
- The same questions asked again, with before and after side by side.

We measure GPT, Claude, Gemini, Grok and Perplexity. Every number comes from saved AI answers you can read.
