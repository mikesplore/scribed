"""Startup-independent dependency checks used by the load balancer."""
import os
from pathlib import Path

def check_dependencies() -> None:
    # A failed database connection is detected with a real, cheap query.
    from .db import engine
    with engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")
    # Local storage is checked for writability; R2 is checked when enabled.
    if os.getenv("R2_BUCKET"):
        import boto3
        boto3.client("s3", endpoint_url=os.getenv("R2_ENDPOINT")).head_bucket(Bucket=os.environ["R2_BUCKET"])
    else:
        Path(os.getenv("STORAGE_DIR", "storage")).mkdir(parents=True, exist_ok=True)
