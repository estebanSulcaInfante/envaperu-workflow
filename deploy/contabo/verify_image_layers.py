"""Inspect every saved image layer, rejecting hidden/deleted operational files too."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile


def verify(archive, manifest):
    allowed = {'app/'+path:digest for path,digest in manifest['files'].items()}
    excluded = {PurePosixPath(path).name for path in manifest['excluded_runtime_resources']}
    seen = set()
    with tarfile.open(archive,'r:*') as image:
        records=json.load(image.extractfile('manifest.json'))
        if len(records)!=1: raise ValueError('Expected one image')
        for name in records[0]['Layers']:
            with tarfile.open(fileobj=image.extractfile(name),mode='r|*') as layer:
                for member in layer:
                    path=PurePosixPath(member.name.removeprefix('./'))
                    if path.is_absolute() or '..' in path.parts:
                        raise ValueError('Noncanonical image layer path')
                    if path.name in excluded: raise ValueError('Operational resource found in an image layer')
                    if path.parts and path.parts[0]=='app' and not member.isdir():
                        key=path.as_posix()
                        if not member.isfile() or key not in allowed:
                            raise ValueError('Unapproved /app file or link in image layer: '+key)
                        digest=hashlib.sha256(layer.extractfile(member).read()).hexdigest()
                        if digest!=allowed[key]: raise ValueError('Runtime content mismatch: '+key)
                        seen.add(key)
    if seen!=set(allowed): raise ValueError('Missing approved runtime files')
    print('PASS: every image layer checked; exact runtime allowlist, no unapproved runtime resources')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('archive',type=Path)
    args=parser.parse_args()
    verify(args.archive,json.loads(Path(__file__).with_name('runtime-allowlist.json').read_text()))
