from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from repairbot.config import Config, ProviderConfig, normalize_repository
from repairbot.codex_quota import normalize_usage, read_usage
from repairbot.github import GitHub, PendingCI
from repairbot.process import Result, agent_environment, checked, run
from repairbot.providers import (NoCapacity, Provider, ProviderFailure, Quota, Scheduler,
                                 discover, final_response, quota_exhausted)
from repairbot.service import enqueue_message
from repairbot.store import Store
from repairbot.workflow import Workflow


def config(root: Path, **kwargs) -> Config:
    return Config(1, 2, 3, "owner/repo", root, **kwargs)


class ParsingTests(unittest.TestCase):
    def test_cli_formats(self):
        response = {"should_fix": True, "summary": "修复空指针"}
        codex = '\n'.join([json.dumps({"type": "thread.started"}), json.dumps({
            "type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(response)}})])
        claude = json.dumps({"type": "result", "is_error": False, "result": json.dumps(response)}, indent=2)
        self.assertEqual(final_response(codex), response)
        self.assertEqual(final_response(claude), response)
        self.assertEqual(final_response(json.dumps({"type": "result", "structured_output": response})), response)
        self.assertEqual(final_response('```json\n' + json.dumps(response) + '\n```'), response)

    def test_fail_closed_on_bad_response(self):
        for text in ('not json', '[1]', '{"approved":"true"}', '{"type":"result","is_error":true,"result":"oops"}'):
            with self.assertRaises(ProviderFailure):
                final_response(text)

    def test_limit_detection_ignores_agent_quotations(self):
        quoted = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "User reported weekly limit reached"}})
        self.assertFalse(quota_exhausted(Result(0, quoted)))
        self.assertTrue(quota_exhausted(Result(1, "You've hit your usage limit")))
        self.assertTrue(quota_exhausted(Result(0, json.dumps({"type": "result", "is_error": True,
                                                            "result": "Credit balance is too low"}, indent=2))))
        self.assertTrue(quota_exhausted(Result(0, '{"type":"turn.failed","error":{"message":"insufficient_quota"}}')))

    def test_quota_validation(self):
        self.assertEqual(Quota.parse('{"remaining_fraction":0.75}').remaining_fraction, .75)
        for value in (-1, 2, True, "0.5", float("nan")):
            with self.assertRaises(ValueError):
                Quota.parse(json.dumps({"remaining_fraction": value}))

    def test_codex_usage_uses_tightest_window_and_correct_reset(self):
        payload = {"rateLimits": {"primary": {"usedPercent": 10, "resetsAt": 1000},
                                  "secondary": {"usedPercent": 70, "resetsAt": 2000}}}
        self.assertAlmostEqual(normalize_usage(payload)["remaining_fraction"], .3)
        self.assertEqual(normalize_usage(payload)["reset_at"], 2000)
        payload["rateLimits"]["primary"]["usedPercent"] = 100
        payload["rateLimits"]["secondary"]["usedPercent"] = 100
        self.assertEqual(normalize_usage(payload), {"remaining_fraction": 0, "reset_at": 2000})
        payload["rateLimits"]["credits"] = {"hasCredits": True}
        self.assertIsNone(normalize_usage(payload)["remaining_fraction"])

    def test_config_guards(self):
        self.assertEqual(normalize_repository("https://github.com/me/app.git"), "me/app")
        for value in ("https://evil.test/me/app", "../app", "me/app; rm -rf", "-owner/repo/extra"):
            with self.assertRaises(ValueError):
                normalize_repository(value)
        c = config(Path("/tmp"))
        c.report_channel_id = c.listen_channel_id
        with self.assertRaises(ValueError):
            c.validate()

    def test_environment_removes_coordinator_secrets(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DISCORD_TOKEN": "secret", "GH_TOKEN": "github", "ANTHROPIC_API_KEY": "model-key"}):
            env = agent_environment(Path(tmp))
            self.assertNotIn("DISCORD_TOKEN", env)
            self.assertNotIn("GH_TOKEN", env)
            self.assertEqual(env["ANTHROPIC_API_KEY"], "model-key")

    def test_discover_reuses_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codex"
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o700)
            self.assertEqual(discover("codex", str(path)), str(path))
            self.assertIsNone(discover("claude", str(path.parent / "missing")))


class StoreTests(unittest.TestCase):
    def test_dedup_recovery_and_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self.assertTrue(store.enqueue("111", "error"))
            self.assertFalse(store.enqueue("111", "error again"))
            job = store.next_job()
            store.save(job["id"], phase="review", data={"pr": 1})
            store.recover()
            self.assertEqual(store.next_job()["phase"], "review")
            store.save("111", status="waiting", retry_at=time.time() + 60)
            self.assertIsNone(store.next_job())
            self.assertTrue(store.retry_failed("111"))
            self.assertEqual(store.next_job()["data"]["pr"], 1)
            store.set_cursor(2, 200)
            store.set_cursor(2, 100)
            self.assertEqual(store.cursor(2), "200")


class DiscordRoutingTests(unittest.TestCase):
    def test_server_channels_author_filter_and_duplicate_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = config(Path(tmp), allowed_author_ids=[4])
            store = Store(c.state_dir)
            message = NS(id=123, guild=NS(id=1), channel=NS(id=2), author=NS(id=4), content="Error: handler failed", embeds=[])
            self.assertTrue(enqueue_message(c, store, message, 99))
            self.assertFalse(enqueue_message(c, store, message, 99))
            message.id = 124
            for guild, channel, author in ((9, 2, 4), (1, 3, 4), (1, 2, 5), (1, 2, 99)):
                message.guild.id, message.channel.id, message.author.id = guild, channel, author
                self.assertFalse(enqueue_message(c, store, message, 99))
            self.assertEqual(store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_embed_error_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DISCORD_TOKEN": "example-secret"}):
            c = config(Path(tmp))
            store = Store(c.state_dir)
            embed = NS(title="TypeError", description="handler failed", fields=[NS(name="trace", value="example-secret")])
            message = NS(id=456, guild=NS(id=1), channel=NS(id=2), author=NS(id=4), content="", embeds=[embed])
            self.assertTrue(enqueue_message(c, store, message, 99))
            content = store.next_job()["content"]
            self.assertIn("TypeError", content)
            self.assertNotIn("example-secret", content)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.report = AsyncMock()
        self.scheduler = Scheduler(config(self.root), self.store, self.report)
        self.scheduler.providers = [Provider(n, n, ProviderConfig(native_quota=False), "logged_in", Quota(v))
                                    for n, v in (("codex", .3), ("claude", .8))]

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_rank_and_independent_review_preference(self):
        self.assertEqual(self.scheduler.ranked(set())[0].name, "claude")
        self.assertEqual(self.scheduler.ranked(set(), "claude")[0].name, "codex")
        self.store.block("claude", time.time() + 100)
        self.assertEqual(self.scheduler.ranked(set())[0].name, "codex")

    async def test_limit_switch_hands_off_partial_output_and_files(self):
        captured = []

        async def fake_run(argv, **kwargs):
            captured.append(kwargs["stdin"])
            if len(captured) == 1:
                (self.root / "partial.py").write_text("partial fix")
                output = 'previous investigation: null value in handler\n{"type":"error","message":"usage limit reached"}'
                kwargs["transcript"].write_text(output)
                return Result(1, output)
            self.assertTrue((self.root / "partial.py").exists())
            return Result(0, '{"fixed":true,"summary":"修复完成"}')

        self.scheduler.refresh = AsyncMock()
        with patch("repairbot.providers.run", fake_run):
            response, tool = await self.scheduler.session("repair", "Fix error", self.root, self.root / "sessions")
        self.assertEqual(tool, "codex")
        self.assertTrue(response["fixed"])
        self.assertIn("null value in handler", captured[1])
        self.assertGreater(self.store.blocked_until("claude"), time.time())

    async def test_both_exhausted_raise_capacity_and_persist_blocks(self):
        self.scheduler.refresh = AsyncMock()
        with patch("repairbot.providers.run", AsyncMock(return_value=Result(1, "usage limit reached"))):
            with self.assertRaises(NoCapacity):
                await self.scheduler.session("repair", "fix", self.root, self.root / "sessions")
        self.assertEqual(self.scheduler.ranked(set()), [])

    async def test_quota_hook_updates_order(self):
        for p in self.scheduler.providers:
            p.config.quota_command = [p.name, "quota"]
        replies = [Result(0, '{"remaining_fraction":0.9}'), Result(0, '{"remaining_fraction":0.1}')]
        with patch("repairbot.providers.run", AsyncMock(side_effect=replies)):
            await self.scheduler.refresh()
        self.assertEqual(self.scheduler.ranked(set())[0].name, "codex")

    async def test_unknown_quota_does_not_fabricate_value(self):
        self.scheduler.providers[0].config.quota_command = ["missing"]
        with patch("repairbot.providers.run", AsyncMock(side_effect=OSError("missing"))):
            await self.scheduler.refresh()
        self.assertIsNone(self.scheduler.providers[0].quota.remaining_fraction)
        self.assertTrue(self.scheduler.providers[0].quota_error)


class MergeTests(unittest.IsolatedAsyncioTestCase):
    def pr(self, **kwargs):
        result = {"state": "OPEN", "headRefOid": "head", "baseRefOid": "base", "isDraft": False,
                  "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                  "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS", "name": "test"}]}
        result.update(kwargs)
        return result

    async def test_requires_successful_ci_and_unchanged_versions(self):
        for pr, exception in (
            (self.pr(headRefOid="new"), RuntimeError),
            (self.pr(baseRefOid="new"), RuntimeError),
            (self.pr(statusCheckRollup=[]), PendingCI),
            (self.pr(statusCheckRollup=[{"status": "IN_PROGRESS"}]), PendingCI),
            (self.pr(statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}]), RuntimeError),
            (self.pr(statusCheckRollup=[{"status": "COMPLETED", "conclusion": "SKIPPED"}]), RuntimeError),
            (self.pr(statusCheckRollup=[{"__typename": "StatusContext", "state": "ERROR"}]), RuntimeError),
            (self.pr(mergeStateStatus="BLOCKED"), RuntimeError),
        ):
            github = GitHub(config(Path("/tmp")))
            github.pr = AsyncMock(return_value=pr)
            github.gh = AsyncMock()
            with self.assertRaises(exception):
                await github.merge(1, "head", "base")
            github.gh.assert_not_awaited()

    async def test_merge_uses_exact_head_guard(self):
        github = GitHub(config(Path("/tmp")))
        github.pr = AsyncMock(side_effect=[self.pr(), self.pr(state="MERGED")])
        github.gh = AsyncMock()
        await github.merge(1, "head", "base")
        argv = github.gh.call_args.args
        self.assertIn("--match-head-commit", argv)
        self.assertEqual(argv[-1], "head")


class FakeGitHub:
    def __init__(self, root):
        self.root = root
        self.merges = 0
        self.publishes = 0
        self.tests_run = 0

    async def prepare(self, root, branch):
        repo = root / "repo"
        repo.mkdir(exist_ok=True)
        return repo, "main"

    async def clean(self, repo):
        return True

    async def head(self, repo):
        return "fixed" if repo.name.startswith("review-") else "initial"

    async def gh(self, *args):
        return "[]"

    async def git(self, *args):
        return ""

    async def tests(self, *args):
        self.tests_run += 1

    async def publish(self, *args):
        self.publishes += 1
        return await self.pr(1)

    async def pr(self, *args):
        return {"number": 1, "url": "https://github.com/owner/repo/pull/1", "state": "OPEN",
                "headRefOid": "fixed", "baseRefOid": "base"}

    async def assert_version(self, *args):
        return await self.pr(1)

    async def review_copy(self, repo, target, sha):
        target.mkdir(exist_ok=True)
        (target / ".git").mkdir(exist_ok=True)
        return target

    async def merge(self, *args):
        self.merges += 1


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = config(self.root)
        self.store = Store(self.root)
        self.store.enqueue("111", "TypeError: null")
        self.report = AsyncMock()
        self.scheduler = AsyncMock()
        self.scheduler.next_retry = lambda: time.time() + 60
        self.workflow = Workflow(self.config, self.store, self.scheduler, self.report)
        self.workflow.github = FakeGitHub(self.root)

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def responses(self, approved=True):
        return [({"should_fix": True, "summary": "缺陷"}, "codex"),
                ({"fixed": True, "summary": "已修复"}, "claude"),
                ({"approved": approved, "summary": "审查"}, "codex"),
                ({"approved": True, "summary": "审查"}, "claude")]

    def row(self):
        return dict(self.store.db.execute("SELECT * FROM jobs WHERE id='111'").fetchone())

    async def test_full_pipeline_creates_pr_reviews_twice_merges(self):
        self.scheduler.session.side_effect = self.responses()
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(self.workflow.github.publishes, 1)
        self.assertEqual(self.workflow.github.tests_run, 1)
        self.assertEqual(self.workflow.github.merges, 1)
        calls = self.scheduler.session.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["triage", "repair", "review-1", "review-2"])
        self.assertNotEqual(calls[2].args[2], calls[3].args[2])

    async def test_rejected_review_keeps_pr_and_never_merges(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "failed")
        self.assertEqual(self.workflow.github.publishes, 1)
        self.assertEqual(self.workflow.github.merges, 0)

    async def test_environment_error_does_not_create_pr(self):
        self.scheduler.session.return_value = ({"should_fix": False, "summary": "凭据无效"}, "codex")
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(self.workflow.github.publishes, 0)

    async def test_no_capacity_waits_and_resumes_current_phase(self):
        self.scheduler.session.side_effect = [self.responses()[0], NoCapacity("额度耗尽")]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "waiting")
        self.assertEqual(self.row()["phase"], "repair")
        self.store.retry_failed("111")
        self.scheduler.session.side_effect = self.responses()[1:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(self.workflow.github.merges, 1)

    async def test_ci_wait_retry_reuses_reviews_and_pr(self):
        self.scheduler.session.side_effect = self.responses()
        self.workflow.github.merge = AsyncMock(side_effect=PendingCI("running"))
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["phase"], "merge")
        self.assertEqual(self.row()["status"], "waiting")
        self.store.retry_failed("111")
        self.workflow.github.merge = AsyncMock()
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(self.scheduler.session.await_count, 4)
        self.assertEqual(self.workflow.github.publishes, 1)

    async def test_publish_restart_does_not_repeat_ai_repair(self):
        self.store.save("111", phase="publish", data={"repair": {"summary": "已修复"}, "repair_tool": "codex"})
        self.scheduler.session.side_effect = self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(self.scheduler.session.await_count, 2)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_quota_rpc_with_installed_cli_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "fake-codex"
            cli.write_text(f"#!{sys.executable}\n" + '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "initialize":
        print(json.dumps({"id":request["id"],"result":{}}), flush=True)
    elif request["method"] == "account/rateLimits/read":
        print(json.dumps({"id":request["id"],"result":{"rateLimits":{"primary":{"usedPercent":20,"resetsAt":2000}}}}), flush=True)
''')
            cli.chmod(0o700)
            usage = await read_usage(str(cli), Path(tmp), os.environ.copy())
            self.assertEqual(normalize_usage(usage), {"remaining_fraction": .8, "reset_at": 2000})

    async def test_stdin_and_partial_output_survive_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "session.log"
            with self.assertRaises(TimeoutError):
                await run([sys.executable, "-u", "-c", "import sys,time; print(sys.stdin.read(),flush=True); time.sleep(10)"],
                          stdin="handoff", timeout=0.1, transcript=log)
            self.assertIn("handoff", log.read_text())
            self.assertIn("interrupted", log.read_text())

    async def test_review_clone_isolated_at_exact_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "source"
            repo.mkdir()
            await checked(["git", "init", "-b", "main"], cwd=repo)
            await checked(["git", "config", "user.name", "Test"], cwd=repo)
            await checked(["git", "config", "user.email", "test@example.com"], cwd=repo)
            (repo / "app.py").write_text("before")
            await checked(["git", "add", "."], cwd=repo)
            await checked(["git", "commit", "-m", "initial"], cwd=repo)
            github = GitHub(config(root))
            sha = await github.head(repo)
            target = await github.review_copy(repo, root / "review", sha)
            self.assertEqual(await github.head(target), sha)
            (target / "app.py").write_text("changed")
            self.assertEqual((repo / "app.py").read_text(), "before")
            self.assertFalse(await github.clean(target))


if __name__ == "__main__":
    unittest.main()
