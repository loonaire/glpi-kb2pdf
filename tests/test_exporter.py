import os
from pathlib import Path

import pytest

from glpi_kb2pdf import exporter as exporter_module
from glpi_kb2pdf.exporter import (
    ExportOptions,
    Exporter,
    filename_fields,
    render_relative_path,
    sanitize_filename,
    slugify,
    validate_filename_template,
)
from glpi_kb2pdf.render import RendererUnavailable, load_weasyprint

from conftest import FakeClient, make_article


def test_slugify():
    assert slugify("Installer Docker sur Debian 13 !") == "installer-docker-sur-debian-13"
    assert slugify("Écran bleu : « IRQL »") == "ecran-bleu-irql"
    assert slugify("???") == "article"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('VPN: config "client" ?', "VPN_ config _client_ _"),
        ("  trop   d'espaces.  ", "trop d'espaces"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("", "_"),
    ],
)
def test_sanitize_filename(value, expected):
    assert sanitize_filename(value) == expected


def test_filename_fields():
    fields = filename_fields(make_article(), {2: "Réseau > VPN"})
    assert fields == {
        "id": "12",
        "slug": "configurer-le-vpn-client",
        "name": "Configurer le VPN _client_",
        "date": "2026-03-15",
        "category": "Réseau/VPN",
    }
    assert filename_fields(make_article(categories=[]), {})["category"] == "Sans catégorie"


def test_render_relative_path():
    fields = {"id": "12", "slug": "vpn", "name": "VPN", "date": "2026-03-15", "category": "Réseau/VPN"}
    assert render_relative_path("{id}-{slug}", fields, "pdf") == Path("12-vpn.pdf")
    assert render_relative_path("{category}/{date} {name}", fields, "pdf") == Path("Réseau/VPN/2026-03-15 VPN.pdf")
    assert render_relative_path("../{slug}.pdf", fields, "pdf") == Path("vpn.pdf")
    assert render_relative_path("{slug}", fields, "html") == Path("vpn.html")


def test_validate_filename_template():
    validate_filename_template("{category}/{id}-{slug}")
    with pytest.raises(ValueError, match="{titre}"):
        validate_filename_template("{titre}")
    with pytest.raises(ValueError):
        validate_filename_template("{id")


def html_options(tmp_path, **kwargs):
    return ExportOptions(output_dir=tmp_path, format="html", **kwargs)


def test_export_html(tmp_path):
    client = FakeClient()
    result = Exporter(client, html_options(tmp_path)).export_article(make_article())
    assert result.status == "ok", result.message
    assert result.path == tmp_path / "12-configurer-le-vpn-client.html"
    html = result.path.read_text(encoding="utf-8")
    assert "data:image/png;base64," in html
    assert "ticket.form.php" not in html  # lien vers GLPI retiré
    assert "Réseau &gt; VPN" in html
    assert "@page" in html  # feuille de style intégrée
    assert client.downloads == [5]
    assert not list(tmp_path.glob("*.tmp"))


def test_export_with_custom_css_and_category_folders(tmp_path):
    css = tmp_path / "perso.css"
    css.write_text(".kb-title { color: red; }", encoding="utf-8")
    options = html_options(tmp_path / "out", filename="{category}/{id}", css_files=[css])
    result = Exporter(FakeClient(), options).export_article(make_article())
    assert result.path == tmp_path / "out" / "Réseau" / "VPN" / "12.html"
    assert ".kb-title { color: red; }" in result.path.read_text(encoding="utf-8")


def test_missing_image_is_a_warning_not_a_failure(tmp_path):
    client = FakeClient(documents={})
    result = Exporter(client, html_options(tmp_path)).export_article(make_article())
    assert result.status == "ok"
    assert len(result.warnings) == 1


def test_skip_unchanged(tmp_path):
    exporter = Exporter(FakeClient(), html_options(tmp_path, skip_unchanged=True))
    article = make_article()
    assert exporter.export_article(article).status == "ok"
    assert Exporter(FakeClient(), html_options(tmp_path, skip_unchanged=True)).export_article(article).status == "skipped"

    # un fichier plus ancien que la dernière modification est régénéré
    path = tmp_path / "12-configurer-le-vpn-client.html"
    os.utime(path, (0, 0))
    assert Exporter(FakeClient(), html_options(tmp_path, skip_unchanged=True)).export_article(article).status == "ok"


def test_same_filename_gets_article_id(tmp_path):
    articles = [make_article(1, name="Doublon"), make_article(2, name="Doublon")]
    exporter = Exporter(FakeClient(articles), html_options(tmp_path, filename="{slug}"))
    paths = [result.path.name for result in exporter.export_articles(articles)]
    assert paths == ["doublon.html", "doublon-2.html"]


def test_export_ids_reports_missing_articles(tmp_path):
    exporter = Exporter(FakeClient(), html_options(tmp_path))
    results = list(exporter.export_ids([999, 12]))
    assert [r.status for r in results] == ["error", "ok"]
    assert "999" in results[0].message


def test_render_failure_is_reported_per_article(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("HTML inattendu")

    monkeypatch.setattr(exporter_module, "html_to_pdf", boom)
    result = Exporter(FakeClient(), ExportOptions(output_dir=tmp_path)).export_article(make_article())
    assert result.status == "error"
    assert "HTML inattendu" in result.message


def test_missing_renderer_stops_the_export(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RendererUnavailable("pas de Pango")

    monkeypatch.setattr(exporter_module, "html_to_pdf", unavailable)
    with pytest.raises(RendererUnavailable):
        Exporter(FakeClient(), ExportOptions(output_dir=tmp_path)).export_article(make_article())


def test_export_pdf(tmp_path):
    try:
        load_weasyprint()
    except RendererUnavailable:
        pytest.skip("WeasyPrint / Pango indisponible")
    output = tmp_path / "article.pdf"
    result = Exporter(FakeClient(), ExportOptions(output_file=output)).export_article(make_article())
    assert result.status == "ok", result.message
    assert output.read_bytes().startswith(b"%PDF-")
