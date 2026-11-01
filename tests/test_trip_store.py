"""行程存储：不按会话覆盖（一次会话可以有多份），容量满时最旧的先淘汰且能查到「不在了」。
"""

from __future__ import annotations

from core.itinerary import Itinerary
from core.trip_store import TripStore


def _trip(trip_id: str, city: str = "北京") -> Itinerary:
    return Itinerary(trip_id=trip_id, city=city, start_date="2026-03-01", end_date="2026-03-02")


def test_put_and_get_roundtrip() -> None:
    store = TripStore()
    store.put(_trip("t1"))
    got = store.get("t1")
    assert got is not None and got.trip_id == "t1"


def test_unknown_trip_returns_none_not_empty_object() -> None:
    """查不到必须是 None——静默返回一个空行程会让人以为"生成成功了但没内容"。"""
    assert TripStore().get("nope") is None


def test_same_session_can_hold_multiple_trips() -> None:
    """不按 session 覆盖：改一版再生成，旧的那份要还在（「编辑行程」的前提）。"""
    store = TripStore()
    store.put(_trip("t1"))
    store.put(_trip("t2"))
    assert store.get("t1") is not None
    assert store.get("t2") is not None
    assert store.ids() == ["t1", "t2"], "应按存入顺序记录"


def test_capacity_evicts_oldest_first() -> None:
    store = TripStore(max_trips=2)
    store.put(_trip("t1"))
    store.put(_trip("t2"))
    store.put(_trip("t3"))
    assert store.get("t1") is None, "最旧的应被淘汰"
    assert store.get("t2") is not None
    assert store.get("t3") is not None
    assert store.ids() == ["t2", "t3"]


def test_capacity_rejects_silly_values() -> None:
    store = TripStore(max_trips=0)
    assert store.max_trips == 1, "上限至少为 1，否则什么都存不进去"


def test_putting_same_id_twice_does_not_duplicate_order() -> None:
    store = TripStore(max_trips=5)
    store.put(_trip("t1", "北京"))
    store.put(_trip("t1", "上海"))
    assert store.ids() == ["t1"], "同一个 id 不该在顺序表里出现两次"
    assert store.get("t1").city == "上海", "应是最新那份"


def test_clear_empties_everything() -> None:
    store = TripStore()
    store.put(_trip("t1"))
    store.clear()
    assert store.get("t1") is None
    assert store.ids() == []
