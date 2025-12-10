#!/bin/bash
set -e  # Exit on error

# -------------------------------------------------------
# Parse arguments: --local, --tag <version>
# -------------------------------------------------------
LOCAL_MODE=false
VERSION_TAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local)
      LOCAL_MODE=true
      shift
      ;;
    --tag)
      VERSION_TAG="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: $0 [--local] [--tag <version>]"
      exit 1
      ;;
  esac
done

if [[ "$LOCAL_MODE" == true ]]; then
  echo "🔧 Building in LOCAL mode (no ECR push)"
fi

if [[ -n "$VERSION_TAG" ]]; then
  echo "🏷️ Version tag detected: ${VERSION_TAG}"
fi

# -------------------------------------------------------
# Download model weights (always from dev S3)
# -------------------------------------------------------
MODEL_FILE="sam_vit_h_4b8939.pth"
S3_PATH="s3://golfstripes-model-data/models/segment-anything/sam_vit_h_4b8939.pth"

if [ ! -f "$MODEL_FILE" ]; then
  echo "📦 Model file not found, downloading from S3..."
  aws s3 cp "$S3_PATH" "$MODEL_FILE"
else
  echo "✅ Model file already exists, skipping download."
fi

# -------------------------------------------------------
# Image/repo configuration
# -------------------------------------------------------
LOCAL_IMAGE="golfstripes-segment-anything:local"
LATEST_LOCAL_TAG="golfstripes-segment-anything:latest"
LOCAL_VERSIONED_TAG=""

ECR_BASE="842676010442.dkr.ecr.us-east-1.amazonaws.com/golfstripes-segment-anything"

if [[ -n "$VERSION_TAG" ]]; then
  LOCAL_VERSIONED_TAG="golfstripes-segment-anything:${VERSION_TAG}"
fi

# -------------------------------------------------------
# Local-only build
# -------------------------------------------------------
if [[ "$LOCAL_MODE" == true ]]; then
  echo "🔧 Building Docker image (local only)..."
  docker build -t "$LOCAL_IMAGE" .
  echo "✅ Done! Local image built: $LOCAL_IMAGE"
  exit 0
fi

# -------------------------------------------------------
# Full ECS/Fargate build (linux/amd64)
# -------------------------------------------------------
echo "🔧 Building Docker image for ECS/Fargate..."
docker buildx build --platform linux/amd64 -t "$LATEST_LOCAL_TAG" .

# Tag versioned image locally
if [[ -n "$VERSION_TAG" ]]; then
  docker tag "$LATEST_LOCAL_TAG" "$LOCAL_VERSIONED_TAG"
fi

# -------------------------------------------------------
# Authenticate to ECR
# -------------------------------------------------------
echo "🔐 Logging in to ECR..."
aws ecr get-login-password --region us-east-1 --profile golfstripes \
  | docker login --username AWS --password-stdin \
    842676010442.dkr.ecr.us-east-1.amazonaws.com

# -------------------------------------------------------
# Push latest tag
# -------------------------------------------------------
echo "🚀 Pushing latest tag to ECR..."
docker tag "$LATEST_LOCAL_TAG" "${ECR_BASE}:latest"
docker push "${ECR_BASE}:latest"

# -------------------------------------------------------
# Push version tag if present
# -------------------------------------------------------
if [[ -n "$VERSION_TAG" ]]; then
  echo "🚀 Pushing version tag ${VERSION_TAG} to ECR..."
  docker tag "$LOCAL_VERSIONED_TAG" "${ECR_BASE}:${VERSION_TAG}"
  docker push "${ECR_BASE}:${VERSION_TAG}"
fi

echo "✅ Done!"
if [[ -n "$VERSION_TAG" ]]; then
  echo "   → Pushed: latest and ${VERSION_TAG}"
else
  echo "   → Pushed: latest"
fi
