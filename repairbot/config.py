from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProviderConfig:
    executable: str | None = None
    quota_command: list[str] = field(default_factory=list)
    model: str | None = None
    native_quota: bool = True


@dataclass
class Config:
    guild_id: int
    listen_channel_id: int
    report_channel_id: int
    repository: str
    state_dir: Path
    reviewers: int = 2
    auto_merge: bool = True
    require_ci: bool = True
    test_commands: list[list[str]] = field(default_factory=list)
    allowed_author_ids: list[int] = field(default_factory=list)
    error_pattern: str = r"(?i)(error|exception|traceback|fatal|panic|报错|错误)"
    session_timeout_seconds: int = 1800
    cooldown_seconds: int = 3600
    max_message_chars: int = 24000
    startup_scan_messages: int = 50
    providers: dict[str, ProviderConfig] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> Config:
        source = Path(path).resolve()
        data = json.loads(source.read_text(encoding="utf-8"))
        data["providers"] = {k: ProviderConfig(**v) for k, v in data.get("providers", {}).items()}
        data["state_dir"] = (source.parent / data.get("state_dir", ".repairbot")).resolve()
        for key in ("guild_id", "listen_channel_id", "report_channel_id"):
            data[key] = int(data[key])
        config = cls(**data)
        config.validate()
        return config

    def validate(self) -> None:
        self.repository = normalize_repository(self.repository)
        if self.listen_channel_id == self.report_channel_id:
            raise ValueError("监听频道和报告频道必须不同")
        if self.reviewers not in (1, 2):
            raise ValueError("reviewers 必须为 1 或 2")
        if any(i <= 0 for i in (self.guild_id, self.listen_channel_id, self.report_channel_id)):
            raise ValueError("Discord ID 必须为正整数")
        if set(self.providers) - {"codex", "claude"}:
            raise ValueError("仅支持 codex 和 claude")
        if min(self.session_timeout_seconds, self.cooldown_seconds, self.max_message_chars) <= 0:
            raise ValueError("超时、冷却时间和消息长度必须为正数")
        if not 0 <= self.startup_scan_messages <= 1000:
            raise ValueError("startup_scan_messages 必须为 0..1000")
        re.compile(self.error_pattern)
        for argv in self.test_commands + [p.quota_command for p in self.providers.values() if p.quota_command]:
            if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
                raise ValueError("命令必须为非空字符串数组，不接受 shell 字符串")


def normalize_repository(value: str) -> str:
    value = value.removeprefix("https://github.com/").removesuffix("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("repository 必须为 owner/repo 或 https://github.com/owner/repo")
    if any(part in (".", "..") for part in value.split("/")):
        raise ValueError("无效的 repository")
    return value
