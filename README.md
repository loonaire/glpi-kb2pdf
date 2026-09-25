# glpi-kb2pdf

Outil en ligne de commande qui exporte les articles de la base de connaissance
GLPI en PDF, prêts à être imprimés, archivés ou diffusés hors de GLPI.

- Récupère les articles via l'API REST de GLPI (API v2, GLPI 11 et plus).
- Intègre au document les images stockées dans GLPI, qu'un lecteur de PDF ne
  pourrait pas charger sans être connecté à GLPI.
- Retire les liens vers GLPI, inutilisables hors de GLPI, en gardant leur texte.
- Ajoute un en-tête (titre, catégories, dates de création et de mise à jour, auteur),
  un pied de page (auteur, titre, numéro de page) et renseigne les métadonnées du PDF.
- Exporte un article, une sélection (catégorie, recherche, filtre) ou toute la base,
  et ne régénère au besoin que les articles modifiés.

```
$ glpi-kb2pdf export --all -o exports
OK      #1     Configurer le VPN -> exports\1-configurer-le-vpn.pdf
OK      #2     Installer Docker sur Debian 13 -> exports\2-installer-docker-sur-debian-13.pdf

2 exporté(s), 0 ignoré(s), 0 en échec
```

## Prérequis

### Côté GLPI

1. **GLPI 11 ou plus**, avec l'API activée (*Configuration > Générale > API*).
2. Un **client OAuth** (*Configuration > Clients OAuth*) avec le type
   d'autorisation **Password** et la portée **api**. Notez son identifiant et son secret.
3. Un **compte GLPI** qui a accès en lecture à la base de connaissance. L'export
   contient exactement les articles que ce compte voit dans GLPI.

### Côté poste ou serveur

- **Python 3.10 ou plus**.
- La bibliothèque **Pango**, utilisée par [WeasyPrint](https://weasyprint.org) pour générer les PDF :

| Système        | Commande |
|----------------|----------|
| Debian, Ubuntu | `sudo apt install libpango-1.0-0 libpangoft2-1.0-0` |
| RHEL, Rocky    | `sudo dnf install pango` |
| macOS          | `brew install pango` |
| Windows        | installer [MSYS2](https://www.msys2.org), puis dans un terminal MSYS2 : `pacman -S mingw-w64-ucrt-x86_64-pango` |

Sous Windows, l'outil trouve seul les DLL installées dans `C:\msys64`. Si MSYS2
est installé ailleurs, indiquez leur dossier dans la variable d'environnement
`WEASYPRINT_DLL_DIRECTORIES`, par exemple `D:\msys64\ucrt64\bin`.

Sans Pango, l'export au format HTML (`--format html`) fonctionne quand même.

## Installation

```bash
git clone https://github.com/loonaire/glpi-kb2pdf.git
cd glpi-kb2pdf
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux / macOS
pip install .
```

Pour une installation globale isolée, `pipx install .` fonctionne aussi. La
commande `glpi-kb2pdf` est alors disponible. `python -m glpi_kb2pdf` est équivalent.

### Sans installation

Seules les dépendances sont nécessaires ; le script `glpi-kb2pdf.py` lance
l'outil directement depuis le dossier du projet :

```bash
pip install -r requirements.txt
python glpi-kb2pdf.py check
python glpi-kb2pdf.py export 12
```

Toutes les commandes et options décrites ci-dessous s'utilisent de la même façon,
en remplaçant `glpi-kb2pdf` par `python glpi-kb2pdf.py`.

## Configuration

```bash
glpi-kb2pdf init      # crée ./glpi-kb2pdf.toml, à compléter
glpi-kb2pdf check     # vérifie la configuration, la connexion et le moteur PDF
```

```
$ glpi-kb2pdf check
Configuration         C:\kb\glpi-kb2pdf.toml
Connexion      OK     https://glpi.example.com/api.php (utilisateur export-kb)
Articles       OK     214 article(s) visible(s)
URL publique          https://glpi.example.com
Moteur PDF     OK     WeasyPrint 70.0
```

Le fichier de configuration est cherché dans cet ordre :

1. le fichier indiqué par `--config` ou par la variable `GLPI_KB2PDF_CONFIG` ;
2. `glpi-kb2pdf.toml` dans le dossier courant ;
3. `%APPDATA%\glpi-kb2pdf\config.toml` sous Windows, `~/.config/glpi-kb2pdf/config.toml` sinon.

Chaque option de la section `[glpi]` peut aussi venir d'une variable
d'environnement : `GLPI_URL`, `GLPI_CLIENT_ID`, `GLPI_CLIENT_SECRET`,
`GLPI_USERNAME`, `GLPI_PASSWORD`, `GLPI_VERIFY_SSL`, `GLPI_TIMEOUT`,
`GLPI_ENTITY`, `GLPI_RECURSIVE`.

**Priorité :** options de la ligne de commande > variables d'environnement >
fichier de configuration.

Si aucun mot de passe n'est fourni, il est demandé au lancement. Pour un usage
automatisé, utilisez `GLPI_PASSWORD` ou la clé `password`, et protégez le fichier
de configuration : il contient des secrets.

Les chemins relatifs du fichier (`output_dir`, `css`, certificat de `verify_ssl`)
sont relatifs au dossier du fichier de configuration.

## Utilisation

```bash
glpi-kb2pdf list                          # articles visibles
glpi-kb2pdf list --search vpn             # titre ou contenu contenant « vpn »
glpi-kb2pdf categories                    # catégories et leurs identifiants

glpi-kb2pdf export 12                     # un article, dans le dossier configuré
glpi-kb2pdf export 12 -o procedure.pdf    # un article, nom de fichier imposé
glpi-kb2pdf export 3 7 12 -o exports      # plusieurs articles
glpi-kb2pdf export --category 4           # une catégorie et ses sous-catégories
glpi-kb2pdf export --all                  # toute la base de connaissance
```

### Sélection des articles

Les options de sélection fonctionnent avec `list` et `export`, et se combinent
entre elles : un article doit remplir toutes les conditions.

| Option | Effet |
|--------|-------|
| `--category ID` | articles de la catégorie et de ses sous-catégories (option répétable) |
| `--search TEXTE` | titre ou contenu contenant le texte, sans tenir compte de la casse |
| `--filter RSQL` | filtre brut de l'API GLPI, par exemple `"date_mod=gt=2026-01-01"` ou `"is_faq==1"` |
| `--all` | tous les articles (`export` uniquement) |

### Options d'export

| Option | Effet |
|--------|-------|
| `-o, --output` | dossier de destination, ou nom du fichier si un seul article est exporté |
| `--format pdf\|html` | `html` produit un fichier autonome, images comprises |
| `--filename MODELE` | nom des fichiers, voir ci-dessous |
| `--css FICHIER` | feuille de style supplémentaire (option répétable) |
| `--no-header` | pas d'en-tête : le contenu de l'article seul (le pied de page est conservé) |
| `--no-remote-images` | ne télécharge pas les images hébergées hors de GLPI |
| `--skip-unchanged` | ignore les articles non modifiés depuis leur dernier export |

Le modèle de nom de fichier accepte les champs `{id}`, `{slug}` (titre
simplifié), `{name}` (titre), `{date}` (dernière modification, AAAA-MM-JJ) et
`{category}` (première catégorie). Un `/` crée des sous-dossiers :

```bash
# exports/Réseau/VPN/12-configurer-le-vpn.pdf
glpi-kb2pdf export --all -o exports --filename "{category}/{id}-{slug}"
```

### Export automatique

`--skip-unchanged` ne régénère que les articles modifiés depuis le dernier
passage. Associé à une tâche planifiée, il maintient une copie PDF à jour de la base.

Tâche cron quotidienne sous Linux :

```cron
30 2 * * * GLPI_PASSWORD=... /opt/glpi-kb2pdf/.venv/bin/glpi-kb2pdf export --all --skip-unchanged -q -c /etc/glpi-kb2pdf.toml
```

Sous Windows, créez une tâche planifiée qui lance
`C:\glpi-kb2pdf\.venv\Scripts\glpi-kb2pdf.exe export --all --skip-unchanged -q -c C:\glpi-kb2pdf\glpi-kb2pdf.toml`.

Codes de sortie : `0` succès, `1` au moins un article en échec (ou connexion
impossible), `2` erreur de configuration ou d'utilisation.

## Personnaliser la mise en page

La feuille de style par défaut est dans
[`src/glpi_kb2pdf/styles/default.css`](src/glpi_kb2pdf/styles/default.css).
Une feuille ajoutée avec `--css` (ou la clé `css` de la configuration) est
appliquée après elle et peut donc remplacer n'importe laquelle de ses règles :

```css
/* A4 paysage, logo en haut à droite, titre aux couleurs de l'entreprise */
@page {
    size: A4 landscape;
    @top-right { content: url("logo.png"); }
}
.kb-title { color: #b91c1c; }
.kb-header { border-bottom-color: #b91c1c; }
```

Les chemins relatifs dans `url()` partent du dossier de la feuille de style.
Les classes disponibles sont `.kb-header`, `.kb-title`, `.kb-categories`, `.kb-meta`
(colonnes `.kb-meta-left`, `.kb-meta-center`, `.kb-meta-right`), `.kb-label`
et `.kb-content`. Le contenu de l'article est dans `.kb-content`.

## Développement

```bash
pip install -e ".[dev]"
pytest
```

Les tests se lancent aussi sans installer le paquet (`pip install -r requirements.txt pytest responses`, puis `pytest`).

| Module | Rôle |
|--------|------|
| `glpi.py` | client de l'API GLPI : authentification OAuth2, pagination, erreurs |
| `content.py` | préparation du HTML : images intégrées, liens nettoyés, document final |
| `render.py` | conversion HTML → PDF avec WeasyPrint, détection de Pango |
| `exporter.py` | orchestration de l'export, noms de fichiers |
| `config.py` | fichier TOML, variables d'environnement |
| `cli.py` | ligne de commande |

Les tests n'ont besoin ni d'un serveur GLPI ni de Pango : l'API est simulée, et
le test de génération PDF est ignoré si Pango est absent.

## Licence

Distribué sous licence MIT, voir [LICENSE](LICENSE).
