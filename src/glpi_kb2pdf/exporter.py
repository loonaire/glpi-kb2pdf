"""Export des articles : préparation du contenu, rendu et écriture des fichiers."""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from glpi_kb2pdf.content import article_categories, article_title, build_document, parse_date, prepare_content
from glpi_kb2pdf.glpi import GlpiApiError, GlpiAuthError, GlpiClient, GlpiConnectionError, GlpiNotFoundError
from glpi_kb2pdf.render import RendererUnavailable, default_css, html_to_pdf

log = logging.getLogger(__name__)

FILENAME_FIELDS = ("id", "slug", "name", "date", "category")
NO_CATEGORY = "Sans catégorie"

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}


# ----------------------------------------------------------------------
#  Noms de fichiers
# ----------------------------------------------------------------------

def sanitize_filename(value: str, max_length: int = 120) -> str:
    """Rend une chaîne utilisable comme nom de fichier sous Windows comme sous Linux."""
    value = _INVALID_FILENAME_CHARS.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if value.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    return value[:max_length].rstrip(" .") or "_"


def slugify(value: str, max_length: int = 80) -> str:
    """« Installer Docker sur Debian 13 » → « installer-docker-sur-debian-13 »."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value[:max_length].strip("-") or "article"


def filename_fields(article: Mapping[str, Any], category_names: Mapping[int, str]) -> dict[str, str]:
    title = article_title(article)
    modified = parse_date(article.get("date_mod")) or parse_date(article.get("date_creation"))
    categories = article_categories(article, category_names)
    # La catégorie devient une arborescence de dossiers : « Parent > Enfant » → Parent/Enfant
    category = "/".join(sanitize_filename(part) for part in (categories[0] if categories else NO_CATEGORY).split(" > "))
    return {
        "id": str(article.get("id")),
        "slug": slugify(title),
        "name": sanitize_filename(title),
        "date": f"{modified:%Y-%m-%d}" if modified else "",
        "category": category,
    }


def render_relative_path(template: str, fields: Mapping[str, str], extension: str) -> Path:
    """Applique le modèle de nom de fichier et ajoute l'extension."""
    raw = template.format(**fields)
    parts = [sanitize_filename(part) for part in re.split(r"[\\/]", raw) if part.strip() not in ("", ".", "..")]
    if not parts:
        parts = [fields.get("slug") or "article"]
    name = parts[-1]
    if not name.lower().endswith(f".{extension}"):
        name = f"{name}.{extension}"
    return Path(*parts[:-1], name)


def validate_filename_template(template: str) -> None:
    sample = dict.fromkeys(FILENAME_FIELDS, "x")
    try:
        template.format(**sample)
    except (KeyError, IndexError, ValueError, AttributeError) as exc:
        raise ValueError(
            f"modèle de nom de fichier invalide « {template} » ({exc}). "
            f"Champs disponibles : {', '.join('{' + name + '}' for name in FILENAME_FIELDS)}"
        ) from None


# ----------------------------------------------------------------------
#  Export
# ----------------------------------------------------------------------

@dataclass
class ExportOptions:
    output_dir: Path = Path(".")
    output_file: Path | None = None  # chemin exact, pour l'export d'un seul article
    filename: str = "{id}-{slug}"
    format: str = "pdf"
    header: bool = True
    remote_images: bool = True
    css_files: Sequence[Path] = ()
    skip_unchanged: bool = False


@dataclass
class ExportResult:
    article_id: int
    title: str = ""
    status: str = "ok"  # "ok", "skipped" ou "error"
    path: Path | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)


class Exporter:
    def __init__(self, client: GlpiClient, options: ExportOptions):
        self.client = client
        self.options = options
        self._used_paths: set[Path] = set()

    @cached_property
    def category_names(self) -> dict[int, str]:
        """Nom complet (« Parent > Enfant ») de chaque catégorie, par identifiant."""
        try:
            categories = self.client.list_categories()
        except (GlpiNotFoundError, GlpiApiError) as exc:
            log.warning("Catégories indisponibles, seuls leurs noms courts seront affichés : %s", exc)
            return {}
        return {cat["id"]: cat.get("completename") or cat.get("name") or "" for cat in categories}

    @cached_property
    def internal_urls(self) -> list[str]:
        """Adresses de GLPI : les liens qui y pointent sont retirés du PDF."""
        urls = [self.client.root_url]
        public_url = self.client.get_public_url()
        if public_url and public_url not in urls:
            urls.append(public_url)
        return urls

    @cached_property
    def _inline_css(self) -> str:
        """CSS intégrée aux exports HTML (le PDF reçoit les feuilles séparément)."""
        sheets = [default_css()] + [path.read_text(encoding="utf-8") for path in self.options.css_files]
        return "\n".join(sheets)

    def target_path(self, article: Mapping[str, Any]) -> Path:
        if self.options.output_file is not None:
            return self.options.output_file
        fields = filename_fields(article, self.category_names)
        path = self.options.output_dir / render_relative_path(self.options.filename, fields, self.options.format)
        if path in self._used_paths:
            # deux articles donneraient le même fichier : on ajoute l'identifiant
            path = path.with_name(f"{path.stem}-{article.get('id')}{path.suffix}")
        self._used_paths.add(path)
        return path

    def export_ids(self, article_ids: Iterable[int]) -> Iterator[ExportResult]:
        for article_id in article_ids:
            try:
                article = self.client.get_article(article_id)
            except (GlpiNotFoundError, GlpiApiError) as exc:
                yield ExportResult(article_id, status="error", message=str(exc))
                continue
            yield self.export_article(article)

    def export_articles(self, articles: Iterable[Mapping[str, Any]]) -> Iterator[ExportResult]:
        for article in articles:
            yield self.export_article(article)

    def export_article(self, article: Mapping[str, Any]) -> ExportResult:
        """Exporte un article. Les erreurs propres à l'article sont renvoyées dans le
        résultat ; celles qui empêchent tout export (connexion, moteur PDF) sont levées."""
        result = ExportResult(int(article["id"]), article_title(article))
        result.path = self.target_path(article)

        if self.options.skip_unchanged and _is_up_to_date(result.path, article):
            result.status = "skipped"
            result.message = "inchangé depuis le dernier export"
            return result

        documents: dict[int, tuple[bytes, str]] = {}

        def fetch_document(doc_id: int) -> tuple[bytes, str]:
            if doc_id not in documents:
                documents[doc_id] = self.client.download_document(doc_id)
            return documents[doc_id]

        try:
            body, result.warnings = prepare_content(
                str(article.get("content") or ""),
                fetch_document,
                internal_urls=self.internal_urls,
                remote_images=self.options.remote_images,
            )
            document_options = {"category_names": self.category_names, "header": self.options.header}
            if self.options.format == "html":
                html = build_document(article, body, inline_css=self._inline_css, **document_options)
                data = html.encode("utf-8")
            else:
                data = html_to_pdf(build_document(article, body, **document_options), self.options.css_files)
            _write_atomic(result.path, data)
        except PermissionError as exc:
            result.status = "error"
            result.message = f"écriture impossible ({exc}) : le fichier est-il ouvert dans un autre programme ?"
        except OSError as exc:
            result.status = "error"
            result.message = f"écriture impossible : {exc}"
        except (GlpiNotFoundError, GlpiApiError) as exc:
            result.status = "error"
            result.message = str(exc)
        except (RendererUnavailable, GlpiConnectionError, GlpiAuthError):
            raise
        except Exception as exc:  # erreur de rendu inattendue : les autres articles sont tout de même exportés
            log.debug("Échec de l'export de l'article %s", result.article_id, exc_info=True)
            result.status = "error"
            result.message = f"{type(exc).__name__} : {exc}"
        return result


def _is_up_to_date(path: Path, article: Mapping[str, Any]) -> bool:
    modified = parse_date(article.get("date_mod"))
    if modified is None or not path.is_file():
        return False
    return path.stat().st_mtime >= modified.timestamp()


def _write_atomic(path: Path, data: bytes) -> None:
    """Écrit dans un fichier temporaire puis le renomme : pas de fichier à moitié écrit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
