"""Validate the exact reviewed Engineering source CI run."""
import json
import os
import urllib.request

SOURCE_SHA = '6154e1f98d9f8629d9ee3a0d140f07e7dd779824'
RUN_ID = 41896378594
BRANCH = 'codex/publish-wip-bom-api-20261104'
REPOSITORY = 'estebanSulcaInfante/envaperu-workflow'
RUN_URL = f'https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}'
REQUIRED_JOBS = {'personal-agent-bridge', 'native-ui-overlay', 'fast-suite', 'postgres-smoke'}
PYTHON_BASE = 'python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f'


def validate_inputs(source_sha, python_base):
    if source_sha != SOURCE_SHA:
        raise ValueError('Only reviewed Engineering source SHA is approved')
    if python_base != PYTHON_BASE:
        raise ValueError('Only the reviewed immutable Python base is approved')


def select_success(run, source_sha):
    if (
        source_sha != SOURCE_SHA
        or run.get('id') != RUN_ID
        or run.get('head_sha') != SOURCE_SHA
        or run.get('head_branch') != BRANCH
        or run.get('event') != 'push'
        or run.get('path') != '.github/workflows/tests.yml'
        or run.get('repository', {}).get('full_name') != REPOSITORY
        or run.get('status') != 'completed'
        or run.get('conclusion') != 'success'
    ):
        raise ValueError('Reviewed Engineering CI identity or success does not match')
    return run['html_url']


def validate_jobs(jobs):
    rows = jobs.get('jobs', []) if isinstance(jobs, dict) else jobs
    successful = {
        job.get('name')
        for job in rows
        if job.get('status') == 'completed' and job.get('conclusion') == 'success'
    }
    missing = REQUIRED_JOBS - successful
    if missing:
        raise ValueError('Reviewed CI run is missing successful jobs: ' + ', '.join(sorted(missing)))
    return True


if __name__ == '__main__':
    validate_inputs(os.environ['SOURCE_SHA'], os.environ['PYTHON_BASE'])
    headers = {
        'Accept': 'application/vnd.github+json',
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
    }
    url = f'https://api.github.com/repos/{REPOSITORY}/actions/runs/{RUN_ID}'
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        run = json.load(response)
    jobs_url = f'{url}/jobs?per_page=100'
    with urllib.request.urlopen(urllib.request.Request(jobs_url, headers=headers), timeout=30) as response:
        jobs = json.load(response)
    validate_jobs(jobs)
    print('Verified Engineering source CI:', select_success(run, os.environ['SOURCE_SHA']))
