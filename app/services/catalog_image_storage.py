"""Almacenamiento reversible de imagenes del catalogo.

El modo ``database`` conserva el comportamiento UAT. El modo ``supabase_s3``
usa el endpoint compatible con S3 de Supabase Storage y puede mantener una
copia temporal en PostgreSQL durante el corte.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Mapping

from flask import current_app


SUPPORTED_STORAGE_MODES = {"database", "supabase_s3"}
S3_REQUIRED_SETTINGS = (
    "SUPABASE_S3_ENDPOINT",
    "SUPABASE_S3_REGION",
    "SUPABASE_S3_ACCESS_KEY_ID",
    "SUPABASE_S3_SECRET_ACCESS_KEY",
    "SUPABASE_STORAGE_BUCKET",
)


class CatalogImageStorageError(RuntimeError):
    """Error controlado al leer o escribir una imagen."""


@dataclass(frozen=True)
class CatalogImage:
    content: bytes
    mime_type: str


def validate_catalog_image_storage_config(config: Mapping[str, Any]) -> None:
    mode = str(config.get("CATALOG_IMAGE_STORAGE", "database")).lower()
    if mode not in SUPPORTED_STORAGE_MODES:
        raise RuntimeError(
            "CATALOG_IMAGE_STORAGE debe ser database o supabase_s3"
        )
    if mode != "supabase_s3":
        return
    missing = [name for name in S3_REQUIRED_SETTINGS if not config.get(name)]
    if missing:
        raise RuntimeError(
            "Falta configurar almacenamiento S3: " + ", ".join(missing)
        )


def has_catalog_image(entity: Any) -> bool:
    return bool(
        getattr(entity, "imagen_storage_key", None)
        or getattr(entity, "imagen_data", None)
    )


def _safe_identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-.")
    if not normalized:
        raise CatalogImageStorageError("Identificador de imagen invalido")
    return normalized


class CatalogImageStorage:
    def __init__(self, config: Mapping[str, Any], *, s3_client=None):
        validate_catalog_image_storage_config(config)
        self.mode = str(config.get("CATALOG_IMAGE_STORAGE", "database")).lower()
        self.keep_database_copy = bool(
            config.get("CATALOG_IMAGE_KEEP_DATABASE_COPY", True)
        )
        self.bucket = str(config.get("SUPABASE_STORAGE_BUCKET", ""))
        self._config = config
        self._client = s3_client

    def _s3_client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config as BotoConfig

            self._client = boto3.client(
                "s3",
                endpoint_url=self._config["SUPABASE_S3_ENDPOINT"],
                region_name=self._config["SUPABASE_S3_REGION"],
                aws_access_key_id=self._config["SUPABASE_S3_ACCESS_KEY_ID"],
                aws_secret_access_key=self._config[
                    "SUPABASE_S3_SECRET_ACCESS_KEY"
                ],
                config=BotoConfig(s3={"addressing_style": "path"}),
            )
        return self._client

    @staticmethod
    def object_key(category: str, identity: str) -> str:
        return (
            f"catalog/{_safe_identifier(category)}/"
            f"{_safe_identifier(identity)}/image"
        )

    def load(self, entity: Any) -> CatalogImage | None:
        key = getattr(entity, "imagen_storage_key", None)
        if key and self.mode == "supabase_s3":
            try:
                response = self._s3_client().get_object(
                    Bucket=self.bucket,
                    Key=key,
                )
                content = response["Body"].read()
                mime = response.get("ContentType") or entity.imagen_mime
                return CatalogImage(content=content, mime_type=mime)
            except Exception as exc:
                if not getattr(entity, "imagen_data", None):
                    raise CatalogImageStorageError(
                        "No se pudo recuperar la imagen desde Storage"
                    ) from exc
                current_app.logger.warning(
                    "Storage no disponible para %s; usando copia PostgreSQL",
                    key,
                    exc_info=True,
                )

        content = getattr(entity, "imagen_data", None)
        if not content:
            return None
        return CatalogImage(content=content, mime_type=entity.imagen_mime)

    def store(
        self,
        entity: Any,
        *,
        category: str,
        identity: str,
        mime_type: str,
        content: bytes,
    ) -> str | None:
        digest = hashlib.sha256(content).hexdigest()
        entity.imagen_mime = mime_type
        entity.imagen_sha256 = digest
        entity.imagen_size_bytes = len(content)

        if self.mode == "database":
            entity.imagen_data = content
            entity.imagen_storage_key = None
            return None

        key = self.object_key(category, identity)
        try:
            self._s3_client().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentType=mime_type,
                CacheControl="private, max-age=300",
                Metadata={"sha256": digest},
            )
        except Exception as exc:
            raise CatalogImageStorageError(
                "No se pudo guardar la imagen en Storage"
            ) from exc

        entity.imagen_storage_key = key
        entity.imagen_data = content if self.keep_database_copy else None
        return key

    def delete(self, entity: Any) -> None:
        key = getattr(entity, "imagen_storage_key", None)
        if key:
            if self.mode != "supabase_s3":
                raise CatalogImageStorageError(
                    "Storage S3 no esta configurado para eliminar el objeto"
                )
            try:
                self._s3_client().delete_object(Bucket=self.bucket, Key=key)
            except Exception as exc:
                raise CatalogImageStorageError(
                    "No se pudo eliminar la imagen de Storage"
                ) from exc

        entity.imagen_mime = None
        entity.imagen_data = None
        entity.imagen_storage_key = None
        entity.imagen_sha256 = None
        entity.imagen_size_bytes = None


def get_catalog_image_storage() -> CatalogImageStorage:
    extension_key = "catalog_image_storage"
    storage = current_app.extensions.get(extension_key)
    if storage is None:
        storage = CatalogImageStorage(current_app.config)
        current_app.extensions[extension_key] = storage
    return storage
