import pytest

from cerebro import embeddings, indexer, summaries

pytest.importorskip("numpy")  # skip unless the `semantic` extra is installed


class FakeModel:
    """Deterministic toy embeddings: presence of each vocab word -> a dimension."""

    VOCAB = ["stock", "checkout", "shipping", "attendance"]

    def encode(self, docs):
        import numpy as np

        rows = [[1.0 if w in d.lower() else 0.0 for w in self.VOCAB] for d in docs]
        return np.asarray(rows, dtype="float32")


@pytest.fixture
def fake_model(monkeypatch):
    monkeypatch.setattr(embeddings, "model_available", lambda: True)
    monkeypatch.setattr(embeddings, "_model", lambda: FakeModel())


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_build_and_semantic_search(tmp_path, project, fake_model):
    config, conn = project
    write(tmp_path, "checkout.py", "def validate():\n    return 1\n")
    write(tmp_path, "attendance.py", "def punch():\n    return 1\n")
    indexer.reindex(config, conn)
    summaries.record(conn, "checkout.py", "Handles checkout and stock validation")
    summaries.record(conn, "attendance.py", "Employee attendance punch clock")

    res = embeddings.build(config, conn)
    assert res["ok"] and res["embedded"] == 2

    hits = embeddings.search(config, conn, "stock at checkout", limit=2)
    assert hits[0][0] == "checkout.py"  # ranked by meaning, not just keywords


def test_build_is_incremental(tmp_path, project, fake_model):
    config, conn = project
    write(tmp_path, "a.py", "x = 1\n")
    indexer.reindex(config, conn)
    assert embeddings.build(config, conn)["embedded"] == 1
    assert embeddings.build(config, conn)["embedded"] == 0  # unchanged -> skipped


def test_search_noop_without_index(project, fake_model):
    config, conn = project
    assert embeddings.search(config, conn, "anything") == []


def test_search_lands_on_the_specific_symbol(tmp_path, project, fake_model):
    """Per-symbol granularity: two functions in one file, search returns the exact
    one (with its line), not just the file."""
    config, conn = project
    write(
        tmp_path,
        "svc.py",
        "def validate_stock():\n    return 1\n\n\ndef punch_attendance():\n    return 1\n",
    )
    indexer.reindex(config, conn)
    summaries.record(conn, "svc.py", "Mixed service module")

    embeddings.build(config, conn)
    hits = embeddings.search(config, conn, "stock", limit=3)

    # shape is (path, name, line, score); top hit is the stock symbol itself
    assert hits[0][0] == "svc.py"
    assert hits[0][1] == "validate_stock"
    assert hits[0][2] is not None  # carries a line number to jump to


def test_symbol_rows_replaced_when_file_changes(tmp_path, project, fake_model):
    """A changed file replaces its symbol rows (no stale vectors linger)."""
    config, conn = project
    write(tmp_path, "m.py", "def shipping_label():\n    return 1\n")
    indexer.reindex(config, conn)
    embeddings.build(config, conn)
    assert any(
        h[1] == "shipping_label"
        for h in embeddings.search(config, conn, "shipping", limit=5)
    )

    # Rename the function; the old symbol must disappear from results.
    write(tmp_path, "m.py", "def checkout_flow():\n    return 1\n")
    indexer.reindex(config, conn)
    embeddings.build(config, conn)
    names = {h[1] for h in embeddings.search(config, conn, "shipping checkout", limit=5)}
    assert "checkout_flow" in names
    assert "shipping_label" not in names
