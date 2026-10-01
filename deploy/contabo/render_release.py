"""Pin the release manifest after image publication; no network or deployment."""
import argparse
import json
from pathlib import Path
import re


def render(template, evidence, postgres_image, expected_sha):
    if not re.fullmatch(r'[0-9a-f]{40}', expected_sha) or evidence['source_sha'] != expected_sha:
        raise ValueError('Source SHA mismatch')
    api_image = evidence['api_image']
    if not re.fullmatch(r'ghcr\.io/estebansulcainfante/envaperu-scm-api@sha256:[0-9a-f]{64}', api_image):
        raise ValueError('Invalid API digest reference')
    if not re.fullmatch(r'postgres:(?:15|16|17)(?:[.\w-]*)@sha256:[0-9a-f]{64}', postgres_image):
        raise ValueError('PostgreSQL 15-17 exact digest required after source inventory')
    for name, value in [('SCM_API_IMAGE', api_image), ('SCM_POSTGRES_IMAGE', postgres_image)]:
        template, count = re.subn(r'\$\{' + name + r':\?[^}]+\}', value, template)
        if count != 1:
            raise ValueError('Expected exactly one image placeholder')
    return '# RELEASE SOURCE: ' + expected_sha + '\n' + template


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--postgres-image', required=True)
    parser.add_argument('--expected-source', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    template = Path(__file__).with_name('compose.yaml').read_text()
    result = render(template, json.loads(args.evidence.read_text()), args.postgres_image, args.expected_source)
    # Refuse silent replacement of any reviewed manifest.
    with args.output.open('x', encoding='utf-8', newline='\n') as output:
        output.write(result)
