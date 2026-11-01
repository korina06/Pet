"""行程存储：进程内存、按 `trip_id` 存、有上限并按插入顺序淘汰。

不按会话覆盖——一次会话可以产出多份行程，被淘汰的那份要能被查出"不在了"。
"""

from __future__ import annotations

from core.itinerary import Itinerary


class TripStore:
    """按 trip_id 存行程，容量有限、最旧的先淘汰。"""

    def __init__(self, max_trips: int = 50) -> None:
        self._trips: dict[str, Itinerary] = {}
        self._order: list[str] = []
        self._max_trips = max(1, int(max_trips))

    @property
    def max_trips(self) -> int:
        return self._max_trips

    def put(self, itinerary: Itinerary) -> Itinerary:
        """存一份行程。重复的 trip_id 不会出现（生成时是新 id）。"""
        trip_id = itinerary.trip_id
        if trip_id not in self._trips:
            self._order.append(trip_id)
        self._trips[trip_id] = itinerary
        while len(self._order) > self._max_trips:
            oldest = self._order.pop(0)
            self._trips.pop(oldest, None)
        return itinerary

    def get(self, trip_id: str) -> Itinerary | None:
        return self._trips.get(str(trip_id))

    def ids(self) -> list[str]:
        """按存入顺序返回 id（最旧在前）。"""
        return list(self._order)

    def clear(self) -> None:
        self._trips.clear()
        self._order.clear()
