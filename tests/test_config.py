from pathlib import Path

import pytest

from glpi_kb2pdf.config import CONFIG_TEMPLATE, ConfigError, find_config_file, load_config, missing_connection_settings

CONFIG = """
[glpi]
url = "https://glpi.example.com"
client_id = "id-fichier"
client_secret = "secret"
username = "alexis"
verify_ssl = "certs/ca.pem"
timeout = 12

[export]
output_dir = "exports"
css = "style.css"
header = false
"""


def write_config(directory: Path, content: str = CONFIG) -> Path:
    path = directory / "glpi-kb2pdf.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_defaults_without_file(isolated_env):
    config = load_config(env={})
    assert config.source is None
    assert config.export.format == "pdf"
    assert config.export.filename == "{id}-{slug}"
    assert missing_connection_settings(config.glpi) == ["url", "client_id", "client_secret", "username"]


def test_file_is_found_in_current_directory(isolated_env):
    path = write_config(isolated_env)
    config = load_config(env={})
    assert config.source == path
    assert config.glpi.client_id == "id-fichier"
    assert config.glpi.timeout == 12.0
    assert config.export.header is False


def test_relative_paths_are_resolved_from_the_config_file(isolated_env):
    folder = isolated_env / "conf"
    folder.mkdir()
    path = write_config(folder)
    config = load_config(path, env={})
    base = path.resolve().parent
    assert config.export.output_dir == base / "exports"
    assert config.export.css == [base / "style.css"]
    assert config.glpi.verify_ssl == str(base / "certs" / "ca.pem")


def test_priority_cli_over_env_over_file(isolated_env):
    write_config(isolated_env)
    env = {"GLPI_CLIENT_ID": "id-env", "GLPI_USERNAME": "env-user", "GLPI_VERIFY_SSL": "false"}
    config = load_config(env=env, overrides={"glpi.username": "cli-user", "glpi.url": None})
    assert config.glpi.client_id == "id-env"
    assert config.glpi.username == "cli-user"
    assert config.glpi.url == "https://glpi.example.com"  # None n'écrase rien
    assert config.glpi.verify_ssl is False


def test_config_path_from_environment(isolated_env):
    folder = isolated_env / "ailleurs"
    folder.mkdir()
    path = write_config(folder)
    assert find_config_file(env={"GLPI_KB2PDF_CONFIG": str(path)}) == path


def test_user_config_location(isolated_env):
    user_dir = isolated_env / "user" / "glpi-kb2pdf"
    user_dir.mkdir(parents=True)
    path = user_dir / "config.toml"
    path.write_text('[glpi]\nurl = "http://x"\n', encoding="utf-8")
    env = {"APPDATA": str(isolated_env / "user"), "XDG_CONFIG_HOME": str(isolated_env / "user")}
    assert find_config_file(env=env) == path


def test_explicit_missing_file(isolated_env):
    with pytest.raises(ConfigError, match="introuvable"):
        load_config(Path("absent.toml"), env={})


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[glpi]\nuser = 'x'\n", "option inconnue « user »"),
        ("[sortie]\nformat = 'pdf'\n", "section inconnue"),
        ("[export]\nformat = 'docx'\n", "format"),
        ("[glpi]\ntimeout = -1\n", "timeout"),
        ("[glpi]\nrecursive = 'peut-être'\n", "recursive"),
        ("[glpi\n", "TOML"),
    ],
)
def test_invalid_files(isolated_env, content, message):
    write_config(isolated_env, content)
    with pytest.raises(ConfigError, match=message):
        load_config(env={})


def test_invalid_env_value(isolated_env):
    with pytest.raises(ConfigError, match="GLPI_TIMEOUT"):
        load_config(env={"GLPI_TIMEOUT": "dix"})


def test_template_is_valid(isolated_env):
    write_config(isolated_env, CONFIG_TEMPLATE)
    config = load_config(env={})
    assert config.glpi.url == "https://glpi.example.com"
    assert config.glpi.verify_ssl is True
    assert config.export.output_dir == isolated_env.resolve() / "export"
