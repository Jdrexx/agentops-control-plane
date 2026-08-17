from __future__ import annotations

import hashlib
import os
# Fixed pg_restore binary and argument vector; shell execution is disabled.
import subprocess  # nosec B404
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import boto3


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> None:
    database = urlsplit(required("RESTORE_DATABASE_URL"))
    if not all((database.hostname, database.username, database.path)):
        raise RuntimeError("RESTORE_DATABASE_URL is invalid")
    pg_environment = os.environ.copy()
    pg_environment.update(
        {
            "PGHOST": database.hostname,
            "PGPORT": str(database.port or 5432),
            "PGUSER": unquote(database.username),
            "PGDATABASE": unquote(database.path.lstrip("/")),
            "PGPASSWORD": unquote(database.password or ""),
        }
    )
    bucket = required("AWS_S3_BUCKET_NAME")
    client = boto3.client(
        "s3",
        endpoint_url=required("AWS_ENDPOINT_URL"),
        aws_access_key_id=required("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=required("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "auto"),
    )
    objects = client.list_objects_v2(Bucket=bucket, Prefix="postgres/").get("Contents", [])
    if not objects:
        raise RuntimeError("no database backups found")
    latest = max(objects, key=lambda item: item["LastModified"])
    key = latest["Key"]
    expected_checksum = client.head_object(Bucket=bucket, Key=key)["Metadata"].get("sha256")
    if not expected_checksum:
        raise RuntimeError("backup checksum metadata is missing")
    with tempfile.TemporaryDirectory(prefix="agentops-restore-") as directory:
        backup = Path(directory) / "agentops.dump"
        client.download_file(bucket, key, str(backup))
        checksum = hashlib.sha256(backup.read_bytes()).hexdigest()
        if checksum != expected_checksum:
            raise RuntimeError("backup checksum verification failed")
        # Fixed pg_restore binary and argument vector; shell execution is disabled.
        subprocess.run(  # noqa: S603  # nosec B603
            [
                "/usr/bin/pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--dbname",
                pg_environment["PGDATABASE"],
                str(backup),
            ],
            check=True,
            env=pg_environment,
            timeout=900,
        )
    print(f"restore completed: {key} sha256={checksum}")


if __name__ == "__main__":
    main()
