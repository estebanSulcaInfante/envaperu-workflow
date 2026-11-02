"""Create fresh API and bridge Docker contexts from exact hashed allowlists."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def _safe_source_file(source, relative):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError('Invalid runtime path')
    path = source/rel
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
        raise ValueError('Runtime file must not escape source')
    return path


def _copy_allowlisted_files(source, destination, files, source_prefix=Path('.'), source_sha=None):
    for relative, expected in files.items():
        path = _safe_source_file(source, source_prefix/relative)
        if source_sha:
            content = subprocess.check_output([
                'git', '-C', str(source), 'show', f'{source_sha}:{(source_prefix/relative).as_posix()}',
            ])
        else:
            content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError('Unapproved runtime bytes: '+relative)
        target = destination/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(content)


def _assert_context_files(destination, expected):
    actual_files = {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()}
    if actual_files != set(expected):
        raise ValueError('Unexpected context file')


def create_api_context(source, destination, recipe):
    manifest = json.loads((recipe/'runtime-allowlist.json').read_text())
    actual = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    if actual != manifest['source_sha']: raise ValueError('Source SHA not approved')
    destination.mkdir(exist_ok=False)
    _copy_allowlisted_files(source, destination, manifest['files'], source_sha=manifest['source_sha'])
    shutil.copyfile(recipe/'Dockerfile',destination/'Dockerfile')
    shutil.copyfile(recipe/'Dockerfile.dockerignore',destination/'Dockerfile.dockerignore')
    _assert_context_files(destination, set(manifest['files'])|{'Dockerfile','Dockerfile.dockerignore'})
    print('Context verified:',len(manifest['files']),'approved runtime files, including owner-approved runtime template')


def create_bridge_context(source, destination, recipe):
    manifest = json.loads((recipe/'bridge-runtime-allowlist.json').read_text())
    actual = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    if actual != manifest['source_sha']: raise ValueError('Source SHA not approved')
    prefix = Path(manifest['source_prefix'])
    destination.mkdir(exist_ok=False)
    _copy_allowlisted_files(source, destination, manifest['files'], prefix, manifest['source_sha'])
    context_files = manifest.get('context_files', {})
    _copy_allowlisted_files(source, destination, context_files, prefix, manifest['source_sha'])
    _assert_context_files(destination, set(manifest['files']) | set(context_files))
    base_image = manifest.get('base_image', '')
    if not base_image or base_image not in (destination/'Dockerfile').read_text():
        raise ValueError('Bridge Dockerfile base is not the reviewed immutable image')
    print('Bridge context verified:',len(manifest['files']),'approved runtime files; tests, fixtures and secrets excluded')


# Backwards-compatible API entry point used by the focused recipe tests.
def create_context(source, destination, recipe):
    return create_api_context(source, destination, recipe)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('source',type=Path)
    parser.add_argument('destination',type=Path)
    parser.add_argument('--bridge',action='store_true')
    args=parser.parse_args()
    if args.bridge:
        create_bridge_context(args.source,args.destination,Path(__file__).parent)
    else:
        create_api_context(args.source,args.destination,Path(__file__).parent)
