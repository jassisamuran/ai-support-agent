import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

PAGE_SIZE = 5
CACHE_TTL = 300
ORDER_SNAP_TTL = 60


class NavigationIntent(str, Enum):
    NEXT = "next"
    PREVIOUS = "previous"
    FIRST = "first"
    LAST = "last"
    SPECIFIC = "specific"
    REFRESH = "refresh"


@dataclass
class OrderSnapshot:
    order_id: str
    data: dict
    fetched_at: float = field(default_factory=time.time)
    status_at_fetch: str = ""

    def is_stale(self) -> bool:
        return (time.time() - self.fetched_at) > ORDER_SNAP_TTL

    def status_changed(self, new_data: dict) -> bool:
        return new_data.get("status") != self.status_at_fetch


@dataclass
class PaginationState:
    """Full pagination state for one conversation.
    stores ALL orders fetched (the full month list),
    current page cursor, and per-order snapshots."""

    conversation_id: str
    resource_type: str
    all_items: list[dict]
    current_page: int = 1
    page_size: int = PAGE_SIZE
    fetched_at: float = field(default_factory=time.time)
    snapshots: dict[str, OrderSnapshot] = field(default_factory=dict)
    stale_ids: set[str] = field(default_factory=set)

    @property
    def total_items(self) -> int:
        return len(self.all_items)

    @property
    def total_pages(self) -> int:
        if self.total_items == 0:
            return 1
        return (self.total_items + self.page_size - 1) // self.page_size

    @property
    def current_items(self) -> list[dict]:
        start = (self.current_page - 1) * self.page_size
        end = start + self.page_size
        return self.all_items[start:end]

    @property
    def has_next(self) -> bool:
        return self.current_page < self.total_pages

    @property
    def has_previous(self) -> bool:
        return self.current_page > 1

    @property
    def is_list_stale(self) -> bool:
        return (time.time() - self.fetched_at) > CACHE_TTL

    def snapshot_order(self, order: dict):
        oid = order.get("id") or order.get("order_id", "")
        self.snapshots[oid] = OrderSnapshot(
            order_id=order,
            data=order,
            fetched_at=time.time(),
            status_at_fetch=order.get("status", ""),
        )

    def mark_statle(self, order_id: str):
        self.stale_ids.add(order_id)

    def clear_stale(self):
        self.stale_ids.clear()


_cache: dict[str, PaginationState] = {}


def get_state(
    conversation_id: str, resource_type: str = "orders"
) -> Optional[PaginationState]:
    key = f"{conversation_id}:{resource_type}"
    return _cache[key]
