"""
Cloudflare R2 storage client (S3-compatible).

Used for persisting the encrypted data bundle.
"""

import asyncio
import logging
from functools import lru_cache

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)

_BUNDLE_KEY = "bundle/encrypted_bundle.json.gz"
_VERSION_KEY = "bundle/version.json"


@lru_cache(maxsize=1)
def _get_s3_client():
    """Lazy singleton S3 client for Cloudflare R2."""
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=BotoConfig(
            retries={"max_attempts": 3, "mode": "adaptive"},
            connect_timeout=10,
            read_timeout=30,
        ),
        region_name="auto",
    )


async def r2_upload_bundle(gzip_bytes: bytes, version_json: bytes) -> bool:
    """Upload gzip-compressed bundle + version metadata to R2."""
    try:
        s3 = _get_s3_client()
        bucket = settings.r2_bucket

        def _upload():
            s3.put_object(
                Bucket=bucket,
                Key=_BUNDLE_KEY,
                Body=gzip_bytes,
                ContentType="application/octet-stream",
            )
            s3.put_object(
                Bucket=bucket,
                Key=_VERSION_KEY,
                Body=version_json,
                ContentType="application/json",
            )

        await asyncio.to_thread(_upload)
        logger.info(
            "Bundle uploaded to R2: %.1fKB gzip",
            len(gzip_bytes) / 1024,
        )
        return True
    except Exception as exc:
        logger.warning("R2 upload failed: %s", exc)
        return False


async def r2_download_bundle() -> bytes | None:
    """Download gzip-compressed bundle from R2. Returns None if not found."""
    try:
        s3 = _get_s3_client()

        def _download():
            resp = s3.get_object(Bucket=settings.r2_bucket, Key=_BUNDLE_KEY)
            return resp["Body"].read()

        data = await asyncio.to_thread(_download)
        logger.info("Bundle downloaded from R2: %.1fKB", len(data) / 1024)
        return data
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchKey":
            logger.info("No bundle found in R2")
        else:
            logger.warning("R2 download failed: %s", exc)
        return None
    except Exception as exc:
        logger.warning("R2 download failed: %s", exc)
        return None


async def r2_download_version() -> bytes | None:
    """Download version metadata from R2. Returns None if not found."""
    try:
        s3 = _get_s3_client()

        def _download():
            resp = s3.get_object(Bucket=settings.r2_bucket, Key=_VERSION_KEY)
            return resp["Body"].read()

        return await asyncio.to_thread(_download)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchKey":
            return None
        logger.warning("R2 version download failed: %s", exc)
        return None
    except Exception as exc:
        logger.warning("R2 version download failed: %s", exc)
        return None
