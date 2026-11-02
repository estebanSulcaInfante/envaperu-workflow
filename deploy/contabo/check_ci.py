"""Validate the explicitly reviewed source CI run, not any green branch."""
import json
import os
import re
import urllib.request
SOURCE_SHA='831d6deb940dcefc5f7837129f1741662642f516'
RUN_ID=41127566753
BRANCH='codex/personal-agent-simple-20261030'
REPOSITORY='estebanSulcaInfante/envaperu-workflow'
RUN_URL=f'https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}'
REQUIRED_JOBS={'personal-agent-bridge','fast-suite','postgres-smoke','native-ui-overlay'}
def validate_inputs(source_sha, python_base):
    if source_sha!=SOURCE_SHA: raise ValueError('Only reviewed source SHA is approved')
    if not re.fullmatch(r'python:3\.12[.\w-]*@sha256:[0-9a-f]{64}',python_base):
        raise ValueError('Approved Python 3.12 immutable base required')
def select_success(run, source_sha):
    if (source_sha!=SOURCE_SHA or run.get('id')!=RUN_ID or run.get('head_sha')!=SOURCE_SHA
        or run.get('head_branch')!=BRANCH or run.get('event')!='push'
        or run.get('path')!='.github/workflows/tests.yml'
        or run.get('repository',{}).get('full_name')!=REPOSITORY
        or run.get('status')!='completed' or run.get('conclusion')!='success'):
        raise ValueError('Reviewed CI run identity or success does not match')
    return run['html_url']
def validate_jobs(jobs):
    rows=jobs.get('jobs',[]) if isinstance(jobs,dict) else jobs
    successful={job.get('name') for job in rows
                if job.get('status')=='completed' and job.get('conclusion')=='success'}
    missing=REQUIRED_JOBS-successful
    if missing: raise ValueError('Reviewed CI run is missing successful jobs: '+', '.join(sorted(missing)))
    return True
if __name__=='__main__':
    validate_inputs(os.environ['SOURCE_SHA'],os.environ['PYTHON_BASE'])
    url=f'https://api.github.com/repos/{REPOSITORY}/actions/runs/{RUN_ID}'
    headers={'Accept':'application/vnd.github+json','Authorization':'Bearer '+os.environ['GH_TOKEN']}
    with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=30) as response:
        run=json.load(response)
    jobs_url=f'https://api.github.com/repos/{REPOSITORY}/actions/runs/{RUN_ID}/jobs?per_page=100'
    with urllib.request.urlopen(urllib.request.Request(jobs_url,headers=headers),timeout=30) as response:
        jobs=json.load(response)
    validate_jobs(jobs)
    print('Verified reviewed CI:',select_success(run,os.environ['SOURCE_SHA']))
