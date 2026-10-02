from __future__ import annotations

import asyncio
import fnmatch
import json
import os
from pathlib import Path

from .config import Config
from .process import agent_environment, checked, run

# AI sessions can write inside the working tree, including .git/. Never let a planted hook or
# config entry (core.fsmonitor, filters, sshCommand, pushurl...) run with coordinator credentials.
GIT_HARDENING = ["-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false"]


# Caches and build output that test runs (by the AI session or the coordinator) leave behind.
GENERATED = ["__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".coverage",
             "htmlcov", "node_modules", ".DS_Store"]
COMMIT_PATHSPEC = ["."] + [f":(exclude,glob){prefix}{name}{suffix}" for name in GENERATED
                           for prefix in ("", "**/") for suffix in ("", "/**")]


class PendingCI(RuntimeError):
    pass


class BranchBehind(RuntimeError):
    pass


class NeedsHumanApproval(RuntimeError):
    pass


def trusted_config(repo: Path) -> Path:
    # Kept outside the working tree so AI sessions confined to the repository cannot modify it.
    return repo.parent / f".{repo.name}.gitconfig"


def restore_config(repo: Path) -> None:
    snapshot = trusted_config(repo)
    if not snapshot.is_file():
        return
    git_dir = repo / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise RuntimeError(f"{repo} 的 .git 目录已被替换，停止执行")
    config = git_dir / "config"
    trusted = snapshot.read_bytes()
    if config.is_symlink() or not config.is_file() or config.read_bytes() != trusted:
        config.unlink(missing_ok=True)
        config.write_bytes(trusted)


def changed_path_risks(name_status: str, patterns: list[str]) -> list[str]:
    risks = []
    for line in name_status.splitlines():
        status, *paths = line.split("\t")
        if status.startswith("D"):
            risks.append(f"删除 {paths[0]}")
        for path in paths:
            if any(fnmatch.fnmatch(path, pattern) for pattern in patterns):
                risks.append(f"修改受保护路径 {path}")
    return list(dict.fromkeys(risks))


class GitHub:
    def __init__(self, config: Config):
        self.config = config

    async def gh(self, *args: str, cwd: Path | None = None) -> str:
        return await checked(["gh", *args], cwd=cwd, timeout=180)

    async def git(self, cwd: Path, *args: str) -> str:
        restore_config(cwd)
        return await checked(["git", *GIT_HARDENING, *args], cwd=cwd, timeout=180)

    async def trust(self, repo: Path) -> None:
        if not trusted_config(repo).is_file():
            trusted_config(repo).write_bytes((repo / ".git" / "config").read_bytes())

    async def prepare(self, root: Path, branch: str) -> tuple[Path, str]:
        repo = root / "repo"
        if not (repo / ".git").is_dir():
            if repo.exists():
                raise RuntimeError(f"不完整克隆目录 {repo}；请检查后移走再重试")
            root.mkdir(parents=True, exist_ok=True)
            await self.gh("repo", "clone", self.config.repository, str(repo))
            await self.git(repo, "config", "user.name", "Discord Repair Bot")
            await self.git(repo, "config", "user.email", "repairbot@users.noreply.github.com")
        await self.trust(repo)
        metadata = json.loads(await self.gh("repo", "view", self.config.repository, "--json", "defaultBranchRef"))
        base = metadata["defaultBranchRef"]["name"]
        await self.git(repo, "fetch", "origin", base)
        exists = await run(["git", *GIT_HARDENING, "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo)
        if exists.code:
            await self.git(repo, "checkout", "-b", branch, f"origin/{base}")
        else:
            await self.git(repo, "checkout", branch)
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

    async def commit(self, repo: Path, title: str) -> None:
        if await self.clean(repo):
            return
        await self.git(repo, "add", "--all", "--", *COMMIT_PATHSPEC)
        restore_config(repo)
        staged = await run(["git", *GIT_HARDENING, "diff", "--cached", "--quiet"], cwd=repo)
        if staged.code:
            await self.git(repo, "commit", "-m", title)

    async def discard(self, repo: Path) -> None:
        # Drop files produced by test runs (caches, coverage, build output) so they never reach the PR.
        await self.git(repo, "reset", "--hard", "HEAD")
        await self.git(repo, "clean", "-fd")

    async def risky_changes(self, repo: Path, base_sha: str, sha: str) -> list[str]:
        diff = await self.git(repo, "diff", "--no-ext-diff", "--no-renames", "--name-status", f"{base_sha}...{sha}", "--")
        return changed_path_risks(diff, self.config.protected_paths)

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
        await self.commit(repo, title)
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
                                        "--json", "number,url,state,headRefOid,baseRefOid,baseRefName,mergeStateStatus,mergeable,isDraft,statusCheckRollup,reviewDecision"))

    async def review_copy(self, repo: Path, target: Path, sha: str) -> Path:
        if not target.exists():
            await checked(["git", *GIT_HARDENING, "clone", "--no-hardlinks", "--", str(repo), str(target)], timeout=180)
        await self.trust(target)
        if not await self.clean(target):
            raise RuntimeError("审查目录已有文件改动，停止自动审查")
        await self.git(target, "fetch", "origin")
        await self.git(target, "checkout", "--detach", sha)
        return target

    async def assert_version(self, number: int, sha: str) -> dict:
        # The base branch may advance: the reviewed diff (merge-base...head) is unchanged while the head is.
        pr = await self.pr(number)
        if pr["headRefOid"] != sha:
            raise RuntimeError("PR 提交已改变，已有审查失效；停止自动合并")
        if pr["state"] not in ("OPEN", "MERGED"):
            raise RuntimeError("PR 已关闭")
        return pr

    async def update_branch(self, number: int, *, attempts: int = 15) -> dict:
        before = (await self.pr(number))["headRefOid"]
        await self.gh("pr", "update-branch", str(number), "--repo", self.config.repository)
        # GitHub creates the merge commit asynchronously.
        for _ in range(attempts):
            pr = await self.pr(number)
            if pr["headRefOid"] != before:
                return pr
            await asyncio.sleep(2)
        raise RuntimeError("GitHub 未在预期时间内更新 PR 分支")

    async def merge(self, number: int, sha: str, *, checks_grace: bool = False) -> None:
        pr = await self.assert_version(number, sha)
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
        if not checks and checks_grace:
            raise PendingCI("等待可能存在的 CI 检查注册")
        if pr["mergeStateStatus"] == "BEHIND":
            raise BranchBehind("分支保护要求 PR 与目标分支同步")
        if pr["mergeable"] == "UNKNOWN" or pr["mergeStateStatus"] == "UNKNOWN":
            raise PendingCI("GitHub 正在计算合并状态")
        # The PR is opened by the coordinator's own account, and GitHub never lets an author approve their own PR.
        if pr.get("reviewDecision") == "REVIEW_REQUIRED":
            raise NeedsHumanApproval("仓库要求人工批准 PR；Repairbot 用创建 PR 的同一账号操作，无法自行批准")
        if pr.get("reviewDecision") == "CHANGES_REQUESTED":
            raise NeedsHumanApproval("GitHub 上有审查者要求修改此 PR")
        if pr["mergeable"] != "MERGEABLE" or pr["mergeStateStatus"] not in ("CLEAN", "HAS_HOOKS"):
            raise RuntimeError(f"GitHub 分支保护或冲突阻止合并：{pr['mergeStateStatus']}")
        # gh --match-head-commit prevents a changed head from slipping through the final request.
        try:
            await self.gh("pr", "merge", str(number), "--repo", self.config.repository,
                          "--squash", "--match-head-commit", sha)
        except RuntimeError as exc:
            text = str(exc).lower()
            if any(marker in text for marker in ("permission", "not authorized", "resource not accessible", "http 403")):
                raise NeedsHumanApproval("gh 账号没有此仓库的合并权限：" + str(exc)[-500:]) from exc
            raise
        final = await self.pr(number)
        if final["state"] != "MERGED":
            raise PendingCI("GitHub 尚未完成合并")
