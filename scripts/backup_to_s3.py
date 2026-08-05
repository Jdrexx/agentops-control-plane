from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> None:
    database_url = required("DATABASE_URL")
    bucket = required("AWS_S3_BUCKET_NAME")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    key = f"postgres/agentops-{timestamp}.dump"
    with tempfile.TemporaryDirectory(prefix="agentops-backup-") as directory:
        backup = Path(directory) / "agentops.dump"
        subprocess.run(  # noqa: S603 -- fixed binary and argument vector, no shell.
            [
                "/usr/bin/pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--file={backup}",
                database_url,
            ],
            check=True,
            timeout=900,
        )
        checksum = hashlib.sha256(backup.read_bytes()).hexdigest()
        client = boto3.client(
            "s3",
            endpoint_url=required("AWS_ENDPOINT_URL"),
            aws_access_key_id=required("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=required("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_DEFAULT_REGION", "auto"),
        )
        client.upload_file(
            str(backup),
            bucket,
            key,
            ExtraArgs={"Metadata": {"sha256": checksum}},
        )
        cutoff = datetime.now(UTC) - timedelta(days=int(os.getenv("BACKUP_RETENTION_DAYS", "30")))
        for item in client.list_objects_v2(Bucket=bucket, Prefix="postgres/").get("Contents", []):
            if item["LastModified"] < cutoff:
                client.delete_object(Bucket=bucket, Key=item["Key"])
    print(f"backup uploaded: {key} sha256={checksum}")


if __name__ == "__main__":
    main()
