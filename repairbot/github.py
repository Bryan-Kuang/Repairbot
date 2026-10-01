from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .process import agent_environment, checked, run


class PendingCI(RuntimeError):
    pass


class GitHub:
    def __init__(self, config: Config):
        self.config = config

    async def gh(self, *args: str, cwd: Path | None = None) -> str:
        return await checked(["gh", *args], cwd=cwd, timeout=180)

    async def git(self, cwd: Path, *args: str) -> str:
        return await checked(["git", *args], cwd=cwd, timeout=180)

    async def prepare(self, root: Path, branch: str) -> tuple[Path, str]:
        repo = root / "repo"
        if not (repo / ".git").is_dir():
            if repo.exists():
                raise RuntimeError(f"不完整克隆目录 {repo}；请检查后移走再重试")
            root.mkdir(parents=True, exist_ok=True)
            await self.gh("repo", "clone", self.config.repository, str(repo))
        metadata = json.loads(await self.gh("repo", "view", self.config.repository, "--json", "defaultBranchRef"))
        base = metadata["defaultBranchRef"]["name"]
        await self.git(repo, "fetch", "origin", base)
        exists = await run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo)
        if exists.code:
            await self.git(repo, "checkout", "-b", branch, f"origin/{base}")
        else:
            await self.git(repo, "checkout", branch)
        await self.git(repo, "config", "user.name", "Discord Repair Bot")
        await self.git(repo, "config", "user.email", "repairbot@users.noreply.github.com")
        return repo, base

    async def head(self, repo: Path) -> str:
        return (await self.git(repo, "rev-parse", "HEAD")).strip()

    async def clean(self, repo: Path) -> bool:
        return not (await self.git(repo, "status", "--porcelain")).strip()

    async def assert_origin(self, repo: Path) -> None:
        url = (await self.git(repo, "remote", "get-url", "origin")).strip().removesuffix(".git").rstrip("/")
        expected = self.config.repository
        if url not in (f"https://github.com/{expected}", f"git@github.com:{expected}", f"ssh://git@github.com/{expected}"):
            raise RuntimeError("工作目录 origin 已改变，停止自动发布")

    async def tests(self, repo: Path, artifacts: Path) -> None:
        for index, command in enumerate(self.config.test_commands):
            result = await run(command, cwd=repo, env=agent_environment(self.config.state_dir),
                               timeout=self.config.session_timeout_seconds,
                               transcript=artifacts / f"test-{index}.log")
            if result.code:
                raise RuntimeError(f"测试命令 {command[0]} 未通过；日志：{artifacts / f'test-{index}.log'}")

    async def publish(self, repo: Path, branch: str, base: str, title: str, body: str, root: Path) -> dict:
        await self.assert_origin(repo)
        if (await self.git(repo, "branch", "--show-current")).strip() != branch:
            raise RuntimeError("工作分支已改变，停止自动发布")
        existing = json.loads(await self.gh("pr", "list", "--repo", self.config.repository,
                                            "--head", branch, "--state", "all", "--json", "number,url,state,headRefOid"))
        if existing:
            pr = existing[0]
            if pr["state"] == "CLOSED":
                raise RuntimeError("此修复 PR 已被关闭，停止自动重开")
            return pr
        if not await self.clean(repo):
            await self.git(repo, "add", "--all")
            await self.git(repo, "commit", "-m", title)
        if not (await self.git(repo, "diff", "--stat", f"origin/{base}...HEAD")).strip():
            raise RuntimeError("AI 没有产生可提交的修复")
        await self.git(repo, "push", "origin", f"HEAD:refs/heads/{branch}")
        body_file = root / "pr-body.md"
        body_file.write_text(body, encoding="utf-8")
        url = (await self.gh("pr", "create", "--repo", self.config.repository, "--base", base,
                             "--head", branch, "--title", title, "--body-file", str(body_file))).strip()
        return await self.pr(url)

    async def pr(self, number: int | str) -> dict:
        return json.loads(await self.gh("pr", "view", str(number), "--repo", self.config.repository,
                                        "--json", "number,url,state,headRefOid,baseRefOid,baseRefName,mergeStateStatus,mergeable,isDraft,statusCheckRollup"))

    async def review_copy(self, repo: Path, target: Path, sha: str) -> Path:
        if not target.exists():
            await checked(["git", "clone", "--no-hardlinks", "--", str(repo), str(target)], timeout=180)
        if not await self.clean(target):
            raise RuntimeError("审查目录已有文件改动，停止自动审查")
        await self.git(target, "fetch", "origin")
        await self.git(target, "checkout", "--detach", sha)
        return target

    async def assert_version(self, number: int, sha: str, base_sha: str) -> dict:
        pr = await self.pr(number)
        if pr["headRefOid"] != sha or pr["baseRefOid"] != base_sha:
            raise RuntimeError("PR 或目标分支版本已改变，已有审查失效；停止自动合并")
        if pr["state"] not in ("OPEN", "MERGED"):
            raise RuntimeError("PR 已关闭")
        return pr

    async def merge(self, number: int, sha: str, base_sha: str) -> None:
        pr = await self.assert_version(number, sha, base_sha)
        if pr["state"] == "MERGED":
            return
        if pr["isDraft"]:
            raise RuntimeError("PR 为草稿，停止自动合并")
        checks = pr["statusCheckRollup"] or []
        # Fail closed: neither cancelled, neutral nor skipped checks count as successful CI.
        for check in checks:
            if check.get("__typename") == "StatusContext" or "state" in check:
                state = check.get("state")
                if state in ("PENDING", "EXPECTED"):
                    raise PendingCI("CI 尚未完成")
                if state != "SUCCESS":
                    raise RuntimeError(f"CI 未成功：{check.get('context', state)}")
            else:
                if check.get("status") != "COMPLETED":
                    raise PendingCI("CI 尚未完成")
                if check.get("conclusion") != "SUCCESS":
                    raise RuntimeError(f"CI 未成功：{check.get('name', check.get('conclusion'))}")
        if self.config.require_ci and not checks:
            raise PendingCI("仓库尚无 CI 结果；require_ci=true，等待检查出现")
        if pr["mergeable"] == "UNKNOWN" or pr["mergeStateStatus"] in ("UNKNOWN", "BEHIND"):
            raise PendingCI("GitHub 正在计算合并状态，或目标分支已前进")
        if pr["mergeable"] != "MERGEABLE" or pr["mergeStateStatus"] not in ("CLEAN", "HAS_HOOKS"):
            raise RuntimeError(f"GitHub 分支保护或冲突阻止合并：{pr['mergeStateStatus']}")
        # gh --match-head-commit prevents a changed head from slipping through the final request.
        await self.gh("pr", "merge", str(number), "--repo", self.config.repository,
                      "--squash", "--match-head-commit", sha)
        final = await self.pr(number)
        if final["state"] != "MERGED":
            raise PendingCI("GitHub 尚未完成合并")
