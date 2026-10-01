"""Fail closed without displaying configuration or importing the application."""
import os
import sys
from urllib.parse import urlsplit


def validate(env):
    errors = []
    if env.get("SCM_AUTH_MODE") != "supabase":
        errors.append("SCM_AUTH_MODE")
    db = urlsplit(env.get("DATABASE_URL", ""))
    if (db.scheme not in ("postgresql", "postgresql+psycopg2")
            or db.username != "scm_api" or not db.password
            or db.hostname != "db" or db.path != "/scm_pilot"):
        errors.append("DATABASE_URL")
    for key in ("SUPABASE_URL", "SUPABASE_S3_ENDPOINT"):
        value = urlsplit(env.get(key, ""))
        if value.scheme != "https" or not value.hostname or value.username or value.password:
            errors.append(key)
    base = env.get("SUPABASE_URL", "").rstrip("/")
    if env.get("SUPABASE_JWT_ISSUER", "").rstrip("/") != base + "/auth/v1":
        errors.append("SUPABASE_JWT_ISSUER")
    for key in ("SUPABASE_JWT_AUDIENCE", "SUPABASE_S3_REGION",
                "SUPABASE_S3_ACCESS_KEY_ID", "SUPABASE_S3_SECRET_ACCESS_KEY",
                "SUPABASE_STORAGE_BUCKET"):
        if not env.get(key, "").strip():
            errors.append(key)
    origins = env.get("ALLOWED_ORIGINS", "").split(",")
    if any(not x.strip().startswith("https://") or "*" in x for x in origins):
        errors.append("ALLOWED_ORIGINS")
    if env.get("SCM_DEMO_MODE", "").strip():
        errors.append("SCM_DEMO_MODE")
    return errors


if __name__ == "__main__":
    try:
        invalid = validate(os.environ)
    except ValueError:
        invalid = ["malformed URL configuration"]
    if invalid:
        sys.exit("Configuration rejected; check variable names: " + ", ".join(invalid))
    if len(sys.argv) < 2:
        sys.exit("Missing process command")
    os.execvp(sys.argv[1], sys.argv[1:])
