import json
import os
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any

import boto3

DEST_DIR = os.getcwd()

LOCAL_IMAGE_FILE = os.path.join(DEST_DIR, "user_image.jpg")
LOCAL_LABEL_FILE = os.path.join(DEST_DIR, "user_image.txt")

OUTPUT_FILES = {
    "SEGMENTED": {
        "file_name": "mask.png",
        "media_type": "image/png",
        "source": "measurement-model",
        "metadata": {
            "description": "Ferrule segmentation mask",
            "source_variants": ["ORIGINAL", "OBJECT_DETECTION_LABELS"],
        },
    },
    "FRUSTUM": {
        "file_name": "frustum.jpg",
        "media_type": "image/jpeg",
        "source": "measurement-model",
        "metadata": {
            "description": "Extracted ferrule frustum image",
            "source_variants": ["ORIGINAL", "OBJECT_DETECTION_LABELS"],
        },
    },
    "MEASUREMENT_PROCESSED": {
        "file_name": "processed.jpg",
        "media_type": "image/jpeg",
        "source": "measurement-model",
        "metadata": {
            "description": "Processed measurement visualization",
            "source_variants": ["ORIGINAL", "OBJECT_DETECTION_LABELS"],
        },
    },
    "MEASUREMENTS": {
        "file_name": "measurements.json",
        "media_type": "application/json",
        "source": "measurement-model",
        "metadata": {
            "description": "Extracted ferrule measurements",
            "source_variants": ["ORIGINAL", "OBJECT_DETECTION_LABELS"],
        },
    },
}

s3 = boto3.client("s3")


class StepFunctionReporter:
    def __init__(self, task_token=None):
        self.task_token = task_token or os.environ.get("TASK_TOKEN")
        self.client = boto3.client("stepfunctions") if self.task_token else None

    def send_success(self, output: dict | None = None):
        if self.client and self.task_token:
            print("✅ Sending task success to Step Functions...")
            self.client.send_task_success(
                taskToken=self.task_token,
                output=json.dumps(output or {"status": "done"}),
            )

    def send_failure(self, error="TaskFailed", cause=None):
        if self.client and self.task_token:
            print("❌ Sending task failure to Step Functions...")
            self.client.send_task_failure(
                taskToken=self.task_token,
                error=error,
                cause=cause or "Unknown failure",
            )


def normalize_api_url(url: str) -> str:
    return url.rstrip("/")


def api_headers() -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    token = os.environ.get("MEDIA_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def http_get_json(url: str) -> dict[str, Any]:
    print(f"🌐 GET {url}")

    req = urllib.request.Request(
        url=url,
        method="GET",
        headers=api_headers(),
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET failed: {url} status={e.code} body={body}") from e


def http_post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    print(f"🌐 POST {url}")
    print(f"📦 Payload: {json.dumps(payload)}")

    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        data=data,
        method="POST",
        headers=api_headers(),
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST failed: {url} status={e.code} body={body}") from e


def get_media_variant(
        *,
        media_api_url: str,
        media_id: str,
        variant: str,
) -> dict[str, Any]:
    url = f"{media_api_url}/media/{media_id}/variants/{variant}"
    response = http_get_json(url)

    media = response.get("media")
    if not media:
        raise ValueError(f"Media API response for {variant} did not include media object")

    s3_bucket = media.get("s3_bucket")
    s3_key = media.get("s3_key")

    if not s3_bucket or not s3_key:
        raise ValueError(f"Media variant {variant} missing s3_bucket or s3_key")

    return response


def download_file(
        *,
        bucket: str,
        key: str,
        dest_path: str,
) -> None:
    print(f"⬇️ Downloading s3://{bucket}/{key} to {dest_path}")
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    with open(dest_path, "wb") as f:
        s3.download_fileobj(bucket, key, f)


def upload_file(
        *,
        bucket: str,
        key: str,
        source_path: str,
        content_type: str,
) -> None:
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Expected output not found: {source_path}")

    print(f"📤 Uploading {source_path} to s3://{bucket}/{key}")

    s3.upload_file(
        Filename=source_path,
        Bucket=bucket,
        Key=key,
        ExtraArgs={
            "ContentType": content_type,
        },
    )


def build_output_s3_key(
        *,
        media_id: str,
        file_name: str,
) -> str:
    return f"media/{media_id}/{file_name}"


def register_existing_variant(
        *,
        media_api_url: str,
        media_id: str,
        variant: str,
        usage_type: str,
        s3_bucket: str,
        s3_key: str,
        media_type: str,
        source: str,
        metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{media_api_url}/media/{media_id}/variants/{variant}/register"

    payload = {
        "usage_type": usage_type,
        "s3_bucket": s3_bucket,
        "s3_key": s3_key,
        "media_type": media_type,
        "source": source,
    }

    if metadata:
        payload["metadata"] = metadata

    return http_post_json(url, payload)


def run_measurement_script() -> None:
    print("✅ Downloads complete. Running ferrule measurement script...")

    command = [
        "python3",
        "-u",
        "find_ferrule_measurements_v2.py",
        "--image",
        LOCAL_IMAGE_FILE,
        "--yolo",
        LOCAL_LABEL_FILE,
    ]

    try:
        subprocess.run(command, check=True, stdout=sys.stdout, stderr=sys.stderr)
        print("✅ Ferrule measurement script completed")
    except subprocess.CalledProcessError as e:
        print("❌ Ferrule measurement script failed")
        raise e


def validate_outputs() -> None:
    expected_files = [config["file_name"] for config in OUTPUT_FILES.values()]
    missing = [file_name for file_name in expected_files if not os.path.exists(file_name)]

    if missing:
        raise FileNotFoundError(
            f"Expected output file(s) not found: {', '.join(missing)}"
        )


def process_media(
        *,
        media_id: str,
        media_api_url: str,
) -> dict[str, Any]:
    media_api_url = normalize_api_url(media_api_url)

    print(f"📦 Starting measurement workflow for media_id: {media_id}")

    original_response = get_media_variant(
        media_api_url=media_api_url,
        media_id=media_id,
        variant="original",
    )

    labels_response = get_media_variant(
        media_api_url=media_api_url,
        media_id=media_id,
        variant="object_detection_labels",
    )

    original_media = original_response["media"]
    labels_media = labels_response["media"]

    usage_type = original_response.get("usage_type") or "user-uploaded"

    media_bucket = original_media["s3_bucket"]

    if labels_media["s3_bucket"] != media_bucket:
        raise ValueError(
            "Original image and object detection labels are in different buckets"
        )

    download_file(
        bucket=original_media["s3_bucket"],
        key=original_media["s3_key"],
        dest_path=LOCAL_IMAGE_FILE,
    )

    download_file(
        bucket=labels_media["s3_bucket"],
        key=labels_media["s3_key"],
        dest_path=LOCAL_LABEL_FILE,
    )

    run_measurement_script()
    validate_outputs()

    registrations: dict[str, Any] = {}
    outputs: dict[str, Any] = {}

    for variant, config in OUTPUT_FILES.items():
        file_name = config["file_name"]
        media_type = config["media_type"]
        source = config["source"]
        metadata = config.get("metadata")

        local_path = os.path.join(DEST_DIR, file_name)

        s3_key = build_output_s3_key(
            media_id=media_id,
            file_name=file_name,
        )

        upload_file(
            bucket=media_bucket,
            key=s3_key,
            source_path=local_path,
            content_type=media_type,
        )

        registration = register_existing_variant(
            media_api_url=media_api_url,
            media_id=media_id,
            variant=variant,
            usage_type=usage_type,
            s3_bucket=media_bucket,
            s3_key=s3_key,
            media_type=media_type,
            source=source,
            metadata=metadata,
        )

        response_key = variant.lower()

        outputs[response_key] = {
            "variant": variant,
            "s3_bucket": media_bucket,
            "s3_key": s3_key,
            "media_type": media_type,
        }

        registrations[response_key] = registration

    print("✅ All expected files uploaded and registered. Job complete.")

    return {
        "status": "done",
        "media_id": media_id,
        "source": {
            "original": {
                "variant": "ORIGINAL",
                "s3_bucket": original_media["s3_bucket"],
                "s3_key": original_media["s3_key"],
            },
            "object_detection_labels": {
                "variant": "OBJECT_DETECTION_LABELS",
                "s3_bucket": labels_media["s3_bucket"],
                "s3_key": labels_media["s3_key"],
            },
        },
        "outputs": outputs,
        "registrations": registrations,
    }


def main():
    reporter = StepFunctionReporter()

    try:
        media_id = os.environ.get("MEDIA_ID")
        media_api_url = os.environ.get("MEDIA_API_URL")

        if not media_id:
            raise ValueError("Missing required environment variable: MEDIA_ID")

        if not media_api_url:
            raise ValueError("Missing required environment variable: MEDIA_API_URL")

        output = process_media(
            media_id=media_id,
            media_api_url=media_api_url,
        )

        reporter.send_success(output)

    except Exception as e:
        cause = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        print(cause)

        reporter.send_failure(
            error=type(e).__name__,
            cause=cause,
        )

        raise


if __name__ == "__main__":
    main()
