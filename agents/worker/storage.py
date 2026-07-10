
import io

from minio import Minio
from minio.error import S3Error


class Storage:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str) -> None:
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=False)
        self._bucket = bucket

    def ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._client.put_object(self._bucket, key, io.BytesIO(data), len(data), content_type=content_type)

    def get(self, key: str) -> bytes:
        response = self._client.get_object(self._bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except S3Error:
            return False


def snapshot_key(run_id: str) -> str:
    return f"runs/{run_id}/snapshot.bundle"


def step_bundle_key(run_id: str, step_idx: int) -> str:
    return f"runs/{run_id}/steps/{step_idx}.bundle"


def step_diff_key(run_id: str, step_idx: int) -> str:
    return f"runs/{run_id}/steps/{step_idx}.diff"


def final_patch_key(run_id: str) -> str:
    return f"runs/{run_id}/final.patch"


def report_key(run_id: str) -> str:
    return f"runs/{run_id}/report.json"
