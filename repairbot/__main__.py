from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .process import run
from .providers import Scheduler, probe
from .store import Store


@contextmanager
def single_instance(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (root / "service.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock.close()
        raise RuntimeError("同一 state_dir 已有实例在运行") from exc
    try:
        yield
    finally:
        lock.close()


async def doctor(config: Config, store: Store) -> dict:
    async def silent(_):
        pass
    scheduler = Scheduler(config, store, silent)
    await asyncio.gather(*(probe(p) for p in scheduler.providers))
    # Diagnostics only: do not change provider cooldowns used by the running service.
    await scheduler.refresh(persist=False)
    github_logged_in = False
    if shutil.which("gh"):
        try:
            github_logged_in = (await run(["gh", "auth", "status"], timeout=20)).code == 0
        except (OSError, TimeoutError):
            pass
    return {
        "git_installed": bool(shutil.which("git")),
        "gh_installed": bool(shutil.which("gh")),
        "github_logged_in": github_logged_in,
        "discord_token_present": bool(os.environ.get("DISCORD_TOKEN")),
        "providers": [{"name": p.name, "path": p.executable, "auth": p.auth,
                       "remaining_fraction": p.quota.remaining_fraction,
                       "quota_query_error": p.quota_error,
                       "blocked_until": store.blocked_until(p.name)} for p in scheduler.providers],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord 自动修复服务")
    parser.add_argument("command", choices=["run", "doctor", "jobs", "retry"])
    parser.add_argument("job_id", nargs="?")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        store = Store(config.state_dir)
        store.bind(config.repository, config.guild_id, config.listen_channel_id, config.report_channel_id)
        if args.command == "doctor":
            print(json.dumps(asyncio.run(doctor(config, store)), ensure_ascii=False, indent=2))
        elif args.command == "jobs":
            rows = store.db.execute("SELECT id,status,phase,updated,retry_at FROM jobs ORDER BY updated DESC LIMIT 50").fetchall()
            print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
        elif args.command == "retry":
            if not args.job_id or not args.job_id.isdecimal():
                parser.error("retry 需要 Discord 消息 ID")
            if not store.retry_failed(args.job_id):
                raise RuntimeError("任务不存在，或不处于 failed/waiting 状态")
            print("任务已重新排队")
        else:
            if not shutil.which("git") or not shutil.which("gh"):
                raise RuntimeError("请先安装 git 和 GitHub CLI (gh)")
            from .service import serve
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                                handlers=[logging.StreamHandler(), logging.FileHandler(config.state_dir / "service.log", encoding="utf-8")])
            with single_instance(config.state_dir):
                asyncio.run(serve(config, store))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, RuntimeError, TypeError) as exc:
        parser.exit(1, f"错误：{exc}\n")


if __name__ == "__main__":
    main()
