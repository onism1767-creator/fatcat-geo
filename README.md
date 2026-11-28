# FatCat GEO

English · [中文](README.zh.md) · [Install and use](#install-and-use) · [What GEO is](#what-geo-is)

See and improve how often AI mentions your brand. FatCat GEO is open source, runs on your own computer and starts with one click.

Below is a report we measured using Xiaomi as the example brand.

![A FatCat GEO report measured with Xiaomi as the example brand](assets/report-example-en.png)

## Install and use

FatCat GEO works on Windows 10 and 11, and there is nothing to install.

1. Go to the [Releases page](https://github.com/onism1767-creator/fatcat-geo/releases) and download the file whose name ends in `windows-x64.zip`.
2. Unzip it.
3. Double-click `FatCat-GEO.bat`. A black window will open, and then your browser will open the FatCat GEO page. Keep the black window open for as long as you're using FatCat GEO.
4. At the bottom of the page, add an API key from Anthropic, OpenAI, Google Gemini, xAI or Perplexity. One key is enough to start, and both the key and your results stay on your computer.
5. Enter your brand name, website and category, then work through the six steps on the page.
6. When you've finished, double-click `Stop-FatCat-GEO.bat` to shut it down.

These are the six steps on the page:

| Step | What happens |
|---|---|
| 1. Quick Check | If you've run a Quick Check on the FatCat GEO website, you can add the result here. Otherwise skip this step. |
| 2. Brand details | Your brand name, website and category, which you only enter once. |
| 3. Measure | Press "Start the measurement tool" and then "Open the measurement tool". The tool drafts the questions and shows you the estimated cost before it asks the AIs anything. |
| 4. Improve | FatCat reads your site for free and lists what to change. |
| 5. Measure again | Once your changes are live, press "Measure again now" to ask the same questions a second time. The run starts as soon as you press the button, which shows about how much it will cost. When it's done, press "Compare with this one" to see the before and after side by side. |
| 6. Report | Everything from the steps above goes into one long report, like the image at the top of this page. |

Steps 3 and 5 use your own AI keys, and you pay the AI providers directly. For the Xiaomi report above we asked 5 AIs 24 questions, 3 times each, and it cost $19.23.

### Run from source

You'll need Python 3.12, [uv](https://docs.astral.sh/uv/) and [PostgreSQL](https://www.postgresql.org/download/windows/) installed first.

```
uv sync --all-packages
uv run python -m playwright install chromium
uv run python -m fatcat serve
```

Then open http://127.0.0.1:8500/ in your browser. The second command installs a browser that FatCat uses to read pages which only show their content after running scripts.

## What GEO is

GEO is short for generative engine optimization, which means getting AI search to find your site and cite it.

There are four steps between an AI crawler reading your site and an AI describing your brand in an answer, and FatCat GEO measures each of them:

| Step | What the AI does | What we measure |
|---|---|---|
| 1. Read | AI crawlers read your site | Which AI crawlers are allowed in, and which checks your pages fail |
| 2. Find | The AI searches the web before it answers | How often your site turns up in those searches |
| 3. Cite | The AI links to sources in its answer | How often the answers link to your site, and how often they link to other pages about you |
| 4. Describe | The AI names brands and describes them | How often you're named, how you compare with competitors, and what the AIs say about you |

After a measurement you also get a list of changes to make on your site, each one tied to the AI answers it is based on. Once you've made them, you can ask the same questions again and see the before and after side by side.

We measure GPT, Claude, Gemini, Grok and Perplexity, and we save their answers, so you can read the answers behind every number in the report.
