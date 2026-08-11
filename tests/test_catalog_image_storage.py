import hashlib
import io

import pytest
from PIL import Image

from app.services.catalog_image_storage import (
    CatalogImageStorage,
    CatalogImageStorageError,
    CatalogImageValidationError,
    has_catalog_image,
    validate_catalog_image_content,
    validate_catalog_image_storage_config,
)


class ImageEntity:
    imagen_mime = None
    imagen_data = None
    imagen_storage_key = None
    imagen_sha256 = None
    imagen_size_bytes = None


class Body:
    def __init__(self, content):
        self.content = content

    def read(self):
        return self.content


class FakeS3Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = {
            "Body": kwargs["Body"],
            "ContentType": kwargs["ContentType"],
            "Metadata": kwargs["Metadata"],
        }

    def get_object(self, **kwargs):
        item = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {
            "Body": Body(item["Body"]),
            "ContentType": item["ContentType"],
        }

    def delete_object(self, **kwargs):
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)


def s3_config(**overrides):
    config = {
        "CATALOG_IMAGE_STORAGE": "supabase_s3",
        "CATALOG_IMAGE_KEEP_DATABASE_COPY": False,
        "SUPABASE_S3_ENDPOINT": "https://example.storage.supabase.co/storage/v1/s3",
        "SUPABASE_S3_REGION": "us-east-1",
        "SUPABASE_S3_ACCESS_KEY_ID": "server-only",
        "SUPABASE_S3_SECRET_ACCESS_KEY": "server-only-secret",
        "SUPABASE_STORAGE_BUCKET": "catalog-images",
    }
    config.update(overrides)
    return config


def _image_bytes(image_format):
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color=(24, 96, 160)).save(
        output,
        format=image_format,
    )
    return output.getvalue()


@pytest.mark.parametrize(
    ("mime_type", "image_format"),
    [
        ("image/png", "PNG"),
        ("image/jpeg", "JPEG"),
        ("image/webp", "WEBP"),
    ],
)
def test_image_validator_decodes_real_supported_images(
    mime_type, image_format
):
    validate_catalog_image_content(
        mime_type,
        _image_bytes(image_format),
    )


@pytest.mark.parametrize(
    ("mime_type", "content"),
    [
        ("image/png", b"\x89PNG\r\n\x1a\n"),
        (
            "image/png",
            _image_bytes("PNG") + b"<script>polyglot</script>",
        ),
        (
            "image/jpeg",
            _image_bytes("JPEG")
            + b"<script>polyglot</script>"
            + b"\xff\xd9",
        ),
    ],
)
def test_image_validator_rejects_truncated_and_trailing_polyglot(
    mime_type, content
):
    with pytest.raises(CatalogImageValidationError) as caught:
        validate_catalog_image_content(mime_type, content)
    assert caught.value.code == "IMAGEN_CONTENIDO_INVALIDO"


def test_database_mode_preserves_legacy_behavior():
    entity = ImageEntity()
    storage = CatalogImageStorage({"CATALOG_IMAGE_STORAGE": "database"})

    storage.store(
        entity,
        category="pieza-color",
        identity="PC-000001",
        mime_type="image/png",
        content=b"png-content",
    )

    assert has_catalog_image(entity)
    assert entity.imagen_storage_key is None
    assert entity.imagen_data == b"png-content"
    assert entity.imagen_sha256 == hashlib.sha256(b"png-content").hexdigest()
    assert storage.load(entity).content == b"png-content"

    storage.delete(entity)
    assert not has_catalog_image(entity)


def test_supabase_s3_mode_writes_reads_and_deletes_private_object():
    entity = ImageEntity()
    client = FakeS3Client()
    storage = CatalogImageStorage(s3_config(), s3_client=client)

    key = storage.store(
        entity,
        category="producto-terminado",
        identity="PT-000001",
        mime_type="image/webp",
        content=b"webp-content",
    )

    digest = hashlib.sha256(b"webp-content").hexdigest()
    assert key == (
        "catalog/producto-terminado/PT-000001/"
        f"sha256-{digest}"
    )
    assert entity.imagen_data is None
    assert entity.imagen_storage_key == key
    assert storage.load(entity).content == b"webp-content"

    storage.delete(entity)
    assert not has_catalog_image(entity)
    assert client.objects == {}


def test_supabase_s3_mode_can_keep_database_copy_for_cutover(app):
    entity = ImageEntity()
    client = FakeS3Client()
    storage = CatalogImageStorage(
        s3_config(CATALOG_IMAGE_KEEP_DATABASE_COPY=True),
        s3_client=client,
    )
    storage.store(
        entity,
        category="pieza-color",
        identity="PC-000002",
        mime_type="image/png",
        content=b"fallback",
    )
    client.objects.clear()

    with app.app_context():
        assert storage.load(entity).content == b"fallback"


def test_supabase_s3_configuration_fails_fast_when_secret_is_missing():
    with pytest.raises(RuntimeError, match="SUPABASE_S3_SECRET_ACCESS_KEY"):
        validate_catalog_image_storage_config(
            s3_config(SUPABASE_S3_SECRET_ACCESS_KEY="")
        )


def test_s3_delete_failure_does_not_clear_database_metadata():
    class FailingDeleteClient(FakeS3Client):
        def delete_object(self, **kwargs):
            raise OSError("storage unavailable")

    entity = ImageEntity()
    entity.imagen_storage_key = "catalog/pieza-color/PC-1/image"
    entity.imagen_data = b"fallback"
    storage = CatalogImageStorage(
        s3_config(CATALOG_IMAGE_KEEP_DATABASE_COPY=True),
        s3_client=FailingDeleteClient(),
    )

    with pytest.raises(CatalogImageStorageError):
        storage.delete(entity)

    assert entity.imagen_storage_key
    assert entity.imagen_data == b"fallback"
