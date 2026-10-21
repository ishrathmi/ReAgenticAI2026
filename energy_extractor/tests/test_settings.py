from energy_extractor import fetch, settings


def test_env_file_first_then_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# a comment\nOPENALEX_EMAIL = "me@example.org"\nOPENALEX_API_KEY=\n', encoding="utf-8")
    monkeypatch.setenv("OPENALEX_EMAIL", "other@example.org")
    monkeypatch.setenv("OPENALEX_API_KEY", "from-environment")
    assert settings.setting("OPENALEX_EMAIL", env) == "me@example.org"      # .env wins, quotes stripped
    assert settings.setting("OPENALEX_API_KEY", env) == "from-environment"  # blank in .env: environment
    assert settings.setting("NOT_SET_ANYWHERE", env) is None
    assert settings.setting("OPENALEX_EMAIL", tmp_path / "missing.env") == "other@example.org"


def test_openalex_gets_its_key_or_email_from_settings(monkeypatch):
    urls = []
    monkeypatch.setattr(fetch, "_get", lambda url, tries=3, data=None: urls.append(url) or b"{}")
    values = {"OPENALEX_EMAIL": "me@example.org"}
    monkeypatch.setattr(fetch, "setting", values.get)
    fetch._openalex("/works", filter="doi:10.1000/x")
    assert "mailto=me%40example.org" in urls[-1]
    values["OPENALEX_API_KEY"] = "k123"
    fetch._openalex("/works", filter="doi:10.1000/x")
    assert "api_key=k123" in urls[-1] and "mailto" not in urls[-1]
