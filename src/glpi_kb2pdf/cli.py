"""Interface en ligne de commande de glpi-kb2pdf."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import logging
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from glpi_kb2pdf import __version__
from glpi_kb2pdf.config import (
    CONFIG_FILENAME,
    CONFIG_TEMPLATE,
    OUTPUT_FORMATS,
    Config,
    ConfigError,
    load_config,
    missing_connection_settings,
)
from glpi_kb2pdf.content import article_categories, article_title, parse_date
from glpi_kb2pdf.exporter import ExportOptions, Exporter, ExportResult, validate_filename_template
from glpi_kb2pdf.glpi import GlpiClient, GlpiError
from glpi_kb2pdf.render import RendererUnavailable, weasyprint_version

log = logging.getLogger("glpi_kb2pdf")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


class UsageError(Exception):
    """Arguments de la ligne de commande incohérents."""


# ----------------------------------------------------------------------
#  Analyse des arguments
# ----------------------------------------------------------------------

class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):  # type: ignore[no-untyped-def]
        return super().add_usage(usage, actions, groups, "utilisation : " if prefix is None else prefix)


class _Parser(argparse.ArgumentParser):
    """ArgumentParser dont les textes générés automatiquement sont en français."""

    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("formatter_class", _HelpFormatter)
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "arguments"
        self._optionals.title = "options"
        self.add_argument("-h", "--help", action="help", help="affiche cette aide")

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog} : erreur : {message}\n")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    group = common.add_argument_group("options générales")
    group.add_argument("-c", "--config", type=Path, metavar="FICHIER",
                       help="fichier de configuration (par défaut : ./glpi-kb2pdf.toml, puis celui de l'utilisateur)")
    group.add_argument("-v", "--verbose", action="count", default=0,
                       help="affiche le détail des opérations (-vv : ajoute les messages du moteur PDF)")
    group.add_argument("-q", "--quiet", action="store_true", help="n'affiche que les erreurs")

    connection = argparse.ArgumentParser(add_help=False)
    group = connection.add_argument_group("connexion à GLPI (prioritaire sur la configuration)")
    group.add_argument("--url", help="URL de GLPI ou de son API")
    group.add_argument("-u", "--username", help="nom d'utilisateur GLPI")
    group.add_argument("-k", "--insecure", action="store_true", help="ne pas vérifier le certificat TLS du serveur")

    selection = argparse.ArgumentParser(add_help=False)
    group = selection.add_argument_group("sélection des articles")
    group.add_argument("--category", type=int, action="append", metavar="ID",
                       help="articles de la catégorie ID, sous-catégories incluses (option répétable)")
    group.add_argument("--search", metavar="TEXTE", help="articles dont le titre ou le contenu contient TEXTE")
    group.add_argument("--filter", metavar="RSQL",
                       help="filtre RSQL de l'API GLPI, par exemple \"date_mod=gt=2026-01-01\"")

    parser = _Parser(
        prog="glpi-kb2pdf",
        description="Exporte les articles de la base de connaissance GLPI en PDF.",
        epilog="Aide d'une commande : glpi-kb2pdf COMMANDE --help",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}", help="affiche la version")
    commands = parser.add_subparsers(dest="command", metavar="COMMANDE", required=True)

    sub = commands.add_parser("init", parents=[common], help="crée un fichier de configuration à compléter",
                              description="Crée un fichier de configuration commenté, à compléter.")
    sub.add_argument("path", nargs="?", type=Path, default=Path(CONFIG_FILENAME), metavar="FICHIER",
                     help=f"fichier à créer (par défaut : ./{CONFIG_FILENAME})")
    sub.add_argument("--force", action="store_true", help="remplace le fichier s'il existe")

    commands.add_parser("check", parents=[common, connection],
                        help="vérifie la configuration, la connexion à GLPI et le moteur PDF",
                        description="Vérifie la configuration, la connexion à GLPI et le moteur PDF.")

    sub = commands.add_parser("list", parents=[common, connection, selection], help="liste les articles",
                              description="Liste les articles de la base de connaissance visibles par le compte.")
    sub.add_argument("--json", action="store_true", help="sortie au format JSON")

    sub = commands.add_parser("categories", parents=[common, connection], help="liste les catégories",
                              description="Liste les catégories de la base de connaissance.")
    sub.add_argument("--json", action="store_true", help="sortie au format JSON")

    sub = commands.add_parser(
        "export", parents=[common, connection, selection], help="exporte des articles en PDF",
        description="Exporte des articles en PDF (ou en HTML autonome).",
        epilog="Exemples :\n"
               "  glpi-kb2pdf export 12\n"
               "  glpi-kb2pdf export 12 -o procedure.pdf\n"
               "  glpi-kb2pdf export 3 7 12 -o exports/\n"
               "  glpi-kb2pdf export --category 4 --filename \"{category}/{id}-{slug}\"\n"
               "  glpi-kb2pdf export --all --skip-unchanged",
    )
    sub.add_argument("ids", nargs="*", type=int, metavar="ID", help="identifiants des articles à exporter")
    sub.add_argument("--all", action="store_true", help="exporte tous les articles visibles")
    group = sub.add_argument_group("sortie")
    group.add_argument("-o", "--output", type=Path, metavar="CHEMIN",
                       help="dossier de destination, ou nom du fichier si un seul article est exporté")
    group.add_argument("--format", choices=OUTPUT_FORMATS, help="format de sortie (par défaut : pdf)")
    group.add_argument("--filename", metavar="MODELE",
                       help="modèle de nom de fichier : {id} {slug} {name} {date} {category} (défaut : \"{id}-{slug}\")")
    group.add_argument("--css", type=Path, action="append", metavar="FICHIER",
                       help="feuille de style supplémentaire (option répétable)")
    group.add_argument("--no-header", dest="header", action="store_false", default=None,
                       help="n'ajoute pas l'en-tête (titre, catégories, dates, auteur)")
    group.add_argument("--no-remote-images", dest="remote_images", action="store_false", default=None,
                       help="n'inclut pas les images hébergées hors de GLPI")
    group.add_argument("--skip-unchanged", action="store_true",
                       help="ignore les articles dont le fichier existant est plus récent que la dernière modification")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.quiet)
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "init": cmd_init,
        "check": cmd_check,
        "list": cmd_list,
        "categories": cmd_categories,
        "export": cmd_export,
    }
    try:
        return handlers[args.command](args)
    except (UsageError, ConfigError) as exc:
        log.error("%s", exc)
        return EXIT_USAGE
    except (GlpiError, RendererUnavailable) as exc:
        log.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.error("interrompu")
        return 130


# ----------------------------------------------------------------------
#  Commandes
# ----------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    path: Path = args.path
    if path.exists() and not args.force:
        raise UsageError(f"{path} existe déjà (--force pour le remplacer)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)  # le fichier peut contenir des secrets
    _out(args, f"Configuration créée : {path.resolve()}")
    _out(args, "Complétez la section [glpi], puis vérifiez avec : glpi-kb2pdf check")
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    config = _load_config(args)
    ok = True

    def report(label: str, success: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and success
        status = "OK" if success else "ÉCHEC"
        print(f"{label:<14} {status:<6} {detail}")

    source = str(config.source) if config.source else "aucun fichier (variables d'environnement / options)"
    print(f"{'Configuration':<14} {'':<6} {source}")

    missing = missing_connection_settings(config.glpi)
    if missing:
        report("Paramètres", False, f"manquants : {', '.join(missing)}")
    else:
        try:
            with _connect(config) as client:
                client.authenticate()
                report("Connexion", True, f"{client.api_url} (utilisateur {config.glpi.username})")
                count = client.count_articles()
                report("Articles", True, f"{count} article(s) visible(s)" if count is not None else "accessibles")
                public_url = client.get_public_url()
                print(f"{'URL publique':<14} {'':<6} {public_url or 'non lisible avec ce compte (sans conséquence)'}")
        except (GlpiError, ConfigError) as exc:
            report("Connexion", False, str(exc))

    try:
        report("Moteur PDF", True, f"WeasyPrint {weasyprint_version()}")
    except RendererUnavailable as exc:
        report("Moteur PDF", False, str(exc))
    return EXIT_OK if ok else EXIT_ERROR


def cmd_list(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with _connect(config) as client:
        categories = client.list_categories()
        names = {cat["id"]: cat.get("completename") or cat.get("name") or "" for cat in categories}
        rows = [
            {
                "id": article["id"],
                "name": article_title(article),
                "categories": article_categories(article, names),
                "date_mod": article.get("date_mod"),
                "author": (article.get("user") or {}).get("name"),
            }
            for article in _selected_articles(client, args, categories)
        ]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    table = [
        (str(row["id"]), _shorten(row["name"], 60), _shorten(", ".join(row["categories"]), 40), _format_date(row["date_mod"]))
        for row in rows
    ]
    _print_table(("ID", "Titre", "Catégories", "Mis à jour"), table)
    _out(args, f"\n{len(rows)} article(s)")
    return EXIT_OK


def cmd_categories(args: argparse.Namespace) -> int:
    config = _load_config(args)
    with _connect(config) as client:
        categories = client.list_categories()
    rows = sorted(
        ({"id": cat["id"], "name": cat.get("completename") or cat.get("name") or "",
          "parent": (cat.get("parent") or {}).get("id")} for cat in categories),
        key=lambda row: row["name"].lower(),
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    _print_table(("ID", "Catégorie"), [(str(row["id"]), row["name"]) for row in rows])
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    has_selection = bool(args.all or args.category or args.search or args.filter)
    if args.ids and has_selection:
        raise UsageError("indiquez soit des identifiants d'articles, soit --all / --category / --search / --filter")
    if not args.ids and not has_selection:
        raise UsageError("précisez les articles à exporter : des identifiants, ou --all / --category / --search / --filter")

    config = _load_config(args, {
        "export.format": args.format,
        "export.filename": args.filename,
        "export.css": args.css,
        "export.header": args.header,
        "export.remote_images": args.remote_images,
    })
    settings = config.export
    try:
        validate_filename_template(settings.filename)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    for css in settings.css:
        if not css.is_file():
            raise ConfigError(f"feuille de style introuvable : {css}")

    options = ExportOptions(
        output_dir=settings.output_dir,
        filename=settings.filename,
        format=settings.format,
        header=settings.header,
        remote_images=settings.remote_images,
        css_files=settings.css,
        skip_unchanged=args.skip_unchanged,
    )
    if args.output is not None:
        suffix = args.output.suffix.lower()
        if suffix in (".pdf", ".html", ".htm") and not args.output.is_dir():
            if len(args.ids) != 1:
                raise UsageError("un nom de fichier de sortie n'est possible que pour l'export d'un seul article ; "
                                 "indiquez un dossier pour en exporter plusieurs")
            # le format se déduit de l'extension, sauf s'il est imposé par --format
            implied_format = "pdf" if suffix == ".pdf" else "html"
            if args.format and args.format != implied_format:
                raise UsageError(f"l'extension de {args.output} ne correspond pas au format demandé ({args.format})")
            options.format = implied_format
            options.output_file = args.output
        else:
            options.output_dir = args.output

    with _connect(config) as client:
        exporter = Exporter(client, options)
        if args.ids:
            results = exporter.export_ids(args.ids)
        else:
            categories = client.list_categories() if args.category else []
            results = exporter.export_articles(_selected_articles(client, args, categories))
        summary = _report_results(args, results)

    exported, skipped, failed = summary
    _out(args, f"\n{exported} exporté(s), {skipped} ignoré(s), {failed} en échec")
    return EXIT_ERROR if failed else EXIT_OK


# ----------------------------------------------------------------------
#  Fonctions utilitaires
# ----------------------------------------------------------------------

def _load_config(args: argparse.Namespace, extra: dict[str, Any] | None = None) -> Config:
    overrides = {
        "glpi.url": args.url,
        "glpi.username": args.username,
        "glpi.verify_ssl": False if args.insecure else None,
        **(extra or {}),
    }
    config = load_config(args.config, overrides)
    if config.source:
        log.debug("Configuration chargée depuis %s", config.source)
    return config


def _connect(config: Config) -> GlpiClient:
    settings = config.glpi
    missing = missing_connection_settings(settings)
    if missing:
        raise ConfigError(
            f"paramètres de connexion manquants : {', '.join(missing)}.\n"
            "Renseignez-les dans le fichier de configuration (glpi-kb2pdf init pour le créer) "
            "ou par les variables d'environnement GLPI_URL, GLPI_CLIENT_ID, GLPI_CLIENT_SECRET, GLPI_USERNAME."
        )
    password = settings.password
    if not password:
        if not _is_interactive():
            raise ConfigError("mot de passe manquant : définissez GLPI_PASSWORD ou « password » dans la configuration")
        password = getpass.getpass(f"Mot de passe GLPI de {settings.username} : ")
    try:
        return GlpiClient(
            settings.url,
            settings.client_id,
            settings.client_secret,
            settings.username,
            password,
            verify=settings.verify_ssl,
            timeout=settings.timeout,
            entity=settings.entity,
            recursive=settings.recursive,
        )
    except ValueError as exc:  # URL invalide
        raise ConfigError(str(exc)) from None


def _is_interactive() -> bool:
    """Indique si l'entrée standard est une vraie console, où le mot de passe peut être saisi."""
    if sys.stdin is None or not sys.stdin.isatty():
        return False
    if os.name != "nt":
        return True
    # Sous Windows, isatty() est aussi vrai pour le périphérique NUL (< nul, tâche
    # planifiée…) : getpass attendrait alors indéfiniment une saisie au clavier.
    import ctypes
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    except (OSError, ValueError):
        return False
    mode = ctypes.c_uint32()
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def rsql_quote(value: str) -> str:
    """Met une valeur entre guillemets pour un filtre RSQL.

    GLPI ne gère pas l'échappement : on choisit le type de guillemet absent de la
    valeur, et à défaut les guillemets doubles sont remplacés par un joker.
    """
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace('"', "*") + '"'


def category_descendants(categories: Iterable[dict[str, Any]], root_id: int) -> set[int]:
    """Identifiant de la catégorie et de toutes ses sous-catégories (vide si inconnue)."""
    children: dict[int | None, list[int]] = defaultdict(list)
    known = set()
    for category in categories:
        known.add(category["id"])
        children[(category.get("parent") or {}).get("id")].append(category["id"])
    if root_id not in known:
        return set()
    found: set[int] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current not in found:
            found.add(current)
            stack.extend(children.get(current, []))
    return found


def _selected_articles(
    client: GlpiClient, args: argparse.Namespace, categories: list[dict[str, Any]]
) -> Iterable[dict[str, Any]]:
    articles = client.iter_articles(_selection_filter(args, categories))
    if not args.category:
        return articles
    # Avec un filtre sur les catégories, l'API ne renvoie que les catégories qui
    # correspondent au filtre : les articles trouvés sont relus pour les avoir toutes.
    return client.iter_articles_by_ids([article["id"] for article in articles])


def _selection_filter(args: argparse.Namespace, categories: list[dict[str, Any]]) -> str | None:
    """Traduit les options de sélection en filtre RSQL (conditions combinées par ET)."""
    clauses = []
    if args.category:
        ids: set[int] = set()
        for category_id in args.category:
            descendants = category_descendants(categories, category_id)
            if not descendants:
                raise UsageError(f"catégorie {category_id} introuvable (voir : glpi-kb2pdf categories)")
            ids |= descendants
        clauses.append(f"categories.id=in=({','.join(str(i) for i in sorted(ids))})")
    if args.search:
        pattern = rsql_quote(f"*{args.search}*")
        clauses.append(f"(name=ilike={pattern},content=ilike={pattern})")
    if args.filter:
        clauses.append(f"({args.filter})")
    return ";".join(clauses) or None


_STATUS_LABELS = {"ok": "OK", "skipped": "IGNORÉ", "error": "ÉCHEC"}


def _report_results(args: argparse.Namespace, results: Iterable[ExportResult]) -> tuple[int, int, int]:
    counts = {"ok": 0, "skipped": 0, "error": 0}
    for result in results:
        counts[result.status] += 1
        label = _STATUS_LABELS[result.status]
        title = f" {result.title}" if result.title else ""
        if result.status == "error":
            log.error("#%s%s : %s", result.article_id, title, result.message)
            continue
        detail = _display_path(result.path) + (f" ({result.message})" if result.message else "")
        _out(args, f"{label:<7} #{result.article_id:<5}{title} -> {detail}")
        for warning in result.warnings:
            log.warning("#%s : %s", result.article_id, warning)
    return counts["ok"], counts["skipped"], counts["error"]


def _display_path(path: Path | None) -> str:
    """Chemin relatif au dossier courant quand c'est plus lisible."""
    if path is None:
        return ""
    try:
        relative = os.path.relpath(path)
    except ValueError:  # autre lecteur sous Windows
        return str(path)
    return str(path) if relative.startswith("..") else relative


def _out(args: argparse.Namespace, message: str) -> None:
    if not args.quiet:
        print(message)


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _format_date(value: str | None) -> str:
    date = parse_date(value)
    return f"{date.astimezone():%d/%m/%Y %H:%M}" if date else ""


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [max([len(headers[i])] + [len(row[i]) for row in rows]) for i in range(len(headers))]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)).rstrip())
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())


class _Formatter(logging.Formatter):
    _PREFIXES = {logging.WARNING: "attention : ", logging.ERROR: "erreur : ", logging.CRITICAL: "erreur : "}

    def format(self, record: logging.LogRecord) -> str:
        return self._PREFIXES.get(record.levelno, "") + super().format(record)


def _setup_logging(verbose: int, quiet: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    level = logging.ERROR if quiet else logging.DEBUG if verbose else logging.WARNING
    root.setLevel(logging.WARNING)
    log.setLevel(level)
    # WeasyPrint signale chaque propriété CSS non gérée (fréquent avec du contenu
    # copié depuis le web) : ces messages ne sont affichés qu'avec -vv.
    logging.getLogger("weasyprint").setLevel(logging.WARNING if verbose >= 2 else logging.CRITICAL)
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.DEBUG if verbose >= 2 else logging.WARNING)


def _setup_console() -> None:
    """Évite les plantages d'encodage quand la console ne sait pas afficher un caractère."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
