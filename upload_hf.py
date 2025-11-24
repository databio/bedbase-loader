#!/usr/bin/env python3
"""
CLI tool for uploading folders to Hugging Face
"""
import argparse
import os
from huggingface_hub import HfApi


def main():
    parser = argparse.ArgumentParser(
        description="Upload a folder to Hugging Face Hub"
    )
    parser.add_argument(
        "folder_path",
        type=str,
        help="Path to the local folder to upload"
    )
    parser.add_argument(
        "--repo-id",
        type=str,
        default="databio/bedbase-umap",
        help="Repository ID (default: databio/bedbase-umap)"
    )
    parser.add_argument(
        "--repo-type",
        type=str,
        default="model",
        choices=["model", "dataset", "space"],
        help="Repository type (default: model)"
    )

    args = parser.parse_args()

    # Validate folder path
    if not os.path.isdir(args.folder_path):
        print(f"Error: {args.folder_path} is not a valid directory")
        return 1

    # Get HF token from environment
    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        print("Error: HF_TOKEN environment variable is not set")
        return 1

    # Upload folder
    print(f"Uploading {args.folder_path} to {args.repo_id}...")
    api = HfApi(token=hf_token)
    api.upload_folder(
        folder_path=args.folder_path,
        repo_id=args.repo_id,
        repo_type=args.repo_type,
    )
    print(f"Successfully uploaded to {args.repo_id}")

    return 0


if __name__ == "__main__":
    exit(main())