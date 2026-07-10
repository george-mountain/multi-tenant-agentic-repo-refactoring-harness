
from fastapi import HTTPException
from minio import Minio
from minio.error import S3Error

from app.config import settings

_client = Minio(
    settings.minio_endpoint,
    access_key=settings.minio_root_user,
    secret_key=settings.minio_root_password,
    secure=False,
)


def delete_run_artifacts(run_id: str) -> None:
    """Best-effort removal of all MinIO objects for a run; never raises."""
    prefix = f"runs/{run_id}/"
    try:
        objects = _client.list_objects(settings.minio_bucket, prefix=prefix, recursive=True)
        for obj in objects:
            _client.remove_object(settings.minio_bucket, obj.object_name)
    except S3Error:
        pass


def get_artifact(key: str) -> str:
    try:
        response = _client.get_object(settings.minio_bucket, key)
    except S3Error as exc:
        raise HTTPException(status_code=404, detail=f"artifact {key} not found") from exc
    try:
        return response.read().decode("utf-8", errors="replace")
    finally:
        response.close()
        response.release_conn()
