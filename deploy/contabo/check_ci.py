"""Read-only GitHub gate for exact source SHA; no deployment or credentials printed."""
import json
import os
import re
import urllib.parse
import urllib.request


def validate_inputs(source_sha, python_base):
    if not re.fullmatch(r'[0-9a-f]{40}', source_sha):
        raise ValueError('source_sha must be a full lowercase commit SHA')
    if not re.fullmatch(r'python:3\.12[.\w-]*@sha256:[0-9a-f]{64}', python_base):
        raise ValueError('python_base must be an approved Python 3.12 image pinned by digest')


def select_success(payload, source_sha):
    runs = [r for r in payload.get('workflow_runs', []) if r.get('head_sha') == source_sha]
    if not runs:
        raise ValueError('No backend CI run found for the exact source SHA')
    latest = max(runs, key=lambda r: (r['run_number'], r.get('run_attempt', 1)))
    if latest.get('status') != 'completed' or latest.get('conclusion') != 'success':
        raise ValueError('Latest backend CI run for the source SHA is not successful')
    if latest.get('head_branch') != 'main' or latest.get('event') not in ('push', 'workflow_dispatch'):
        raise ValueError('Source must pass trusted main-branch CI, not a pull-request run')
    return latest['html_url']


if __name__ == '__main__':
    sha = os.environ['SOURCE_SHA']
    validate_inputs(sha, os.environ['PYTHON_BASE'])
    repo = 'estebanSulcaInfante/envaperu-workflow'
    url = f'https://api.github.com/repos/{repo}/actions/workflows/tests.yml/runs?' + urllib.parse.urlencode({'head_sha': sha, 'per_page': 100})
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
               'Authorization': 'Bearer ' + os.environ['GH_TOKEN']}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        payload = json.load(response)
    print('Verified source CI:', select_success(payload, sha))
