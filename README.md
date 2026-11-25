# fatcat-geo（整合中）

把 FatCat GEO 的测量、诊断、改进三个项目接成一个：一个入口、一份型号清单、一条流程、一份报告。

现在还在整合的早期。三个项目各自还在自己的文件夹里，这里只读它们，不往里写。

| 想看 | 在哪 |
|---|---|
| 为什么这么合、每件事留谁的 | [docs/整合方案.md](docs/整合方案.md) |
| 做到哪了、下一步是什么 | [docs/待办.md](docs/待办.md) |
| 整合时看到的问题 | [docs/错误日志.md](docs/错误日志.md) |
| 被测的 AI 型号（唯一的一份） | [catalog/models.json](catalog/models.json) |

## 现在能用的

需要 Python 3.12 和 uv。三个项目的位置写在 `modules.toml`。

```
uv sync
.venv\Scripts\python -m fatcat serve           打开统一入口(本机 http://127.0.0.1:8500/)
.venv\Scripts\python -m fatcat sources         列出现在能拿来出报告的结果
.venv\Scripts\python -m fatcat report --toast-job <单号> --butter-run <文件夹> --weblite <证据包.zip> --lang zh --out 报告.html
.venv\Scripts\python -m fatcat models sync     从 Toast 的适配器声明读出被测型号,存成 catalog/models.json
.venv\Scripts\python -m fatcat models check    核对 Potato 和 Butter 的型号;有对不上的,退出码是 1
.venv\Scripts\python -m pytest -q              测试,不联网、不花钱
```

统一入口的六步:

1. Quick Check(可跳过):把网站上下载的证据包加进来。
2. 品牌信息:只填一次。
3. 测量与诊断:在这一步点「启动测量工具」(它要的本机数据库会一起开),再点「打开测量工具」,带着你填的信息过去。它用你自己的 AI key,开始测之前会先给你看预估花费。
4. 改进:读一遍你的网站(不问 AI,不花钱),出事实清单、内容检查和改动包。
5. 把改动放上网站,再测一次:记下改动是哪天放上网站的,点「现在开始再测」。**这一下马上开始花钱**(照上一单原样重问,没有确认页),按钮上写着大约多少。做完点「和这一单比」,改前改后的数进报告。
6. 报告:把做完的步骤拼成一份长报告;没做的步骤照实写没做。

AI key 只填一次:首页最下面有一块「AI key」。在页面上填的只放在内存里,入口一关就没了;想长期留着,写进本文件夹下的 `.env`(变量名页面上有)。哪一家这里没有,测量工具照旧用它自己的配置。

测量工具是独立于入口的:入口关了它还在,正在测的不会断。要关它,在第 3 步点「关掉测量工具」(正在测量时不让关)。

这里读三个项目的结果;读 Toast 的数据库时连接是只读的。每次运行的结果在 `runs/` 里,不进仓库。阶段 3 起往改进工具里加了一个命令(`python -m butter compare-toast`),见 `docs/待办.md`。
