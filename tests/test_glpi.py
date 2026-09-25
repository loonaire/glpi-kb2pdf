import pytest
import requests
import responses
from responses import matchers

from glpi_kb2pdf.glpi import (
    GlpiApiError,
    GlpiAuthError,
    GlpiClient,
    GlpiConnectionError,
    GlpiNotFoundError,
    normalize_api_url,
)

API = "http://glpi.test/api.php"
TOKEN = {"token_type": "Bearer", "expires_in": 3600, "access_token": "jeton-1", "refresh_token": "r"}


@pytest.fixture
def client():
    with GlpiClient("http://glpi.test", "cid", "secret", "alexis", "mdp") as glpi:
        yield glpi


def add_token(rsps, payload=TOKEN, status=200):
    return rsps.add(responses.POST, f"{API}/token", json=payload, status=status)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://glpi.test", "http://glpi.test/api.php"),
        ("http://glpi.test/", "http://glpi.test/api.php"),
        ("https://srv/glpi", "https://srv/glpi/api.php"),
        ("http://127.0.0.1:8081/api.php", "http://127.0.0.1:8081/api.php"),
        ("http://127.0.0.1:8081/api.php/v2", "http://127.0.0.1:8081/api.php/v2"),
    ],
)
def test_normalize_api_url(url, expected):
    assert normalize_api_url(url) == expected


@pytest.mark.parametrize("url", ["", "glpi.test", "ftp://glpi.test", "http://glpi.test/apirest.php", "http://g/api.php/v1"])
def test_normalize_api_url_rejects(url):
    with pytest.raises(ValueError):
        normalize_api_url(url)


def test_root_url(client):
    assert client.root_url == "http://glpi.test"


@responses.activate
def test_authentication_and_bearer_header(client):
    token_call = responses.add(
        responses.POST, f"{API}/token", json=TOKEN,
        match=[matchers.urlencoded_params_matcher({
            "grant_type": "password", "client_id": "cid", "client_secret": "secret",
            "username": "alexis", "password": "mdp", "scope": "api",
        })],
    )
    responses.add(
        responses.GET, f"{API}/Knowledgebase/Article/12", json={"id": 12},
        match=[matchers.header_matcher({"Authorization": "Bearer jeton-1"})],
    )
    assert client.get_article(12) == {"id": 12}
    assert client.get_article(12) == {"id": 12}
    assert token_call.call_count == 1  # le jeton est réutilisé


@responses.activate
def test_bad_password(client):
    add_token(responses.mock, {"error": "invalid_grant", "error_description": "The user credentials were incorrect."}, 400)
    with pytest.raises(GlpiAuthError, match="mot de passe incorrect"):
        client.authenticate()


@responses.activate
def test_bad_oauth_client(client):
    add_token(responses.mock, {"error": "invalid_client", "error_description": "Client authentication failed"}, 401)
    with pytest.raises(GlpiAuthError, match="client_secret"):
        client.authenticate()


@responses.activate
def test_token_endpoint_missing(client):
    responses.add(responses.POST, f"{API}/token", body="<html>404</html>", status=404)
    with pytest.raises(GlpiApiError, match="GLPI 11"):
        client.authenticate()


@responses.activate
def test_reauthenticates_once_on_401(client):
    token_call = add_token(responses.mock)
    responses.add(responses.GET, f"{API}/Knowledgebase/Article/1", status=401, json={"title": "expired"})
    responses.add(responses.GET, f"{API}/Knowledgebase/Article/1", json={"id": 1})
    assert client.get_article(1) == {"id": 1}
    assert token_call.call_count == 2


@responses.activate
def test_article_not_found(client):
    add_token(responses.mock)
    responses.add(responses.GET, f"{API}/Knowledgebase/Article/999", status=404,
                  json={"status": "ERROR_ITEM_NOT_FOUND", "title": "Not found", "detail": None})
    with pytest.raises(GlpiNotFoundError, match="Article 999 introuvable"):
        client.get_article(999)


@responses.activate
def test_api_error_message_includes_detail(client):
    add_token(responses.mock)
    responses.add(responses.GET, f"{API}/Knowledgebase/Article", status=400,
                  json={"status": "ERROR_INVALID_PARAMETER", "title": "RSQL query has invalid filters",
                        "detail": {"bogus": "Unknown property"}})
    with pytest.raises(GlpiApiError, match="bogus : Unknown property"):
        list(client.iter_articles("bogus==1"))


@responses.activate
def test_pagination_uses_content_range(client):
    add_token(responses.mock)
    pages = [([{"id": 1}, {"id": 2}], "0-1/5"), ([{"id": 3}, {"id": 4}], "2-3/5"), ([{"id": 5}], "4-4/5")]
    for start, (items, content_range) in zip((0, 2, 4), pages, strict=True):
        responses.add(
            responses.GET, f"{API}/Knowledgebase/Article", json=items, headers={"Content-Range": content_range},
            match=[matchers.query_param_matcher({"start": str(start), "limit": "2", "sort": "id:asc"})],
        )
    items = list(client._paginate("/Knowledgebase/Article", {"sort": "id:asc"}, page_size=2))
    assert [item["id"] for item in items] == [1, 2, 3, 4, 5]


@responses.activate
def test_iter_articles_by_ids_chunks(client):
    add_token(responses.mock)
    for chunk in ("1,2", "3"):
        responses.add(
            responses.GET, f"{API}/Knowledgebase/Article", json=[{"id": int(i)} for i in chunk.split(",")],
            match=[matchers.query_param_matcher({"filter": f"id=in=({chunk})", "sort": "id:asc",
                                                 "start": "0", "limit": "100"})],
        )
    assert [a["id"] for a in client.iter_articles_by_ids([1, 2, 3], chunk_size=2)] == [1, 2, 3]


@responses.activate
def test_download_document_uses_content_type(client):
    add_token(responses.mock)
    responses.add(responses.GET, f"{API}/Management/Document/5/Download", body=b"PNGDATA",
                  content_type="image/png")
    assert client.download_document(5) == (b"PNGDATA", "image/png")


@responses.activate
def test_download_document_falls_back_to_metadata_mime(client):
    add_token(responses.mock)
    responses.add(responses.GET, f"{API}/Management/Document/5/Download", body=b"GIF",
                  content_type="application/octet-stream")
    responses.add(responses.GET, f"{API}/Management/Document/5", json={"id": 5, "mime": "image/gif"})
    assert client.download_document(5) == (b"GIF", "image/gif")


@responses.activate
def test_public_url(client):
    add_token(responses.mock)
    responses.add(responses.GET, f"{API}/Setup/Config/core/url_base",
                  json={"context": "core", "name": "url_base", "value": "http://glpi.public:8080/"})
    assert client.get_public_url() == "http://glpi.public:8080"


@responses.activate
def test_public_url_without_rights(client):
    add_token(responses.mock)
    responses.add(responses.GET, f"{API}/Setup/Config/core/url_base", status=403,
                  json={"status": "ERROR_RIGHT_MISSING", "title": "You don't have permission"})
    assert client.get_public_url() is None


@responses.activate
def test_connection_error(client):
    responses.add(responses.POST, f"{API}/token", body=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(GlpiConnectionError, match="Impossible de joindre"):
        client.authenticate()


@responses.activate
def test_entity_headers():
    add_token(responses.mock)
    responses.add(
        responses.GET, f"{API}/Knowledgebase/Article/1", json={"id": 1},
        match=[matchers.header_matcher({"GLPI-Entity": "4", "GLPI-Entity-Recursive": "false"})],
    )
    with GlpiClient("http://glpi.test", "c", "s", "u", "p", entity=4, recursive=False) as glpi:
        assert glpi.get_article(1) == {"id": 1}
