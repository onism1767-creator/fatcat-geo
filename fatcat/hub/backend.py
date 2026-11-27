"""统一入口和三个项目之间的那一层:怎么把信息递过去、怎么把结果取回来,都收在这里。

页面那边(app.py)不知道 Toast、Butter 具体怎么调;测试时把这一层整个换成假的。
这里不替用户花钱:问 AI 的那一步只是把用户带到测量工具自己的确认页,预估和「开始」都在那一页,由用户自己点。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from fatcat.hub.tools import RUNNING, MeasureTool, nobody_listening
from fatcat.keys import KeyStore
from fatcat.modules import Module, ModuleError
from fatcat.project import Project
from fatcat.sources import butter, toast, weblite

_TIMEOUT = httpx.Timeout(10.0, read=180.0)  # 建任务时测量工具可能要出题,等得久一点


ACTIVE_STATES = ("CONFIRMED", "RUNNING")  # 测量工具里「已确认、正在问 AI」的两种状态:这时候关它等于白花钱


class NeedsConfirm(ModuleError):
    """这件事做了会丢东西,要用户再点一次确认才做。"""


class Backend:
    """modules:几个项目在哪。state_dir:记测量工具的进程号和启动日志的地方。keys:key 只填一次的那一处。"""

    def __init__(self, modules: dict[str, Module], *, state_dir: Path | None = None,
                 keys: KeyStore | None = None, tool: MeasureTool | None = None) -> None:
        self.modules = modules
        self.keys = keys or KeyStore({}, None)
        self.tool = tool
        if tool is None and state_dir is not None and "toast" in modules:
            self.tool = MeasureTool(modules["toast"], state_dir, self.keys.child_env)

    # ---------------------------------------------------------------- key(只填一次)

    @property
    def key_names(self) -> dict[str, str]:
        return self.keys.names

    def key_status(self) -> dict[str, str | None]:
        return self.keys.where()

    def key_remember(self, provider: str, value: str) -> None:
        self.keys.remember(provider, value)

    def key_forget(self, provider: str) -> None:
        self.keys.forget(provider)

    # ---------------------------------------------------------------- 开、关测量工具

    def tool_status(self) -> dict[str, Any]:
        if self.tool is None:
            return {"state": RUNNING if self.toast_alive() else "stopped", "ours": False, "note": ""}
        return self.tool.status()

    def tool_start(self) -> None:
        if self.tool is None:
            raise ModuleError("这里没有配置怎么启动测量工具;请在它自己的文件夹里启动。")
        self.tool.start()

    def tool_stop(self, drafts: list[str], *, force: bool = False) -> None:
        """关掉从这里启动的测量工具。正在测量时不关;有还没确认的草稿时,要用户再确认一次才关。

        drafts:从统一入口交过去、还停在确认页的单号(草稿只在测量工具的内存里,关了就没了)。
        """
        if self.tool is None:
            raise ModuleError("这里没有配置怎么关测量工具;它是在哪开的,就在哪关。")
        try:
            active = [job["job_id"] for job in self.toast_jobs() if job.get("state") in ACTIVE_STATES]
        except ModuleError as exc:
            raise ModuleError(f"读不到测量工具现在有没有在测量,所以没有关它({exc})") from exc
        if active:
            raise ModuleError(f"测量工具正在测量({'、'.join(active)}),现在关掉这一单就断了,已经花的钱也白花。等它测完再关。")
        if drafts and not force:
            raise NeedsConfirm(f"有 {len(drafts)} 单还停在测量工具的确认页({'、'.join(drafts)})。"
                               "它们只存在测量工具的内存里,关掉就没了,起草题目的钱要再花一次。")
        self.tool.stop()

    # ---------------------------------------------------------------- 测量与诊断(Toast)

    @property
    def toast_url(self) -> str:
        return self.modules["toast"].url

    def toast_jobs(self) -> list[dict[str, Any]]:
        return toast.list_jobs(self.modules["toast"])

    def toast_view(self, job_id: str) -> dict[str, Any]:
        return toast.load_view(self.modules["toast"], job_id)

    def toast_alive(self) -> bool:
        if nobody_listening(self.toast_url):
            return False
        try:
            return httpx.get(self.toast_url + "/", timeout=3.0).status_code == 200
        except httpx.HTTPError:
            return False

    def toast_start(self, project: Project) -> str:
        """替用户把首页那张表交给测量工具,返回接下来该打开的地址(它的确认页或品类页)。

        只交表,不开始测量:题目、预估花费和「开始」都在测量工具自己的确认页上。
        交表这一步测量工具会用用户的 key 核对品类、起草题目,有几次调用的花费,页面上的按钮旁边写明了。
        """
        if not self.toast_alive():
            raise ModuleError(f"测量工具没有打开({self.toast_url})。先点「启动测量工具」,等它开好再点这一步。")
        form = {"brand_name": project.brand, "aliases": project.aliases, "website": project.website,
                "category": project.category, "locale": project.locale, "questions": "", "generate": "on"}
        try:
            answer = httpx.post(self.toast_url + "/jobs", data=form, follow_redirects=False, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ModuleError(f"把信息交给测量工具时没连上:{exc}") from exc
        location = answer.headers.get("location")
        if answer.status_code in (301, 302, 303, 307, 308) and location:
            # 成了是它的确认页或品类页;没成是带着错误码回它的首页。两种都把人带过去,由它自己的页面说清楚
            return self.toast_url + location
        raise ModuleError(f"测量工具没有接受这张表(HTTP {answer.status_code})。请到它的首页看提示:{self.toast_url}/")

    def toast_export(self, job_id: str) -> bytes:
        """测量工具的结果下载包(zip)。要它的网页开着。

        要中文的(lang=zh):包里的汇总是按界面语言写的,而接它的改进工具目前只有中文,照抄进去的那几行才不会中英混在一起。
        """
        try:
            answer = httpx.get(f"{self.toast_url}/jobs/{job_id}/download", params={"lang": "zh"},
                               follow_redirects=False, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            raise ModuleError(f"取测量结果的下载包时没连上:{exc}") from exc
        if answer.status_code != 200 or not answer.content.startswith(b"PK"):
            raise ModuleError(f"测量工具没有给出下载包(HTTP {answer.status_code});这一单可能还没做完")
        return answer.content

    def toast_retest(self, job_id: str) -> str:
        """让测量工具按这一单的做法再测一次,返回新一单的地址。**这一步会马上开始问 AI、花钱**:
        测量工具的再测没有确认页,上限沿用上一单。所以只在用户自己点了写明花费的那个按钮之后才调用。
        """
        if not self.toast_alive():
            raise ModuleError(f"测量工具没有打开({self.toast_url})。先点「启动测量工具」,等它开好再点这一步。")
        try:
            # 它只是开一张新单、起后台任务,应该马上答复;等久了就当没成,不占着项目的锁
            answer = httpx.post(f"{self.toast_url}/jobs/{job_id}/retest", follow_redirects=False, timeout=30.0)
        except httpx.HTTPError as exc:
            raise ModuleError(f"让测量工具再测时没连上:{exc}") from exc
        location = answer.headers.get("location") or ""
        if answer.status_code not in (301, 302, 303, 307, 308) or not location:
            raise ModuleError(f"测量工具没有接受再测(HTTP {answer.status_code})。请到它的页面看提示:{self.toast_url}/")
        if "error=" in location:  # 它带着错误码回首页:多半是哪一家的 key 现在没有了
            raise ModuleError(f"测量工具没有开始再测,它的提示在这里:{self.toast_url}{location}")
        if location.rstrip("/").endswith(f"/jobs/{job_id}"):
            raise ModuleError("测量工具没有开新的一单:上一单可能还没做完。")
        return self.toast_url + location

    def butter_compare(self, before_zip: Path, after_zip: Path, deploy_day: str | None, out_json: Path,
                       site_run: str | None = None) -> dict[str, Any]:
        """比改之前和再测两单的下载包。不问 AI、不花钱;数是测量工具存下的判定,比对的规矩是改进工具的。

        site_run:读网站那次运行的文件夹。给了就按「哪道题对应哪个改过的页面」分处理组和对照组;
        分不出来时改进工具退回全部不点名的题,并在结果里说明。
        """
        module = self.modules["butter"]
        python = module.python if module.python and module.python.is_file() else shutil.which("python")
        if python is None:
            raise ModuleError("找不到能跑改进工具的 Python")
        # 改进工具是在它自己的文件夹里跑的,相对路径到了那边就指错地方:一律给完整路径
        before_zip, after_zip, out_json = before_zip.resolve(), after_zip.resolve(), out_json.resolve()
        out_json.unlink(missing_ok=True)  # 改进工具不覆盖已有的文件;这是本项目自己文件夹里的上一份结果
        command = [str(python), "-B", "-m", "butter", "compare-toast", "--before", str(before_zip),
                   "--after", str(after_zip), "--json", str(out_json)]
        if deploy_day:
            command += ["--deployed", deploy_day]
        if site_run and Path(site_run).is_dir():
            command += ["--run", str(Path(site_run).resolve())]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        try:
            done = subprocess.run(command, cwd=module.require_path(), env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModuleError(f"对比没跑起来:{exc}") from exc
        if done.returncode != 0 or not out_json.is_file():
            tail = " / ".join((done.stdout + done.stderr).strip().splitlines()[-3:])
            raise ModuleError(tail or f"对比没做成(退出码 {done.returncode})")
        return json.loads(out_json.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- 证据包(Web Lite)

    def check_bundle(self, path: Path) -> dict[str, Any]:
        return weblite.load_bundle(path)

    # ---------------------------------------------------------------- 改进(Butter)

    def butter_read(self, project: Project, out_dir: Path, export_zip: Path | None) -> tuple[bool, str, str | None]:
        """读一遍网站:事实、内容检查、改动包。不调模型、不花钱(--no-draft)。等它跑完才返回。

        返回(成没成, 没成时的原话, 运行文件夹)。结果写进本项目自己的文件夹,不往 Butter 的文件夹里写。
        """
        module = self.modules["butter"]
        python = module.python if module.python and module.python.is_file() else shutil.which("python")
        if python is None:
            return False, "找不到能跑改进工具的 Python", None
        out_dir = out_dir.resolve()  # 改进工具在它自己的文件夹里跑,相对路径到了那边就指错地方
        command = [str(python), "-B", "-m", "butter", "run", project.website, "--no-draft", "--out", str(out_dir)]
        if export_zip is not None:
            command += ["--toast-export", str(export_zip.resolve())]
        # 这一步不调模型,所以不把 key 递过去:用不着的进程不给
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        try:
            done = subprocess.run(command, cwd=module.require_path(), env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=900)
        except (OSError, subprocess.SubprocessError, ModuleError) as exc:
            return False, f"改进工具没跑起来:{exc}", None
        runs = butter.find_runs(out_dir)
        if done.returncode != 0 or not runs:
            tail = " / ".join((done.stdout + done.stderr).strip().splitlines()[-3:])
            return False, tail or f"改进工具退出码 {done.returncode}", None
        return True, "", runs[0]["dir"]

    def butter_load(self, run_dir: str) -> dict[str, Any]:
        return butter.load_run(Path(run_dir))
