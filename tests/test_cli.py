import os
from unittest.mock import MagicMock

from cos2pag import cli


def test_main_loads_env_file_before_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MY_TEST_SECRET=from-dotenv\n")
    (tmp_path / "config.yaml").write_text(
        "cos:\n  token: \"${MY_TEST_SECRET}\"\n"
    )
    monkeypatch.delenv("MY_TEST_SECRET", raising=False)

    captured = {}

    def fake_sync_bucket(config, bucket_name, dry_run=False, tenant=None):
        captured["config"] = config
        report = MagicMock()
        report.steps = []
        return report

    monkeypatch.setattr(cli, "sync_bucket", fake_sync_bucket)

    exit_code = cli.main(["my-bucket"])

    assert exit_code == 0
    assert captured["config"]["cos"]["token"] == "from-dotenv"
    assert os.environ["MY_TEST_SECRET"] == "from-dotenv"


def test_main_env_file_overrides_stale_shell_export(tmp_path, monkeypatch):
    # A variable already exported in the shell (e.g. from an earlier
    # `source .env` used to test with curl) must not shadow a
    # since-corrected value in the .env file.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MY_TEST_SECRET=corrected-value\n")
    (tmp_path / "config.yaml").write_text(
        "cos:\n  token: \"${MY_TEST_SECRET}\"\n"
    )
    monkeypatch.setenv("MY_TEST_SECRET", "stale-shell-value")

    captured = {}

    def fake_sync_bucket(config, bucket_name, dry_run=False, tenant=None):
        captured["config"] = config
        report = MagicMock()
        report.steps = []
        return report

    monkeypatch.setattr(cli, "sync_bucket", fake_sync_bucket)

    cli.main(["my-bucket"])

    assert captured["config"]["cos"]["token"] == "corrected-value"
