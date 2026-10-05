from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from repairbot.config import Config, ProviderConfig, normalize_repository
from repairbot.codex_quota import normalize_usage, read_usage
from repairbot.github import BranchBehind, GitHub, NeedsHumanApproval, PendingCI, changed_path_risks
from repairbot.process import Result, agent_environment, checked, run
from repairbot.providers import (NoCapacity, Provider, ProviderFailure, Quota, Scheduler,
                                 discover, final_response, quota_exhausted)
from repairbot.service import enqueue_message, fingerprint
from repairbot.store import Store
from repairbot.workflow import Workflow


def config(root: Path, **kwargs) -> Config:
    kwargs.setdefault("allowed_author_ids", [4])
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
        command = json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                                                                  "aggregated_output": "raise RateLimitError('rate limit')"}})
        self.assertFalse(quota_exhausted(Result(1, command)))
        self.assertFalse(quota_exhausted(Result(1, json.dumps({"type": "result", "is_error": False,
                                                               "result": "fixed the rate_limit handler"}, indent=2))))

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

    def test_legacy_review_fix_limit_is_ignored_when_loading_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'guild_id': 1, 'listen_channel_id': 2, 'report_channel_id': 3,
                                        'repository': 'owner/repo', 'allowed_author_ids': [4],
                                        'max_review_fix_rounds': 0}))
            c = Config.load(str(path))
            self.assertFalse(hasattr(c, 'max_review_fix_rounds'))

    def test_config_types_and_author_ids(self):
        c = config(Path("/tmp"), allowed_author_ids=["123456789012345678", 4])
        c.validate()
        self.assertEqual(c.allowed_author_ids, [123456789012345678, 4])
        for kwargs in ({"auto_merge": "false", "allowed_author_ids": [4]},
                       {"require_ci": "true", "allowed_author_ids": [4]},
                       {"reviewers": "2", "allowed_author_ids": [4]},
                       {"allowed_author_ids": ["abc"]},
                       {"allowed_author_ids": [True]},
                       {"allowed_author_ids": []},
                       {"allowed_author_ids": [], "auto_merge": False},
                       {"error_pattern": "("},
                       {"protected_paths": "x"}):
            with self.assertRaises(ValueError, msg=kwargs):
                config(Path("/tmp"), **kwargs).validate()
        config(Path("/tmp"), auto_merge=False).validate()
        with self.assertRaises(ValueError):
            Config(1, 2, 3, 123, Path("/tmp"), allowed_author_ids=[4]).validate()

    def test_config_file_errors_are_readable(self):
        base = {"guild_id": 1, "listen_channel_id": 2, "report_channel_id": 3,
                "repository": "owner/repo", "allowed_author_ids": [4]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for extra, message in (({"reviewer": 1}, "reviewer"), ({"providers": {"codex": {"exe": "x"}}}, "providers.codex.exe")):
                path.write_text(json.dumps({**base, **extra}))
                with self.assertRaisesRegex(ValueError, message):
                    Config.load(str(path))
            path.write_text(json.dumps({k: v for k, v in base.items() if k != "repository"}))
            with self.assertRaisesRegex(ValueError, "repository"):
                Config.load(str(path))
            path.write_text(json.dumps(base))
            self.assertEqual(Config.load(str(path)).repository, "owner/repo")

    def test_protected_paths_and_deletions_are_flagged(self):
        patterns = config(Path("/tmp")).protected_paths
        diff = "M\tsrc/app.py\nA\ttests/test_app.py\nM\t.github/workflows/ci.yml\nD\ttests/test_old.py\nM\tdocs/CODEOWNERS"
        self.assertEqual(changed_path_risks(diff, patterns),
                         ["修改受保护路径 .github/workflows/ci.yml", "删除 tests/test_old.py", "修改受保护路径 docs/CODEOWNERS"])
        self.assertEqual(changed_path_risks("M\tsrc/app.py", patterns), [])

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
            store.save("111", status="failed", data={"pr": 1, "ci_wait_since": 1.0, "ci_notified": True})
            self.assertTrue(store.retry_failed("111"))
            self.assertEqual(store.next_job()["data"], {"pr": 1})
            self.assertFalse(store.retry_failed("missing"))
            store.set_cursor(2, 200)
            store.set_cursor(2, 100)
            self.assertEqual(store.cursor(2), "200")


class DedupThrottleTests(unittest.TestCase):
    def test_fingerprint_ignores_volatile_parts(self):
        a = "2026-10-01 12:00:01 Error: request 4821 failed at 0x7ffd12ab id=3f2a1b4c-0000-4000-8000-123456789abc"
        b = "2026-10-02 08:15:44 Error:  request 99 failed at 0x1 id=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.assertEqual(fingerprint(a), fingerprint(b))
        self.assertNotEqual(fingerprint("KeyError: 'user'"), fingerprint("TypeError: 'user'"))

    def test_duplicates_recorded_once_and_window_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self.assertTrue(store.enqueue("1", "Error A", "fp", 3600))
            self.assertFalse(store.enqueue("2", "Error A", "fp", 3600))
            self.assertFalse(store.enqueue("2", "Error A", "fp", 3600))  # replayed history
            row = store.db.execute("SELECT status,data FROM jobs WHERE id='2'").fetchone()
            self.assertEqual((row[0], json.loads(row[1])), ("duplicate", {"duplicate_of": "1"}))
            self.assertTrue(store.enqueue("3", "Error B", "other", 3600))
            # Finished long ago: outside the window, the same error is handled again.
            store.save("1", status="done")
            store.db.execute("UPDATE jobs SET updated=0 WHERE id='1'")
            self.assertTrue(store.enqueue("4", "Error A", "fp", 3600))
            # Window 0 disables dedup; retry forces a duplicate to run.
            self.assertTrue(store.enqueue("5", "Error A", "fp", 0))
            self.assertTrue(store.retry_failed("2"))
            self.assertEqual(store.db.execute("SELECT status,data FROM jobs WHERE id='2'").fetchone()[:], ("queued", "{}"))

    def test_hourly_throttle_holds_new_jobs_but_not_in_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            for job_id in ("1", "2", "3"):
                store.enqueue(job_id, "Error " + job_id)
            first = store.next_job(max_starts_per_hour=1)
            self.assertEqual(first["id"], "1")
            self.assertIsNone(store.next_job(max_starts_per_hour=1))
            # A started job that went back to the queue (e.g. waiting on CI) still continues.
            store.save("1", status="waiting", phase="merge")
            self.assertEqual(store.next_job(max_starts_per_hour=1)["id"], "1")
            store.db.execute("UPDATE jobs SET started=1 WHERE id='1'")
            self.assertEqual(store.next_job(max_starts_per_hour=1)["id"], "2")
            self.assertEqual(store.next_job()["id"], "3")

    def test_migrates_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            import sqlite3
            db = sqlite3.connect(Path(tmp) / "state.sqlite3")
            db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, content TEXT NOT NULL, status TEXT NOT NULL, "
                       "phase TEXT NOT NULL DEFAULT 'triage', data TEXT NOT NULL DEFAULT '{}', "
                       "updated REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO jobs(id,content,status,phase,updated) VALUES('9','Error','queued','repair',1)")
            db.commit()
            db.close()
            store = Store(Path(tmp))
            self.assertEqual(store.next_job(max_starts_per_hour=1)["id"], "9")
            self.assertTrue(store.enqueue("10", "Error", "fp", 3600))


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

    async def test_quiet_sessions_suppress_progress_and_quota_switch_notifications(self):
        self.scheduler.refresh = AsyncMock()
        replies = [Result(1, 'usage limit reached'), Result(0, '{"fixed":true,"summary":"done"}')]
        with patch('repairbot.providers.run', AsyncMock(side_effect=replies)):
            response, tool = await self.scheduler.session('repair', 'fix', self.root,
                                                         self.root / 'sessions', quiet=True)
        self.assertTrue(response['fixed'])
        self.assertEqual(tool, 'codex')
        self.assertGreater(self.store.blocked_until('claude'), time.time())
        self.report.assert_not_awaited()

    async def test_repair_resumes_original_session_after_scheduler_restart(self):
        session_id = '11111111-1111-4111-8111-111111111111'
        for name in ('codex', 'claude'):
            artifacts = self.root / name
            self.scheduler.refresh = AsyncMock()
            output = (json.dumps({'type': 'thread.started', 'thread_id': session_id}) + '\n'
                      + '{"fixed":true,"summary":"done"}' if name == 'codex' else
                      json.dumps({'type': 'result', 'session_id': session_id,
                                  'result': '{"fixed":true,"summary":"done"}'}))
            with patch('repairbot.providers.run', AsyncMock(return_value=Result(0, output))) as runner:
                await self.scheduler.session('repair', 'fix', self.root, artifacts, prefer=name)
                self.assertEqual(runner.call_args.args[0][0], name)
            restarted = Scheduler(config(self.root), self.store, self.report)
            restarted.providers = self.scheduler.providers
            restarted.refresh = AsyncMock()
            with patch('repairbot.providers.run', AsyncMock(return_value=Result(0, output))) as runner:
                await restarted.session('repair', 'address review', self.root, artifacts,
                                        prefer=name, resume=True)
            argv = runner.call_args.args[0]
            self.assertIn(session_id, argv)
            self.assertIn('resume' if name == 'codex' else '--resume', argv)
            self.assertIn('workspace-write' if name == 'codex' else 'acceptEdits', argv)

    async def test_unavailable_original_tool_hands_feedback_to_other_tool(self):
        self.scheduler.refresh = AsyncMock()
        self.store.block('claude', time.time() + 60)
        with patch('repairbot.providers.run', AsyncMock(return_value=Result(0, '{"fixed":true,"summary":"done"}'))) as runner:
            await self.scheduler.session('repair', 'review: missing null guard', self.root,
                                         self.root / 'sessions', prefer='claude', resume=True)
        self.assertEqual(runner.call_args.args[0][0], 'codex')
        self.assertIn('missing null guard', runner.call_args.kwargs['stdin'])

    async def test_missing_native_session_falls_back_to_saved_context(self):
        self.scheduler.refresh = AsyncMock()
        artifacts = self.root / 'sessions'
        artifacts.mkdir()
        session_id = '11111111-1111-4111-8111-111111111111'
        (artifacts / 'session-ids.json').write_text(json.dumps({'codex': session_id}))
        (artifacts / 'repair-codex-1.jsonl').write_text('previous fix: guard empty input')
        replies = [Result(1, 'No session found'), Result(0, '{"fixed":true,"summary":"done"}')]
        with patch('repairbot.providers.run', AsyncMock(side_effect=replies)) as runner:
            await self.scheduler.session('repair', 'address review', self.root, artifacts,
                                         prefer='codex', resume=True)
        first, second = runner.call_args_list
        self.assertIn('resume', first.args[0])
        self.assertNotIn('resume', second.args[0])
        self.assertEqual(second.args[0][0], 'codex')
        self.assertIn('guard empty input', second.kwargs['stdin'])

    async def test_session_id_in_full_transcript_survives_output_tail_truncation(self):
        self.scheduler.refresh = AsyncMock()
        artifacts = self.root / 'sessions'
        session_id = '11111111-1111-4111-8111-111111111111'

        async def fake_run(argv, **kwargs):
            output = '{"fixed":true,"summary":"done"}'
            kwargs['transcript'].write_text(json.dumps({'type': 'thread.started', 'thread_id': session_id})
                                           + '\n' + output)
            return Result(0, output)

        with patch('repairbot.providers.run', fake_run):
            await self.scheduler.session('repair', 'fix', self.root, artifacts, prefer='codex')
        self.assertEqual(json.loads((artifacts / 'session-ids.json').read_text())['codex'], session_id)

    async def test_native_id_recovery_does_not_load_large_transcript_into_memory(self):
        session_id = '11111111-1111-4111-8111-111111111111'
        transcript, metadata = self.root / 'long.jsonl', self.root / 'session-ids.json'
        with transcript.open('w') as handle:
            handle.write(json.dumps({'type': 'thread.started', 'thread_id': session_id}) + '\n')
            for _ in range(1024):
                handle.write(json.dumps({'type': 'item.completed', 'text': 'x' * 8192}) + '\n')
        ids = {}
        tracemalloc.start()
        try:
            Scheduler.remember_session('codex', '{"fixed":true,"summary":"done"}', transcript, metadata, ids)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(ids['codex'], session_id)
        self.assertLess(peak, 4_000_000)

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

    async def test_exhausted_plus_failure_waits_instead_of_failing(self):
        self.scheduler.refresh = AsyncMock()
        replies = [Result(1, "usage limit reached"), Result(1, "crash")]
        with patch("repairbot.providers.run", AsyncMock(side_effect=replies)):
            with self.assertRaises(NoCapacity):
                await self.scheduler.session("repair", "fix", self.root, self.root / "sessions")

    async def test_quota_query_does_not_lift_cli_reported_limit(self):
        self.store.block("claude", time.time() + 100)
        self.scheduler.providers[1].config.quota_command = ["claude-quota"]
        with patch("repairbot.providers.run", AsyncMock(return_value=Result(0, '{"remaining_fraction":0.5}'))):
            await self.scheduler.refresh()
        self.assertGreater(self.store.blocked_until("claude"), time.time())
        with patch("repairbot.providers.run", AsyncMock(return_value=Result(0, '{"remaining_fraction":0}'))):
            await self.scheduler.refresh(persist=False)
        self.assertEqual(self.store.blocked_until("codex"), 0)
        self.scheduler.providers[0].config.quota_command = ["codex-quota"]
        with patch("repairbot.providers.run", AsyncMock(return_value=Result(0, '{"remaining_fraction":0}'))):
            await self.scheduler.refresh()
        self.assertGreater(self.store.blocked_until("codex"), time.time())
        with patch("repairbot.providers.run", AsyncMock(return_value=Result(0, '{"remaining_fraction":0.4}'))):
            await self.scheduler.refresh()
        self.assertEqual(self.store.blocked_until("codex"), 0)

    async def test_claude_repair_bash_limited_to_test_commands(self):
        claude = self.scheduler.providers[1]
        argv = self.scheduler.command(claude, "repair")
        self.assertNotIn("Bash", ",".join(argv))
        self.scheduler.config.test_commands = [["python3", "-m", "pytest", "-q"]]
        argv = self.scheduler.command(claude, "repair")
        allowed = argv[argv.index("--allowedTools") + 1:]
        self.assertIn("Bash(python3 -m pytest -q:*)", allowed)
        self.assertNotIn("Bash", allowed)
        self.assertNotIn("Edit", self.scheduler.command(claude, "triage"))

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

    async def test_update_same_pr_uses_lease_and_recovers_already_pushed_commit(self):
        github = GitHub(config(Path('/tmp')))
        github.assert_origin = AsyncMock()
        github.head = AsyncMock(return_value='new')
        github.git = AsyncMock(side_effect=['repairbot/discord-111', '', ''])
        github.pr = AsyncMock(side_effect=[self.pr(), self.pr(headRefOid='new')])
        await github.update_pr(Path('/tmp/repo'), 'repairbot/discord-111', 1, 'head', 'new')
        push = github.git.call_args_list[-1].args
        self.assertIn('--force-with-lease=refs/heads/repairbot/discord-111:head', push)
        self.assertIn('new:refs/heads/repairbot/discord-111', push)
        github.git.reset_mock(side_effect=True)
        github.git.return_value = 'repairbot/discord-111'
        github.pr = AsyncMock(return_value=self.pr(headRefOid='new'))
        await github.update_pr(Path('/tmp/repo'), 'repairbot/discord-111', 1, 'head', 'new')
        self.assertFalse(any('push' in call.args for call in github.git.call_args_list))

    async def test_update_same_pr_rejects_external_commit_and_closed_pr(self):
        github = GitHub(config(Path('/tmp')))
        github.assert_origin = AsyncMock()
        github.head = AsyncMock(return_value='new')
        github.git = AsyncMock(return_value='repairbot/discord-111')
        for pr in (self.pr(headRefOid='human'), self.pr(state='MERGED')):
            github.pr = AsyncMock(return_value=pr)
            with self.assertRaises(RuntimeError):
                await github.update_pr(Path('/tmp/repo'), 'repairbot/discord-111', 1, 'head', 'new')
        self.assertFalse(any('push' in call.args for call in github.git.call_args_list))

    async def test_requires_successful_ci_and_unchanged_versions(self):
        for pr, exception in (
            (self.pr(headRefOid="new"), RuntimeError),
            (self.pr(mergeStateStatus="BEHIND"), BranchBehind),
            (self.pr(statusCheckRollup=[]), PendingCI),
            (self.pr(statusCheckRollup=[{"status": "IN_PROGRESS"}]), PendingCI),
            (self.pr(statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}]), RuntimeError),
            (self.pr(statusCheckRollup=[{"status": "COMPLETED", "conclusion": "SKIPPED"}]), RuntimeError),
            (self.pr(statusCheckRollup=[{"__typename": "StatusContext", "state": "ERROR"}]), RuntimeError),
            (self.pr(mergeStateStatus="BLOCKED"), RuntimeError),
            (self.pr(mergeStateStatus="BLOCKED", reviewDecision="REVIEW_REQUIRED"), NeedsHumanApproval),
            (self.pr(reviewDecision="CHANGES_REQUESTED"), NeedsHumanApproval),
        ):
            github = GitHub(config(Path("/tmp")))
            github.pr = AsyncMock(return_value=pr)
            github.gh = AsyncMock()
            with self.assertRaises(exception):
                await github.merge(1, "head")
            github.gh.assert_not_awaited()

    async def test_base_move_keeps_reviews_and_grace_waits_for_checks(self):
        github = GitHub(config(Path("/tmp"), require_ci=False))
        github.pr = AsyncMock(return_value=self.pr(statusCheckRollup=[]))
        github.gh = AsyncMock()
        with self.assertRaises(PendingCI):
            await github.merge(1, "head", checks_grace=True)
        github.pr = AsyncMock(side_effect=[self.pr(baseRefOid="moved", statusCheckRollup=[]), self.pr(state="MERGED")])
        await github.merge(1, "head")
        github.gh.assert_awaited()

    async def test_merge_permission_denied_needs_human(self):
        github = GitHub(config(Path("/tmp")))
        github.pr = AsyncMock(return_value=self.pr())
        github.gh = AsyncMock(side_effect=RuntimeError("gh exited 1: GraphQL: Resource not accessible by integration"))
        with self.assertRaises(NeedsHumanApproval):
            await github.merge(1, "head")
        github.gh = AsyncMock(side_effect=RuntimeError("gh exited 1: network down"))
        with self.assertRaises(RuntimeError) as caught:
            await github.merge(1, "head")
        self.assertNotIsInstance(caught.exception, NeedsHumanApproval)

    async def test_merge_uses_exact_head_guard(self):
        github = GitHub(config(Path("/tmp")))
        github.pr = AsyncMock(side_effect=[self.pr(), self.pr(state="MERGED")])
        github.gh = AsyncMock()
        await github.merge(1, "head")
        argv = github.gh.call_args.args
        self.assertIn("--match-head-commit", argv)
        self.assertEqual(argv[-1], "head")


class FakeGitHub:
    def __init__(self, root):
        self.root = root
        self.merges = 0
        self.publishes = 0
        self.tests_run = 0
        self.events = []
        self.risks = []
        self.head_sha = "fixed"
        self.base_sha = "base"
        self.behind = 0
        self.approval_required = False
        self.local_sha = 'initial'
        self.revisions = 0

    async def prepare(self, root, branch):
        repo = root / "repo"
        repo.mkdir(exist_ok=True)
        return repo, "main"

    async def clean(self, repo):
        return True

    async def head(self, repo):
        return self.head_sha if repo.name.startswith("review-") else self.local_sha

    async def gh(self, *args):
        return "[]"

    async def git(self, *args):
        if args[1:3] == ('reset', '--hard'):
            self.local_sha = args[3]
        return ""

    async def tests(self, *args):
        self.events.append("tests")
        self.tests_run += 1

    async def commit(self, *args):
        self.events.append("commit")
        self.local_sha = 'fixed' if self.local_sha == 'initial' else f'revised-{self.revisions + 1}'

    async def discard(self, *args):
        self.events.append("discard")

    async def risky_changes(self, *args):
        return list(self.risks)

    async def publish(self, *args):
        self.publishes += 1
        return await self.pr(1)

    async def pr(self, *args):
        return {"number": 1, "url": "https://github.com/owner/repo/pull/1", "state": "OPEN",
                "headRefOid": self.head_sha, "baseRefOid": self.base_sha}

    async def assert_version(self, *args):
        if args[1] != self.head_sha:
            raise RuntimeError('PR 提交已改变')
        return await self.pr(1)

    async def update_pr(self, repo, branch, number, expected_sha, new_sha):
        if self.head_sha not in (expected_sha, new_sha):
            raise RuntimeError('PR 提交已改变')
        self.revisions += 1
        self.head_sha = new_sha
        return await self.pr(number)

    async def update_branch(self, *args):
        self.head_sha, self.base_sha = "updated", "base2"
        return await self.pr(1)

    async def review_copy(self, repo, target, sha):
        target.mkdir(exist_ok=True)
        (target / ".git").mkdir(exist_ok=True)
        return target

    async def merge(self, *args, **kwargs):
        if self.approval_required:
            raise NeedsHumanApproval("仓库要求人工批准")
        if self.behind:
            self.behind -= 1
            raise BranchBehind("落后")
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
                ({"approved": approved, "pr_needed": True, "summary": "审查"}, "codex"),
                ({"approved": True, "pr_needed": True, "summary": "审查"}, "claude")]

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
        self.assertEqual([c.kwargs["avoid"] for c in calls[2:]], ["claude", "claude"])
        self.assertEqual(self.workflow.github.events, ["commit", "tests", "discard"])

    async def test_protected_changes_are_not_auto_merged(self):
        self.workflow.github.risks = ["修改受保护路径 .github/workflows/ci.yml"]
        self.scheduler.session.side_effect = self.responses()
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(self.workflow.github.merges, 0)
        self.assertIn(".github", self.report.call_args.args[0])

    async def test_required_human_approval_is_reported_and_retryable(self):
        self.workflow.github.approval_required = True
        self.scheduler.session.side_effect = self.responses()
        await self.workflow.execute(self.store.next_job())
        self.assertEqual((self.row()["status"], self.row()["phase"]), ("failed", "merge"))
        self.assertTrue(json.loads(self.row()["data"])["needs_human"])
        message = self.report.call_args.args[0]
        self.assertIn("人工批准", message)
        self.assertIn("https://github.com/owner/repo/pull/1", message)
        self.workflow.github.approval_required = False
        self.assertTrue(self.store.retry_failed("111"))
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(self.workflow.github.merges, 1)

    async def test_behind_branch_is_updated_and_reviewed_again(self):
        self.workflow.github.behind = 1
        self.scheduler.session.side_effect = self.responses() + self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["phase"], "review")
        self.assertEqual(self.row()["status"], "queued")
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(self.workflow.github.merges, 1)
        data = json.loads(self.row()["data"])
        self.assertEqual((data["sha"], data["base_sha"], data["branch_updates"]), ("updated", "base2", 1))
        self.assertTrue(all(r["sha"] == "updated" for r in data["reviews"]))

    async def test_base_move_during_review_keeps_reviews(self):
        self.scheduler.session.side_effect = self.responses()
        original = self.scheduler.session.side_effect

        async def session(*args, **kwargs):
            if args[0] == "review-1":
                self.workflow.github.base_sha = "moved"
            return next(original)
        self.scheduler.session.side_effect = session
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual(json.loads(self.row()["data"])["base_sha"], "moved")

    async def test_unnecessary_pr_stops_on_first_review_and_reports_reason(self):
        self.scheduler.session.side_effect = self.responses()[:2] + [
            ({'approved': False, 'pr_needed': False, 'summary': '报错来自外部服务配置，代码无需修改'}, 'codex')]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual((self.row()['status'], self.row()['phase']), ('done', 'unnecessary'))
        self.assertEqual(self.workflow.github.publishes, 1)
        self.assertEqual(self.workflow.github.merges, 0)
        self.assertEqual(self.scheduler.session.await_count, 3)
        self.assertIn('PR 不需要', self.report.call_args.args[0])
        self.assertIn('外部服务配置', self.report.call_args.args[0])
        self.assertIn('/pull/1', self.report.call_args.args[0])
        self.assertIsNone(self.store.next_job())
        self.assertFalse(self.store.retry_failed('111'))

    async def test_one_review_can_stop_unnecessary_pr_after_another_approved(self):
        self.scheduler.session.side_effect = self.responses()[:3] + [
            ({'approved': False, 'pr_needed': False, 'summary': '已有重试逻辑覆盖此错误'}, 'claude')]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual((self.row()['status'], self.row()['phase']), ('done', 'unnecessary'))
        data = json.loads(self.row()['data'])
        self.assertEqual(len(data['review_history'][-1]['reviews']), 2)
        self.assertFalse(data['unnecessary_review']['result']['pr_needed'])
        self.assertEqual(self.workflow.github.merges, 0)

    async def test_second_review_can_stop_unnecessary_pr_after_first_requests_revision(self):
        self.scheduler.session.side_effect = self.responses(False)[:3] + [
            ({'approved': False, 'pr_needed': False, 'summary': '原基线已正确处理这个错误'}, 'claude')]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual((self.row()['status'], self.row()['phase']), ('done', 'unnecessary'))
        self.assertEqual(self.scheduler.session.await_count, 4)
        self.assertEqual(self.workflow.github.revisions, 0)
        self.assertEqual(self.workflow.github.merges, 0)
        self.assertIn('原基线已正确处理', self.report.call_args.args[0])

    async def test_missing_or_invalid_pr_necessity_does_not_merge_or_revise(self):
        for value in (None, 'false', 0):
            with self.subTest(value=value):
                self.store.save('111', status='queued', phase='triage', data={})
                response = {'approved': True, 'summary': 'invalid'}
                if value is not None:
                    response['pr_needed'] = value
                self.scheduler.session.side_effect = self.responses()[:2] + [(response, 'codex')]
                await self.workflow.execute(self.store.next_job())
                self.assertEqual(self.row()['status'], 'failed')
                self.assertIn('pr_needed', self.report.call_args.args[0])
                self.assertEqual(self.workflow.github.merges, 0)
                self.assertEqual(self.workflow.github.revisions, 0)
                self.workflow.github.local_sha = 'initial'

    async def test_saved_legacy_reviews_reassess_pr_necessity_before_merging(self):
        old_artifacts = self.root / 'jobs' / '111' / 'sessions' / 'review-fixed-1'
        old_artifacts.mkdir(parents=True)
        (old_artifacts / 'review-codex-1.jsonl').write_text('old approval without necessity assessment')
        for phase in ('review', 'merge'):
            with self.subTest(phase=phase):
                legacy = {'tool': 'codex', 'sha': 'fixed', 'result': {'approved': True, 'summary': '旧审查'}}
                self.store.save('111', status='queued', phase=phase,
                                data={'pr': 1, 'url': 'https://github.com/owner/repo/pull/1',
                                      'sha': 'fixed', 'base_sha': 'base', 'reviews': [legacy, legacy],
                                      'risks': [], 'repair_tool': 'claude'})
                self.scheduler.session.side_effect = [
                    ({'approved': False, 'pr_needed': False, 'summary': '原报错不需要代码修改'}, 'codex')]
                await self.workflow.execute(self.store.next_job())
                self.assertEqual((self.row()['status'], self.row()['phase']), ('done', 'unnecessary'))
                self.assertEqual(self.workflow.github.merges, 0)
                self.assertNotEqual(self.scheduler.session.call_args.args[3], old_artifacts)

    async def test_rejected_first_review_is_preserved_while_second_waits_for_quota(self):
        self.scheduler.session.side_effect = self.responses(False)[:3] + [NoCapacity('额度耗尽')]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual((self.row()['status'], self.row()['phase']), ('waiting', 'review'))
        self.assertFalse(json.loads(self.row()['data'])['reviews'][0]['result']['approved'])
        self.store.retry_failed('111')
        self.scheduler.session.side_effect = [self.responses()[3]]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual((self.row()['status'], self.row()['phase']), ('queued', 'revise'))
        self.assertEqual(self.scheduler.session.await_count, 5)

    async def test_review_rejection_fixes_same_pr_and_restarts_all_reviews(self):
        replies = self.responses()[:3] + [({'approved': False, 'pr_needed': True, 'summary': '缺少空值回归测试'}, 'codex')]
        self.scheduler.session.side_effect = replies
        await self.workflow.execute(self.store.next_job())
        self.assertEqual((self.row()['status'], self.row()['phase']), ('queued', 'revise'))
        rejected = json.loads(self.row()['data'])
        self.assertEqual(rejected['review_fix_rounds'], 1)
        self.assertFalse(rejected['review_history'][0]['reviews'][-1]['result']['approved'])
        self.scheduler.session.side_effect = [self.responses()[1]] + self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.workflow.github.publishes, 1)
        self.assertEqual(self.workflow.github.revisions, 1)
        self.assertEqual(self.workflow.github.tests_run, 2)
        self.assertEqual(self.workflow.github.merges, 1)
        calls = self.scheduler.session.call_args_list
        repair = calls[4]
        self.assertEqual(repair.args[0], 'repair')
        self.assertIn('缺少空值回归测试', repair.args[1])
        self.assertEqual(repair.args[3], calls[1].args[3])
        self.assertEqual(repair.kwargs, {'prefer': 'claude', 'resume': True, 'quiet': True})
        self.assertNotEqual(calls[2].args[3], calls[5].args[3])
        data = json.loads(self.row()['data'])
        self.assertTrue(all(r['sha'] == 'revised-1' for r in data['reviews']))

    async def test_rejection_checkpoint_atomically_preserves_revise_phase_and_round_count(self):
        self.scheduler.session.side_effect = self.responses(False)
        save = self.store.save
        interrupted = False

        def interrupted_save(job_id, **values):
            nonlocal interrupted
            save(job_id, **values)
            if values.get('data', {}).get('review_history') and not interrupted:
                interrupted = True
                raise asyncio.CancelledError()

        self.store.save = interrupted_save
        with self.assertRaises(asyncio.CancelledError):
            await self.workflow.execute(self.store.next_job())
        self.store.save = save
        self.assertEqual((self.row()['status'], self.row()['phase']), ('queued', 'revise'))
        self.assertEqual(json.loads(self.row()['data'])['review_fix_rounds'], 1)
        self.scheduler.session.side_effect = [self.responses()[1]] + self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.workflow.github.revisions, 1)

    async def test_review_revisions_continue_beyond_old_limit_until_all_approve(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        for index in range(5):
            self.scheduler.session.side_effect = [self.responses()[1]] + self.responses(False)[2:]
            await self.workflow.execute(self.store.next_job())
            self.assertEqual((self.row()['status'], self.row()['phase']), ('queued', 'revise'))
            self.assertEqual(json.loads(self.row()['data'])['review_fix_rounds'], index + 2)
            # Reopening the coordinator's database must not impose a fresh round limit.
            self.store.db.close()
            self.store = Store(self.root)
            self.workflow.store = self.store
        self.scheduler.session.side_effect = [self.responses()[1]] + self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.workflow.github.revisions, 6)
        self.assertEqual(self.workflow.github.publishes, 1)
        self.assertEqual(self.workflow.github.merges, 1)

    async def test_revision_progress_is_silent_until_final_result(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        self.report.reset_mock()
        self.scheduler.session.side_effect = [self.responses()[1]] + self.responses(False)[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'queued')
        self.report.assert_not_awaited()
        for call in self.scheduler.session.call_args_list[2:]:
            self.assertTrue(call.kwargs['quiet'])
        self.scheduler.session.side_effect = [self.responses()[1]] + self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'done')
        self.report.assert_awaited_once()
        self.assertIn('已合并', self.report.call_args.args[0])

    async def test_revision_quota_wait_is_silent(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        self.report.reset_mock()
        self.scheduler.session.side_effect = NoCapacity('额度耗尽')
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'waiting')
        self.report.assert_not_awaited()

    async def test_unnecessary_decision_survives_interruption_without_another_review(self):
        self.scheduler.session.side_effect = self.responses()[:2] + [
            ({'approved': False, 'pr_needed': False, 'summary': '无需修改'}, 'codex')]
        def interrupt_report(message):
            if 'PR 不需要' in message:
                raise asyncio.CancelledError()

        self.report.side_effect = interrupt_report
        with self.assertRaises(asyncio.CancelledError):
            await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['phase'], 'unnecessary')
        self.report.side_effect = None
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.scheduler.session.await_count, 3)
        self.assertEqual(self.workflow.github.merges, 0)

    async def test_unnecessary_final_report_is_retried_after_hard_crash(self):
        class SimulatedCrash(BaseException):
            pass

        self.scheduler.session.side_effect = self.responses()[:2] + [
            ({'approved': False, 'pr_needed': False, 'summary': '无需修改'}, 'codex')]

        def crash_before_notice(message):
            if 'PR 不需要' in message:
                raise SimulatedCrash()

        self.report.side_effect = crash_before_notice
        with self.assertRaises(SimulatedCrash):
            await self.workflow.execute(self.store.next_job())
        self.store.recover()
        resumed = self.store.next_job()
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed['phase'], 'unnecessary')
        self.report.side_effect = None
        self.report.reset_mock()
        await self.workflow.execute(resumed)
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.scheduler.session.await_count, 3)
        self.report.assert_awaited_once()
        self.assertIn('PR 不需要', self.report.call_args.args[0])

    async def test_revision_quota_wait_retains_feedback_and_round_count(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        self.scheduler.session.side_effect = NoCapacity('额度耗尽')
        await self.workflow.execute(self.store.next_job())
        self.assertEqual((self.row()['status'], self.row()['phase']), ('waiting', 'revise'))
        self.store.retry_failed('111')
        self.scheduler.session.side_effect = [self.responses()[1]] + self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(json.loads(self.row()['data'])['review_fix_rounds'], 1)

    async def test_revision_publish_restart_does_not_repeat_ai_or_tests(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        original_update = self.workflow.github.update_pr
        self.workflow.github.update_pr = AsyncMock(side_effect=RuntimeError('network failed'))
        self.scheduler.session.side_effect = [self.responses()[1]]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual((self.row()['status'], self.row()['phase']), ('failed', 'revise_publish'))
        self.workflow.github.update_pr = original_update
        self.store.retry_failed('111')
        self.scheduler.session.side_effect = self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.workflow.github.tests_run, 2)
        self.assertEqual(self.workflow.github.publishes, 1)

    async def test_external_head_change_stops_revision_before_ai(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        self.workflow.github.head_sha = 'human-commit'
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'failed')
        self.assertEqual(self.scheduler.session.await_count, 4)
        self.assertEqual(self.workflow.github.revisions, 0)

    async def test_revision_test_failure_does_not_push_and_retry_runs_tests_again(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        self.scheduler.session.side_effect = [self.responses()[1]]
        original_tests = self.workflow.github.tests
        self.workflow.github.tests = AsyncMock(side_effect=RuntimeError('tests failed'))
        await self.workflow.execute(self.store.next_job())
        self.assertEqual((self.row()['status'], self.row()['phase']), ('failed', 'revise_publish'))
        self.assertEqual(self.workflow.github.revisions, 0)
        self.workflow.github.tests = original_tests
        self.store.retry_failed('111')
        self.scheduler.session.side_effect = self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.workflow.github.tests_run, 2)

    async def test_revision_without_changes_stops_instead_of_reviewing_old_commit(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        self.scheduler.session.side_effect = [self.responses()[1]]
        self.workflow.github.commit = AsyncMock()
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'failed')
        self.assertIn('未产生新的提交', self.report.call_args.args[0])
        self.assertEqual(self.workflow.github.revisions, 0)
        self.assertEqual(self.workflow.github.merges, 0)

    async def test_revision_recomputes_protected_paths(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        self.workflow.github.risks = ['修改受保护路径 CODEOWNERS']
        self.scheduler.session.side_effect = [self.responses()[1]] + self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.workflow.github.revisions, 1)
        self.assertEqual(self.workflow.github.merges, 0)
        self.assertIn('CODEOWNERS', self.report.call_args.args[0])

    async def test_push_completed_before_crash_resumes_without_ai_or_tests(self):
        self.scheduler.session.side_effect = self.responses(False)
        await self.workflow.execute(self.store.next_job())
        original_update = self.workflow.github.update_pr

        async def interrupted_push(*args):
            await original_update(*args)
            raise asyncio.CancelledError()

        self.workflow.github.update_pr = interrupted_push
        self.scheduler.session.side_effect = [self.responses()[1]]
        with self.assertRaises(asyncio.CancelledError):
            await self.workflow.execute(self.store.next_job())
        self.workflow.github.update_pr = original_update
        self.scheduler.session.side_effect = self.responses()[2:]
        await self.workflow.execute(self.store.next_job())
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.scheduler.session.await_count, 7)
        self.assertEqual(self.workflow.github.tests_run, 2)

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
    async def test_review_revision_pipeline_with_real_git_updates_same_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_root = root / 'jobs' / '111'
            repo, remote = job_root / 'repo', root / 'remote.git'
            repo.mkdir(parents=True)
            await checked(['git', 'init', '--bare', str(remote)])
            await checked(['git', 'init', '-b', 'main'], cwd=repo)
            await checked(['git', 'config', 'user.name', 'Test'], cwd=repo)
            await checked(['git', 'config', 'user.email', 'test@example.com'], cwd=repo)
            await checked(['git', 'remote', 'add', 'origin', str(remote)], cwd=repo)
            c = config(root, test_commands=[[sys.executable, '-c',
                                             "from pathlib import Path; assert Path('app.py').read_text() == 'final fix'"]])
            store = Store(root)
            try:
                github = GitHub(c)
                await github.trust(repo)
                (repo / 'app.py').write_text('base')
                await github.commit(repo, 'base')
                base = await github.head(repo)
                await github.git(repo, 'push', 'origin', 'HEAD:refs/heads/main')
                await github.git(repo, 'checkout', '-b', 'repairbot/discord-111')
                (repo / 'app.py').write_text('first fix')
                await github.commit(repo, 'first fix')
                first = await github.head(repo)
                await github.git(repo, 'push', 'origin', 'HEAD:refs/heads/repairbot/discord-111')

                async def pr(number):
                    head = (await checked(['git', '--git-dir', str(remote), 'rev-parse',
                                           'refs/heads/repairbot/discord-111'])).strip()
                    return {'number': 1, 'url': 'https://github.com/owner/repo/pull/1',
                            'state': 'OPEN', 'headRefOid': head, 'baseRefOid': base}

                github.pr = pr
                github.prepare = AsyncMock(return_value=(repo, 'main'))
                github.assert_origin = AsyncMock()
                github.merge = AsyncMock()
                scheduler = AsyncMock()

                async def session(phase, prompt, cwd, artifacts, **kwargs):
                    if phase == 'repair':
                        self.assertIn('missing null guard', prompt)
                        self.assertTrue(kwargs['resume'])
                        (cwd / 'app.py').write_text('final fix')
                        return {'fixed': True, 'summary': 'guard added'}, 'codex'
                    diff = (cwd / '.git' / 'repairbot-review.patch').read_text()
                    approved = 'final fix' in diff
                    return {'approved': approved, 'pr_needed': True, 'summary': 'ok' if approved else 'missing null guard'}, 'claude'

                scheduler.session.side_effect = session
                store.enqueue('111', 'TypeError: null')
                store.save('111', phase='review', data={'pr': 1, 'url': (await pr(1))['url'],
                           'sha': first, 'base_sha': base, 'reviews': [], 'repair_tool': 'codex',
                           'repair': {'fixed': True, 'summary': 'first fix'}})
                workflow = Workflow(c, store, scheduler, AsyncMock())
                workflow.github = github
                await workflow.execute(store.next_job())
                self.assertEqual(store.next_job()['phase'], 'revise')
                store.recover()
                await workflow.execute(store.next_job())
                row = store.db.execute("SELECT status,data FROM jobs WHERE id='111'").fetchone()
                self.assertEqual(row['status'], 'done')
                data = json.loads(row['data'])
                self.assertNotEqual(data['sha'], first)
                self.assertEqual((await pr(1))['headRefOid'], data['sha'])
                self.assertEqual(len(data['reviews']), 2)
                self.assertTrue(await github.clean(repo))
                github.merge.assert_awaited_once()
                self.assertEqual(scheduler.session.await_count, 5)
            finally:
                store.db.close()

    async def test_real_git_revision_push_preserves_concurrent_remote_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, repo = root / 'remote.git', root / 'repo'
            branch = 'repairbot/discord-111'
            await checked(['git', 'init', '--bare', str(remote)])
            repo.mkdir()
            await checked(['git', 'init', '-b', branch], cwd=repo)
            await checked(['git', 'config', 'user.name', 'Test'], cwd=repo)
            await checked(['git', 'config', 'user.email', 'test@example.com'], cwd=repo)
            await checked(['git', 'remote', 'add', 'origin', str(remote)], cwd=repo)
            github = GitHub(config(root))
            github.assert_origin = AsyncMock()  # This test uses a local bare remote.
            (repo / 'app.py').write_text('initial')
            await github.commit(repo, 'initial')
            old_sha = await github.head(repo)
            await github.git(repo, 'push', 'origin', f'HEAD:refs/heads/{branch}')
            (repo / 'app.py').write_text('review fix')
            await github.commit(repo, 'revision')
            new_sha = await github.head(repo)

            async def remote_head():
                return (await checked(['git', '--git-dir', str(remote), 'rev-parse', f'refs/heads/{branch}'])).strip()

            async def pr(number):
                return {'state': 'OPEN', 'headRefOid': await remote_head(), 'baseRefOid': old_sha}

            github.pr = pr
            result = await github.update_pr(repo, branch, 1, old_sha, new_sha)
            self.assertEqual(result['headRefOid'], new_sha)
            await github.update_pr(repo, branch, 1, old_sha, new_sha)
            # Create an alternative remote commit and emulate a write after the GitHub check.
            human_sha = (await checked(['git', 'commit-tree', f'{old_sha}^{{tree}}', '-p', old_sha,
                                        '-m', 'human change'], cwd=repo)).strip()
            await github.git(repo, 'push', 'origin', f'{human_sha}:refs/heads/human')
            await checked(['git', '--git-dir', str(remote), 'update-ref', f'refs/heads/{branch}', old_sha])

            async def racing_pr(number):
                snapshot = await pr(number)
                await checked(['git', '--git-dir', str(remote), 'update-ref', f'refs/heads/{branch}', human_sha])
                return snapshot

            github.pr = racing_pr
            with self.assertRaises(RuntimeError):
                await github.update_pr(repo, branch, 1, old_sha, new_sha)
            self.assertEqual(await remote_head(), human_sha)

    async def test_large_stdin_with_large_output_does_not_deadlock(self):
        script = "import sys; sys.stdout.write('x' * 300000); sys.stdout.flush(); print(len(sys.stdin.read()))"
        result = await run([sys.executable, "-c", script], stdin="y" * 300000, timeout=20)
        self.assertEqual(result.code, 0)
        self.assertTrue(result.output.rstrip().endswith("300000"))

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

    async def test_planted_git_config_and_hooks_do_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            await checked(["git", "init", "-b", "main"], cwd=repo)
            await checked(["git", "config", "user.name", "Test"], cwd=repo)
            await checked(["git", "config", "user.email", "test@example.com"], cwd=repo)
            github = GitHub(config(root))
            await github.trust(repo)
            marker = root / "pwned"
            hook = repo / ".git" / "hooks" / "pre-commit"
            hook.write_text(f"#!/bin/sh\necho \"$DISCORD_TOKEN\" > {marker}\n")
            hook.chmod(0o700)
            with (repo / ".git" / "config").open("a") as handle:
                handle.write(f"[core]\n\tfsmonitor = \"echo $DISCORD_TOKEN > {marker}; false\"\n")
            (repo / "app.py").write_text("fix")
            with patch.dict(os.environ, {"DISCORD_TOKEN": "secret"}):
                self.assertFalse(await github.clean(repo))
                await github.commit(repo, "fix")
            self.assertFalse(marker.exists())
            self.assertNotIn("fsmonitor", (repo / ".git" / "config").read_text())
            self.assertTrue(await github.clean(repo))

    async def test_child_processes_do_not_receive_discord_token(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN": "secret"}):
            result = await run([sys.executable, "-c", "import os; print(os.environ.get('DISCORD_TOKEN'))"])
        self.assertEqual(result.output.strip(), "None")

    async def test_discard_removes_test_artifacts_but_keeps_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            await checked(["git", "init", "-b", "main"], cwd=repo)
            await checked(["git", "config", "user.name", "Test"], cwd=repo)
            await checked(["git", "config", "user.email", "test@example.com"], cwd=repo)
            github = GitHub(config(root))
            (repo / "app.py").write_text("fix")
            (repo / "pkg" / "__pycache__").mkdir(parents=True)
            (repo / "pkg" / "__pycache__" / "app.cpython-311.pyc").write_text("cache")
            (repo / ".pytest_cache").mkdir()
            (repo / ".pytest_cache" / "README.md").write_text("cache")
            await github.commit(repo, "fix")
            committed = await github.git(repo, "show", "--name-only", "--format=", "HEAD")
            self.assertEqual(committed.split(), ["app.py"])
            await github.commit(repo, "nothing staged")
            (repo / ".coverage").write_text("junk")
            (repo / "app.py").write_text("modified by test")
            await github.discard(repo)
            self.assertFalse((repo / ".coverage").exists())
            self.assertFalse((repo / ".pytest_cache").exists())
            self.assertEqual((repo / "app.py").read_text(), "fix")


if __name__ == "__main__":
    unittest.main()
