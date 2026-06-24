from apps.api.app.main import bootstrap_local_resources_if_needed
from apps.api.app.settings import Settings


def test_bootstrap_runs_when_local_resources_are_enabled(monkeypatch) -> None:
    captured: dict[str, dict[str, str]] = {}

    def fake_ensure_jobs_table(**kwargs) -> None:
        captured["jobs_table"] = kwargs

    def fake_ensure_input_bucket(**kwargs) -> None:
        captured["input_bucket"] = kwargs

    def fake_configure_input_bucket_cors(**kwargs) -> None:
        captured["input_bucket_cors"] = kwargs

    def fake_ensure_ingest_queue(**kwargs):
        captured["ingest_queue"] = kwargs
        return "http://localhost:4566/000000000000/elastic-ingest", "arn:aws:sqs:us-east-1:000000000000:elastic-ingest"

    def fake_configure_bucket_notifications(**kwargs) -> None:
        captured["notifications"] = kwargs

    monkeypatch.setattr("apps.api.app.main.ensure_jobs_table", fake_ensure_jobs_table)
    monkeypatch.setattr("apps.api.app.main.ensure_input_bucket", fake_ensure_input_bucket)
    monkeypatch.setattr("apps.api.app.main.configure_input_bucket_cors", fake_configure_input_bucket_cors)
    monkeypatch.setattr("apps.api.app.main.ensure_ingest_queue", fake_ensure_ingest_queue)
    monkeypatch.setattr("apps.api.app.main.configure_bucket_notifications", fake_configure_bucket_notifications)

    settings = Settings(
        store_backend="dynamodb",
        jobs_table_name="elastic-jobs",
        input_bucket_name="elastic-inputs",
        aws_region="us-east-1",
        aws_endpoint_url="http://localhost:4566",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        auto_create_jobs_table=True,
        auto_create_input_bucket=True,
        auto_create_ingest_queue=True,
        auto_configure_bucket_notifications=True,
    )

    bootstrap_local_resources_if_needed(settings)

    assert captured == {
        "jobs_table": {
            "table_name": "elastic-jobs",
            "endpoint_url": "http://localhost:4566",
            "region_name": "us-east-1",
            "aws_access_key_id": "test",
            "aws_secret_access_key": "test",
        },
        "input_bucket": {
            "bucket_name": "elastic-inputs",
            "endpoint_url": "http://localhost:4566",
            "region_name": "us-east-1",
            "aws_access_key_id": "test",
            "aws_secret_access_key": "test",
        },
        "input_bucket_cors": {
            "bucket_name": "elastic-inputs",
            "endpoint_url": "http://localhost:4566",
            "region_name": "us-east-1",
            "aws_access_key_id": "test",
            "aws_secret_access_key": "test",
        },
        "ingest_queue": {
            "queue_name": "elastic-ingest",
            "endpoint_url": "http://localhost:4566",
            "region_name": "us-east-1",
            "aws_access_key_id": "test",
            "aws_secret_access_key": "test",
        },
        "notifications": {
            "bucket_name": "elastic-inputs",
            "queue_url": "http://localhost:4566/000000000000/elastic-ingest",
            "queue_arn": "arn:aws:sqs:us-east-1:000000000000:elastic-ingest",
            "endpoint_url": "http://localhost:4566",
            "region_name": "us-east-1",
            "aws_access_key_id": "test",
            "aws_secret_access_key": "test",
        },
    }


def test_bootstrap_skips_when_disabled(monkeypatch) -> None:
    called = 0

    def fake_ensure_jobs_table(**kwargs) -> None:
        nonlocal called
        called += 1

    def fake_ensure_input_bucket(**kwargs) -> None:
        nonlocal called
        called += 1

    def fake_configure_input_bucket_cors(**kwargs) -> None:
        nonlocal called
        called += 1

    def fake_ensure_ingest_queue(**kwargs):
        nonlocal called
        called += 1
        return "", ""

    def fake_configure_bucket_notifications(**kwargs) -> None:
        nonlocal called
        called += 1

    monkeypatch.setattr("apps.api.app.main.ensure_jobs_table", fake_ensure_jobs_table)
    monkeypatch.setattr("apps.api.app.main.ensure_input_bucket", fake_ensure_input_bucket)
    monkeypatch.setattr("apps.api.app.main.configure_input_bucket_cors", fake_configure_input_bucket_cors)
    monkeypatch.setattr("apps.api.app.main.ensure_ingest_queue", fake_ensure_ingest_queue)
    monkeypatch.setattr("apps.api.app.main.configure_bucket_notifications", fake_configure_bucket_notifications)

    settings = Settings(
        store_backend="dynamodb",
        auto_create_jobs_table=False,
        auto_create_input_bucket=False,
        auto_create_ingest_queue=False,
        auto_configure_bucket_notifications=False,
    )
    bootstrap_local_resources_if_needed(settings)

    assert called == 0
