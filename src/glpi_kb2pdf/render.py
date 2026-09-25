"""Conversion HTML → PDF avec WeasyPrint.

WeasyPrint repose sur la bibliothèque native Pango. Sous Linux elle est
fournie par le paquet ``libpango-1.0-0`` (ou équivalent) ; sous Windows il faut
l'installer via MSYS2 (voir le README). Les emplacements MSYS2 et GTK usuels
sont détectés automatiquement.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
from collections.abc import Sequence
from functools import cache
from importlib import resources
from pathlib import Path
from types import ModuleType

log = logging.getLogger(__name__)

# Emplacements Windows usuels des DLL Pango (MSYS2, GTK3 runtime)
_WINDOWS_DLL_DIRS = (
    r"C:\msys64\ucrt64\bin",
    r"C:\msys64\mingw64\bin",
    r"C:\Program Files\GTK3-Runtime Win64\bin",
)

INSTALL_HELP = """\
WeasyPrint ne trouve pas la bibliothèque Pango, nécessaire à la génération des PDF.
  - Debian/Ubuntu : sudo apt install libpango-1.0-0 libpangoft2-1.0-0
  - RHEL/Rocky    : sudo dnf install pango
  - macOS         : brew install pango
  - Windows       : installez MSYS2 (https://www.msys2.org), puis dans un terminal MSYS2 :
                        pacman -S mingw-w64-ucrt-x86_64-pango
                    Si MSYS2 n'est pas dans C:\\msys64, définissez la variable
                    WEASYPRINT_DLL_DIRECTORIES avec le dossier contenant les DLL
                    (ex. D:\\msys64\\ucrt64\\bin).
En attendant, l'export au format HTML (--format html) reste disponible."""


class RendererUnavailable(Exception):
    """Le moteur PDF (WeasyPrint) ne peut pas être chargé."""


def default_css() -> str:
    return resources.files("glpi_kb2pdf").joinpath("styles/default.css").read_text(encoding="utf-8")


def _configure_windows_dll_directories() -> None:
    if os.name != "nt" or os.environ.get("WEASYPRINT_DLL_DIRECTORIES"):
        return
    for directory in _WINDOWS_DLL_DIRS:
        if (Path(directory) / "libgobject-2.0-0.dll").is_file():
            log.debug("DLL Pango trouvées dans %s", directory)
            os.environ["WEASYPRINT_DLL_DIRECTORIES"] = directory
            return


@cache
def load_weasyprint() -> ModuleType:
    """Importe WeasyPrint, en expliquant comment l'installer en cas d'échec."""
    _configure_windows_dll_directories()
    try:
        # WeasyPrint affiche sa propre bannière d'erreur : elle est remplacée par INSTALL_HELP
        with contextlib.redirect_stdout(io.StringIO()):
            import weasyprint
    except ImportError as exc:
        raise RendererUnavailable(
            "Le module Python weasyprint n'est pas installé : pip install weasyprint"
        ) from exc
    except OSError as exc:  # bibliothèques natives introuvables
        raise RendererUnavailable(INSTALL_HELP) from exc
    return weasyprint


def weasyprint_version() -> str:
    return load_weasyprint().__version__


def html_to_pdf(html: str, css_files: Sequence[Path] = (), base_url: str | None = None) -> bytes:
    """Convertit un document HTML complet en PDF et retourne son contenu."""
    weasyprint = load_weasyprint()
    stylesheets = [weasyprint.CSS(string=default_css())]
    stylesheets += [weasyprint.CSS(filename=str(path)) for path in css_files]
    document = weasyprint.HTML(string=html, base_url=base_url)
    # presentational_hints : respecte les attributs HTML (width d'une image,
    # bordures de tableau…) que l'éditeur de GLPI utilise abondamment.
    return document.write_pdf(stylesheets=stylesheets, presentational_hints=True)
