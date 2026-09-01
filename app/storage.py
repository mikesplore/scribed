"""PDF storage for local development and Cloudflare R2 in production."""
import os
from pathlib import Path

def using_r2() -> bool:
    # An explicit storage directory is the local/test override. This also
    # prevents a developer's loaded production .env from sending test PDFs to
    # R2 when tests use monkeypatch.setenv("STORAGE_DIR", ...).
    return bool(os.getenv("R2_BUCKET")) and os.getenv("STORAGE_DIR", "storage") == "storage"

def upload_pdf(key: str, content: bytes) -> str:
    if using_r2():
        import boto3
        client = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                              aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                              aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                              region_name="auto")
        client.put_object(Bucket=os.environ["R2_BUCKET"], Key=key, Body=content,
                          ContentType="application/pdf")
        return f"r2://{os.environ['R2_BUCKET']}/{key}"
    path = Path(os.getenv("STORAGE_DIR", "storage")) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)

def read_pdf(location: str) -> bytes:
    if location.startswith("r2://"):
        bucket, key = location[5:].split("/", 1)
        import boto3
        client = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                              aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                              aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return Path(location).read_bytes()

def delete_pdf(location: str) -> None:
    if location.startswith("r2://"):
        bucket, key = location[5:].split("/", 1)
        import boto3
        boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                     aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                     aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto").delete_object(Bucket=bucket, Key=key)
    else:
        path = Path(location)
        if path.is_file(): path.unlink()
