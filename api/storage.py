import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


USER_FILES_BUCKET = "user-files"


def connect_rustfs():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["RUSTFS_ENDPOINT"],
        aws_access_key_id=os.environ["RUSTFS_ACCESS_KEY"],
        aws_secret_access_key=os.environ["RUSTFS_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=30),
    )


def ensure_bucket(s3, bucket: str) -> None:
    """Create an S3 bucket if it does not exist yet."""
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        s3.create_bucket(Bucket=bucket)
