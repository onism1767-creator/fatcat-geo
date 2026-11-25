"""统一入口的网页:首页填一次信息,项目页按六步往下走,最后出一份长报告。

只在本机用(入口只绑 127.0.0.1)。没有登录,所以照测量工具的做法守两道门:主机名只认本机的两个名字;
POST 只认本站页面发来的。样子只求能用,版式以后在网站那边统一设计。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from fatcat import project as projects
from fatcat.hub.backend import NeedsConfirm
from fatcat.hub.tools import FAILED, RUNNING, STARTING
from fatcat.keys import FROM_FILE, FROM_PAGE
from fatcat.modules import ModuleError
from fatcat.project import Project
from fatcat.report import Sources, build, render, stamp_now
from fatcat.report.html import _CSS

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
_JOB_IN_URL = re.compile(r"/jobs/([0-9a-f]{6,32})(?:[/?]|$)")  # 测量工具的单号是它自己生成的十六进制串
BUNDLE_NAME = "quick-check.zip"
EXPORT_NAME = "measure-export.zip"
BEFORE_NAME, AFTER_NAME, COMPARE_NAME = "compare-before.zip", "compare-after.zip", "compare.json"
NO_EXPORT = "no-export"  # 记在项目里的记号:这次读网站时没拿到测量结果的下载包(页面上按语言说明)

T: dict[str, dict[str, str]] = {
    "en": {
        "title": "FatCat GEO", "tagline": "Measure, diagnose, improve, measure again. Fill in once, follow the steps.",
        "new": "Start with a brand", "brand": "Brand name", "aliases": "Other names (comma separated, optional)",
        "website": "Main website", "category": "Category", "locale": "Language of the questions",
        "create": "Create", "projects": "Your brands", "none": "Nothing yet.", "back": "All brands",
        "done": "Done", "todo": "Not yet", "running": "Running", "failed": "Failed", "later": "Not connected yet",
        "s0": "Quick Check (optional)", "s0_help": "Run Quick Check on the website, download the evidence bundle, "
              "and add it here. It fills the “Can AI systems read your site” chapter.",
        "s0_add": "Add evidence bundle", "s0_have": "Evidence bundle added.",
        "s1": "Brand details", "s1_help": "Filled in once; every later step uses these.",
        "s2": "Measure and diagnose", "s2_help": "Asks the AIs your questions and finds where you drop out. "
              "This opens the measurement tool with your details filled in. It uses your own AI keys: drafting the "
              "questions makes a few calls right away, and the tool shows the full cost estimate before any "
              "measuring starts.",
        "s2_start": "Open the measurement tool", "s2_pick": "Finished runs for this site",
        "tool_boot": "Start the measurement tool",
        "tool_stopped": "The measurement tool is not running. Start it here; no need to open anything else.",
        "tool_starting": "Starting the measurement tool and the local database it needs. Usually a few seconds; "
                         "a few minutes if the database has to start. This page refreshes by itself.",
        "tool_failed": "The measurement tool did not start: {why}",
        "tool_ours": "The measurement tool is running (started from here).",
        "tool_other": "The measurement tool is running. It was not started from here, so it is not stopped from "
                      "here either.",
        "tool_stop": "Stop the measurement tool", "tool_stop_anyway": "Stop it anyway",
        "keys": "AI keys",
        "keys_help": "Enter them here once. Keys typed here stay in this program's memory only: they are not "
                     "written to any file and are gone when this tool closes. To keep them, put them in the .env "
                     "file in this project's folder, using the variable names below. They are handed to the "
                     "measurement tool when it starts. For a provider with no key here, the measurement tool "
                     "still uses its own settings.",
        "keys_page": "entered here (memory only)", "keys_file": "in .env", "keys_none": "none here",
        "keys_save": "Remember", "keys_forget": "Forget",
        "keys_restart": "The measurement tool receives keys at the moment it starts. After changing a key, stop "
                        "it and start it again.",
        "s2_use": "Use this result", "s2_using": "Using run {job}.", "s2_none": "No finished run for this site yet.",
        "s2_unread": "The measurement tool's data could not be read: {why}",
        "s2_pending": "The run you started from here is waiting in the measurement tool ({job}). It is kept only "
                      "while that tool stays open.",
        "s2_pending_open": "Go back to it",
        "s3": "Improve", "s3_help": "Reads your site (up to 10 pages, no AI, free), lists the facts it found, checks "
              "the content and prepares a change package.",
        "s3_start": "Read the site", "s3_again": "Read the site again",
        "s3_no_export": "The measurement tool was not running, so this reading went ahead without the measurement "
                        "results (the official-domain list and the diagnosis notes). Start the measurement tool "
                        "and read the site again to include them.",
        "s4": "Put the changes live, then measure again",
        "s4_help": "Put the changes on your site, record the day here, wait, then measure again with the same "
                   "questions and the same AI models. The report then shows before and after side by side. "
                   "The measurement used as “before” must have been made no more than 7 days before the changes "
                   "went live.",
        "s4_need": "Finish “Measure and diagnose” first: that run is the “before”.",
        "s4_before": "Before: run {job}.",
        "s4_day": "Day the changes went live", "s4_day_save": "Record", "s4_day_have": "Changes went live on {day}.",
        "s4_cost": "Measuring again repeats run {job} exactly: same questions, same models, same number of asks. "
                   "It starts asking the AIs as soon as you press the button; there is no confirmation page. "
                   "That run cost ${spent}. The spending cap is the same as that run's: {cap}.",
        "s4_no_cap": "none", "s4_go": "Measure again now (about ${spent})",
        "s4_tool": "Start the measurement tool (step 3) before measuring again.",
        "s4_pending": "A re-measurement is in the measurement tool ({job}).", "s4_pending_open": "See its progress",
        "s4_pick": "Finished re-measurements", "s4_use": "Compare with this one",
        "s4_done": "Compared run {before} with run {after}. The numbers are in the report, chapter 9.",
        "s4_failed": "The comparison did not work: {why}",
        "s5": "Report", "s5_help": "One long report from everything done so far. Steps not done yet say so.",
        "s5_en": "Open report (English)", "s5_zh": "Open report (中文)",
        "s5_nothing": "Nothing to report yet: finish at least one step above.",
        "err": "Something went wrong: {why}",
    },
    "zh": {
        "title": "FatCat GEO", "tagline": "测量、诊断、改进、再测。信息填一次,照着步骤走。",
        "new": "从一个品牌开始", "brand": "品牌名", "aliases": "别名(用逗号隔开,可不填)",
        "website": "官网", "category": "品类", "locale": "提问用的语言",
        "create": "创建", "projects": "你的品牌", "none": "还没有。", "back": "全部品牌",
        "done": "做完了", "todo": "还没做", "running": "正在做", "failed": "没做成", "later": "还没接上",
        "s0": "Quick Check(可跳过)", "s0_help": "在网站上做一次 Quick Check,下载证据包,加到这里。"
              "它会填上报告里「AI 读不读得到你的网站」那一章。",
        "s0_add": "加入证据包", "s0_have": "证据包已加入。",
        "s1": "品牌信息", "s1_help": "只填这一次,后面每一步都用它。",
        "s2": "测量与诊断", "s2_help": "拿你的问题去问各家 AI,看你卡在哪一步。这里会带着你填的信息打开测量工具。"
              "它用的是你自己的 AI key:起草题目会马上有几次调用;正式开始测之前,它会先给你看完整的预估花费。",
        "s2_start": "打开测量工具", "s2_pick": "这个网站已经做完的测量",
        "tool_boot": "启动测量工具",
        "tool_stopped": "测量工具现在没开。在这里启动就行,不用另外去开别的东西。",
        "tool_starting": "正在启动测量工具,连它要的本机数据库一起。一般十几秒;数据库要现开的话要几分钟。这一页会自己刷新。",
        "tool_failed": "测量工具没启动起来:{why}",
        "tool_ours": "测量工具开着(从这里启动的)。",
        "tool_other": "测量工具开着。它不是从这里启动的,所以这里也不管关。",
        "tool_stop": "关掉测量工具", "tool_stop_anyway": "仍然关掉",
        "keys": "AI key",
        "keys_help": "只在这里填一次。在这里填的 key 只放在这个程序的内存里:不写进任何文件,入口一关就没了。"
                     "想长期留着,就写进本项目文件夹下的 .env 文件,变量名见下面。启动测量工具时会把它们递过去。"
                     "哪一家这里没有,测量工具照旧用它自己的配置。",
        "keys_page": "这里填了(只在内存里)", "keys_file": "在 .env 里", "keys_none": "这里没有",
        "keys_save": "记下", "keys_forget": "忘掉",
        "keys_restart": "测量工具是在启动那一刻拿到 key 的:改了 key 以后,要把它关掉再启动一次才生效。",
        "s2_use": "用这次的结果", "s2_using": "正在用 {job} 这一单。", "s2_none": "这个网站还没有做完的测量。",
        "s2_unread": "读不到测量工具的数据:{why}",
        "s2_pending": "从这里交过去的那一单还在测量工具里等着({job})。测量工具开着它才在,关了就没了。",
        "s2_pending_open": "回到这一单",
        "s3": "改进", "s3_help": "读一遍你的网站(最多 10 页,不问 AI,不花钱),列出读到的事实,检查内容,准备一份改动包。",
        "s3_start": "读网站", "s3_again": "重新读一遍",
        "s3_no_export": "读网站的时候测量工具没开,所以这一次没有带上测量结果(官网域名清单、诊断结论)。"
                        "想带上,就先启动测量工具,再重新读一遍。",
        "s4": "把改动放上网站,再测一次",
        "s4_help": "把改动放上网站,在这里记下是哪天放的,等一段时间,再用同样的题、同样的 AI 型号测一次。"
                   "报告里会把改前改后的数并排列出来。当作「改之前」的那一单,要是放上网站之前 7 天以内测的才算数。",
        "s4_need": "先做完「测量与诊断」:那一单就是「改之前」。",
        "s4_before": "改之前:{job} 这一单。",
        "s4_day": "改动放上网站的日子", "s4_day_save": "记下", "s4_day_have": "改动是 {day} 放上网站的。",
        "s4_cost": "再测会照 {job} 这一单原样重来:同样的题、同样的型号、同样的次数。一点按钮就马上开始问 AI,没有确认页。"
                   "那一单花了 ${spent}。这次的花费上限和那一单一样:{cap}。",
        "s4_no_cap": "没有设", "s4_go": "现在开始再测(大约 ${spent})",
        "s4_tool": "再测之前,先在第 3 步启动测量工具。",
        "s4_pending": "有一单再测在测量工具里({job})。", "s4_pending_open": "看它的进度",
        "s4_pick": "已经做完的再测", "s4_use": "和这一单比",
        "s4_done": "已经把 {before} 和 {after} 两单比过了。数在报告第 9 章。",
        "s4_failed": "对比没做成:{why}",
        "s5": "报告", "s5_help": "把到现在做完的都拼成一份长报告。没做的步骤会照实写没做。",
        "s5_en": "打开报告(English)", "s5_zh": "打开报告(中文)",
        "s5_nothing": "还没有东西可以出报告:先做完上面至少一步。",
        "err": "出了点问题:{why}",
    },
}

_HUB_CSS = """
form.stack { display: grid; gap: 0.6rem; max-inline-size: 32rem; }
label { display: grid; gap: 0.2rem; font-size: 0.92rem; }
input[type=text], input[type=password], select { font: inherit; padding: 0.4rem 0.5rem; border: 1px solid var(--line); border-radius: 4px;
  background: var(--bg); color: var(--fg); }
button { font: inherit; padding: 0.45rem 0.9rem; border: 1px solid var(--fg); border-radius: 4px;
  background: var(--fg); color: var(--bg); cursor: pointer; justify-self: start; }
button.quiet { background: var(--bg); color: var(--fg); }
ol.steps { list-style: none; padding: 0; }
ol.steps > li { border: 1px solid var(--line); border-radius: 6px; padding: 0.4rem 1rem 0.9rem; margin-block: 0.9rem; }
.tag { font-size: 0.8rem; padding: 0.1rem 0.5rem; border: 1px solid var(--line); border-radius: 999px;
  margin-inline-start: 0.5rem; color: var(--muted); white-space: nowrap; }
.inline { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center; margin-block: 0.4rem; }
a { color: inherit; }
"""


def _lang(request: Request) -> str:
    return "zh" if request.query_params.get("lang") == "zh" else "en"


def _page(lang: str, title: str, body: str, status: int = 200) -> HTMLResponse:
    other = "en" if lang == "zh" else "zh"
    switch = f'<p class="sub"><a href="?lang={other}">{"English" if other == "en" else "中文"}</a></p>'
    return HTMLResponse(
        f'<!doctype html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{escape(title)}</title>\n<style>{_CSS}{_HUB_CSS}</style>\n</head>\n<body>\n"
        f"<header><h1>{escape(title)}</h1>{switch}</header>\n<main>{body}</main>\n</body>\n</html>\n",
        status_code=status)


def _error(t: dict[str, str], why: str) -> str:
    return f'<p class="warn" role="alert">{escape(t["err"].format(why=why))}</p>' if why else ""


def _keys_block(lang: str, keys: dict[str, str | None], names: dict[str, str], tool_running: bool) -> str:
    """key 那一块。只显示每一家有没有、从哪来;填的内容永远不放回页面里。"""
    if not keys:
        return ""
    t = T[lang]
    label = {FROM_PAGE: t["keys_page"], FROM_FILE: t["keys_file"], None: t["keys_none"]}
    rows = ""
    for provider, source in keys.items():
        forget = (f'<button type="submit" class="quiet" formaction="/keys/forget?lang={lang}" name="provider" '
                  f'value="{escape(provider)}" formnovalidate>{escape(t["keys_forget"])}</button>'
                  if source == FROM_PAGE else "")
        rows += (f'<label>{escape(provider)} · <code>{escape(names.get(provider, ""))}</code> · '
                 f'{escape(label[source])}<span class="inline"><input type="password" name="key_{escape(provider)}" '
                 f'autocomplete="off" spellcheck="false">{forget}</span></label>')
    restart = f'<p class="sub">{escape(t["keys_restart"])}</p>' if tool_running else ""
    return (f'<h2>{escape(t["keys"])}</h2><p>{escape(t["keys_help"])}</p>'
            f'<form class="stack" method="post" action="/keys?lang={lang}" autocomplete="off">{rows}'
            f'<button type="submit" class="quiet">{escape(t["keys_save"])}</button></form>{restart}')


def _home(lang: str, items: list[Project], error: str = "", status: int = 200, keys_block: str = "") -> HTMLResponse:
    t = T[lang]
    rows = "".join(f'<li><a href="/projects/{p.id}?lang={lang}">{escape(p.brand)}</a> · {escape(p.website)} · '
                   f"{escape(p.created_at[:16])}</li>" for p in items)
    options = "".join(f'<option value="{code}">{code}</option>' for code in projects.LOCALES)
    body = (
        f'<p>{escape(t["tagline"])}</p>{_error(t, error)}<h2>{escape(t["new"])}</h2>'
        f'<form class="stack" method="post" action="/projects?lang={lang}">'
        f'<label>{escape(t["brand"])}<input type="text" name="brand" required autocomplete="organization"></label>'
        f'<label>{escape(t["aliases"])}<input type="text" name="aliases"></label>'
        f'<label>{escape(t["website"])}<input type="text" name="website" required inputmode="url" '
        'placeholder="https://yourbrand.com" autocomplete="url"></label>'
        f'<label>{escape(t["category"])}<input type="text" name="category" required></label>'
        f'<label>{escape(t["locale"])}<select name="locale">{options}</select></label>'
        f'<button type="submit">{escape(t["create"])}</button></form>'
        f'<h2>{escape(t["projects"])}</h2>' + (f"<ul>{rows}</ul>" if rows else f'<p>{escape(t["none"])}</p>')
        + keys_block)
    return _page(lang, t["title"], body, status)


def _same_site(job_site: str | None, project: Project) -> bool:
    host = (urlsplit(job_site or "").hostname or "").lower().removeprefix("www.")
    return bool(host) and host == project.host.removeprefix("www.")


def _step(title: str, tag: str, help_text: str, extra: str) -> str:
    return f'<li><h3>{escape(title)}<span class="tag">{escape(tag)}</span></h3><p>{escape(help_text)}</p>{extra}</li>'


def _project_page(lang: str, p: Project, jobs: list[dict[str, Any]] | None, jobs_error: str, error: str = "",
                  status: int = 200, toast_url: str = "", tool: dict[str, Any] | None = None,
                  confirm_stop: str = "") -> HTMLResponse:
    t = T[lang]
    post = lambda path: f'method="post" action="/projects/{p.id}/{path}?lang={lang}"'  # noqa: E731

    quick = (f'<p>{escape(t["s0_have"])}</p>' if p.quick_check else "") + (
        f'<form class="inline" {post("quick-check")} enctype="multipart/form-data">'
        f'<input type="file" name="bundle" accept=".zip" required aria-label="{escape(t["s0_add"])}">'
        f'<button type="submit" class="quiet">{escape(t["s0_add"])}</button></form>')

    details = (f"<ul><li>{escape(t['brand'])}: {escape(p.brand)}</li><li>{escape(t['aliases'])}: {escape(p.aliases)}"
               f"</li><li>{escape(t['website'])}: {escape(p.website)}</li><li>{escape(t['category'])}: "
               f"{escape(p.category)}</li><li>{escape(t['locale'])}: {escape(p.locale)}</li></ul>")

    tool = tool or {"state": RUNNING, "ours": False, "note": ""}

    def tool_form(action: str, label: str, extra: str = "", quiet: bool = True) -> str:
        return (f'<form class="inline" method="post" action="/tools/measure/{action}?lang={lang}">'
                f'<input type="hidden" name="back" value="{escape(p.id)}">{extra}'
                f'<button type="submit"{" class=quiet" if quiet else ""}>{escape(label)}</button></form>')

    if tool["state"] == RUNNING:
        measure = (f'<form class="inline" {post("measure/start")}><button type="submit">{escape(t["s2_start"])}'
                   "</button></form>")
        if confirm_stop:  # 关了会丢草稿:把会丢什么说清楚,再点一次才关
            measure += (f'<p class="warn" role="alert">{escape(confirm_stop)}</p>'
                        + tool_form("stop", t["tool_stop_anyway"], '<input type="hidden" name="force" value="1">'))
        elif tool["ours"]:
            measure += f'<p class="sub">{escape(t["tool_ours"])}</p>' + tool_form("stop", t["tool_stop"])
        else:
            measure += f'<p class="sub">{escape(t["tool_other"])}</p>'
    elif tool["state"] == STARTING:
        measure = f'<p>{escape(t["tool_starting"])}</p>'
    else:
        measure = (f'<p class="warn">{escape(t["tool_failed"].format(why=tool["note"]))}</p>'
                   if tool["state"] == FAILED else "")
        measure += f'<p>{escape(t["tool_stopped"])}</p>' + tool_form("start", t["tool_boot"], quiet=False)
    if p.toast_pending and p.toast_pending != p.toast_job:
        measure += (f'<p>{escape(t["s2_pending"].format(job=p.toast_pending))} '
                    f'<a href="{escape(toast_url)}/jobs/{p.toast_pending}/confirm">{escape(t["s2_pending_open"])}</a></p>')
    if p.toast_job:
        measure += f'<p>{escape(t["s2_using"].format(job=p.toast_job))}</p>'
    if jobs is None:
        measure += f'<p class="sub">{escape(t["s2_unread"].format(why=jobs_error))}</p>'
    else:
        mine = [j for j in jobs if j.get("report_versions") and _same_site(j.get("website"), p)]
        if mine:
            measure += f'<h4>{escape(t["s2_pick"])}</h4>' + "".join(
                f'<form class="inline" {post("measure/attach")}><input type="hidden" name="job_id" '
                f'value="{escape(j["job_id"])}"><span>{escape(j["job_id"])} · {escape(str(j["brand"]))} · '
                f'{escape(str(j["created_at"])[:16])}</span><button type="submit" class="quiet">'
                f'{escape(t["s2_use"])}</button></form>' for j in mine)
        elif not p.toast_job:
            measure += f'<p class="sub">{escape(t["s2_none"])}</p>'

    improve_tag = {"none": t["todo"], "running": t["running"], "done": t["done"], "failed": t["failed"]}[p.improve_state]
    improve = ""
    if p.improve_state == "failed" and p.improve_note:
        improve += f'<p class="warn">{escape(p.improve_note)}</p>'
    if p.improve_state == "done" and p.improve_note == NO_EXPORT:
        improve += f'<p class="sub">{escape(t["s3_no_export"])}</p>'
    if p.improve_state != "running":
        label = t["s3_again"] if p.improve_state == "done" else t["s3_start"]
        improve += f'<form class="inline" {post("improve/start")}><button type="submit">{escape(label)}</button></form>'

    # 第 5 步:改完再测
    before_job = p.baseline_job or p.toast_job
    again = ""
    if not before_job:
        again += f'<p class="sub">{escape(t["s4_need"])}</p>'
    else:
        again += f'<p>{escape(t["s4_before"].format(job=before_job))}</p>'
        if p.deploy_day:
            again += f'<p>{escape(t["s4_day_have"].format(day=p.deploy_day))}</p>'
        again += (f'<form class="inline" {post("deploy")}><label>{escape(t["s4_day"])}<input type="date" name="day" '
                  f'value="{escape(p.deploy_day or "")}" required></label>'
                  f'<button type="submit" class="quiet">{escape(t["s4_day_save"])}</button></form>')
        known = {j["job_id"]: j for j in (jobs or [])}
        if p.retest_pending and p.retest_pending != p.retest_job:
            again += (f'<p>{escape(t["s4_pending"].format(job=p.retest_pending))} '
                      f'<a href="{escape(toast_url)}/jobs/{p.retest_pending}">{escape(t["s4_pending_open"])}</a></p>')
        elif p.deploy_day and before_job in known:
            if tool["state"] == RUNNING:
                spent = known[before_job].get("spent_usd")
                cap = known[before_job].get("budget_usd")
                spent_text = f"{float(spent):.2f}" if spent is not None else "?"
                cap_text = f"${float(cap):.2f}" if cap is not None else t["s4_no_cap"]
                again += (f'<p>{escape(t["s4_cost"].format(job=before_job, spent=spent_text, cap=cap_text))}</p>'
                          f'<form class="inline" {post("remeasure/start")}><button type="submit">'
                          f'{escape(t["s4_go"].format(spent=spent_text))}</button></form>')
            else:
                again += f'<p class="sub">{escape(t["s4_tool"])}</p>'
        finished = [j for j in (jobs or []) if j.get("parent") == before_job and j.get("report_versions")]
        if finished:
            again += f'<h4>{escape(t["s4_pick"])}</h4>' + "".join(
                f'<form class="inline" {post("remeasure/attach")}><input type="hidden" name="job_id" '
                f'value="{escape(j["job_id"])}"><span>{escape(j["job_id"])} · {escape(str(j["created_at"])[:16])}</span>'
                f'<button type="submit" class="quiet">{escape(t["s4_use"])}</button></form>' for j in finished)
        if p.compare_note:
            again += f'<p class="warn">{escape(t["s4_failed"].format(why=p.compare_note))}</p>'
        if p.compare_file and p.retest_job:
            again += f'<p>{escape(t["s4_done"].format(before=before_job, after=p.retest_job))}</p>'

    has_any = bool(p.quick_check or p.toast_job or p.improve_state == "done")
    report = (f'<p class="inline"><a href="/projects/{p.id}/report?lang=en">{escape(t["s5_en"])}</a>'
              f'<a href="/projects/{p.id}/report?lang=zh">{escape(t["s5_zh"])}</a></p>'
              if has_any else f'<p class="sub">{escape(t["s5_nothing"])}</p>')

    steps = "".join([
        _step(t["s0"], t["done"] if p.quick_check else t["todo"], t["s0_help"], quick),
        _step(t["s1"], t["done"], t["s1_help"], details),
        _step(t["s2"], t["done"] if p.toast_job else t["todo"], t["s2_help"], measure),
        _step(t["s3"], improve_tag, t["s3_help"], improve),
        _step(t["s4"], t["done"] if p.compare_file else t["todo"], t["s4_help"], again),
        _step(t["s5"], t["done"] if has_any else t["todo"], t["s5_help"], report),
    ])
    waiting = p.improve_state == "running" or tool["state"] == STARTING
    refresh = '<meta http-equiv="refresh" content="5">' if waiting else ""
    body = (f'{refresh}<p><a href="/?lang={lang}">{escape(t["back"])}</a></p>{_error(t, error)}'
            f'<ol class="steps">{steps}</ol>')
    return _page(lang, f"{p.brand} · {t['title']}", body, status)


def create_app(root: Path, backend: Any, *, inline_jobs: bool = False) -> FastAPI:
    """root:项目存放的文件夹。backend:和三个项目打交道的那一层。inline_jobs:后台任务当场跑完(测试用)。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for stale in projects.list_all(root):  # 上次进程退出时还在读网站的:照实标成没做成,不留一个永远「正在做」
        if stale.improve_state == "running":
            projects.update(root, stale.id, improve_state="failed", improve_note="上次没跑完程序就关了,请重新点一次。")
    app = FastAPI(title="FatCat GEO")

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = (urlsplit("//" + request.headers.get("host", "")).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            return PlainTextResponse("Open this tool on this computer via http://127.0.0.1 or http://localhost.",
                                     status_code=400)
        origin = request.headers.get("origin")
        if request.method == "POST" and origin is not None:
            own = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin.lower() != own.lower():
                return PlainTextResponse("This request came from another site; only this tool's own pages may act.",
                                         status_code=403)
        return await call_next(request)

    def show(lang: str, p: Project, error: str = "", status: int = 200, confirm_stop: str = "") -> HTMLResponse:
        tool = backend.tool_status()
        try:
            jobs, jobs_error = backend.toast_jobs(), ""
        except ModuleError as exc:
            jobs, jobs_error = None, str(exc)
        return _project_page(lang, p, jobs, jobs_error, error, status, backend.toast_url, tool, confirm_stop)

    def home_page(lang: str, error: str = "", status: int = 200) -> HTMLResponse:
        block = _keys_block(lang, backend.key_status(), backend.key_names,
                            backend.tool_status()["state"] == RUNNING)
        return _home(lang, projects.list_all(root), error, status, block)

    def back(p: Project, lang: str) -> RedirectResponse:
        return RedirectResponse(f"/projects/{p.id}?lang={lang}", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return home_page(_lang(request))

    @app.post("/keys")
    async def keys_remember(request: Request) -> Response:
        """记下页面上填的 key。只进内存;这里不记日志,回页面时也不带填的内容。"""
        lang = _lang(request)
        form = await request.form()
        try:
            for name, value in form.items():
                if name.startswith("key_") and isinstance(value, str) and value.strip():
                    backend.key_remember(name[4:], value)
        except ModuleError as exc:
            return home_page(lang, str(exc), 400)
        return RedirectResponse(f"/?lang={lang}", status_code=303)

    @app.post("/keys/forget")
    def keys_forget(request: Request, provider: str = Form("")) -> Response:
        backend.key_forget(provider)
        return RedirectResponse(f"/?lang={_lang(request)}", status_code=303)

    def project_or_none(project_id: str) -> Project | None:
        try:
            return projects.load(root, project_id) if project_id else None
        except ModuleError:
            return None

    def after_tool(lang: str, p: Project | None) -> RedirectResponse:
        return RedirectResponse(f"/projects/{p.id}?lang={lang}" if p else f"/?lang={lang}", status_code=303)

    @app.post("/tools/measure/start")
    def tool_start(request: Request, back: str = Form("")) -> Response:
        lang, p = _lang(request), project_or_none(back)
        try:
            backend.tool_start()
        except ModuleError as exc:
            return show(lang, p, str(exc), 502) if p else home_page(lang, str(exc), 502)
        return after_tool(lang, p)

    @app.post("/tools/measure/stop")
    def tool_stop(request: Request, back: str = Form(""), force: str = Form("")) -> Response:
        lang, p = _lang(request), project_or_none(back)
        waiting = [q for q in projects.list_all(root) if q.toast_pending and q.toast_pending != q.toast_job]
        try:
            backend.tool_stop(sorted({q.toast_pending for q in waiting}), force=force == "1")
        except NeedsConfirm as exc:
            return show(lang, p, "", 409, str(exc)) if p else home_page(lang, str(exc), 409)
        except ModuleError as exc:
            return show(lang, p, str(exc), 409) if p else home_page(lang, str(exc), 409)
        for q in waiting:  # 草稿跟着测量工具一起没了:不再留着「回到这一单」的链接
            projects.update(root, q.id, toast_pending=None)
        return after_tool(lang, project_or_none(back))

    @app.post("/projects")
    def create(request: Request, brand: str = Form(""), aliases: str = Form(""), website: str = Form(""),
               category: str = Form(""), locale: str = Form("en-US")) -> Response:
        lang = _lang(request)
        try:
            p = projects.create(root, brand=brand, aliases=aliases, website=website, category=category, locale=locale)
        except ModuleError as exc:
            return home_page(lang, str(exc), 400)
        return back(p, lang)

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_page(request: Request, project_id: str) -> HTMLResponse:
        lang = _lang(request)
        try:
            return show(lang, projects.load(root, project_id))
        except ModuleError as exc:
            return home_page(lang, str(exc), 404)

    @app.post("/projects/{project_id}/quick-check")
    async def add_bundle(request: Request, project_id: str, bundle: UploadFile = File(...)) -> Response:
        lang = _lang(request)
        p = projects.load(root, project_id)
        data = await bundle.read(MAX_BUNDLE_BYTES + 1)
        target = projects.folder(root, p.id) / BUNDLE_NAME
        incoming = target.with_suffix(".incoming")
        try:
            if len(data) > MAX_BUNDLE_BYTES:
                raise ModuleError("这个文件超过 50 MB,不像证据包,没有收")
            incoming.write_bytes(data)
            backend.check_bundle(incoming)  # 先验过是证据包再收下;不是的不留在项目里
            incoming.replace(target)
        except ModuleError as exc:
            incoming.unlink(missing_ok=True)
            return show(lang, p, str(exc), 400)
        return back(projects.update(root, p.id, quick_check=BUNDLE_NAME), lang)

    @app.post("/projects/{project_id}/measure/start")
    def measure_start(request: Request, project_id: str) -> Response:
        lang = _lang(request)
        p = projects.load(root, project_id)
        try:
            url = backend.toast_start(p)
        except ModuleError as exc:
            return show(lang, p, str(exc), 502)
        found = _JOB_IN_URL.search(url)
        if found:  # 记下交过去的是哪一单:浏览器这就离开本页了,回来时要有路找回去
            projects.update(root, p.id, toast_pending=found.group(1))
        return RedirectResponse(url, status_code=303)

    @app.post("/projects/{project_id}/measure/attach")
    def measure_attach(request: Request, project_id: str, job_id: str = Form("")) -> Response:
        lang = _lang(request)
        p = projects.load(root, project_id)
        try:
            job = next((j for j in backend.toast_jobs() if j["job_id"] == job_id), None)
            if job is None or not job.get("report_versions"):
                raise ModuleError(f"没有 {job_id} 这一单,或者它还没有出报告")
            if not _same_site(job.get("website"), p):
                raise ModuleError(f"{job_id} 测的是 {job.get('website')},不是这个品牌的官网")
        except ModuleError as exc:
            return show(lang, p, str(exc), 400)
        changes: dict[str, object] = {"toast_job": job_id}
        if p.toast_pending == job_id:  # 等的就是这一单,做完挂上了,不用再留着「还在等」的提示
            changes["toast_pending"] = None
        return back(projects.update(root, p.id, **changes), lang)

    @app.post("/projects/{project_id}/deploy")
    def deploy_day(request: Request, project_id: str, day: str = Form("")) -> Response:
        """记下改动是哪天放上网站的。只认真的日子,而且不能是还没到的日子。"""
        lang = _lang(request)
        p = projects.load(root, project_id)
        try:
            when = date.fromisoformat(day.strip())
        except ValueError:
            return show(lang, p, f"这不是一个日子:{day.strip()[:20] or '(空)'}。照 2026-11-25 这样写。", 400)
        if when > date.today():
            return show(lang, p, f"{when.isoformat()} 还没到。这里记的是改动已经放上网站的那一天。", 400)
        return back(projects.update(root, p.id, deploy_day=when.isoformat()), lang)

    @app.post("/projects/{project_id}/remeasure/start")
    def remeasure_start(request: Request, project_id: str) -> Response:
        """用户点了写明花费的那个按钮:让测量工具照上一单再测。这一步马上开始花钱,所以只走这一个入口。"""
        lang = _lang(request)
        with projects.LOCK:  # 连点两下只起一单
            p = projects.load(root, project_id)
            before = p.baseline_job or p.toast_job
            if not before or not p.deploy_day:
                return show(lang, p, "先做完测量,并记下改动放上网站的日子,才能再测。", 400)
            if p.retest_pending and p.retest_pending != p.retest_job:
                return back(p, lang)  # 已经有一单在测了
            try:
                url = backend.toast_retest(before)
            except ModuleError as exc:
                return show(lang, p, str(exc), 502)
            found = _JOB_IN_URL.search(url)
            projects.update(root, p.id, baseline_job=before, retest_pending=found.group(1) if found else None)
        return RedirectResponse(url, status_code=303)

    @app.post("/projects/{project_id}/remeasure/attach")
    def remeasure_attach(request: Request, project_id: str, job_id: str = Form("")) -> Response:
        """挂上一单做完的再测,并把改前改后比一遍。不问 AI、不花钱。"""
        lang = _lang(request)
        p = projects.load(root, project_id)
        before = p.baseline_job or p.toast_job
        base = projects.folder(root, p.id)
        try:
            job = next((j for j in backend.toast_jobs() if j["job_id"] == job_id), None)
            if job is None or not job.get("report_versions"):
                raise ModuleError(f"没有 {job_id} 这一单,或者它还没有出报告")
            if not before or job.get("parent") != before:
                raise ModuleError(f"{job_id} 不是 {before} 这一单的再测,不能拿来比")
            (base / BEFORE_NAME).write_bytes(backend.toast_export(before))
            (base / AFTER_NAME).write_bytes(backend.toast_export(job_id))
            backend.butter_compare(base / BEFORE_NAME, base / AFTER_NAME, p.deploy_day, base / COMPARE_NAME)
        except ModuleError as exc:
            projects.update(root, p.id, compare_note=str(exc))
            return show(lang, projects.load(root, p.id), "", 400)
        changes: dict[str, object] = {"baseline_job": before, "retest_job": job_id, "compare_file": COMPARE_NAME,
                                      "compare_note": ""}
        if p.retest_pending == job_id:
            changes["retest_pending"] = None
        return back(projects.update(root, p.id, **changes), lang)

    def run_improve(project_id: str) -> None:
        p = projects.load(root, project_id)
        base = projects.folder(root, p.id)
        export: Path | None = None
        missing = ""
        if p.toast_job:  # 有测量结果就把它的下载包一起递过去:改进那边会用里面的官网域名,并照抄诊断结论
            try:
                data = backend.toast_export(p.toast_job)
                export = base / EXPORT_NAME
                export.write_bytes(data)
            except ModuleError:
                missing = NO_EXPORT  # 测量工具没开着就拿不到下载包:不拦着读网站,但要在页面上说明少了这一份
        ok, note, run_dir = backend.butter_read(p, base / "improve", export)
        if ok:  # 跑的这段时间里项目可能被别的操作改过:只改自己这三项
            projects.update(root, project_id, improve_state="done", improve_note=missing, improve_run=run_dir)
        else:
            projects.update(root, project_id, improve_state="failed", improve_note=note)

    @app.post("/projects/{project_id}/improve/start")
    def improve_start(request: Request, project_id: str) -> Response:
        lang = _lang(request)
        with projects.LOCK:  # 「没在跑才开始」要和「标成正在跑」一口气做完,否则连点两下会起两次
            p = projects.load(root, project_id)
            starting = p.improve_state != "running"
            if starting:
                p = projects.update(root, p.id, improve_state="running", improve_note="")
        if starting:
            if inline_jobs:
                run_improve(p.id)
            else:
                threading.Thread(target=run_improve, args=(p.id,), daemon=True).start()
        return back(p, lang)

    @app.get("/projects/{project_id}/report", response_class=HTMLResponse)
    def report(request: Request, project_id: str) -> HTMLResponse:
        lang = _lang(request)
        try:
            p = projects.load(root, project_id)
        except ModuleError as exc:
            return home_page(lang, str(exc), 404)
        base = projects.folder(root, p.id)
        try:
            sources = Sources()
            if p.toast_job:
                sources.toast = backend.toast_view(p.toast_job)
            if p.improve_state == "done" and p.improve_run:
                sources.butter = backend.butter_load(p.improve_run)
            if p.quick_check:
                sources.weblite = backend.check_bundle(base / p.quick_check)
            if p.compare_file and (base / p.compare_file).is_file():
                sources.compare = json.loads((base / p.compare_file).read_text(encoding="utf-8"))
            page = render(build(sources, lang, generated_at=stamp_now()))
        except ModuleError as exc:
            return show(lang, p, str(exc), 400)
        (base / f"report-{lang}.html").write_text(page, encoding="utf-8")
        return HTMLResponse(page)

    @app.exception_handler(ModuleError)
    async def module_error(request: Request, exc: ModuleError) -> HTMLResponse:
        return home_page(_lang(request), str(exc), 404)

    return app
