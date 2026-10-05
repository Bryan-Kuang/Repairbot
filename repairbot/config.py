from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
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
    dedup_window_seconds: int = 86400
    max_jobs_per_hour: int = 10
    # Changes here (or any file deletion) keep the PR for a human instead of auto-merging.
    protected_paths: list[str] = field(default_factory=lambda: [
        ".github/*", "CODEOWNERS", "*/CODEOWNERS", ".gitmodules", ".gitattributes", "*/.gitattributes"])
    providers: dict[str, ProviderConfig] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> Config:
        source = Path(path).resolve()
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置文件必须为 JSON 对象")
        # Older deployments may still have a round cap; revisions are now unlimited.
        data.pop("max_review_fix_rounds", None)
        unknown_keys(data, cls, "")
        providers = data.get("providers", {})
        if not isinstance(providers, dict) or not all(isinstance(v, dict) for v in providers.values()):
            raise ValueError("providers 必须为对象")
        for name, value in providers.items():
            unknown_keys(value, ProviderConfig, f"providers.{name}.")
        data["providers"] = {k: ProviderConfig(**v) for k, v in providers.items()}
        for key in ("guild_id", "listen_channel_id", "report_channel_id", "repository"):
            if key not in data:
                raise ValueError(f"缺少配置项 {key}")
        data["state_dir"] = (source.parent / data.get("state_dir", ".repairbot")).resolve()
        for key in ("guild_id", "listen_channel_id", "report_channel_id"):
            data[key] = discord_id(data[key], key)
        config = cls(**data)
        config.validate()
        return config

    def validate(self) -> None:
        if not isinstance(self.repository, str):
            raise ValueError("repository 必须为字符串")
        self.repository = normalize_repository(self.repository)
        for key in ("auto_merge", "require_ci"):
            if not isinstance(getattr(self, key), bool):
                raise ValueError(f"{key} 必须为 true 或 false（不能是字符串）")
        for key in ("reviewers", "session_timeout_seconds", "cooldown_seconds", "max_message_chars", "startup_scan_messages",
                    "dedup_window_seconds", "max_jobs_per_hour"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} 必须为整数")
        for name, provider in self.providers.items():
            if not isinstance(provider.native_quota, bool):
                raise ValueError(f"providers.{name}.native_quota 必须为 true 或 false")
        if not isinstance(self.allowed_author_ids, list):
            raise ValueError("allowed_author_ids 必须为数组")
        # Discord IDs are often copied as strings; compare them as integers.
        self.allowed_author_ids = [discord_id(v, "allowed_author_ids") for v in self.allowed_author_ids]
        if not self.allowed_author_ids:
            # Incident text reaches AI sessions that edit code and run tests on this machine.
            raise ValueError("必须配置 allowed_author_ids，避免任何频道成员都能让 AI 会话在本机执行代码")
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
        if min(self.dedup_window_seconds, self.max_jobs_per_hour) < 0:
            raise ValueError("dedup_window_seconds 和 max_jobs_per_hour 不能为负数（0 表示关闭）")
        if not 0 <= self.startup_scan_messages <= 1000:
            raise ValueError("startup_scan_messages 必须为 0..1000")
        if not isinstance(self.error_pattern, str):
            raise ValueError("error_pattern 必须为字符串")
        try:
            re.compile(self.error_pattern)
        except re.error as exc:
            raise ValueError(f"error_pattern 不是有效正则：{exc}") from None
        if not isinstance(self.protected_paths, list) or not all(isinstance(v, str) and v for v in self.protected_paths):
            raise ValueError("protected_paths 必须为非空字符串数组")
        if not isinstance(self.test_commands, list):
            raise ValueError("test_commands 必须为数组")
        for name, provider in self.providers.items():
            if not isinstance(provider.quota_command, list):
                raise ValueError(f"providers.{name}.quota_command 必须为数组")
        for argv in self.test_commands + [p.quota_command for p in self.providers.values() if p.quota_command]:
            if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
                raise ValueError("命令必须为非空字符串数组，不接受 shell 字符串")


def unknown_keys(data: dict, cls, prefix: str) -> None:
    extra = set(data) - {f.name for f in fields(cls)}
    if extra:
        raise ValueError("未知配置项：" + "、".join(prefix + k for k in sorted(extra)))


def discord_id(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdecimal() or int(value) <= 0:
        raise ValueError(f"{name} 中的 Discord ID 必须为正整数或数字字符串")
    return int(value)


def normalize_repository(value: str) -> str:
    value = value.removeprefix("https://github.com/").removesuffix("/").removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("repository 必须为 owner/repo 或 https://github.com/owner/repo")
    if any(part in (".", "..") for part in value.split("/")):
        raise ValueError("无效的 repository")
    return value
