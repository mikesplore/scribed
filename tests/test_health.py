from types import SimpleNamespace

from app.health import check_dependencies


def test_r2_health_check_uses_configured_storage_credentials(monkeypatch):
    from app import db

    monkeypatch.setenv("R2_BUCKET", "test-bucket")
    monkeypatch.setenv("R2_ENDPOINT", "https://r2.example.test")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "test-secret-key")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def exec_driver_sql(self, statement):
            assert statement == "SELECT 1"

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(db, "engine", Engine())
    calls = {}

    class S3Client:
        def head_bucket(self, **kwargs):
            calls["bucket"] = kwargs["Bucket"]

    def create_client(service, **kwargs):
        calls["service"] = service
        calls["config"] = kwargs
        return S3Client()

    monkeypatch.setitem(__import__("sys").modules, "boto3", SimpleNamespace(client=create_client))

    check_dependencies()

    assert calls["service"] == "s3"
    assert calls["bucket"] == "test-bucket"
    assert calls["config"] == {
        "endpoint_url": "https://r2.example.test",
        "aws_access_key_id": "test-access-key",
        "aws_secret_access_key": "test-secret-key",
        "region_name": "auto",
    }
