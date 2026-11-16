import pytest

from notes_mcp.store import NoteError, NoteStore, to_fts_query


@pytest.fixture
def store():
    s = NoteStore(":memory:")
    s.add("Postgres connection pooling", "Use PgBouncer in transaction mode. Keep pool size small.", ["work", "databases"])
    s.add("Weekend hike", "Trail was muddy but the view from the ridge was great.", ["personal"])
    s.add("Python async tips", "Never block the event loop; use asyncio.to_thread for blocking calls.", ["work", "python"])
    return s


def test_search_ranks_and_snippets(store):
    hits = store.search("connection pool")
    assert hits[0]["title"] == "Postgres connection pooling"
    assert "**" in hits[0]["snippet"]
    assert hits[0]["score"] > 0


def test_search_uses_stemming(store):
    # porter stemmer: "blocking" matches "block"
    assert store.search("blocking")[0]["title"] == "Python async tips"


def test_search_with_tag_filter(store):
    assert store.search("the", tag="personal")[0]["title"] == "Weekend hike"
    assert store.search("pool", tag="personal") == []


@pytest.mark.parametrize("nasty", ['"unbalanced', "title:hike", "NEAR(a b)", "pool*", "'); DROP TABLE notes; --"])
def test_search_is_injection_safe(store, nasty):
    store.search(nasty)  # must not raise an SQLite/FTS syntax error
    assert store.count() == 3


def test_fts_query_quotes_every_word():
    assert to_fts_query('pool "size"') == '"pool" OR "size"'
    with pytest.raises(NoteError):
        to_fts_query("!!! ???")


def test_update_reindexes(store):
    store.update(2, content="Saw a heron at the lake.")
    assert store.search("heron")[0]["id"] == 2
    assert store.search("muddy") == []
    assert store.get(2).title == "Weekend hike"  # untouched fields stay


def test_delete_and_missing(store):
    store.delete(1)
    assert store.search("pgbouncer") == []
    with pytest.raises(NoteError):
        store.get(1)
    with pytest.raises(NoteError):
        store.delete(1)


def test_tags_validation_and_counts(store):
    assert store.tags() == {"work": 2, "databases": 1, "personal": 1, "python": 1}
    with pytest.raises(NoteError):
        store.add("x", "y", ["Bad Tag!"])
    with pytest.raises(NoteError):
        store.add("x", "y", [f"t{i}" for i in range(11)])
    assert store.add("x", "y", ["  Work ", "work"]).tags == ["work"]  # normalised + deduplicated


def test_length_limits(store):
    with pytest.raises(NoteError):
        store.add("", "content")
    with pytest.raises(NoteError):
        store.add("t", "x" * 20_001)


def test_persists_to_disk(tmp_path):
    path = tmp_path / "notes.db"
    s = NoteStore(path)
    s.add("Saved", "on disk", [])
    s.close()
    assert NoteStore(path).search("disk")[0]["title"] == "Saved"
