"""Crash-safe retained-topic journal and a process ownership lock."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import GatewayConfig


@contextmanager
def instance_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "gateway.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another gateway owns this state directory") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class TopicJournal:
    def __init__(self, config: GatewayConfig):
        self.path = Path(config.state_dir) / "topics.json"
        self.identity = {
            "gateway": config.id,
            "topic_prefix": config.topic_prefix,
            "discovery_prefix": config.discovery_prefix,
        }

    def load(self, allowed: Callable[[str], bool]) -> set[str]:
        if not self.path.exists():
            return set()
        if self.path.stat().st_size > 4_000_000:
            raise ValueError("topic journal exceeds size limit")
        document = json.loads(self.path.read_text())
        if document.get("identity") != self.identity or document.get("version") != 1:
            raise ValueError(
                "topic journal identity mismatch; retire old discovery before renaming"
            )
        topics = document.get("topics")
        if not isinstance(topics, list) or len(topics) > 20000:
            raise ValueError("invalid topic journal")
        if not all(isinstance(topic, str) and allowed(topic) for topic in topics):
            raise ValueError("topic journal contains an unowned topic")
        return set(topics)

    def save(self, topics: set[str]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        document = {"version": 1, "identity": self.identity, "topics": sorted(topics)}
        descriptor, temporary = tempfile.mkstemp(prefix="topics-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(document, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
