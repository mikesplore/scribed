from app.storage import using_r2


def test_production_uses_r2_even_with_custom_storage_dir(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("R2_BUCKET", "scribed")
    monkeypatch.setenv("STORAGE_DIR", "app/storage")

    assert using_r2() is True
