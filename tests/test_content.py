import base64

import pytest
from bs4 import BeautifulSoup

from glpi_kb2pdf.content import build_document, document_id_from_url, prepare_content
from glpi_kb2pdf.glpi import GlpiNotFoundError

from conftest import PNG_1PX, make_article

INTERNAL = ["http://api.local:8081", "http://glpi.local"]


def fetcher(calls=None, documents=None):
    documents = documents if documents is not None else {5: (PNG_1PX, "image/png")}

    def fetch(doc_id):
        if calls is not None:
            calls.append(doc_id)
        if doc_id not in documents:
            raise GlpiNotFoundError(f"Document {doc_id} introuvable")
        return documents[doc_id]

    return fetch


def prepare(html, **kwargs):
    kwargs.setdefault("internal_urls", INTERNAL)
    result, warnings = prepare_content(html, kwargs.pop("fetch", fetcher()), **kwargs)
    return BeautifulSoup(result, "html.parser"), warnings


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8080/front/document.send.php?docid=1&itemtype=KnowbaseItem&items_id=1", 1),
        ("https://autre-hote/glpi/front/document.send.php?docid=42", 42),
        ("/front/document.send.php?docid=7", 7),
        ("document.send.php?docid=3", 3),
        ("http://glpi.local/front/document.send.php?file=_pictures/x.png", None),
        ("http://glpi.local/front/document.send.php?docid=abc", None),
        ("https://example.com/image.png?docid=1", None),
        ("data:image/png;base64,AAAA", None),
    ],
)
def test_document_id_from_url(url, expected):
    assert document_id_from_url(url) == expected


def test_glpi_image_is_embedded_whatever_the_host():
    calls = []
    soup, warnings = prepare(
        '<img src="http://127.0.0.1:8080/front/document.send.php?docid=5&amp;itemtype=KnowbaseItem" '
        'srcset="x 2x" width="300">',
        fetch=fetcher(calls),
    )
    img = soup.find("img")
    assert img["src"] == "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
    assert "srcset" not in img.attrs
    assert img["width"] == "300"
    assert calls == [5]
    assert warnings == []


def test_missing_glpi_image_becomes_placeholder():
    soup, warnings = prepare('<p><img src="/front/document.send.php?docid=99"></p>')
    assert soup.find("img") is None
    assert "document GLPI n° 99" in soup.get_text()
    assert len(warnings) == 1 and "99" in warnings[0]


def test_data_uri_images_are_untouched():
    html = '<img src="data:image/png;base64,AAAA">'
    soup, _ = prepare(html)
    assert soup.find("img")["src"] == "data:image/png;base64,AAAA"


def test_remote_images_kept_by_default():
    soup, _ = prepare('<img src="https://example.com/a.png">')
    assert soup.find("img")["src"] == "https://example.com/a.png"


def test_remote_images_can_be_excluded():
    soup, _ = prepare('<img src="https://example.com/a.png">', remote_images=False)
    assert soup.find("img") is None
    assert "Image externe non incluse" in soup.get_text()


def test_relative_image_outside_documents_is_reported():
    soup, warnings = prepare('<img src="/pics/logo.png">')
    assert soup.find("img") is None
    assert warnings


def test_links_to_glpi_are_unwrapped_but_text_kept():
    soup, _ = prepare(
        '<p><a href="http://glpi.local/front/ticket.form.php?id=3">ticket</a> '
        '<a href="http://api.local:8081/front/computer.form.php?id=1">poste</a> '
        '<a href="/front/central.php">accueil</a></p>'
    )
    assert soup.find("a") is None
    assert soup.get_text() == "ticket poste accueil"


def test_external_links_and_anchors_are_kept():
    soup, _ = prepare(
        '<a href="https://docs.docker.com">docs</a><a href="#etape-2">étape 2</a>'
        '<a href="mailto:support@example.com">mail</a><a name="ancre"></a>'
    )
    assert [a.get("href") for a in soup.find_all("a")] == [
        "https://docs.docker.com", "#etape-2", "mailto:support@example.com", None
    ]


def test_links_wrapping_images_are_unwrapped():
    soup, _ = prepare('<a href="https://example.com/big.png"><img src="https://example.com/small.png"></a>')
    assert soup.find("a") is None
    assert soup.find("img") is not None


def test_iframe_becomes_link():
    soup, _ = prepare('<iframe src="https://www.youtube.com/embed/xyz"></iframe><video></video>')
    assert soup.find("iframe") is None and soup.find("video") is None
    assert soup.find("a")["href"] == "https://www.youtube.com/embed/xyz"
    assert "non reproductible" in soup.get_text()


def test_build_document_header_and_metadata():
    article = make_article(categories=[{"id": 2, "name": "VPN"}, {"id": 9, "name": "Inconnue"}])
    html = build_document(article, "<p>corps</p>", category_names={2: "Réseau > VPN"})
    soup = BeautifulSoup(html, "html.parser")
    assert soup.title.string == "Configurer le VPN <client>"
    assert soup.find("h1").get_text() == "Configurer le VPN <client>"
    assert "&lt;client&gt;" in html  # le titre est échappé
    assert soup.find("p", class_="kb-categories").get_text(" ", strip=True) == "Catégories Réseau > VPN · Inconnue"
    slots = [div.get_text(" ", strip=True) for div in soup.find("div", class_="kb-meta").find_all("div")]
    assert slots == ["Créé le 10/01/2026", "Auteur alexis", "Mis à jour le 15/03/2026"]
    assert "n° 12" not in html
    # repris dans le pied de page
    assert soup.body["data-title"] == "Configurer le VPN <client>"
    assert soup.body["data-author"] == "alexis"
    assert soup.find("meta", attrs={"name": "keywords"})["content"] == "Réseau > VPN, Inconnue"
    assert soup.find("meta", attrs={"name": "dcterms.modified"})["content"].startswith("2026-03-15T12:28:14")
    assert soup.find("main").decode_contents().strip() == "<p>corps</p>"


def test_build_document_without_header():
    html = build_document(make_article(categories=[]), "<p>corps</p>", header=False, inline_css="p{color:red}")
    soup = BeautifulSoup(html, "html.parser")
    assert soup.find("header") is None
    assert soup.find("style").string.strip() == "p{color:red}"
    assert soup.find("meta", attrs={"name": "keywords"}) is None
    assert soup.body["data-title"] == "Configurer le VPN <client>"  # le pied de page reste rempli


def test_build_document_keeps_slots_when_information_is_missing():
    article = make_article(categories=[], user=None, date_creation=None)
    soup = BeautifulSoup(build_document(article, ""), "html.parser")
    assert soup.find("p", class_="kb-categories") is None
    slots = [div.get_text(strip=True) for div in soup.find("div", class_="kb-meta").find_all("div")]
    assert slots == ["", "", "Mis à jour le15/03/2026"]
    assert not soup.body.has_attr("data-author")
