"""Verify an OpenClaw base image plus the sealed Control UI overlay layer."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


def _canonical(name):
    path = PurePosixPath(name.removeprefix('./'))
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Noncanonical native image layer path')
    return path


def _image_records(image):
    records = json.load(image.extractfile('manifest.json'))
    if len(records) != 1 or not records[0].get('Layers'):
        raise ValueError('Expected one native image with layers')
    return records[0]


def _config(image, record):
    return json.load(image.extractfile(record['Config']))


def verify(archive, metadata, base_archive):
    base_image = metadata.get('baseImage', '')
    if not re.fullmatch(r'ghcr\.io/openclaw/openclaw@sha256:[0-9a-f]{64}', base_image):
        raise ValueError('Native base image must be the pinned official OpenClaw image')
    upstream = metadata.get('upstream') or {}
    overlay = metadata.get('overlay') or {}
    labels_expected = {
        'org.opencontainers.image.version': upstream.get('version'),
        'org.opencontainers.image.revision': upstream.get('revision'),
        'io.envaperu.openclaw.base-image': base_image,
        'io.envaperu.openclaw.patch.sha256': upstream.get('patchSha256'),
        'io.envaperu.openclaw.control-ui.build-id': overlay.get('buildId'),
        'io.envaperu.openclaw.control-ui.built-at': overlay.get('builtAt'),
    }
    if any(not isinstance(value, str) or not value for value in labels_expected.values()):
        raise ValueError('Native overlay metadata is incomplete')
    with tarfile.open(base_archive, 'r:*') as base, tarfile.open(archive, 'r:*') as image:
        base_record = _image_records(base)
        records = _image_records(image)
        base_layers = base_record['Layers']
        native_layers = records['Layers']
        if len(native_layers) <= len(base_layers) or native_layers[:len(base_layers)] != base_layers:
            raise ValueError('Native image layers do not preserve the pinned base prefix')
        added_layers = native_layers[len(base_layers):]
        config = _config(image, records)
        base_config = _config(base, base_record)
        base_diff_ids = (base_config.get('rootfs') or {}).get('diff_ids') or []
        native_diff_ids = (config.get('rootfs') or {}).get('diff_ids') or []
        if len(native_diff_ids) <= len(base_diff_ids) or native_diff_ids[:len(base_diff_ids)] != base_diff_ids:
            raise ValueError('Native image diff IDs do not preserve the pinned base prefix')
        runtime = config.get('config') or {}
        labels = runtime.get('Labels') or {}
        if any(labels.get(key) != value for key, value in labels_expected.items()):
            raise ValueError('Native image labels do not match sealed overlay metadata')
        for item in runtime.get('Env') or []:
            key = str(item).partition('=')[0]
            if key.startswith(('SCM_', 'OPENCLAW_')):
                raise ValueError('Native overlay must not add SCM/OpenClaw environment configuration')
        overlay_paths = set()
        for layer_name in added_layers:
            with tarfile.open(fileobj=image.extractfile(layer_name), mode='r|*') as layer:
                for member in layer:
                    path = _canonical(member.name)
                    if member.isdir():
                        continue
                    if not member.isfile() or member.issym() or member.islnk():
                        raise ValueError('Native overlay links or non-files are forbidden')
                    if path.as_posix() != 'app/dist/build-info.json' and not path.as_posix().startswith('app/dist/control-ui/'):
                        raise ValueError('Native overlay writes outside the sealed Control UI paths: ' + path.as_posix())
                    overlay_paths.add(path.as_posix())
    if 'app/dist/build-info.json' not in overlay_paths or not any(path.startswith('app/dist/control-ui/') for path in overlay_paths):
        raise ValueError('Native overlay layer is missing required Control UI files')
    print('PASS: pinned official OpenClaw base metadata and sealed Control UI overlay verified')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--base-archive', type=Path, required=True)
    args = parser.parse_args()
    verify(args.archive, json.loads(args.metadata.read_text()), args.base_archive)
