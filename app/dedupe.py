"""Near-duplicate grouping via pairwise cosine similarity + Union-Find.

Two independent signals can trigger a merge:
  1. CLIP cosine similarity >= ``threshold`` (broad "semantically similar").
  2. pHash Hamming distance <= ``hash_max_distance`` (tight "same media,
     minor edit/logo/crop/recompression"), when both posts have a hash.

Either signal is sufficient — this is the "exact media, regardless of
logo/edit" use case (REL-DUP), which is intentionally stricter/narrower
than the general semantic-similarity use case (REL-SEM / ``/similar``).
"""

from __future__ import annotations

from typing import Hashable, Iterable, Sequence, TypeVar

from app.phash import hamming_distance

T = TypeVar("T", bound=Hashable)


class UnionFind:
    def __init__(self, items: Iterable[T]) -> None:
        self.parent: dict[T, T] = {item: item for item in items}
        self.rank: dict[T, int] = {item: 0 for item in self.parent}

    def find(self, x: T) -> T:
        parent = self.parent[x]
        if parent != x:
            self.parent[x] = self.find(parent)
        return self.parent[x]

    def union(self, a: T, b: T) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b, strict=True)))


def dedupe_post_uids(
    post_uids: Sequence[str],
    vectors: dict[str, list[float]],
    threshold: float,
    hashes: dict[str, str] | None = None,
    hash_max_distance: int | None = None,
) -> tuple[list[str], list[str], list[list[str]], list[str]]:
    """Group similar/duplicate posts with Union-Find.

    Parameters
    ----------
    hashes
        Optional {post_uid: phash-hex}. When given together with
        ``hash_max_distance``, a pair also merges if their pHash Hamming
        distance is within that bound — independent of the cosine check.
    hash_max_distance
        Max Hamming distance (0..64 for the default 8x8 pHash) to treat two
        posts as the same underlying media.

    Returns
    -------
    unique_post_uids
        One representative per group (first occurrence in ``post_uids``).
    removed_post_uids
        All other members of multi-post groups (input order).
    groups
        Full connected components (debug); order follows first-seen members.
    missing_post_uids
        Requested uids with no vector in the store (never silently dropped).
    """
    hashes = hashes or {}

    # Preserve order; drop exact duplicate ids in the request itself
    seen: set[str] = set()
    ordered: list[str] = []
    for uid in post_uids:
        uid = uid.strip() if isinstance(uid, str) else uid
        if not uid or uid in seen:
            continue
        seen.add(uid)
        ordered.append(uid)

    missing = [uid for uid in ordered if uid not in vectors]
    present = [uid for uid in ordered if uid in vectors]

    if not present:
        return [], [], [], missing

    uf = UnionFind(present)
    n = len(present)
    for i in range(n):
        uid_i = present[i]
        vi = vectors[uid_i]
        hash_i = hashes.get(uid_i)
        for j in range(i + 1, n):
            uid_j = present[j]
            if cosine_similarity(vi, vectors[uid_j]) >= threshold:
                uf.union(uid_i, uid_j)
                continue
            hash_j = hashes.get(uid_j)
            if (
                hash_i
                and hash_j
                and hash_max_distance is not None
                and hamming_distance(hash_i, hash_j) <= hash_max_distance
            ):
                uf.union(uid_i, uid_j)

    root_to_group: dict[str, list[str]] = {}
    for uid in present:
        root = uf.find(uid)
        root_to_group.setdefault(root, []).append(uid)

    seen_roots: set[str] = set()
    unique: list[str] = []
    removed: list[str] = []
    groups: list[list[str]] = []

    for uid in present:
        root = uf.find(uid)
        if root in seen_roots:
            continue
        seen_roots.add(root)
        group = root_to_group[root]
        groups.append(group)
        unique.append(group[0])
        removed.extend(group[1:])

    return unique, removed, groups, missing
