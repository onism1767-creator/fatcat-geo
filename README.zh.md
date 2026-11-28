# FatCat GEO

[English](README.md) · 中文 · [安装和使用](#安装和使用) · [GEO 是什么](#geo-是什么)

直观地提升你的品牌被 AI 提到的概率。开源，本地部署，点击即用。

下面是我们使用小米这个品牌作为示例测量出的报告。

![FatCat GEO 使用小米作为示例测量出的报告](assets/report-example-zh.png)

## 安装和使用

FatCat GEO 支持 Windows 10 和 Windows 11，不需要安装任何东西。

1. 打开 [Releases 页面](https://github.com/onism1767-creator/fatcat-geo/releases)，下载文件名以 `windows-x64.zip` 结尾的压缩包。
2. 解压这个压缩包。
3. 双击 `FatCat-GEO.bat`。这时会弹出一个黑色窗口，随后浏览器会打开 FatCat GEO 的页面。使用期间不要关闭这个黑色窗口。
4. 在页面最下方填入一家 AI 的 key，Anthropic、OpenAI、Google Gemini、xAI 和 Perplexity 都可以。有一家的 key 就可以开始测量，key 和测量结果都留在你自己的电脑上。
5. 填写品牌名、官网和品类，然后按照页面上的六个步骤操作。
6. 使用结束后，双击 `Stop-FatCat-GEO.bat` 关闭程序。

页面上的六个步骤分别是：

| 步骤 | 做什么 |
|---|---|
| 1. Quick Check | 如果你在 FatCat GEO 网站上做过 Quick Check，可以把结果添加到这里；没有做过就跳过这一步。 |
| 2. 品牌信息 | 填写品牌名、官网和品类，只需要填一次。 |
| 3. 测量 | 先点击“启动测量工具”，再点击“打开测量工具”。工具会起草要问 AI 的问题，并在提问之前显示预估的费用。 |
| 4. 改进 | FatCat 会免费读取你的网站，并列出建议修改的地方。 |
| 5. 再测 | 修改上线之后，点击“现在开始再测”，把同样的问题再问一遍。点击后会立刻开始，按钮上标有大概的费用。测完之后点击“和这一单比”，就能并排查看修改前后的结果。 |
| 6. 报告 | 把以上各步的结果汇总成一份长报告，也就是本页开头的那张图。 |

第 3 步和第 5 步使用你自己的 key，费用直接付给各家 AI 公司。测量开头那份小米的报告时，我们向 5 家 AI 提了 24 个问题，每个问题问 3 次，一共花了 19.23 美元。

### 从源代码运行

需要先安装 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 [PostgreSQL](https://www.postgresql.org/download/windows/)。

```
uv sync --all-packages
uv run python -m playwright install chromium
uv run python -m fatcat serve
```

然后在浏览器里打开 http://127.0.0.1:8500/ 。第二条命令会安装一个浏览器，FatCat 用它来读取那些运行脚本之后才显示内容的网页。

## GEO 是什么

GEO 是 Generative Engine Optimization（生成式引擎优化）的缩写，指的是让 AI 搜索找到你的网站并引用它。

从 AI 爬虫读取你的网站，到 AI 在回答里描述你的品牌，中间有四个步骤，每个步骤 FatCat GEO 都会测量：

| 步骤 | AI 在做什么 | 我们测什么 |
|---|---|---|
| 1. 读 | AI 的爬虫读取你的网站 | 哪些 AI 爬虫可以访问你的网站，页面上有哪些检查没有通过 |
| 2. 找 | AI 在回答之前先上网搜索 | 你的网站在这些搜索里出现了多少次 |
| 3. 引用 | AI 在回答里附上来源链接 | 有多少回答链接到了你的网站，有多少链接到了其他介绍你的网页 |
| 4. 描述 | AI 在回答里提到品牌并介绍它们 | 你被提到了多少次，和竞品相比怎么样，AI 是怎么描述你的 |

测量之后，你还会得到一份网站修改清单，每一条建议都对应着它所依据的 AI 回答。修改完成后，可以把同样的问题再问一遍，并排查看修改前后的结果。

我们测量的 AI 有 GPT、Claude、Gemini、Grok 和 Perplexity。它们的回答都会保存下来，报告里的每个数字都可以对照这些回答来看。
