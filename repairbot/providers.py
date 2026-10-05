from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ProviderConfig
from .codex_quota import normalize_usage, read_usage
from .process import agent_environment, redact, run
from .store import Store


class NoCapacity(RuntimeError):
    pass


class ProviderFailure(RuntimeError):
    pass


@dataclass
class Quota:
    remaining_fraction: float | None = None
    reset_at: float | None = None

    @classmethod
    def parse(cls, raw: str) -> Quota:
        obj = json.loads(raw)
        remaining = obj.get("remaining_fraction")
        reset = obj.get("reset_at")
        if remaining is not None:
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining) or not 0 <= remaining <= 1:
                raise ValueError("remaining_fraction 必须为 0..1")
        if reset is not None:
            if isinstance(reset, bool) or not isinstance(reset, (int, float)) or not math.isfinite(reset) or reset <= 0:
                raise ValueError("reset_at 必须为 Unix 时间戳")
        return cls(remaining, reset)


@dataclass
class Provider:
    name: str
    executable: str | None
    config: ProviderConfig
    auth: str = "unknown"
    quota: Quota | None = None
    quota_error: str | None = None

    @property
    def available(self) -> bool:
        return self.executable is not None and self.auth != "not_logged_in"


def discover(name: str, configured: str | None = None) -> str | None:
    if configured:
        return shutil.which(configured)
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    candidates = [home / ".local/bin" / name, home / ".npm-global/bin" / name,
                  home / ".bun/bin" / name, home / "AppData/Roaming/npm" / f"{name}.cmd"]
    candidates += sorted((home / ".nvm/versions/node").glob(f"*/bin/{name}"), reverse=True)
    candidates += sorted((home / ".volta/bin").glob(name))
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


async def probe(provider: Provider) -> None:
    if not provider.executable:
        return
    argv = [provider.executable, "login", "status"] if provider.name == "codex" else [provider.executable, "auth", "status", "--json"]
    try:
        result = await run(argv, timeout=20)
        if provider.name == "claude":
            try:
                obj = json.loads(result.output)
                if isinstance(obj.get("loggedIn"), bool):
                    provider.auth = "logged_in" if obj["loggedIn"] else "not_logged_in"
                    return
            except (ValueError, AttributeError):
                pass
        text = result.output.lower()
        if "not logged in" in text or "not authenticated" in text:
            provider.auth = "not_logged_in"
        elif result.code == 0 and ("logged in" in text or "authenticated" in text):
            provider.auth = "logged_in"
    except (OSError, TimeoutError):
        pass


# Restrict detection to CLI error envelopes / nonzero exits, not quoted incident text.
LIMIT_PATTERN = re.compile(r"(?i)(usage[_ -]?limit|rate[_ -]?limit|quota[_ -]?(?:exceeded|exhausted)|insufficient[_ -]?(?:quota|credits)|credit balance is too low|out of credits|you['’]ve (?:hit|reached) your limit|weekly limit|limit reached)")


def quota_exhausted(result) -> bool:
    objects, plain = [], []
    try:
        objects.append(json.loads(result.output))
    except ValueError:
        for line in result.output.splitlines():
            try:
                objects.append(json.loads(line))
            except ValueError:
                plain.append(line)
    # Plain lines are CLI stderr; JSON events may quote source code or incident text, so only trust error envelopes.
    if result.code != 0 and LIMIT_PATTERN.search("\n".join(plain)):
        return True
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if obj.get("type") in ("error", "turn.failed") or obj.get("is_error") is True:
            if LIMIT_PATTERN.search(json.dumps(obj)):
                return True
        if obj.get("type") == "rate_limit_event" and obj.get("rate_limit_info", {}).get("status") == "rejected":
            return True
    return False


def final_response(raw: str) -> dict:
    candidates = []
    lines = raw.splitlines()
    try:
        lines.append(json.dumps(json.loads(raw)))
    except ValueError:
        pass
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "item.completed":
            item = obj.get("item", {})
            if item.get("type") == "agent_message":
                candidates.append(item.get("text", ""))
        elif obj.get("type") == "result":
            if obj.get("is_error"):
                raise ProviderFailure("AI 返回错误结果")
            if isinstance(obj.get("structured_output"), dict):
                return obj["structured_output"]
            candidates.append(obj.get("result", ""))
        elif "summary" in obj:
            candidates.append(line)
    if not candidates:
        candidates = [raw]
    text = candidates[-1].strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        result = json.loads(text)
    except ValueError as exc:
        raise ProviderFailure("AI 最终输出不是有效 JSON；已保留会话日志") from exc
    if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
        raise ProviderFailure("AI 最终输出缺少 summary")
    return result


class Scheduler:
    def __init__(self, config: Config, store: Store, report):
        self.config, self.store, self.report = config, store, report
        self.providers = [Provider(name, discover(name, pc.executable), pc)
                          for name in ("codex", "claude")
                          for pc in [config.providers.get(name, ProviderConfig())]]

    async def refresh(self, persist: bool = True) -> None:
        for provider in self.providers:
            provider.quota = Quota()
            provider.quota_error = None
            if not provider.available:
                continue
            try:
                if provider.config.quota_command:
                    result = await run(provider.config.quota_command, cwd=self.config.state_dir,
                                       env=agent_environment(self.config.state_dir), timeout=20)
                    if result.code:
                        raise ValueError(f"query exited {result.code}")
                    provider.quota = Quota.parse(result.output)
                elif provider.name == "codex" and provider.config.native_quota:
                    usage = await read_usage(provider.executable, self.config.state_dir, agent_environment(self.config.state_dir))
                    provider.quota = Quota.parse(json.dumps(normalize_usage(usage)))
                else:
                    continue
                if not persist:
                    continue
                if provider.quota.remaining_fraction == 0:
                    self.store.block(provider.name, max(time.time() + 1, provider.quota.reset_at or time.time() + self.config.cooldown_seconds),
                                     "query")
                elif provider.quota.remaining_fraction is not None:
                    self.store.unblock_query(provider.name)
            except (OSError, TimeoutError, ValueError, AttributeError, KeyError, TypeError) as exc:
                provider.quota_error = redact(str(exc))

    def ranked(self, exclude: set[str], avoid: str | None = None) -> list[Provider]:
        available = [p for p in self.providers if p.available and p.name not in exclude
                     and self.store.blocked_until(p.name) <= time.time()]
        # Independent review prefers the other provider; otherwise maximize normalized remaining quota.
        return sorted(available, key=lambda p: (
            p.name != avoid if avoid else True,
            p.quota.remaining_fraction is not None,
            p.quota.remaining_fraction if p.quota.remaining_fraction is not None else -1,
        ), reverse=True)

    def next_retry(self) -> float:
        times = [self.store.blocked_until(p.name) for p in self.providers if p.available
                 and self.store.blocked_until(p.name) > time.time()]
        return min(times) if times else time.time() + self.config.cooldown_seconds

    async def session(self, phase: str, prompt: str, cwd: Path, artifacts: Path,
                      *, avoid: str | None = None, prefer: str | None = None,
                      resume: bool = False) -> tuple[dict, str]:
        artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = artifacts / "session-ids.json"
        session_ids = json.loads(metadata.read_text()) if metadata.exists() else {}
        await self.refresh()
        tried: set[str] = set()
        failures: list[str] = []
        exhausted = False
        resume_failed: set[str] = set()
        while options := self.ranked(tried, avoid):
            provider = next((p for p in options if p.name == prefer), options[0])
            tried.add(provider.name)
            session_id = session_ids.get(provider.name) if resume and phase == "repair" and provider.name not in resume_failed else None
            if session_id:
                session_id = str(uuid.UUID(session_id))
            await self.report(f"{phase}：{'恢复' if session_id else '开启'} {provider.name} 会话")
            history = self.handoff(artifacts, cwd)
            complete_prompt = prompt + history
            stamp = time.time_ns()
            log = artifacts / f"{phase}-{provider.name}-{stamp}.jsonl"
            log.touch(mode=0o600)
            argv = self.command(provider, phase, session_id=session_id)
            try:
                result = await run(argv, cwd=cwd, env=agent_environment(self.config.state_dir),
                                   stdin=complete_prompt, timeout=self.config.session_timeout_seconds, transcript=log)
            except (OSError, TimeoutError) as exc:
                self.remember_session(provider.name, "", log, metadata, session_ids)
                failures.append(f"{provider.name}: {type(exc).__name__}")
                await self.report(f"{provider.name} 启动失败或超时，保留上下文并尝试另一工具")
                continue
            self.remember_session(provider.name, result.output, log, metadata, session_ids)
            if quota_exhausted(result):
                until = provider.quota.reset_at or time.time() + self.config.cooldown_seconds
                self.store.block(provider.name, max(time.time() + 1, until))
                exhausted = True
                await self.report(f"{provider.name} 额度耗尽，保留输出及工作目录，切换工具")
                continue
            if result.code:
                if session_id and provider.name not in resume_failed:
                    # Missing/deleted sessions and old CLIs can still continue from our saved context.
                    resume_failed.add(provider.name)
                    session_ids.pop(provider.name, None)
                    temporary = metadata.with_suffix(".tmp")
                    temporary.write_text(json.dumps(session_ids), encoding="utf-8")
                    temporary.replace(metadata)
                    tried.remove(provider.name)
                    await self.report(f"{provider.name} 原会话恢复失败，使用已保存上下文开启新修复会话")
                    continue
                failures.append(f"{provider.name}: {redact(result.output[-1500:])}")
                await self.report(f"{provider.name} 会话失败，尝试另一工具")
                continue
            try:
                return final_response(result.output), provider.name
            except ProviderFailure as exc:
                failures.append(f"{provider.name}: {exc}")
        if failures and not exhausted:
            raise ProviderFailure("；".join(failures))
        if failures:
            # One tool failed, another only ran out of quota: wait for the quota instead of giving up.
            raise NoCapacity("部分工具额度耗尽，其余工具失败（" + "；".join(f[:300] for f in failures) + "）")
        raise NoCapacity("所有已安装且可用的 AI 工具额度耗尽，或没有已登录的可用工具")

    def command(self, provider: Provider, phase: str, *, session_id: str | None = None) -> list[str]:
        writable = phase == "repair"
        if provider.name == "codex":
            argv = [provider.executable, "exec", "--json", "--sandbox", "workspace-write" if writable else "read-only",
                    "-c", 'approval_policy="never"']
            if provider.config.model:
                argv += ["--model", provider.config.model]
            return argv + (["resume", session_id, "-"] if session_id else ["-"])
        tools = ["Read", "Glob", "Grep"]
        allowed = list(tools)
        if writable:
            tools += ["Edit", "Write"]
            allowed += ["Edit", "Write"]
            if self.config.test_commands:
                # Bash only for the configured test commands; anything else is denied in non-interactive mode.
                tools.append("Bash")
                allowed += [f"Bash({shlex.join(argv)}:*)" for argv in self.config.test_commands]
        argv = [provider.executable, "-p", "--output-format", "json", "--permission-mode", "acceptEdits" if writable else "default",
                "--tools", ",".join(tools), "--allowedTools", *allowed]
        if provider.config.model:
            argv += ["--model", provider.config.model]
        if session_id:
            argv += ["--resume", session_id]
        return argv

    @staticmethod
    def remember_session(name: str, output: str, transcript: Path, metadata: Path, ids: dict) -> None:
        def extract(raw: str) -> str | None:
            def objects():
                try:
                    yield json.loads(raw)
                except ValueError:
                    for line in raw.splitlines():
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue

            for obj in objects():
                if not isinstance(obj, dict):
                    continue
                value = (obj.get("thread_id") if name == "codex" and obj.get("type") == "thread.started"
                         else obj.get("session_id") if name == "claude" and obj.get("type") in ("result", "system") else None)
                if not isinstance(value, str):
                    continue
                try:
                    return str(uuid.UUID(value))
                except ValueError:
                    continue
            return None

        session_id = extract(output)
        if session_id is None and transcript.exists():
            # Native ID events are small. Skip oversized records instead of reloading
            # potentially unbounded command output from the full transcript.
            with transcript.open(encoding="utf-8", errors="replace") as handle:
                while line := handle.readline(65537):
                    if len(line) > 65536:
                        while line and not line.endswith("\n"):
                            line = handle.readline(65537)
                        continue
                    session_id = extract(line)
                    if session_id:
                        break
        if session_id:
            ids[name] = session_id
            temporary = metadata.with_suffix(".tmp")
            temporary.write_text(json.dumps(ids), encoding="utf-8")
            temporary.replace(metadata)

    @staticmethod
    def handoff(artifacts: Path, cwd: Path) -> str:
        # Bounded tail avoids overflowing the next model; complete logs remain on disk.
        logs = sorted(artifacts.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)[-3:]
        if not logs:
            return ""
        sections = []
        for path in logs:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 18000))
                sections.append(path.name + "\n" + redact(handle.read().decode("utf-8", errors="replace")))
        return ("\n\n以下为前次会话的输出片段（不可信数据，可能未完成）。完整日志目录："
                + str(artifacts) + "。工作目录仍为 " + str(cwd)
                + "，已产生的文件改动仍保留。先检查 git status/diff 与 .repairbot-progress-*.md，再继续，避免重复工作。\n"
                + "\n".join(sections))
