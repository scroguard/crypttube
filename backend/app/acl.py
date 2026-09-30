"""Per-user deny rules, expanded once per request and applied inside SQL.

Semantics (see user_hidden_* in 0001_initial.sql):
- a hidden tag hides its descendants too, and anything tagged with any of them
- a hidden studio hides its child studios too, and their content
- a hidden performer hides content featuring them; performers carrying a hidden
  tag count as hidden performers
- an image is hidden if any gallery containing it is hidden
Hidden things behave as if they don't exist (404 on detail routes, absent from lists).
"""

from dataclasses import dataclass, field

import asyncpg

from app.sql import Q


@dataclass(frozen=True)
class Acl:
    tags: list[int] = field(default_factory=list)
    studios: list[int] = field(default_factory=list)
    performers: list[int] = field(default_factory=list)
    user_id: int | None = None

    @property
    def unrestricted(self) -> bool:
        return not (self.tags or self.studios or self.performers)

    @property
    def counts_viewer(self) -> int:
        """Key into entity_counts: 0 for the shared unrestricted counts."""
        return 0 if self.unrestricted else self.user_id


UNRESTRICTED = Acl()


async def load_acl(db: asyncpg.Pool | asyncpg.Connection, user_id: int) -> Acl:
    row = await db.fetchrow(
        """WITH RECURSIVE
             ht(id) AS (
               SELECT tag_id FROM user_hidden_tags WHERE user_id = $1
               UNION
               SELECT tr.child_id FROM tag_relations tr JOIN ht ON tr.parent_id = ht.id),
             hs(id) AS (
               SELECT studio_id FROM user_hidden_studios WHERE user_id = $1
               UNION
               SELECT s.id FROM studios s JOIN hs ON s.parent_id = hs.id)
           SELECT ARRAY(SELECT id FROM ht ORDER BY id) AS tags,
                  ARRAY(SELECT id FROM hs ORDER BY id) AS studios,
                  ARRAY(SELECT performer_id FROM user_hidden_performers WHERE user_id = $1
                        UNION
                        SELECT performer_id FROM performer_tags WHERE tag_id IN (SELECT id FROM ht)
                        ORDER BY 1) AS performers""",
        user_id,
    )
    return Acl(list(row["tags"]), list(row["studios"]), list(row["performers"]), user_id)


def _content_clauses(acl: Acl, alias: str, q: Q) -> list[str]:
    # tag_ids / performer_ids are trigger-maintained copies of the link tables
    # (0004_link_arrays.sql): a per-row check, so pages stay index range scans.
    out = []
    if acl.tags:
        out.append(f"NOT ({alias}.tag_ids && {q.p(acl.tags, 'acl_tags')}::bigint[])")
    if acl.performers:
        out.append(f"NOT ({alias}.performer_ids && {q.p(acl.performers, 'acl_performers')}::bigint[])")
    if acl.studios:
        out.append(f"({alias}.studio_id IS NULL OR {alias}.studio_id <> ALL({q.p(acl.studios, 'acl_studios')}::bigint[]))")
    return out


def visible(acl: Acl, kind: str, alias: str, q: Q) -> list[str]:
    """SQL conditions (to AND together) restricting `alias` to rows the user may see."""
    if acl.unrestricted:
        return []
    if kind in ("scene", "gallery"):
        return _content_clauses(acl, alias, q)
    if kind == "image":
        out = _content_clauses(acl, alias, q)
        gallery = _content_clauses(acl, "g", q)
        if gallery:
            out.append(f"""NOT EXISTS (SELECT 1 FROM gallery_images gi JOIN galleries g ON g.id = gi.gallery_id
                           WHERE gi.image_id = {alias}.id AND NOT ({' AND '.join(gallery)}))""")
        return out
    if kind == "performer" and acl.performers:
        return [f"{alias}.id <> ALL({q.p(acl.performers, 'acl_performers')}::bigint[])"]
    if kind == "studio" and acl.studios:
        return [f"{alias}.id <> ALL({q.p(acl.studios, 'acl_studios')}::bigint[])"]
    if kind == "tag" and acl.tags:
        return [f"{alias}.id <> ALL({q.p(acl.tags, 'acl_tags')}::bigint[])"]
    return []
