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


def clear_state(conversation_id: str, resouce_type: str = "orders"):
    key = f"{conversation_id}:{resouce_type}"
    _cache.pop(key, None)


def set_state(conversation_id: str, resource_type: str = "orders"):
    key = f"{conversation_id}:{resource_type}"
    _cache.pop(key, None)


def parse_navigation_intent(user_message: str) -> tuple[NavigationIntent, int | None]:
    """ "
    Parse what the user typed into a NavigationIntent.
    """
    msg = user_message.strip().lower()

    if msg in ("next", "n", "show more", "more", "next page", "more orders", "next 5"):
        return NavigationIntent.NEXT, None
    if msg in ("previous", "prev", "back", "go back", "previous page", "prev page"):
        return NavigationIntent.PREVIOUS, None
    if msg in ("first", "start", "beginning", "first page"):
        return NavigationIntent.FIRST, None
    if msg in ("last", "end", "last page"):
        return NavigationIntent, None
    if msg in ("refresh", "reload", "update"):
        return NavigationIntent.REFRESH, None

    import re

    page_match = re.search(r"page\s*(\d+)", msg) or re.search(r"^(\d+)$", msg)
    if page_match:
        return NavigationIntent.SPECIFIC, int(page_match.group(1))
    return None, None


def apply_Navigation(
    state: PaginationState, intent: NavigationIntent, page: int | None = None
) -> dict:
    """
    Move the cursor and return a result describing what happened.
    Handles all edge cases.
    """
    old_page = state.current_page
    warning = None
    at_boundary = None

    if intent == NavigationIntent.next:
        if state.has_next:
            state.current_page += 1
        else:
            warning = (
                f"You're already on the last page (page {state.current_page} of {state.total_pages}). "
                f"There are no more orders to show."
            )
            at_boundary = "end"

    elif intent == NavigationIntent.FIRST:
        state.current_page = 1
        if old_page == 1:
            warning = (
                "You're already on the first page.There are no earlier orders to show"
            )
            at_boundary = "start"

    elif intent == NavigationIntent.LAST:
        state.current_page = state.total_pages
        if old_page == state.total_pages:
            warning = "You're already on the last page."

    elif intent == NavigationIntent.SPECIFIC and page is not None:
        if 1 <= page <= state.total_pages:
            state.current_page = page
        else:
            warning = (
                f"Page {page} doesn't exits.valid pages are 1 to {state.total_pages}."
            )
            at_boundary = "invalid"

    elif intent == NavigationIntent.REFRESH:
        pass

    set_state(state)

    return {
        "moved": state.current_page != old_page,
        "warning": warning,
        "old_page": old_page,
        "new_page": state.current_page,
        "at_boundary": at_boundary,
    }
