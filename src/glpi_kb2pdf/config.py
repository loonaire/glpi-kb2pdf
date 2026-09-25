"""Configuration : fichier TOML, variables d'environnement et options de la ligne de commande.

Ordre de priorité, du plus fort au plus faible :
ligne de commande > variables d'environnement > fichier de configuration > valeurs par défaut.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

CONFIG_FILENAME = "glpi-kb2pdf.toml"
CONFIG_ENV_VAR = "GLPI_KB2PDF_CONFIG"
OUTPUT_FORMATS = ("pdf", "html")


class ConfigError(Exception):
    """Configuration absente, incomplète ou invalide."""


@dataclass
class GlpiSettings:
    url: str = ""
    client_id: str = ""
    client_secret: str = ""
    username: str = ""
    password: str = ""
    verify_ssl: bool | str = True
    timeout: float = 30.0
    entity: int | None = None
    recursive: bool = True


@dataclass
class ExportSettings:
    output_dir: Path = Path(".")
    filename: str = "{id}-{slug}"
    format: str = "pdf"
    css: list[Path] = field(default_factory=list)
    header: bool = True
    remote_images: bool = True


@dataclass
class Config:
    glpi: GlpiSettings = field(default_factory=GlpiSettings)
    export: ExportSettings = field(default_factory=ExportSettings)
    source: Path | None = None  # fichier de configuration effectivement chargé


# ----------------------------------------------------------------------
#  Conversion et validation des valeurs
# ----------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "on", "oui", "vrai"}
_FALSE = {"0", "false", "no", "off", "non", "faux"}


def _to_str(value: Any, base_dir: Path | None) -> str:
    if not isinstance(value, (str, int)):
        raise ValueError("texte attendu")
    return str(value).strip()


def _to_bool(value: Any, base_dir: Path | None) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError("booléen attendu (true / false)")


def _to_verify(value: Any, base_dir: Path | None) -> bool | str:
    """true / false, ou chemin d'un certificat d'autorité."""
    try:
        return _to_bool(value, base_dir)
    except ValueError:
        return str(_to_path(value, base_dir))


def _to_timeout(value: Any, base_dir: Path | None) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError("nombre de secondes attendu") from None
    if timeout <= 0:
        raise ValueError("doit être strictement positif")
    return timeout


def _to_optional_int(value: Any, base_dir: Path | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("entier attendu")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("entier attendu") from None


def _to_path(value: Any, base_dir: Path | None) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("chemin attendu")
    path = Path(str(value).strip()).expanduser()
    if base_dir is not None and not path.is_absolute():
        path = base_dir / path
    return path


def _to_path_list(value: Any, base_dir: Path | None) -> list[Path]:
    if isinstance(value, (str, Path)):
        value = [value] if str(value).strip() else []
    if not isinstance(value, list):
        raise ValueError("chemin ou liste de chemins attendu")
    return [_to_path(item, base_dir) for item in value]


def _to_format(value: Any, base_dir: Path | None) -> str:
    text = str(value).strip().lower()
    if text not in OUTPUT_FORMATS:
        raise ValueError(f"valeurs possibles : {', '.join(OUTPUT_FORMATS)}")
    return text


Converter = Callable[[Any, "Path | None"], Any]

_FIELDS: dict[str, dict[str, Converter]] = {
    "glpi": {
        "url": _to_str,
        "client_id": _to_str,
        "client_secret": _to_str,
        "username": _to_str,
        "password": _to_str,
        "verify_ssl": _to_verify,
        "timeout": _to_timeout,
        "entity": _to_optional_int,
        "recursive": _to_bool,
    },
    "export": {
        "output_dir": _to_path,
        "filename": _to_str,
        "format": _to_format,
        "css": _to_path_list,
        "header": _to_bool,
        "remote_images": _to_bool,
    },
}

# Variables d'environnement reconnues → (section, option)
ENV_VARS: dict[str, tuple[str, str]] = {
    "GLPI_URL": ("glpi", "url"),
    "GLPI_CLIENT_ID": ("glpi", "client_id"),
    "GLPI_CLIENT_SECRET": ("glpi", "client_secret"),
    "GLPI_USERNAME": ("glpi", "username"),
    "GLPI_PASSWORD": ("glpi", "password"),
    "GLPI_VERIFY_SSL": ("glpi", "verify_ssl"),
    "GLPI_TIMEOUT": ("glpi", "timeout"),
    "GLPI_ENTITY": ("glpi", "entity"),
    "GLPI_RECURSIVE": ("glpi", "recursive"),
}


def _set(config: Config, section: str, key: str, raw: Any, origin: str, base_dir: Path | None = None) -> None:
    converter = _FIELDS[section][key]
    try:
        value = converter(raw, base_dir)
    except ValueError as exc:
        raise ConfigError(f"{origin} : valeur invalide pour « {key} » ({exc})") from None
    setattr(getattr(config, section), key, value)


# ----------------------------------------------------------------------
#  Chargement
# ----------------------------------------------------------------------

def default_config_locations(env: Mapping[str, str] | None = None) -> list[Path]:
    """Emplacements recherchés, par ordre de priorité, quand aucun fichier n'est indiqué."""
    env = os.environ if env is None else env
    locations = [Path.cwd() / CONFIG_FILENAME]
    if os.name == "nt":
        if env.get("APPDATA"):
            locations.append(Path(env["APPDATA"]) / "glpi-kb2pdf" / "config.toml")
    else:
        config_home = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        locations.append(Path(config_home) / "glpi-kb2pdf" / "config.toml")
    return locations


def find_config_file(explicit: Path | None = None, env: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if env is None else env
    if explicit is None and env.get(CONFIG_ENV_VAR):
        explicit = Path(env[CONFIG_ENV_VAR])
    if explicit is not None:
        explicit = explicit.expanduser()
        if not explicit.is_file():
            raise ConfigError(f"Fichier de configuration introuvable : {explicit}")
        return explicit
    return next((path for path in default_config_locations(env) if path.is_file()), None)


def _apply_file(config: Config, path: Path) -> None:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} : syntaxe TOML invalide ({exc})") from None
    except OSError as exc:
        raise ConfigError(f"Lecture impossible de {path} : {exc}") from None

    base_dir = path.resolve().parent
    for section, values in data.items():
        if section not in _FIELDS:
            raise ConfigError(f"{path} : section inconnue [{section}] (sections possibles : [glpi], [export])")
        if not isinstance(values, dict):
            raise ConfigError(f"{path} : [{section}] doit être une section")
        for key, raw in values.items():
            if key not in _FIELDS[section]:
                known = ", ".join(_FIELDS[section])
                raise ConfigError(f"{path} : option inconnue « {key} » dans [{section}] (options : {known})")
            _set(config, section, key, raw, str(path), base_dir)


def load_config(
    path: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Charge la configuration.

    ``overrides`` contient les valeurs issues de la ligne de commande, sous la
    forme ``{"glpi.url": ..., "export.format": ...}`` ; les valeurs ``None`` sont ignorées.
    """
    env = os.environ if env is None else env
    config = Config()
    config.source = find_config_file(path, env)
    if config.source is not None:
        _apply_file(config, config.source)

    for var, (section, key) in ENV_VARS.items():
        if env.get(var):
            _set(config, section, key, env[var], f"variable d'environnement {var}")

    for name, value in (overrides or {}).items():
        if value is None:
            continue
        section, key = name.split(".", 1)
        _set(config, section, key, value, "ligne de commande")
    return config


def missing_connection_settings(settings: GlpiSettings) -> list[str]:
    """Options de connexion obligatoires encore vides (le mot de passe peut être saisi)."""
    return [key for key in ("url", "client_id", "client_secret", "username") if not getattr(settings, key)]


# ----------------------------------------------------------------------
#  Modèle de fichier de configuration
# ----------------------------------------------------------------------

CONFIG_TEMPLATE = """\
# Configuration de glpi-kb2pdf
#
# Fichiers recherchés (le premier trouvé est utilisé) :
#   1. celui indiqué par --config ou par la variable GLPI_KB2PDF_CONFIG ;
#   2. ./glpi-kb2pdf.toml (dossier courant) ;
#   3. %APPDATA%\\glpi-kb2pdf\\config.toml (Windows) ou ~/.config/glpi-kb2pdf/config.toml.
# Les variables d'environnement (GLPI_URL, GLPI_PASSWORD…) sont prioritaires sur ce fichier.

[glpi]
# URL racine de GLPI, ou directement celle de l'API (https://glpi.example.com/api.php)
url = "https://glpi.example.com"

# Client OAuth déclaré dans GLPI (Configuration > Clients OAuth),
# avec le type d'autorisation « Password » et la portée « api »
client_id = ""
client_secret = ""

# Compte GLPI utilisé pour lire la base de connaissance
username = ""
# Laisser vide pour saisir le mot de passe à chaque exécution,
# ou le fournir par la variable d'environnement GLPI_PASSWORD.
password = ""

# Vérification du certificat TLS : true, false, ou chemin d'un certificat d'autorité (.pem)
verify_ssl = true
# Délai maximal d'une requête, en secondes
timeout = 30
# Entité GLPI à utiliser (par défaut : celle du profil de l'utilisateur)
# entity = 0
# Inclure les articles des sous-entités
recursive = true

[export]
# Dossier de destination (relatif à ce fichier s'il n'est pas absolu)
output_dir = "export"
# Modèle de nom de fichier, sans extension. Champs disponibles :
#   {id} {slug} {name} {date} {category}
# Un « / » crée des sous-dossiers, par exemple "{category}/{id}-{slug}".
filename = "{id}-{slug}"
# Format de sortie : "pdf" ou "html"
format = "pdf"
# Feuilles de style CSS supplémentaires (chemins relatifs à ce fichier)
css = []
# En-tête avec le titre, les catégories, les dates de création et de mise à jour et l'auteur
header = true
# Inclure les images hébergées hors de GLPI (nécessite un accès Internet)
remote_images = true
"""
