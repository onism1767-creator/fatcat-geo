"""提案文档要用的固定内容(纯数据)。每一句都能在项目文档里查到出处;查不到的不写,留给"待补"。

- WHY_ADD:13 条扩展条目各自"为什么建议加"。取自 research/06-synthesis.md §7 的"建议""依据"两栏、
  docs/组件形态决策.md 决定 7、设计文档 §17 第 10 问。
- AMENDMENTS:决定 7 列的 6 项修改类提案。"建议""依据"是 06 §7 的原文;"细节"是在 research/02–05 里
  核到的具体数字,逐条带出处。条目 ID 在生成文档时拿 Toast 的注册表核对,这里只记决定 7 里的写法。
"""

from __future__ import annotations

from typing import Final, NamedTuple

SOURCE_06 = "research/06-synthesis.md §7"
DECISION_7 = "docs/组件形态决策.md 决定 7"

# 条目 ID → (06 §7"建议"栏原文, 06 §7"依据"栏原文, 补充说明)
WHY_ADD: Final[dict[str, tuple[str, str, str]]] = {
    "EVIDENCE_TITLE_QUERY_MATCH_V1": ("标题/H1/slug 复述查询 + 子问题各一 H2", "5 管线", ""),
    "EVIDENCE_PRICE_SPECS_COMPARISON_V1": ("商业题页面显式价格/规格/对比表", "5 管线", ""),
    "EVIDENCE_FRESHNESS_VISIBLE_DATE_V1": ("可见真实更新日期 + 30–90 天刷新;禁伪造", "6 管线", ""),
    "EVIDENCE_ANSWER_FIRST_V1": ("首屏自包含结论", "3 管线,仅 ChatGPT", ""),
    "EVIDENCE_TONE_DECLARATIVE_V1": ("确定性、第三人称、去促销", "4 管线",
                                     "决定 7:本期只作报告建议,起草器不改段落"),
    "EVIDENCE_LOCAL_LANGUAGE_PAGE_V1": ("目标市场本地语言版本", "02 #6", "决定 7 标的是「B 观测」"),
    "EVIDENCE_ACCESS_META_SNIPPET_V1": ("noindex/nosnippet/NOARCHIVE/max-snippet 检查", "04 G4", ""),
    "EVIDENCE_ACCESS_USER_FETCHER_SSR_V1": ("用户触发抓取器不守 robots、不渲染 JS → 首个 HTML 含主内容", "04 G3", ""),
    "EVIDENCE_HIDDEN_TEXT_INJECTION_V1": ("新增禁止项(E 存档)", "Google/Bing 政策、SafeGEO(04、01)",
                                          "决定 7:E 级存档,供检查器挂 ID,永不作为建议输出"),
    "EVIDENCE_FAKE_TIMESTAMP_V1": ("新增禁止项(E 存档)", "Google/Bing 政策、SafeGEO(04、01)",
                                   "决定 7:E 级存档,供检查器挂 ID,永不作为建议输出"),
    "EVIDENCE_SCALED_AI_PAGES_V1": ("新增禁止项(E 存档)", "Google/Bing 政策、SafeGEO(04、01)",
                                    "决定 7:E 级存档,供检查器挂 ID,永不作为建议输出"),
}
# 06 §7 里没有、2026-11-14 用户定补的两条(设计文档 §17 第 10 问)
ADDED_1114: Final[dict[str, str]] = {
    "EVIDENCE_NUMERIC_TRACEABILITY_V1":
        "2026-11-14 用户定补(设计文档 §17 第 10 问)。Butter 的「数值可溯源」审计原先挂在 Toast 的 "
        "EVIDENCE_STATS_V1 上,那一条说的是「加数据可能有用」,和「数字要有出处」不是一回事,所以单独立一条",
    "EVIDENCE_JSONLD_BODY_CONSISTENCY_V1":
        "2026-11-14 用户定补(设计文档 §17 第 10 问)。Butter 的「JSON-LD 与正文一致」审计原先挂在 Toast 的 "
        "EVIDENCE_STRUCT_V1 上,没有专门对应的条目,所以单独立一条",
}


class Detail(NamedTuple):
    text: str
    source: str  # 在项目文档里的出处


class Amendment(NamedTuple):
    key: str                 # 决定 7 里的写法
    applies_to: tuple[str, ...]  # 涉及的条目(决定 7 里的写法,不带 EVIDENCE_ 前缀)
    action: str              # 决定 7 的一句话:改措辞 / 补反证 / 降级 / 维持不进
    suggestion: str          # 06 §7"建议"栏原文
    basis: str               # 06 §7"依据"栏原文
    details: tuple[Detail, ...]
    todo: tuple[str, ...]    # 核不到、要人补的


AMENDMENTS: Final[tuple[Amendment, ...]] = (
    Amendment(
        key="STRUCT_V1", applies_to=("STRUCT_V1",), action="改措辞",
        suggestion="维持 B,但主张改为「结构字段承载事实与查询词」,去掉「排版」含义",
        basis="SAGEO 表 2 结构字段 +22% 检索;Sprinklr 格式 OR≈1",
        details=(
            Detail("SAGEO Arena(arXiv 2602.12187):只改结构字段,检索 +22%、重排 −17%、生成 +2%(仿真,相对变化)",
                   "Butter 扩展条目 EVIDENCE_TITLE_QUERY_MATCH_V1 的 sources;research/06 §7"),
            Detail("What Gets Cited(Sprinklr,SIGIR '26,arXiv 2605.25517;252,000 次试验、6 个模型,仿真):"
                   "「结构化 vs 密集」排版的 OR 是 0.78–1.68,几乎没有影响",
                   "research/02-academic-papers.md §1 一页结论第 2 条;research/03-commercial-tools-and-playbooks.md §1 一页结论第 5 条"),
        ),
        todo=("新措辞的逐字写法(中英 hypothesis、plain):06 §7 只给了方向,没有给字句",),
    ),
    Amendment(
        key="STATS_V1 / QUOTABLE_V1", applies_to=("STATS_V1", "QUOTABLE_V1"), action="补反证",
        suggestion="维持 C,补反证:LIVE-GEO 校验后归零、Bajemon 10 引擎零效应;guardrail 加「数值必须可溯源」",
        basis="02(research/02-academic-papers.md)",
        details=(
            Detail("LIVE-GEO(arXiv 2611.01126,真实网页):「加统计」的 80 条改写全部被数值真实性校验器标出,"
                   "降权后首位率 0.0%",
                   "research/02-academic-papers.md §1 一页结论第 9 条"),
            Detail("Bajemon & Rochet(arXiv 2609.07559,厂商,有商业利益披露):在 10 个引擎设置上重测 2023 年 GEO "
                   "论文的杠杆,引用都没有提高。核验注:10 = 7 个主测 + 3 个复现臂,只复测了 3 个杠杆",
                   "research/02-academic-papers.md 文首核验提示、§1 一页结论第 1 条"),
            Detail("统计数据一行的总评:C(偏负)。仿真里 VoteGEO +1.0~+3.7 个百分点;"
                   "LIVE-GEO 真实引擎首位率 5–11%,真实性校验后 0%",
                   "research/02-academic-papers.md §3 干预措施 × 证据矩阵「统计数据」行"),
            Detail("引语一行的总评:C(偏负)。仿真里 VoteGEO +0.45~+0.78 个百分点;反证是 Bajemon 零效应、"
                   "LIVE-GEO 里 C-SEO 无效",
                   "research/02-academic-papers.md §3 干预措施 × 证据矩阵「引语/专家引用」行"),
        ),
        todo=("补进 sources 的反证条目的逐字写法(中英)",
              "「数值必须可溯源」加进 guardrail 的逐字写法(中英)"),
    ),
    Amendment(
        key="FAQ_V1", applies_to=("FAQ_V1",), action="降级",
        suggestion="降为 D/E,补反证(Zhang Kai −5.7%、Rankability 无优势、FAQ 富媒体停用)",
        basis="04、05(research/04、research/05)",
        details=(
            Detail("Zhang Kai:Q&A 证据类型 −5.74%(06 §7 写作 −5.7%);Rankability:FAQ 无一致优势;"
                   "Chen & Koudas:FAQ −3.4%,不显著",
                   "research/05-cited-page-feature-studies.md §3.1 页面内容特征「Q&A / FAQ 格式」行"),
            Detail("Google 的 FAQ 富媒体结果 2026-05-07 停止显示,2026-06-15 删除 FAQ 文档",
                   "research/04-platform-official-docs-and-standards.md §3.2 的 2026 更新日志"),
            Detail("同一行也有支持的横截面数据:Semrush Q&A 格式 +25.45%、SE Ranking FAQ 正文 4.9 对 4.4、"
                   "Trakkr FAQ schema + 正文 36.9 对 25.4(n=23)。research/05 对这一行的评级是「C(矛盾)」,"
                   "另一处总评写「D/E」,两处不一致,照实列出",
                   "research/05-cited-page-feature-studies.md §3.1「Q&A / FAQ 格式」行、§1 一页结论第 12 条"),
        ),
        todo=("降到 D 还是 E:06 §7 写的是「D/E」,没有定",),
    ),
    Amendment(
        key="LLMSTXT_V1", applies_to=("LLMSTXT_V1",), action="补反证",
        suggestion="维持 E,补 Google 明文、Ahrefs 97% 零请求(任何来源)、Otterly 0.1%",
        basis="04、03(research/04、research/03)",
        details=(
            Detail("Google《Optimizing for generative AI features》指南(2026-05-15 新增,2026-06-15 补充 llms.txt 说明):"
                   "Google Search 不使用这类文件,会忽略它们,对排名不增不减",
                   "research/04-platform-official-docs-and-standards.md §3.2.2、§4.8"),
            Detail("Ahrefs(2026-06-15):137,210 个域里 28% 发布了 llms.txt,其中 97% 在 2026 年 5 月没有收到任何请求",
                   "research/04-platform-official-docs-and-standards.md §1 一页结论第 8 条、§4.8"),
            Detail("Otterly 90 天实验(2026-02-05):62,100 多次 AI 爬虫访问里,只有 84 次(约 0.1%)访问了 /llms.txt",
                   "research/03-commercial-tools-and-playbooks.md §1 一页结论第 4 条"),
        ),
        todo=("补进 sources 的逐字写法(中英)",),
    ),
    Amendment(
        key="THIRDPARTY_COMMUNITY", applies_to=("THIRDPARTY_COMMUNITY",), action="维持不进",
        suggestion="维持暂不进;ChatGPT Reddit 份额 2026-08 后 0.5%",
        basis="05(research/05)",
        details=(
            Detail("Semrush(用 Promptwatch 的数据,2026-08-26):ChatGPT 的引用里 Reddit 的份额从 3.8% 跌到 0.5%"
                   "(2026-07-18 到 08-17)",
                   "research/05-cited-page-feature-studies.md §2 研究清单表 R13、§3.3「论坛 / UGC」行"),
        ),
        todo=(),
    ),
)
