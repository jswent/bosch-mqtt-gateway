from pathlib import Path

import pytest

from bosch_mqtt_gateway.config import GatewayConfig
from bosch_mqtt_gateway.storage import TopicJournal, instance_lock


def test_atomic_journal_round_trip(tmp_path: Path):
    journal = TopicJournal(GatewayConfig(state_dir=str(tmp_path)))
    assert journal.load(lambda _: True) == set()
    journal.save({"a", "b"})
    journal.save({"b", "c"})
    assert journal.load(lambda _: True) == {"b", "c"}
    assert list(tmp_path.glob("topics-*")) == []
    assert journal.path.stat().st_mode & 0o777 == 0o600


def test_corrupt_journal_fails_closed(tmp_path: Path):
    journal = TopicJournal(GatewayConfig(state_dir=str(tmp_path)))
    journal.path.write_text("{broken")
    with pytest.raises(ValueError):
        journal.load(lambda _: True)


def test_namespace_change_fails_closed(tmp_path: Path):
    TopicJournal(GatewayConfig(state_dir=str(tmp_path))).save({"a"})
    changed = TopicJournal(GatewayConfig(id="different", state_dir=str(tmp_path)))
    with pytest.raises(ValueError, match="identity mismatch"):
        changed.load(lambda _: True)


def test_unowned_topic_never_deleted(tmp_path: Path):
    journal = TopicJournal(GatewayConfig(state_dir=str(tmp_path)))
    journal.save({"other/alarm"})
    with pytest.raises(ValueError, match="unowned"):
        journal.load(lambda topic: topic.startswith("bosch/"))


def test_process_lock(tmp_path: Path):
    with instance_lock(tmp_path):
        with pytest.raises(RuntimeError, match="another gateway"):
            with instance_lock(tmp_path):
                pass
    with instance_lock(tmp_path):
        pass
