"""Read usage through the installed CLI's app-server, without reading auth files."""
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
from pathlib import Path


async def read_usage(executable: str, cwd: Path, env: dict[str, str]) -> dict:
    proc = await asyncio.create_subprocess_exec(
        executable, "app-server", cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=2_000_000,
        start_new_session=os.name != "nt",
    )

    async def send(payload: dict) -> None:
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        await proc.stdin.drain()

    async def request(request_id: int, method: str, params) -> dict:
        await send({"id": request_id, "method": method, "params": params})
        while raw := await proc.stdout.readline():
            obj = json.loads(raw)
            if obj.get("id") == request_id:
                if "error" in obj:
                    # Return only error code; the server's message could contain credential details.
                    raise ValueError(f"Codex usage RPC failed: {obj['error'].get('code', 'unknown')}")
                return obj["result"]
            if "id" in obj and "method" in obj:
                await send({"id": obj["id"], "error": {"code": -32601, "message": "Unsupported method"}})
        raise ValueError("Codex app-server exited without usage response")

    async def query() -> dict:
        await request(1, "initialize", {"clientInfo": {"name": "discord_repairbot", "version": "0.1.0"}})
        await send({"method": "initialized", "params": {}})
        return await request(2, "account/rateLimits/read", None)

    try:
        return await asyncio.wait_for(query(), timeout=15)
    finally:
        if proc.returncode is None:
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await proc.wait()


def normalize_usage(payload: dict) -> dict:
    buckets = payload.get("rateLimitsByLimitId") or {}
    snapshot = buckets.get("codex") or payload.get("rateLimits") or {}
    windows = []
    for name in ("primary", "secondary"):
        window = snapshot.get(name)
        if window:
            percent = window.get("usedPercent")
            if isinstance(percent, bool) or not isinstance(percent, (float, int)) or not math.isfinite(percent):
                raise ValueError("Invalid Codex usedPercent")
            windows.append((max(0, min(1, 1 - percent / 100)), window.get("resetsAt")))
    individual = snapshot.get("individualLimit")
    if snapshot.get("spendControlReached") is True:
        return {"remaining_fraction": 0, "reset_at": individual.get("resetsAt") if individual else None}
    if individual:
        windows.append((individual["remainingPercent"] / 100, individual.get("resetsAt")))
    if not windows:
        return {"remaining_fraction": None}
    windows.sort(key=lambda w: w[0])
    fraction, reset = windows[0]
    credits = snapshot.get("credits") or {}
    if fraction == 0 and (credits.get("unlimited") or credits.get("hasCredits")):
        # Absolute credit balance has no comparable denominator. Do not incorrectly block it.
        return {"remaining_fraction": None}
    if payload.get("ordinaryUsageAllowed") is False and not credits.get("hasCredits") and not credits.get("unlimited"):
        fraction = 0
    if fraction == 0:
        resets = [at for remaining, at in windows if remaining == 0 and at is not None]
        reset = max(resets) if resets else reset
    return {"remaining_fraction": fraction, "reset_at": reset}
