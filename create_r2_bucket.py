"""
create_r2_bucket.py

One-time setup: create the Cloudflare R2 bucket used for Bronze storage.
Idempotent — safe to run multiple times; does nothing if the bucket
already exists.

Requires the same R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
R2_BRONZE_BUCKET environment variables as storage.py.
"""

import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

R2_ACCOUNT_ID        = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BRONZE_BUCKET     = os.environ.get("R2_BRONZE_BUCKET", "")

missing = [
    name for name, val in [
        ("R2_ACCOUNT_ID", R2_ACCOUNT_ID),
        ("R2_ACCESS_KEY_ID", R2_ACCESS_KEY_ID),
        ("R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY),
        ("R2_BRONZE_BUCKET", R2_BRONZE_BUCKET),
    ] if not val
]
if missing:
    raise SystemExit(
        f"Missing environment variables: {missing}\n"
        f"Set them first, then rerun this script."
    )

client = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)

try:
    client.head_bucket(Bucket=R2_BRONZE_BUCKET)
    print(f"Bucket '{R2_BRONZE_BUCKET}' already exists — nothing to do.")
except ClientError as e:
    status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if status == 404:
        # R2 — do NOT pass CreateBucketConfiguration/LocationConstraint;
        # unlike AWS S3, R2 buckets are not region-scoped that way and
        # passing it causes errors on R2's API.
        client.create_bucket(Bucket=R2_BRONZE_BUCKET)
        print(f"Created bucket '{R2_BRONZE_BUCKET}'.")
    else:
        # Any other error (403 = bad credentials/permissions, etc.)
        # — surface it rather than silently trying to create.
        raise

# Confirm it's reachable and listable
resp = client.list_objects_v2(Bucket=R2_BRONZE_BUCKET, MaxKeys=1)
print(f"Bucket is reachable. Current object count (sample): "
      f"{resp.get('KeyCount', 0)}")