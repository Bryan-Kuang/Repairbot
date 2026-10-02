from __future__ import annotations

import asyncio
import logging
import os
import re

from .config import Config
from .process import redact
from .providers import Scheduler, probe
from .store import Store
from .workflow import Workflow

log = logging.getLogger(__name__)


def message_content(message) -> str:
    parts = [message.content or ""]
    for embed in message.embeds:
        parts += [embed.title or "", embed.description or ""]
        for field in embed.fields:
            parts += [field.name, field.value]
    return "\n".join(p for p in parts if p)


def enqueue_message(config: Config, store: Store, message, bot_id: int) -> bool:
    if message.guild is None or message.guild.id != config.guild_id or message.channel.id != config.listen_channel_id:
        return False
    if message.author.id == bot_id:
        return False
    if config.allowed_author_ids and message.author.id not in config.allowed_author_ids:
        return False
    text = message_content(message)
    if not re.search(config.error_pattern, text):
        return False
    return store.enqueue(str(message.id), redact(text[:config.max_message_chars]))


async def serve(config: Config, store: Store) -> None:
    try:
        import discord
    except ImportError as exc:
        raise RuntimeError("请先执行 pip install -e . 安装 discord.py") from exc
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("缺少 DISCORD_TOKEN 环境变量")
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    channel = None
    listen = None
    worker = None
    fatal_error = None
    closing = None
    ingest_lock = asyncio.Lock()

    async def report(text: str) -> None:
        safe = redact(text)
        if channel is None:
            log.warning("Report channel unavailable: %s", safe)
            return
        # Do not let a transient Discord failure invalidate a completed merge.
        for offset in range(0, len(safe), 1900):
            chunk = safe[offset:offset + 1900]
            for attempt in range(3):
                try:
                    await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                    break
                except discord.HTTPException:
                    if attempt == 2:
                        log.error("Failed to send report (retained in local log): %s", chunk)
                    else:
                        await asyncio.sleep(2 ** attempt)

    scheduler = Scheduler(config, store, report)
    workflow = Workflow(config, store, scheduler, report)

    async def consume() -> None:
        store.recover()
        while True:
            job = store.next_job()
            if job:
                await workflow.execute(job)
            else:
                await asyncio.sleep(2)

    def worker_stopped(task: asyncio.Task) -> None:
        nonlocal fatal_error, closing
        if task.cancelled() or task.exception() is None:
            return
        # Exit instead of staying connected while no job is processed; a supervisor can restart the service.
        fatal_error = RuntimeError(f"任务 worker 意外退出：{task.exception()}")
        log.error("Job worker crashed", exc_info=task.exception())
        closing = asyncio.create_task(client.close())

    async def ingest(message) -> None:
        enqueue_message(config, store, message, client.user.id)

    async def catch_up() -> None:
        cursor = store.cursor(config.listen_channel_id)
        if cursor:
            after = discord.Object(id=int(cursor))
            async for message in listen.history(limit=None, after=after, oldest_first=True):
                await ingest(message)
                store.set_cursor(config.listen_channel_id, message.id)
        elif config.startup_scan_messages:
            recent = [m async for m in listen.history(limit=config.startup_scan_messages)]
            for message in reversed(recent):
                await ingest(message)
                store.set_cursor(config.listen_channel_id, message.id)
        if store.cursor(config.listen_channel_id) is None:
            # No replay requested / empty channel: establish a cursor for subsequent reconnects.
            store.set_cursor(config.listen_channel_id, discord.utils.time_snowflake(discord.utils.utcnow()))

    @client.event
    async def on_ready():
        nonlocal channel, listen, worker, fatal_error
        try:
            channel = await client.fetch_channel(config.report_channel_id)
            listen = await client.fetch_channel(config.listen_channel_id)
            for candidate in (channel, listen):
                if not isinstance(candidate, discord.TextChannel) or candidate.guild.id != config.guild_id:
                    raise RuntimeError("频道必须为指定服务器内的文字频道")
                perms = candidate.permissions_for(candidate.guild.me)
                if not perms.view_channel:
                    raise RuntimeError("Bot 缺少查看频道权限")
            if not channel.permissions_for(channel.guild.me).send_messages:
                raise RuntimeError("Bot 缺少报告频道发送消息权限")
            if not listen.permissions_for(listen.guild.me).read_message_history:
                raise RuntimeError("Bot 缺少监听频道读取消息历史权限")
            async with ingest_lock:
                await catch_up()
            if worker is None:
                await asyncio.gather(*(probe(p) for p in scheduler.providers))
                state = "；".join(f"{p.name}: {'未安装' if not p.executable else p.auth}" for p in scheduler.providers)
                await report("自动修复服务已启动。仓库：" + config.repository + "。工具：" + state)
                worker = asyncio.create_task(consume())
                worker.add_done_callback(worker_stopped)
        except Exception as exc:
            fatal_error = RuntimeError(f"Discord 初始化失败：{exc}")
            log.exception("Discord initialization failed")
            await client.close()

    @client.event
    async def on_message(message):
        if listen is None or message.channel.id != config.listen_channel_id:
            return
        async with ingest_lock:
            # Replay is idempotent; never advance the replay watermark over an unprocessed gap.
            await catch_up()
            await ingest(message)

    try:
        await client.start(token)
        if fatal_error:
            raise fatal_error
    finally:
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if not client.is_closed():
            await client.close()
