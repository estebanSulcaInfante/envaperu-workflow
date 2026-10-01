"""Create a fresh Docker context from an exact, hashed runtime allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def create_context(source, destination, recipe):
    manifest = json.loads((recipe/'runtime-allowlist.json').read_text())
    actual = subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    if actual != manifest['source_sha']: raise ValueError('Source SHA not approved')
    destination.mkdir(exist_ok=False)
    for relative, expected in manifest['files'].items():
        rel = Path(relative)
        if rel.is_absolute() or '..' in rel.parts: raise ValueError('Invalid runtime path')
        path = source/rel
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError('Runtime file must not escape source')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError('Unapproved runtime bytes: '+relative)
        target = destination/rel
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(content)
    shutil.copyfile(recipe/'Dockerfile',destination/'Dockerfile')
    shutil.copyfile(recipe/'Dockerfile.dockerignore',destination/'Dockerfile.dockerignore')
    actual_files = {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()}
    if actual_files != set(manifest['files'])|{'Dockerfile','Dockerfile.dockerignore'}:
        raise ValueError('Unexpected context file')
    print('Context verified:',len(manifest['files']),'approved runtime files, including owner-approved runtime template')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('source',type=Path)
    parser.add_argument('destination',type=Path)
    args=parser.parse_args()
    create_context(args.source,args.destination,Path(__file__).parent)
