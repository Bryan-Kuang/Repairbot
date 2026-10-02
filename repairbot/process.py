from __future__ import annotations

import asyncio
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path


def redact(text: str) -> str:
    for key in ("DISCORD_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.environ.get(key)
        if value:
            text = text.replace(value, "[REDACTED]")
    return re.sub(r"(?i)(authorization\s*:\s*(?:bearer|bot)\s+)\S+", r"\1[REDACTED]", text)


def coordinator_environment() -> dict[str, str]:
    # The Discord token is only used in-process; no child process (git, gh, tests, hooks) needs it.
    env = os.environ.copy()
    env.pop("DISCORD_TOKEN", None)
    return env


def agent_environment(state_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("DISCORD_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        env.pop(key, None)
    # Keep local model login, but do not hand the coordinator's gh credentials to agents.
    gh_config = state_dir / "agent-gh-config"
    gh_config.mkdir(parents=True, exist_ok=True)
    env["GH_CONFIG_DIR"] = str(gh_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


@dataclass
class Result:
    code: int
    output: str


async def run(argv: list[str], *, cwd: Path | None = None,
              env: dict[str, str] | None = None, stdin: str | None = None,
              timeout: int = 120, transcript: Path | None = None) -> Result:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env if env is not None else coordinator_environment(), stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=os.name != "nt",
    )
    output = bytearray()
    log = transcript.open("ab") if transcript else None

    async def feed() -> None:
        if stdin is None:
            return
        try:
            proc.stdin.write(stdin.encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    async def drain_output() -> None:
        while chunk := await proc.stdout.read(8192):
            if log:
                log.write(chunk)
                log.flush()
            output.extend(chunk)
            if len(output) > 2_000_000:
                del output[:-2_000_000]

    async def collect() -> None:
        # Feed stdin while reading stdout so a large prompt cannot deadlock against a full output pipe.
        await asyncio.gather(feed(), drain_output())
        await proc.wait()

    try:
        await asyncio.wait_for(collect(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await proc.wait()
        if log:
            log.write(b"\n[coordinator: interrupted]\n")
        raise
    finally:
        if log:
            log.close()
    return Result(proc.returncode, output.decode("utf-8", errors="replace"))


async def checked(argv: list[str], **kwargs) -> str:
    result = await run(argv, **kwargs)
    if result.code:
        raise RuntimeError(redact(f"{argv[0]} exited {result.code}: {result.output[-4000:]}"))
    return result.output
