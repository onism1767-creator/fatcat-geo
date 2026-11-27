# FatCat GEO 完整工具

[English](README.md) · 中文

看 AI 回答和你的品牌有关的问题时提没提到你、引没引用你,找出你卡在哪一步,改网站,改完再测。
一条带着走的流程,一份被测 AI 型号清单,一份长报告。

它在你自己的电脑上跑,用你自己的 AI key。什么都不发到 FatCat 的服务器。

## 这个仓库里有什么

| 文件夹 | 做什么 |
|---|---|
| `fatcat/` | 入口页面:品牌信息填一次,照六步往下走;拼出长报告 |
| `measure/` | 拿你的问题去问各家 AI,判定回答,诊断你卡在哪一步 |
| `improve/` | 读你的网站,检查内容,准备改动包,比改前改后 |
| `catalog/models.json` | 被测的 AI 型号(唯一的一份) |

## 最省事的办法:Windows 免安装包

到 [Releases 页面](https://github.com/onism1767-creator/fatcat-geo/releases) 下载
`FatCat-GEO-<版本>-windows-x64.zip`,解压,双击 `FatCat-GEO.bat`。Python 和数据库都在文件夹里,什么都不用装。
免安装包不带无头浏览器:少数要运行脚本才显示内容的被引网页,会照实记成「看不到」。别的都一样。

下面是从源代码安装的办法。

## 装之前

- Windows 10 或 11。
- Python 3.12 和 [uv](https://docs.astral.sh/uv/)。
- PostgreSQL:到 [postgresql.org](https://www.postgresql.org/download/windows/) 下载 Windows 版,一路用默认选项装完。
  测量的结果存在它里面,就在你的电脑上。第一次启动测量工具时,入口会把要用的库建好、开起来。
- 至少一家的 API key:Anthropic、OpenAI、Google Gemini、xAI、Perplexity。

## 安装和运行

```
uv sync --all-packages
uv run python -m playwright install chromium      只要一次;有些网页要在浏览器里才显示得出内容,读它们要用
uv run python -m fatcat serve                     打开 http://127.0.0.1:8500/(只有这台电脑能开)
```

其他命令:

```
uv run python -m fatcat sources                   列出现在能拿来出报告的结果
uv run python -m fatcat report --measure-job <单号> --improve-run <文件夹> --weblite <证据包.zip> --lang zh --out 报告.html
uv run python -m fatcat models check              核对型号清单;对不上退出码是 1
uv run python -m fatcat evidence check            核对证据表的复制件有没有走样
uv run python scripts/check.py                    格式、测试、两项核对、查 key 泄露
uv run python scripts/build_portable.py           打免安装包(Windows;要这台电脑上装着 PostgreSQL,从它那里复制程序文件)
```

## 入口页面上的六步

1. Quick Check(可跳过):把在 FatCat GEO 网站上下载的证据包加进来。
2. 品牌信息:只填一次。
3. 测量与诊断:点「启动测量工具」,再点「打开测量工具」。开始测之前会先给你看完整的预估花费。
4. 改进:读一遍你的网站(不问 AI,不花钱),出事实清单、内容检查和改动包。
5. 把改动放上网站,再测一次:记下改动是哪天放上网站的,点「现在开始再测」。**这一下马上开始花钱**
   (照上一单原样重问,没有确认页),按钮上写着大约多少。做完点「和这一单比」。
6. 报告:把做完的步骤拼成一份长报告;没做的步骤照实写没做。

## 花钱和 key

- 只有两步会花钱:第 3 步的测量、第 5 步的再测。用的是你自己的 AI key,钱直接付给各家 AI。
- key 只填一次,在入口页面最下面。页面上填的只放在内存里;想长期留着,写进本文件夹下的 `.env`,变量名页面上有。
- key 不写进日志、报告和任何结果文件。`.env` 和 `runs/` 不进仓库。
- 测量工具独立于入口运行:关掉入口不会打断正在测的那一单。要关它,在第 3 步点「关掉测量工具」;正在测量时不让关。

## 你该知道的局限

- 回答来自各家的 API,开着联网搜索。它们和你在各家聊天产品里看到的回答不是同一个东西。
- 改前改后只列数字,不说变没变:这几个型号平时自己会波动多少,在这套测法上还没有量过。
- `improve/` 还有它自己的页面,里面有一套旧的改前改后测量。那一套要另外一个采样工具,这个仓库里没有;
  请用入口页面的第 5 步。
- 这里没有任何一条是效果承诺。改了有没有用,再测一次才知道。

## 许可证

MIT,见 [LICENSE](LICENSE)。变更记录见 [CHANGELOG.md](CHANGELOG.md)。
