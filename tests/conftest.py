from __future__ import annotations

import struct
import zlib
from typing import Any

import pytest

from glpi_kb2pdf.glpi import GlpiNotFoundError


def _png_1px() -> bytes:
    """PNG valide de 1 pixel blanc."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b"")


PNG_1PX = _png_1px()

CATEGORIES = [
    {"id": 1, "name": "Réseau", "completename": "Réseau", "parent": None},
    {"id": 2, "name": "VPN", "completename": "Réseau > VPN", "parent": {"id": 1, "name": "Réseau"}},
    {"id": 3, "name": "Postes", "completename": "Postes", "parent": None},
]


def make_article(article_id: int = 12, **overrides: Any) -> dict[str, Any]:
    article = {
        "id": article_id,
        "name": "Configurer le VPN <client>",
        "content": (
            "<p>Intro</p>"
            '<p><a href="http://glpi.local/front/document.send.php?docid=5&amp;itemtype=KnowbaseItem&amp;items_id=12">'
            '<img src="http://glpi.local/front/document.send.php?docid=5&amp;itemtype=KnowbaseItem&amp;items_id=12" '
            'width="300"></a></p>'
            '<p><a href="http://glpi.local/front/ticket.form.php?id=3">ticket 3</a></p>'
        ),
        "categories": [{"id": 2, "name": "VPN"}],
        "user": {"id": 7, "name": "alexis"},
        "date_creation": "2026-01-10T08:00:00+00:00",
        "date_mod": "2026-03-15T12:28:14+00:00",
    }
    article.update(overrides)
    return article


class FakeClient:
    """Double du GlpiClient, sans réseau."""

    api_url = "http://api.local:8081/api.php"
    root_url = "http://api.local:8081"

    def __init__(self, articles: list[dict[str, Any]] | None = None, documents: dict[int, tuple[bytes, str]] | None = None):
        self.articles = {a["id"]: a for a in (articles if articles is not None else [make_article()])}
        self.documents = documents if documents is not None else {5: (PNG_1PX, "image/png")}
        self.downloads: list[int] = []
        self.filters: list[str | None] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass

    def authenticate(self) -> None:
        pass

    def count_articles(self) -> int:
        return len(self.articles)

    def get_public_url(self) -> str:
        return "http://glpi.local"

    def list_categories(self) -> list[dict[str, Any]]:
        return CATEGORIES

    def get_article(self, article_id: int) -> dict[str, Any]:
        if article_id not in self.articles:
            raise GlpiNotFoundError(f"Article {article_id} introuvable")
        return self.articles[article_id]

    def iter_articles(self, rsql_filter: str | None = None, sort: str = "id:asc"):
        self.filters.append(rsql_filter)
        return iter(list(self.articles.values()))

    def iter_articles_by_ids(self, ids):
        return iter([self.articles[i] for i in ids])

    def download_document(self, document_id: int) -> tuple[bytes, str]:
        self.downloads.append(document_id)
        if document_id not in self.documents:
            raise GlpiNotFoundError(f"Document {document_id} introuvable")
        return self.documents[document_id]


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Dossier courant vide et aucune variable GLPI_* : aucune configuration existante n'interfère."""
    import os

    for name in list(os.environ):
        if name.startswith("GLPI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    return tmp_path
