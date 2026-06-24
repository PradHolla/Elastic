from fastapi.testclient import TestClient

from apps.api.app.main import app, get_s3_client


class FakeS3Client:
    def generate_presigned_url(self, ClientMethod: str, Params: dict, ExpiresIn: int, HttpMethod: str) -> str:
        return (
            f"http://localstack:4566/{Params['Bucket']}/{Params['Key']}"
            f"?content-type={Params['ContentType']}&expires={ExpiresIn}"
            f"&method={HttpMethod}&op={ClientMethod}"
        )


app.dependency_overrides[get_s3_client] = lambda: FakeS3Client()
client = TestClient(app)


def test_create_job_returns_upload_instructions() -> None:
    response = client.post(
        "/jobs",
        json={
            "filename": "sample.mov",
            "content_type": "video/quicktime",
            "size_bytes": 73400320,
            "preset": "1080p",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "UPLOADING"
    assert data["preset"] == "1080p"
    assert data["input_bucket"] == "elastic-inputs"
    assert data["input_key"] == f"inputs/{data['job_id']}/source"
    assert data["output_key"] == f"outputs/{data['job_id']}/final/1080p.mp4"
    assert data["upload"]["method"] == "PUT"
    assert data["upload"]["url"].startswith("http://localstack:4566/elastic-inputs/")


def test_get_job_returns_stored_job() -> None:
    create_response = client.post(
        "/jobs",
        json={
            "filename": "sample.mov",
            "content_type": "video/quicktime",
            "size_bytes": 73400320,
            "preset": "1080p",
        },
    )
    job_id = create_response.json()["job_id"]

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == job_id
    assert data["status"] == "UPLOADING"
    assert data["attempt_count"] == 0
    assert data["last_error"] is None


def test_get_job_returns_404_for_unknown_job() -> None:
    response = client.get("/jobs/does-not-exist")

    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found."


def test_list_jobs_returns_newest_first() -> None:
    first = client.post(
        "/jobs",
        json={
            "filename": "first.mov",
            "content_type": "video/quicktime",
            "size_bytes": 73400320,
            "preset": "1080p",
        },
    ).json()
    second = client.post(
        "/jobs",
        json={
            "filename": "second.mov",
            "content_type": "video/quicktime",
            "size_bytes": 73400320,
            "preset": "1080p",
        },
    ).json()

    response = client.get("/jobs?limit=2")

    assert response.status_code == 200
    data = response.json()
    assert [job["job_id"] for job in data] == [second["job_id"], first["job_id"]]
    assert all(job["status"] == "UPLOADING" for job in data)


def test_create_job_rejects_unknown_preset() -> None:
    response = client.post(
        "/jobs",
        json={
            "filename": "sample.mov",
            "content_type": "video/quicktime",
            "size_bytes": 73400320,
            "preset": "720p",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Only the 1080p preset is supported in v1."
