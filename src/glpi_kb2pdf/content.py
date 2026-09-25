"""Préparation du HTML d'un article avant sa conversion en PDF."""

from __future__ import annotations

import base64
import html
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from glpi_kb2pdf import __version__
from glpi_kb2pdf.glpi import GlpiError

# Fonction qui renvoie (contenu, type MIME) d'un document GLPI à partir de son id
DocumentFetcher = Callable[[int], tuple[bytes, str]]

# Schémas de liens qui ont un sens dans un PDF ; les autres (liens relatifs,
# javascript:…) ne fonctionneraient pas une fois le document sorti de GLPI.
_PORTABLE_SCHEMES = ("http", "https", "ftp", "ftps", "mailto", "tel")
_EMBED_TAGS = ["iframe", "video", "audio", "embed", "object"]


def document_id_from_url(url: str) -> int | None:
    """Retourne l'identifiant du document GLPI désigné par l'URL, sinon ``None``.

    Les images collées dans un article pointent vers
    ``<url GLPI>/front/document.send.php?docid=12&itemtype=KnowbaseItem&items_id=3``.
    L'hôte n'est volontairement pas vérifié : c'est l'URL publique de GLPI, qui
    peut différer de l'adresse utilisée pour joindre l'API.
    """
    parts = urlsplit(url)
    if parts.path.rsplit("/", 1)[-1] != "document.send.php":
        return None
    values = parse_qs(parts.query).get("docid", [])
    return int(values[0]) if values and values[0].isdigit() else None


def prepare_content(
    content: str,
    fetch_document: DocumentFetcher,
    *,
    internal_urls: Iterable[str] = (),
    remote_images: bool = True,
) -> tuple[str, list[str]]:
    """Rend le HTML d'un article autonome et adapté à l'impression.

    - les images stockées dans GLPI sont téléchargées via l'API et intégrées
      au document (data URI) ;
    - les images externes sont laissées au moteur PDF, ou remplacées par une
      mention si ``remote_images`` est faux ;
    - les liens vers GLPI (inutilisables hors de GLPI) et les liens entourant
      une image sont retirés, leur contenu est conservé ;
    - les contenus intégrés (iframe, vidéo…) sont remplacés par un lien.

    Retourne le HTML transformé et la liste des avertissements.
    """
    soup = BeautifulSoup(content or "", "html.parser")
    warnings: list[str] = []
    bases = [_split_base(url) for url in internal_urls if url]

    for img in soup.find_all("img"):
        _process_image(soup, img, fetch_document, remote_images, warnings)

    for embed in soup.find_all(_EMBED_TAGS):
        _replace_embed(soup, embed)

    for link in soup.find_all("a"):
        href = str(link.get("href") or "").strip()
        if link.find("img") is not None or _is_unusable_link(href, bases):
            link.unwrap()

    return str(soup), warnings


def _process_image(
    soup: BeautifulSoup,
    img: Tag,
    fetch_document: DocumentFetcher,
    remote_images: bool,
    warnings: list[str],
) -> None:
    src = str(img.get("src") or "").strip()
    if not src or src.startswith("data:"):
        return  # image absente ou déjà intégrée au HTML

    doc_id = document_id_from_url(src)
    if doc_id is not None:
        try:
            data, mime = fetch_document(doc_id)
        except GlpiError as exc:
            warnings.append(f"image non récupérée (document {doc_id}) : {exc}")
            img.replace_with(_placeholder(soup, f"Image non disponible (document GLPI n° {doc_id})"))
            return
        img["src"] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        for attr in ("srcset", "sizes", "loading"):
            img.attrs.pop(attr, None)
        return

    if urlsplit(src).scheme in ("http", "https"):
        if not remote_images:
            img.replace_with(_placeholder(soup, f"Image externe non incluse : {src}"))
        # sinon le moteur PDF téléchargera l'image lui-même
        return

    warnings.append(f"image ignorée, adresse non exploitable hors de GLPI : {src}")
    img.replace_with(_placeholder(soup, "Image non disponible"))


def _placeholder(soup: BeautifulSoup, text: str) -> Tag:
    span = soup.new_tag("span", attrs={"class": "kb-missing"})
    span.string = f"[{text}]"
    return span


def _replace_embed(soup: BeautifulSoup, tag: Tag) -> None:
    src = str(tag.get("src") or tag.get("data") or "")
    if not src and (source := tag.find("source")) is not None:
        src = str(source.get("src") or "")
    paragraph = soup.new_tag("p", attrs={"class": "kb-embed"})
    if urlsplit(src).scheme in ("http", "https"):
        paragraph.append("Contenu intégré : ")
        link = soup.new_tag("a", href=src)
        link.string = src
        paragraph.append(link)
    else:
        paragraph.string = "[Contenu intégré non reproductible dans le PDF]"
    tag.replace_with(paragraph)


def _split_base(url: str) -> tuple[str, str]:
    parts = urlsplit(url.rstrip("/"))
    return parts.netloc.lower(), parts.path


def _is_unusable_link(href: str, bases: list[tuple[str, str]]) -> bool:
    """Indique si un lien doit être retiré : lien vers GLPI ou non portable."""
    if not href or href.startswith("#"):
        return False  # ancre interne au document, conservée
    parts = urlsplit(href)
    if parts.scheme not in _PORTABLE_SCHEMES:
        return True
    if document_id_from_url(href) is not None:
        return True
    netloc = parts.netloc.lower()
    return any(netloc == base_netloc and parts.path.startswith(base_path) for base_netloc, base_path in bases)


# ----------------------------------------------------------------------
#  Document HTML complet
# ----------------------------------------------------------------------

_DOCUMENT_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>{title}</title>
{meta}
{style}
</head>
<body{body_attrs}>
{header}
<main class="kb-content">
{body}
</main>
</body>
</html>
"""


def parse_date(value: str | None) -> datetime | None:
    """Convertit une date ISO 8601 renvoyée par l'API, ou ``None`` si absente/invalide."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def article_title(article: Mapping[str, Any]) -> str:
    return str(article.get("name") or "").strip() or f"Article {article.get('id')}"


def article_categories(article: Mapping[str, Any], category_names: Mapping[int, str]) -> list[str]:
    """Noms complets (« Parent > Enfant ») des catégories de l'article."""
    names = []
    for category in article.get("categories") or []:
        name = category_names.get(category.get("id")) or category.get("name")
        if name:
            names.append(str(name))
    return names


def build_document(
    article: Mapping[str, Any],
    body: str,
    *,
    category_names: Mapping[int, str] | None = None,
    header: bool = True,
    inline_css: str = "",
) -> str:
    """Construit le document HTML final : métadonnées, en-tête et contenu."""
    esc = html.escape
    title = article_title(article)
    categories = article_categories(article, category_names or {})
    author = str((article.get("user") or {}).get("name") or "")
    created = parse_date(article.get("date_creation"))
    modified = parse_date(article.get("date_mod"))

    meta = [f'<meta name="generator" content="glpi-kb2pdf {esc(__version__)}">']
    if author:
        meta.append(f'<meta name="author" content="{esc(author)}">')
    if categories:
        meta.append(f'<meta name="keywords" content="{esc(", ".join(categories))}">')
    if created:
        meta.append(f'<meta name="dcterms.created" content="{created.isoformat()}">')
    if modified:
        meta.append(f'<meta name="dcterms.modified" content="{modified.isoformat()}">')

    header_html = ""
    if header:
        parts = ['<header class="kb-header">', f'<h1 class="kb-title">{esc(title)}</h1>']
        if categories:
            label = "Catégories" if len(categories) > 1 else "Catégorie"
            parts.append(f'<p class="kb-categories">{_labelled(label, " · ".join(categories))}</p>')
        # Trois emplacements fixes (gauche, centre, droite) : chaque information
        # garde sa place même si une autre est absente.
        slots = [
            ("left", "Créé le", _format_date(created)),
            ("center", "Auteur", author),
            ("right", "Mis à jour le", _format_date(modified or created)),
        ]
        parts.append('<div class="kb-meta">')
        parts += [
            f'  <div class="kb-meta-{position}">{_labelled(label, value) if value else ""}</div>'
            for position, label, value in slots
        ]
        parts += ["</div>", "</header>"]
        header_html = "\n".join(parts)

    # Titre et auteur repris dans le pied de page de chaque page (voir default.css)
    body_attrs = f' data-title="{esc(title)}"' + (f' data-author="{esc(author)}"' if author else "")

    return _DOCUMENT_TEMPLATE.format(
        title=esc(title),
        meta="\n".join(meta),
        style=f"<style>\n{inline_css}\n</style>" if inline_css else "",
        body_attrs=body_attrs,
        header=header_html,
        body=body,
    )


def _labelled(label: str, value: str) -> str:
    return f'<span class="kb-label">{html.escape(label)}</span> {html.escape(value)}'


def _format_date(value: datetime | None) -> str:
    return f"{value.astimezone():%d/%m/%Y}" if value else ""
