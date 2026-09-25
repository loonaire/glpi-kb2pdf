import argparse
import json

import pytest

from glpi_kb2pdf import cli
from glpi_kb2pdf.config import CONFIG_FILENAME

from conftest import CATEGORIES, FakeClient, make_article

CONFIG = """
[glpi]
url = "http://api.local:8081"
client_id = "cid"
client_secret = "secret"
username = "alexis"
password = "mdp"

[export]
output_dir = "exports"
format = "html"
"""


@pytest.fixture
def configured(isolated_env, monkeypatch):
    """Configuration valide dans le dossier courant et client GLPI simulé."""
    (isolated_env / CONFIG_FILENAME).write_text(CONFIG, encoding="utf-8")
    client = FakeClient([make_article(12), make_article(13, name="Imprimante", categories=[{"id": 3, "name": "Postes"}])])
    monkeypatch.setattr(cli, "_connect", lambda config: client)
    return client


def test_rsql_quote():
    assert cli.rsql_quote("*vpn*") == '"*vpn*"'
    assert cli.rsql_quote('écran "bleu"') == "'écran \"bleu\"'"
    assert cli.rsql_quote("l'écran \"bleu\"") == '"l\'écran *bleu*"'


def test_category_descendants():
    assert cli.category_descendants(CATEGORIES, 1) == {1, 2}
    assert cli.category_descendants(CATEGORIES, 2) == {2}
    assert cli.category_descendants(CATEGORIES, 42) == set()


def test_selection_filter():
    args = argparse.Namespace(category=[1, 3], search="vpn", filter="date_mod=gt=2026-01-01")
    assert cli._selection_filter(args, CATEGORIES) == (
        'categories.id=in=(1,2,3);(name=ilike="*vpn*",content=ilike="*vpn*");(date_mod=gt=2026-01-01)'
    )
    assert cli._selection_filter(argparse.Namespace(category=None, search=None, filter=None), []) is None
    with pytest.raises(cli.UsageError, match="catégorie 42"):
        cli._selection_filter(argparse.Namespace(category=[42], search=None, filter=None), CATEGORIES)


@pytest.mark.parametrize(
    "argv",
    [["export"], ["export", "12", "--all"], ["export", "12", "13", "-o", "doc.pdf"],
     ["export", "12", "-o", "doc.pdf", "--format", "html"], ["export", "--all", "--filename", "{titre}"]],
)
def test_export_usage_errors(configured, argv, capsys):
    assert cli.main(argv) == cli.EXIT_USAGE
    assert "erreur" in capsys.readouterr().err


def test_init(isolated_env, capsys):
    assert cli.main(["init"]) == cli.EXIT_OK
    assert (isolated_env / CONFIG_FILENAME).read_text(encoding="utf-8").startswith("# Configuration")
    assert cli.main(["init"]) == cli.EXIT_USAGE
    assert cli.main(["init", "--force"]) == cli.EXIT_OK


def test_missing_settings(isolated_env, capsys):
    assert cli.main(["list"]) == cli.EXIT_USAGE
    assert "paramètres de connexion manquants" in capsys.readouterr().err


def test_list(configured, capsys):
    assert cli.main(["list"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Configurer le VPN <client>" in out
    assert "Réseau > VPN" in out
    assert "2 article(s)" in out


def test_list_json_with_category(configured, capsys):
    assert cli.main(["list", "--category", "1", "--json"]) == cli.EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    assert configured.filters == ["categories.id=in=(1,2)"]
    assert rows[0] == {"id": 12, "name": "Configurer le VPN <client>", "categories": ["Réseau > VPN"],
                       "date_mod": "2026-03-15T12:28:14+00:00", "author": "alexis"}


def test_categories(configured, capsys):
    assert cli.main(["categories"]) == cli.EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[2:]] == ["3", "1", "2"]  # tri alphabétique


def test_export_all(configured, isolated_env, capsys):
    assert cli.main(["export", "--all"]) == cli.EXIT_OK
    files = sorted(p.name for p in (isolated_env / "exports").iterdir())
    assert files == ["12-configurer-le-vpn-client.html", "13-imprimante.html"]
    assert "2 exporté(s), 0 ignoré(s), 0 en échec" in capsys.readouterr().out


def test_export_single_file_infers_format(configured, isolated_env):
    assert cli.main(["export", "12", "-o", "sortie/vpn.html"]) == cli.EXIT_OK
    assert (isolated_env / "sortie" / "vpn.html").is_file()


def test_export_reports_failures(configured, isolated_env, capsys):
    assert cli.main(["export", "12", "999"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert "#999" in captured.err
    assert "1 exporté(s), 0 ignoré(s), 1 en échec" in captured.out


def test_export_quiet(configured, capsys):
    assert cli.main(["export", "--all", "-q"]) == cli.EXIT_OK
    assert capsys.readouterr().out == ""


def test_missing_css(configured, capsys):
    assert cli.main(["export", "12", "--css", "absent.css"]) == cli.EXIT_USAGE
    assert "absent.css" in capsys.readouterr().err
