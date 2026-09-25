"""Lance glpi-kb2pdf sans installer le paquet.

    python glpi-kb2pdf.py COMMANDE [options]

Seules les dépendances doivent être installées : pip install -r requirements.txt
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

try:
    from glpi_kb2pdf.cli import main
except ModuleNotFoundError as exc:
    sys.exit(f"Module Python manquant : {exc.name}\nInstallez les dépendances avec : pip install -r requirements.txt")

sys.exit(main())
