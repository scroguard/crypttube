"""Small SQL-building helpers shared by the list/detail endpoints.

Lists use keyset pagination: the cursor carries the last row's (sort_key, id), and
the next page is `WHERE (sort_expr, id) > (key, id) ORDER BY sort_expr, id LIMIT n`.
With an index on exactly (sort_expr, id), every page is a short index range scan,
regardless of how deep the user has scrolled (OFFSET would scan all skipped rows).
"""

import asyncio
import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass

import asyncpg
from fastapi import HTTPException


class Q:
    """Accumulates positional parameters; p() returns the placeholder for a value.
    Values registered under a key are bound once and reused."""

    def __init__(self):
        self.args: list = []
        self._named: dict[str, str] = {}

    def p(self, value, key: str | None = None) -> str:
        if key is not None and key in self._named:
            return self._named[key]
        self.args.append(value)
        placeholder = f"${len(self.args)}"
        if key is not None:
            self._named[key] = placeholder
        return placeholder


@dataclass(frozen=True)
class Sort:
    expr: str               # SQL expression; must match an index `(expr, id)` for speed
    type: str               # Postgres type, used to cast the cursor value back
    collate: str | None = None


def encode_cursor(sort: str, desc: bool, key: str | None, row_id: int) -> str:
    raw = json.dumps([sort, desc, key, row_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, sort: str, desc: bool) -> tuple[str, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        c_sort, c_desc, key, row_id = json.loads(raw)
        if c_sort != sort or c_desc != desc:
            raise ValueError("cursor belongs to a different sort order")
        if not isinstance(key, str):
            raise ValueError("bad key")
        return key, int(row_id)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError) as e:
        raise HTTPException(422, f"invalid cursor: {e}") from e


@dataclass
class Page:
    items: list[dict]
    next_cursor: str | None
    total: int | None = None


@dataclass
class ListQuery:
    alias: str
    from_: str                                   # FROM clause including joins
    select: str                                  # columns of the base row
    sorts: dict[str, Sort]
    where: Callable[[Q], list[str]]              # filters + ACL; called once per query built
    # LATERAL joins and extra columns, evaluated for the page rows only
    enrich: Callable[[Q], tuple[str, str]] = lambda q: ("", "")


def _where_sql(conds: list[str]) -> str:
    return ("WHERE " + " AND ".join(conds)) if conds else ""


async def paginate(pool: asyncpg.Pool, lq: ListQuery, *, sort: str, desc: bool,
                   cursor: str | None, limit: int, with_total: bool = False, offset: int = 0) -> Page:
    if sort not in lq.sorts:
        raise HTTPException(422, f"sort must be one of {sorted(lq.sorts)}")
    s = lq.sorts[sort]
    direction = "DESC" if desc else "ASC"
    key_expr = f"{s.expr} COLLATE {s.collate}" if s.collate else s.expr
    outer_key = f"p.sort_key COLLATE {s.collate}" if s.collate else "p.sort_key"

    q = Q()
    where = lq.where(q)
    if cursor:
        # The key travels as Postgres's own text rendering and is cast back in SQL, so
        # it round-trips exactly (Python date/float conversions don't: e.g. asyncpg
        # encodes date(1, 1, 1) as -infinity).
        key, last_id = decode_cursor(cursor, sort, desc)
        op = "<" if desc else ">"
        where.append(f"({key_expr}, {lq.alias}.id) {op} ({q.p(key)}::text::{s.type}, {q.p(last_id)}::bigint)")
    joins, cols = lq.enrich(q)
    # The row set is limited first (an index range scan), then related data is
    # aggregated for just those rows via LATERAL joins: one round-trip, no N+1.
    sql = f"""
        WITH p AS (
            SELECT {lq.select}, {key_expr} AS sort_key, ({key_expr})::text AS cursor_key
            FROM {lq.from_}
            {_where_sql(where)}
            ORDER BY {key_expr} {direction}, {lq.alias}.id {direction}
            LIMIT {q.p(limit + 1)} OFFSET {q.p(0 if cursor else offset)}
        )
        SELECT p.* {cols}
        FROM p {joins}
        ORDER BY {outer_key} {direction}, p.id {direction}
    """

    async def total():
        if not with_total:
            return None
        cq = Q()
        return await pool.fetchval(
            f"SELECT count(*) FROM {lq.from_} {_where_sql(lq.where(cq))}", *cq.args)

    rows, count = await asyncio.gather(pool.fetch(sql, *q.args), total())
    items = [dict(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(sort, desc, items[-1]["cursor_key"], items[-1]["id"])
    for item in items:
        del item["sort_key"], item["cursor_key"]
    return Page(items, next_cursor, count)
