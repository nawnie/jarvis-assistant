"""Document indexing checks the real SQLite result and privacy boundary without a model."""

import numpy as np

from wk import document_recall, semantic


class FakeRecall:
    def __init__(self, path):
        self.index = semantic.SemanticIndex(None, self._embed, path)

    def _embed(self, texts):
        return np.tile(np.ones(768, dtype=np.float32) / np.sqrt(768), (len(texts), 1))


def test_selected_folder_indexes_readme_and_skips_private_category(tmp_path):
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (chosen / "README.md").write_text("CUDA setup for this project: use the installed SDK.", encoding="utf-8")
    private = chosen / "medical"
    private.mkdir()
    (private / "notes.txt").write_text("private record", encoding="utf-8")
    recall = FakeRecall(tmp_path / "index.db")

    receipt = document_recall.index_folder(recall, chosen)
    rows = recall.index.db.execute("SELECT where_, text FROM vec WHERE source='document'").fetchall()

    assert "Indexed 1 files" in receipt
    assert len(rows) == 1
    assert rows[0][0].endswith("README.md")
    assert "installed SDK" in rows[0][1]
    assert "private record" not in str(rows)
