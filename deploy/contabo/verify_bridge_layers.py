"""Verify every bridge image layer against the source and lockfile policy."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


def _load_lockfile(image, config_name):
    config = json.load(image.extractfile(config_name))
    # The config is not needed for content verification, but reading it ensures
    # malformed docker-save records fail closed before layer inspection.
    if not isinstance(config, dict) or 'rootfs' not in config:
        raise ValueError('Bridge image config is malformed')
    runtime = config.get('config') or {}
    if runtime.get('User') not in {'node', '1000', '1000:1000'}:
        raise ValueError('Bridge image must run as the non-root node user')
    if runtime.get('WorkingDir') != '/app':
        raise ValueError('Bridge image working directory must be /app')
    if runtime.get('Cmd') != ['node', 'src/server.js']:
        raise ValueError('Bridge image command is not the reviewed server command')
    expected = {
        'NODE_ENV': 'production',
        'SCM_BRIDGE_HOST': '0.0.0.0',
        'SCM_BRIDGE_PORT': '5182',
    }
    allowed_keys = {'PATH', 'NODE_VERSION', 'YARN_VERSION', *expected}
    env = {}
    for item in runtime.get('Env') or []:
        key, _, value = str(item).partition('=')
        if not key or key not in allowed_keys:
            raise ValueError('Bridge image contains an unapproved or secret environment key')
        env[key] = value
    if any(env.get(key) != value for key, value in expected.items()):
        raise ValueError('Bridge image environment does not match reviewed non-secret settings')


def _forbidden(path, patterns):
    lower = path.lower()
    return any(
        lower == pattern.lower()
        or lower.startswith(pattern.lower() + '/')
        or f'/{pattern.lower()}/' in lower
        or lower.endswith('/' + pattern.lower())
        or lower.endswith(pattern.lower())
        for pattern in patterns
    )


def verify(archive, manifest):
    if not re.fullmatch(r'node:24-bookworm-slim@sha256:[0-9a-f]{64}', manifest.get('base_image', '')):
        raise ValueError('Bridge base image must be the reviewed immutable Node 24 digest')
    allowed = {'app/' + path: digest for path, digest in manifest['files'].items()}
    excluded = set(manifest.get('excluded_patterns', []))
    seen = {}
    dependency_paths = set()
    dependency_metadata = {}
    lock_bytes = None
    with tarfile.open(archive, 'r:*') as image:
        records = json.load(image.extractfile('manifest.json'))
        if len(records) != 1:
            raise ValueError('Expected one bridge image')
        _load_lockfile(image, records[0]['Config'])
        for name in records[0]['Layers']:
            with tarfile.open(fileobj=image.extractfile(name), mode='r|*') as layer:
                for member in layer:
                    path = PurePosixPath(member.name.removeprefix('./'))
                    if path.is_absolute() or '..' in path.parts:
                        raise ValueError('Noncanonical bridge image layer path')
                    if not path.parts or path.parts[0] != 'app':
                        continue
                    if _forbidden(path.as_posix(), excluded) or path.name.startswith('.env'):
                        raise ValueError('Forbidden bridge runtime path: ' + path.as_posix())
                    if member.isdir():
                        continue
                    if not member.isfile() or member.issym() or member.islnk():
                        raise ValueError('Bridge runtime links or non-files are forbidden: ' + path.as_posix())
                    data = layer.extractfile(member).read()
                    if path.as_posix() in allowed:
                        digest = hashlib.sha256(data).hexdigest()
                        if digest != allowed[path.as_posix()]:
                            raise ValueError('Bridge runtime content mismatch: ' + path.as_posix())
                        if path.as_posix() in seen and seen[path.as_posix()] != digest:
                            raise ValueError('Bridge runtime content changed across layers: ' + path.as_posix())
                        seen[path.as_posix()] = digest
                        if path.name == manifest['dependency_lockfile']:
                            lock_bytes = data
                    elif path.as_posix().startswith('app/node_modules/'):
                        dependency_path = path.as_posix()[len('app/'):]
                        dependency_paths.add(dependency_path)
                        if dependency_path.endswith('/package.json'):
                            dependency_metadata[dependency_path] = json.loads(data)
                    else:
                        raise ValueError('Unapproved bridge /app file: ' + path.as_posix())
    if set(seen) != set(allowed):
        raise ValueError('Missing approved bridge runtime files')
    if lock_bytes is None:
        raise ValueError('Bridge dependency lockfile missing')
    lock = json.loads(lock_bytes)
    packages = lock.get('packages')
    if not isinstance(packages, dict) or '' not in packages:
        raise ValueError('Bridge lockfile has no npm package tree')
    lock_packages = {
        path[len('node_modules/'):]
        for path in packages
        if path.startswith('node_modules/') and path.count('/') == 1
    }
    if lock_packages != set(manifest['dependency_packages']):
        raise ValueError('Bridge lockfile dependency tree is not the reviewed tree')
    actual = {path for path in dependency_paths if path.endswith('/package.json')}
    package_names = {path.split('/')[1] for path in actual if path.count('/') == 2}
    if package_names != set(manifest['dependency_packages']):
        raise ValueError('Bridge dependency tree differs from reviewed packages')
    for name, version in manifest['dependency_packages'].items():
        package_path = f'node_modules/{name}/package.json'
        package = dependency_metadata.get(package_path)
        if not package or package.get('name') != name or package.get('version') != version:
            raise ValueError('Bridge dependency package metadata missing: ' + name)
    print('PASS: every bridge layer checked; exact source files, lockfile dependency tree, no tests/secrets')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('bridge-runtime-allowlist.json'))
    args = parser.parse_args()
    verify(args.archive, json.loads(args.manifest.read_text()))
