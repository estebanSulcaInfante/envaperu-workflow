"""Almacenamiento reversible de imagenes del catalogo.

El modo ``database`` conserva el comportamiento UAT. El modo ``supabase_s3``
usa el endpoint compatible con S3 de Supabase Storage y puede mantener una
copia temporal en PostgreSQL durante el corte.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import re
from typing import Any, Mapping
import warnings

from flask import current_app
from PIL import Image, UnidentifiedImageError


SUPPORTED_STORAGE_MODES = {"database", "supabase_s3"}
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_CATALOG_IMAGE_BYTES = 2 * 1024 * 1024
MAX_CATALOG_IMAGE_PIXELS = 25_000_000
S3_REQUIRED_SETTINGS = (
    "SUPABASE_S3_ENDPOINT",
    "SUPABASE_S3_REGION",
    "SUPABASE_S3_ACCESS_KEY_ID",
    "SUPABASE_S3_SECRET_ACCESS_KEY",
    "SUPABASE_STORAGE_BUCKET",
)


class CatalogImageStorageError(RuntimeError):
    """Error controlado al leer o escribir una imagen."""


class CatalogImageValidationError(CatalogImageStorageError):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class CatalogImage:
    content: bytes
    mime_type: str


def validate_catalog_image_content(mime_type: str, content: bytes) -> None:
    mime = str(mime_type or "").strip().lower()
    if mime not in IMAGE_MIME_TYPES:
        raise CatalogImageValidationError(
            "IMAGEN_FORMATO_INVALIDO",
            "Formato no permitido. Usa JPG, PNG o WebP.",
            415,
        )
    if not content:
        raise CatalogImageValidationError(
            "IMAGEN_VACIA",
            "La imagen esta vacia.",
            400,
        )
    if len(content) > MAX_CATALOG_IMAGE_BYTES:
        raise CatalogImageValidationError(
            "IMAGEN_DEMASIADO_GRANDE",
            "La imagen supera el limite de 2 MB.",
            413,
        )
    if not _has_complete_image_container(mime, content):
        raise CatalogImageValidationError(
            "IMAGEN_CONTENIDO_INVALIDO",
            "El contenido no coincide con el formato declarado.",
            415,
        )
    expected_format = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/webp": "WEBP",
    }[mime]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                actual_format = str(image.format or "").upper()
                width, height = image.size
                if actual_format != expected_format:
                    raise ValueError("image format mismatch")
                if (
                    width <= 0
                    or height <= 0
                    or width * height > MAX_CATALOG_IMAGE_PIXELS
                ):
                    raise ValueError("image dimensions exceed limit")
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as error:
        raise CatalogImageValidationError(
            "IMAGEN_CONTENIDO_INVALIDO",
            "La imagen esta corrupta, truncada o no coincide con su formato.",
            415,
        ) from error


def _has_complete_image_container(mime_type: str, content: bytes) -> bool:
    """Reject header-only and trailing polyglot payloads before decoding."""

    if mime_type == "image/jpeg":
        return _jpeg_ends_at_canonical_eoi(content)
    if mime_type == "image/webp":
        return (
            len(content) >= 12
            and content.startswith(b"RIFF")
            and content[8:12] == b"WEBP"
            and int.from_bytes(content[4:8], "little") + 8 == len(content)
        )
    if mime_type != "image/png" or not content.startswith(
        b"\x89PNG\r\n\x1a\n"
    ):
        return False
    offset = 8
    while offset + 12 <= len(content):
        payload_size = int.from_bytes(content[offset:offset + 4], "big")
        chunk_type = content[offset + 4:offset + 8]
        chunk_end = offset + 12 + payload_size
        if chunk_end > len(content):
            return False
        if chunk_type == b"IEND":
            return payload_size == 0 and chunk_end == len(content)
        offset = chunk_end
    return False


def _jpeg_ends_at_canonical_eoi(content: bytes) -> bool:
    """Parse JPEG markers so a forged trailing EOI cannot hide a polyglot."""

    if len(content) < 4 or not content.startswith(b"\xff\xd8"):
        return False
    position = 2
    in_scan = False
    size = len(content)
    while position < size:
        if in_scan:
            marker_prefix = content.find(b"\xff", position)
            if marker_prefix < 0:
                return False
            position = marker_prefix + 1
            while position < size and content[position] == 0xFF:
                position += 1
            if position >= size:
                return False
            marker = content[position]
            position += 1
            if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                continue
            in_scan = False
        else:
            if content[position] != 0xFF:
                return False
            while position < size and content[position] == 0xFF:
                position += 1
            if position >= size:
                return False
            marker = content[position]
            position += 1

        if marker == 0xD9:
            return position == size
        if marker in {0x01, *range(0xD0, 0xD8)}:
            continue
        if marker == 0xD8 or position + 2 > size:
            return False
        segment_size = int.from_bytes(
            content[position:position + 2],
            "big",
        )
        if segment_size < 2 or position + segment_size > size:
            return False
        position += segment_size
        if marker == 0xDA:
            in_scan = True
    return False


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
    def object_key(category: str, identity: str, digest: str) -> str:
        return (
            f"catalog/{_safe_identifier(category)}/"
            f"{_safe_identifier(identity)}/sha256-{_safe_identifier(digest)}"
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

        key = self.object_key(category, identity, digest)
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

    def delete_key(self, key: str) -> None:
        """Delete an exact object without mutating database metadata."""

        if not key:
            return
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

    def delete(self, entity: Any) -> None:
        key = getattr(entity, "imagen_storage_key", None)
        if key:
            self.delete_key(key)

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
