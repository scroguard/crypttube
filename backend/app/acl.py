"""What a viewer may see, computed once per request and applied inside SQL.

Three layers, all enforced by `visible()`:

1. Deny rules set by an admin (user_hidden_* in 0001_initial.sql):
   - a hidden tag hides its descendants too, and anything tagged with any of them
   - a hidden studio hides its child studios too, and their content
   - a hidden performer hides content featuring them; performers carrying a hidden
     tag count as hidden performers
   Admins are exempt from these.
   A user's own filters (user_filter_*, set on their account page) are applied the same
   way, for every role including admins, while they have them switched on. They can
   also choose the opposite: "only show" tags/performers, in which case content must
   carry at least one of them (tags, performers and studios themselves stay listed).
2. Library visibility (0008_user_libraries.sql): content of another user's library is
   visible only if that library is shared with the viewer or with everyone. This
   applies to admins too: private means private.
3. Personal metadata: a tag/performer/studio owned by another user is visible only
   when attached to content the viewer can see.

An image is also hidden if any gallery containing it is hidden. Hidden things behave
as if they don't exist (404 on detail routes, absent from lists and counts).
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
    libraries: list[int] = field(default_factory=list)  # user libraries hidden from this viewer
    # The viewer's own "only show me" choice: when either is set, content must carry one
    # of these tags or feature one of these performers.
    only_tags: list[int] = field(default_factory=list)
    only_performers: list[int] = field(default_factory=list)
    # Capabilities (not part of what is visible): editor/admin role, and upload right.
    editor: bool = False
    can_upload: bool = False

    @property
    def unrestricted(self) -> bool:
        return not (self.tags or self.studios or self.performers or self.libraries
                    or self.only_tags or self.only_performers)

    @property
    def counts_viewer(self) -> int:
        """Key into entity_counts (0 = the internal see-everything view)."""
        return self.user_id or 0

    def signature(self) -> tuple:
        """Viewers with equal signatures see exactly the same content."""
        return (tuple(self.tags), tuple(self.studios), tuple(self.performers), tuple(self.libraries),
                tuple(self.only_tags), tuple(self.only_performers))


# Internal view that sees everything (maintenance jobs, tests). Never given to a request.
UNRESTRICTED = Acl()

HIDDEN_LIBRARIES_SQL = """
    SELECT l.id FROM library_paths l
    WHERE l.owner_id IS NOT NULL AND l.owner_id <> $1
      AND NOT (l.visibility = 'everyone'
               OR (l.visibility = 'users' AND EXISTS (
                     SELECT 1 FROM library_shares sh WHERE sh.library_id = l.id AND sh.user_id = $1)))
    ORDER BY l.id"""


async def load_acl(db: asyncpg.Pool | asyncpg.Connection, user_id: int, *, deny_rules: bool = True,
                   filters: bool = True) -> Acl:
    """`deny_rules=False` for admins: they skip layer 1 but not library privacy.
    `filters=False` leaves out the user's own filters (to show them what they chose)."""
    libraries = [r["id"] for r in await db.fetch(HIDDEN_LIBRARIES_SQL, user_id)]
    row = await db.fetchrow(
        """WITH RECURSIVE
             mine AS (SELECT $3::boolean AND coalesce(
                        (SELECT filters_enabled FROM user_settings WHERE user_id = $1), true) AS on),
             ht(id) AS (
               (SELECT tag_id FROM user_hidden_tags WHERE user_id = $1 AND $2::boolean
                UNION
                SELECT tag_id FROM user_filter_tags, mine WHERE user_id = $1 AND mine.on AND NOT show_only)
               UNION
               SELECT tr.child_id FROM tag_relations tr JOIN ht ON tr.parent_id = ht.id),
             ot(id) AS (
               SELECT tag_id FROM user_filter_tags, mine WHERE user_id = $1 AND mine.on AND show_only
               UNION
               SELECT tr.child_id FROM tag_relations tr JOIN ot ON tr.parent_id = ot.id),
             hs(id) AS (
               SELECT studio_id FROM user_hidden_studios WHERE user_id = $1 AND $2::boolean
               UNION
               SELECT s.id FROM studios s JOIN hs ON s.parent_id = hs.id)
           SELECT ARRAY(SELECT id FROM ht ORDER BY id) AS tags,
                  ARRAY(SELECT id FROM hs ORDER BY id) AS studios,
                  ARRAY(SELECT performer_id FROM user_hidden_performers WHERE user_id = $1 AND $2::boolean
                        UNION
                        SELECT performer_id FROM user_filter_performers, mine
                        WHERE user_id = $1 AND mine.on AND NOT show_only
                        UNION
                        SELECT performer_id FROM performer_tags WHERE tag_id IN (SELECT id FROM ht)
                        ORDER BY 1) AS performers,
                  ARRAY(SELECT id FROM ot ORDER BY id) AS only_tags,
                  ARRAY(SELECT performer_id FROM user_filter_performers, mine
                        WHERE user_id = $1 AND mine.on AND show_only ORDER BY 1) AS only_performers""",
        user_id, deny_rules, filters,
    )
    return Acl(list(row["tags"]), list(row["studios"]), list(row["performers"]), user_id, libraries,
               list(row["only_tags"]), list(row["only_performers"]))


def _only_clause(acl: Acl, alias: str, q: Q) -> str | None:
    """`alias` carries one of the viewer's "only show" tags or performers."""
    parts = []
    if acl.only_tags:
        parts.append(f"{alias}.tag_ids && {q.p(acl.only_tags, 'acl_only_tags')}::bigint[]")
    if acl.only_performers:
        parts.append(f"{alias}.performer_ids && {q.p(acl.only_performers, 'acl_only_performers')}::bigint[]")
    return f"({' OR '.join(parts)})" if parts else None


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
    if acl.libraries:
        out.append(f"({alias}.library_id IS NULL OR {alias}.library_id <> ALL({q.p(acl.libraries, 'acl_libraries')}::bigint[]))")
    return out


def _personal_clause(acl: Acl, kind: str, alias: str, q: Q) -> list[str]:
    """Someone else's personal tag/performer/studio shows only via visible content.
    "Attached to content I can see" is exactly "has a row in my cached counts"."""
    if acl.user_id is None:
        return []
    me = q.p(acl.user_id, "acl_me")
    return [f"""({alias}.owner_id IS NULL OR {alias}.owner_id = {me}::bigint OR EXISTS (
                SELECT 1 FROM entity_counts ec WHERE ec.viewer = {me}::bigint
                AND ec.kind = '{kind}' AND ec.entity_id = {alias}.id))"""]


def visible(acl: Acl, kind: str, alias: str, q: Q) -> list[str]:
    """SQL conditions (to AND together) restricting `alias` to rows the user may see."""
    if kind in ("performer", "studio", "tag"):
        hidden = {"performer": acl.performers, "studio": acl.studios, "tag": acl.tags}[kind]
        out = [f"{alias}.id <> ALL({q.p(hidden, f'acl_{kind}s')}::bigint[])"] if hidden else []
        return out + _personal_clause(acl, kind, alias, q)
    if kind in ("scene", "gallery"):
        only = _only_clause(acl, alias, q)
        return _content_clauses(acl, alias, q) + ([only] if only else [])
    if kind == "image":
        out = _content_clauses(acl, alias, q)
        only = _only_clause(acl, alias, q)
        if only:
            # An image also counts when a gallery it belongs to matches, so a chosen
            # gallery never opens empty.
            out.append(f"""({only} OR EXISTS (SELECT 1 FROM gallery_images gi JOIN galleries g ON g.id = gi.gallery_id
                           WHERE gi.image_id = {alias}.id AND {_only_clause(acl, "g", q)}))""")
        gallery = _content_clauses(acl, "g", q)
        if gallery:
            out.append(f"""NOT EXISTS (SELECT 1 FROM gallery_images gi JOIN galleries g ON g.id = gi.gallery_id
                           WHERE gi.image_id = {alias}.id AND NOT ({' AND '.join(gallery)}))""")
        return out
    raise ValueError(kind)
