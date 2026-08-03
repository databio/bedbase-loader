#!/usr/bin/env python3
"""
CLI + helper for uploading a file to the BEDbase S3 bucket.

Targets the already-configured ``bedbase`` bucket via ``$AWS_ENDPOINT_URL``,
served over HTTPS at https://data2.bedbase.org/. Credentials come from
``$AWS_ACCESS_KEY_ID`` / ``$AWS_SECRET_ACCESS_KEY`` (boto3 picks them up from the
environment); only the endpoint has to be passed explicitly.
"""

import argparse
import os

import boto3


def upload_file(local_path: str, bucket: str, key: str,
                endpoint_url: str | None = None) -> None:
    """Upload ``local_path`` to ``s3://{bucket}/{key}``."""
    endpoint_url = endpoint_url or os.getenv("AWS_ENDPOINT_URL")
    client = boto3.client("s3", endpoint_url=endpoint_url)
    client.upload_file(local_path, bucket, key)
    print(f"Uploaded {local_path} -> s3://{bucket}/{key}")


def main():
    parser = argparse.ArgumentParser(description="Upload a file to an S3 bucket")
    parser.add_argument("local_path", help="Path to the local file to upload")
    parser.add_argument("key", help="S3 object key (e.g. exports/foo.parquet)")
    parser.add_argument("--bucket", default="bedbase", help="Target bucket")
    parser.add_argument("--endpoint-url", default=None,
                        help="S3 endpoint URL (defaults to $AWS_ENDPOINT_URL)")
    args = parser.parse_args()

    if not os.path.isfile(args.local_path):
        print(f"Error: {args.local_path} is not a file")
        return 1

    upload_file(args.local_path, args.bucket, args.key, endpoint_url=args.endpoint_url)
    return 0


if __name__ == "__main__":
    exit(main())
