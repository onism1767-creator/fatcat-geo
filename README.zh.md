# FatCat GEO

[English](README.md) · 中文 · [安装和使用](#安装和使用) · [GEO 是什么](#geo-是什么)

直观地提升你的品牌被 AI 提到的概率。开源，本地部署，点击即用。

下面是我们给小米测出来的报告，能测的内容都在这一张图里。

![FatCat GEO 给小米测出来的报告](assets/report-example-zh.png)

## 安装和使用

支持 Windows 10 和 11，什么都不用装。

1. 打开 [Releases 页面](https://github.com/onism1767-creator/fatcat-geo/releases)，下载名字以 `windows-x64.zip` 结尾的文件。
2. 解压。
3. 双击 `FatCat-GEO.bat`。会出现一个黑色窗口，浏览器打开 FatCat GEO 的页面。黑色窗口留着别关。
4. 在页面最下面填一家 AI 的 key：Anthropic、OpenAI、Google Gemini、xAI、Perplexity，有一家就能测。FatCat GEO 在你自己的电脑上运行，key 和结果都留在你的电脑里。
5. 填上品牌名、官网和品类，照页面上的六步往下走。
6. 用完双击 `Stop-FatCat-GEO.bat`。

页面上的六步：

| 步骤 | 做什么 |
|---|---|
| 1. Quick Check | 把在 FatCat GEO 网站上做的 Quick Check 结果加进来。可以跳过。 |
| 2. 品牌信息 | 品牌名、官网、品类，填一次。 |
| 3. 测量 | 点「启动测量工具」，再点「打开测量工具」。它起草问题，问 AI 之前先给你看预估花费。 |
| 4. 改进 | 读一遍你的网站，列出建议改的地方。不花钱。 |
| 5. 再测 | 改动放上网站以后，点「现在开始再测」，把同样的问题再问一遍。点下去就开始，按钮上写着大约花多少。测完点「和这一单比」，改前改后并排放。 |
| 6. 报告 | 把上面的结果拼成一份长报告，就是本页开头那一张。 |

第 3 步和第 5 步用你自己的 key，钱直接付给各家 AI。开头那份小米的报告问了 5 家 AI、24 道题、每题 3 次，花了 19.23 美元。

### 从源代码运行

先装好 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 [PostgreSQL](https://www.postgresql.org/download/windows/)。

```
uv sync --all-packages
uv run python -m playwright install chromium
uv run python -m fatcat serve
```

然后打开 http://127.0.0.1:8500/ 。第二条命令装一个浏览器，用来读那些要运行脚本才显示内容的网页。

## GEO 是什么

GEO 是「生成式引擎优化」的缩写，做的事情是让 AI 搜索找到你的网站、引用你的网站。

有人向 AI 提问时，你的品牌要过四步。每一步我们都测：

| 步骤 | AI 在做什么 | 我们测什么 |
|---|---|---|
| 1. 读 | AI 的爬虫来读你的网站 | 哪些 AI 爬虫进得来，页面上哪里没过 |
| 2. 找 | AI 回答之前先上网搜 | 它搜的内容里，你的网站出现了多少次 |
| 3. 引用 | AI 在回答里附上链接 | 多少个回答链接到你的网站，多少个链接到别处讲你的网页 |
| 4. 描述 | AI 在回答里提到品牌、介绍品牌 | 你被提到多少次，和竞品比怎么样，AI 是怎么说你的 |

测完还有两样：

- 一份改网站的清单，每一条都对着它依据的那些回答。
- 改完以后把同样的问题再问一遍，改前改后并排放。

测的是 GPT、Claude、Gemini、Grok 和 Perplexity。每个数字都来自存下来的 AI 回答，你可以自己打开看。
