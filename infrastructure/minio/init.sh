#!/bin/sh
set -eu
umask 077
export MC_CONFIG_DIR=/tmp/mc

# Bucket name is used in a JSON policy; reject unexpected characters first.
case "$MLFLOW_ARTIFACT_BUCKET" in
  ''|*[!a-z0-9.-]*) echo 'Invalid MLflow bucket name' >&2; exit 1 ;;
esac
if [ "$MLFLOW_S3_ACCESS_KEY" = "$MINIO_ROOT_USER" ]; then
  echo 'MLflow must use a separate MinIO account' >&2
  exit 1
fi

mc alias set storage http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing "storage/$MLFLOW_ARTIFACT_BUCKET" >/dev/null
# Re-running init updates the dedicated user and policy; existing artifacts remain intact.
mc admin user add storage "$MLFLOW_S3_ACCESS_KEY" "$MLFLOW_S3_SECRET_KEY" >/dev/null
cat > /tmp/mlflow-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"],
      "Resource": ["arn:aws:s3:::$MLFLOW_ARTIFACT_BUCKET"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
      "Resource": ["arn:aws:s3:::$MLFLOW_ARTIFACT_BUCKET/*"]
    }
  ]
}
EOF
mc admin policy create storage medical-ai-mlflow /tmp/mlflow-policy.json >/dev/null
# `policy attach` can fail with "no net effect" when already attached.
# Check the current assignment rather than ignoring arbitrary attach errors.
user_info="$(mc admin user info storage "$MLFLOW_S3_ACCESS_KEY" --json)"
# Only this dedicated account is managed here. Match the assignment after the
# policyName field using POSIX shell, without depending on jq/grep in mc images.
case "$user_info" in
  *'"policyName"'*medical-ai-mlflow*) ;;
  *) mc admin policy attach storage medical-ai-mlflow --user "$MLFLOW_S3_ACCESS_KEY" >/dev/null ;;
esac
echo 'MLflow artifact bucket and scoped account configured.'
