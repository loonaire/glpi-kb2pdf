"""Client pour l'API REST « haut niveau » de GLPI (API v2, GLPI 11 et plus).

Seules les opérations de lecture nécessaires à l'export de la base de
connaissance sont implémentées. L'authentification utilise le flux OAuth2
« password » : un client OAuth doit être déclaré dans GLPI
(Configuration > Clients OAuth) avec ce type d'autorisation et la portée « api ».
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from glpi_kb2pdf import __version__

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
PAGE_SIZE = 100
# Le jeton est renouvelé s'il expire dans moins de TOKEN_EXPIRY_MARGIN secondes
TOKEN_EXPIRY_MARGIN = 60

# Messages explicites pour les codes d'erreur OAuth2 renvoyés par /token
_OAUTH_HINTS = {
    "invalid_client": "client_id ou client_secret OAuth incorrect",
    "invalid_grant": "nom d'utilisateur ou mot de passe incorrect",
    "unsupported_grant_type": "le client OAuth n'autorise pas le type d'autorisation « password »",
    "invalid_scope": "le client OAuth n'autorise pas la portée « api »",
}


class GlpiError(Exception):
    """Erreur de base du client GLPI."""


class GlpiConnectionError(GlpiError):
    """Le serveur GLPI est injoignable (réseau, DNS, TLS, délai dépassé)."""


class GlpiAuthError(GlpiError):
    """Authentification refusée (identifiants ou client OAuth invalides)."""


class GlpiNotFoundError(GlpiError):
    """L'élément demandé n'existe pas ou n'est pas visible par l'utilisateur."""


class GlpiApiError(GlpiError):
    """Réponse inattendue de l'API."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def normalize_api_url(url: str) -> str:
    """Retourne l'URL de base de l'API v2 à partir de l'URL saisie par l'utilisateur.

    Accepte l'URL racine de GLPI (``https://glpi.example.com``) comme celle
    de l'API (``https://glpi.example.com/api.php``).
    """
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"URL de GLPI invalide : {url!r} (attendu : http(s)://serveur[/chemin])")
    path = parts.path
    if "apirest.php" in path or re.search(r"/api\.php/v1(\.\d+)*$", path):
        raise ValueError(
            "L'ancienne API REST (v1 / apirest.php) n'est pas prise en charge : "
            "indiquez l'URL racine de GLPI ou celle de l'API v2 (…/api.php)."
        )
    if "api.php" not in path:
        path += "/api.php"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class GlpiClient:
    """Client en lecture seule pour la base de connaissance GLPI."""

    def __init__(
        self,
        url: str,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        *,
        verify: bool | str = True,
        timeout: float = DEFAULT_TIMEOUT,
        entity: int | None = None,
        recursive: bool = True,
        session: requests.Session | None = None,
    ):
        self.api_url = normalize_api_url(url)
        self.timeout = timeout
        self._credentials = {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password,
            "scope": "api",
        }
        self._token_expires_at = 0.0
        self._session = session or requests.Session()
        self._session.verify = verify
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": f"glpi-kb2pdf/{__version__}",
            "GLPI-Entity-Recursive": "true" if recursive else "false",
        })
        if entity is not None:
            self._session.headers["GLPI-Entity"] = str(entity)

    @property
    def root_url(self) -> str:
        """URL racine de GLPI déduite de l'URL de l'API (sans ``/api.php``)."""
        return self.api_url.split("/api.php", 1)[0]

    def __enter__(self) -> GlpiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    #  Authentification et requêtes
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Obtient un jeton d'accès OAuth2 et l'ajoute aux en-têtes de la session."""
        # L'en-tête Authorization d'un éventuel ancien jeton ne doit pas être envoyé à /token
        response = self._send("POST", "/token", data=self._credentials, headers={"Authorization": None})
        if response.status_code == 404:
            raise GlpiApiError(
                f"Point d'accès {self.api_url}/token introuvable : vérifiez l'URL "
                "(GLPI 11 minimum, avec l'API activée dans Configuration > Générale > API).",
                404,
            )
        if response.status_code in (400, 401, 403):
            raise GlpiAuthError(_auth_error_message(response))
        _raise_for_status(response)
        payload = _json(response)
        try:
            token = payload["access_token"]
        except (KeyError, TypeError) as exc:
            raise GlpiApiError("Réponse d'authentification inattendue : jeton absent") from exc
        token_type = payload.get("token_type") or "Bearer"
        self._session.headers["Authorization"] = f"{token_type} {token}"
        expires_in = float(payload.get("expires_in") or 3600)
        self._token_expires_at = time.monotonic() + expires_in
        log.debug("Authentifié sur %s (jeton valable %d s)", self.api_url, expires_in)

    def _send(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Envoie une requête brute en traduisant les erreurs réseau."""
        url = self.api_url + path
        log.debug("%s %s %s", method, url, kwargs.get("params") or "")
        try:
            return self._session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.exceptions.SSLError as exc:
            raise GlpiConnectionError(
                f"Erreur TLS/SSL avec {self.api_url} : {exc}\n"
                "Si le certificat est auto-signé, indiquez le chemin du certificat de l'autorité "
                "dans verify_ssl, ou désactivez la vérification (verify_ssl = false / --insecure)."
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise GlpiConnectionError(
                f"Délai dépassé ({self.timeout:g} s) en contactant {self.api_url}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise GlpiConnectionError(f"Impossible de joindre GLPI à l'adresse {self.api_url}") from exc
        except requests.RequestException as exc:
            raise GlpiConnectionError(f"Erreur réseau en contactant {self.api_url} : {exc}") from exc

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Envoie une requête authentifiée, en renouvelant le jeton si nécessaire."""
        if time.monotonic() >= self._token_expires_at - TOKEN_EXPIRY_MARGIN:
            self.authenticate()
        response = self._send(method, path, **kwargs)
        if response.status_code == 401:
            # Jeton expiré ou révoqué côté serveur : une seule nouvelle tentative
            log.debug("Jeton refusé, nouvelle authentification")
            self.authenticate()
            response = self._send(method, path, **kwargs)
        _raise_for_status(response)
        return response

    def _paginate(self, path: str, params: dict[str, Any], page_size: int = PAGE_SIZE) -> Iterator[dict[str, Any]]:
        """Parcourt toutes les pages d'une collection (paramètres start / limit)."""
        params = {key: value for key, value in params.items() if value}
        start = 0
        while True:
            response = self._request("GET", path, params={**params, "start": start, "limit": page_size})
            items = _json(response)
            if not isinstance(items, list):
                raise GlpiApiError(f"Réponse inattendue pour {path} : une liste était attendue")
            yield from items
            start += len(items)
            total = _content_range_total(response)
            if not items or (total is not None and start >= total) or (total is None and len(items) < page_size):
                return

    # ------------------------------------------------------------------
    #  Base de connaissance
    # ------------------------------------------------------------------

    def get_article(self, article_id: int) -> dict[str, Any]:
        try:
            return _json(self._request("GET", f"/Knowledgebase/Article/{int(article_id)}"))
        except GlpiNotFoundError:
            raise GlpiNotFoundError(
                f"Article {article_id} introuvable (inexistant ou non visible avec ce compte)"
            ) from None

    def iter_articles(self, rsql_filter: str | None = None, sort: str = "id:asc") -> Iterator[dict[str, Any]]:
        """Parcourt les articles visibles, éventuellement filtrés (syntaxe RSQL)."""
        return self._paginate("/Knowledgebase/Article", {"filter": rsql_filter, "sort": sort})

    def iter_articles_by_ids(self, article_ids: Iterable[int], chunk_size: int = 50) -> Iterator[dict[str, Any]]:
        """Relit des articles par lots d'identifiants (filtre ``id=in=(…)``)."""
        ids = [int(article_id) for article_id in article_ids]
        for index in range(0, len(ids), chunk_size):
            chunk = ",".join(str(article_id) for article_id in ids[index:index + chunk_size])
            yield from self.iter_articles(f"id=in=({chunk})")

    def count_articles(self) -> int | None:
        """Nombre d'articles visibles, si le serveur l'indique (en-tête Content-Range)."""
        response = self._request("GET", "/Knowledgebase/Article", params={"limit": 1})
        return _content_range_total(response)

    def list_categories(self) -> list[dict[str, Any]]:
        return list(self._paginate("/Knowledgebase/Category", {"sort": "id:asc"}))

    # ------------------------------------------------------------------
    #  Documents et configuration
    # ------------------------------------------------------------------

    def get_document(self, document_id: int) -> dict[str, Any]:
        try:
            return _json(self._request("GET", f"/Management/Document/{int(document_id)}"))
        except GlpiNotFoundError:
            raise GlpiNotFoundError(f"Document {document_id} introuvable") from None

    def download_document(self, document_id: int) -> tuple[bytes, str]:
        """Télécharge le contenu d'un document. Retourne ``(contenu, type MIME)``."""
        try:
            response = self._request(
                "GET", f"/Management/Document/{int(document_id)}/Download", headers={"Accept": "*/*"}
            )
        except GlpiNotFoundError:
            raise GlpiNotFoundError(f"Document {document_id} introuvable") from None
        mime = response.headers.get("Content-Type", "").split(";")[0].strip()
        if not mime or mime == "application/octet-stream":
            mime = self.get_document(document_id).get("mime") or "application/octet-stream"
        return response.content, mime

    def get_public_url(self) -> str | None:
        """URL publique de GLPI (paramètre ``url_base``), si le compte peut la lire.

        Elle sert à reconnaître les liens et images internes à GLPI dans les
        articles, qui peuvent pointer vers une autre adresse que celle de l'API
        (proxy, conteneur, redirection de port…).
        """
        try:
            data = _json(self._request("GET", "/Setup/Config/core/url_base"))
        except GlpiError as exc:
            log.debug("Lecture de url_base impossible : %s", exc)
            return None
        value = data.get("value") if isinstance(data, dict) else None
        return str(value).rstrip("/") if value else None


# ----------------------------------------------------------------------
#  Fonctions utilitaires
# ----------------------------------------------------------------------

def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise GlpiApiError(
            f"Réponse non JSON reçue de {response.url} (HTTP {response.status_code}) : {response.text[:200]!r}",
            response.status_code,
        ) from exc


def _content_range_total(response: requests.Response) -> int | None:
    """Extrait le nombre total d'éléments de l'en-tête ``Content-Range: 0-99/250``."""
    _, _, total = response.headers.get("Content-Range", "").rpartition("/")
    return int(total) if total.isdigit() else None


def _error_message(response: requests.Response) -> str:
    """Construit un message lisible à partir d'une réponse d'erreur de GLPI."""
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()[:200] or response.reason or "réponse vide"
    if not isinstance(data, dict):
        return str(data)[:200]
    if "error" in data:  # format des erreurs OAuth2
        description = data.get("error_description") or data.get("message")
        return f"{data['error']} : {description}" if description else str(data["error"])
    title = data.get("title") or data.get("status") or ""
    detail = data.get("detail")
    if isinstance(detail, dict):
        detail = ", ".join(f"{key} : {value}" for key, value in detail.items())
    if detail:
        return f"{title} ({detail})" if title else str(detail)
    return str(title) or str(data)[:200]


def _auth_error_message(response: requests.Response) -> str:
    try:
        code = response.json().get("error")
    except (ValueError, AttributeError):
        code = None
    hint = _OAUTH_HINTS.get(code or "")
    prefix = f"Authentification GLPI refusée : {hint}" if hint else "Authentification GLPI refusée"
    return f"{prefix} ({_error_message(response)})"


def _raise_for_status(response: requests.Response) -> None:
    if response.ok:
        return
    status = response.status_code
    message = _error_message(response)
    if status == 401:
        raise GlpiAuthError(f"Accès non autorisé : {message}")
    if status == 403:
        raise GlpiApiError(f"Droits insuffisants : {message}", status)
    if status == 404:
        raise GlpiNotFoundError(message)
    raise GlpiApiError(f"Erreur HTTP {status} : {message}", status)
