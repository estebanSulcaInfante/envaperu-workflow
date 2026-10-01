"""Require protected publication environment; never configure permissions here."""
import json
import os
import urllib.request

REPO='estebanSulcaInfante/envaperu-workflow'
ENVIRONMENT='scm-pilot-image-public'
BRANCH='codex/scm-contabo-image'


def validate(environment, policies):
    rules=environment.get('protection_rules',[])
    reviewers=[r for r in rules if r.get('type')=='required_reviewers' and r.get('reviewers')]
    branch_policy=environment.get('deployment_branch_policy') or {}
    allowed=policies.get('branch_policies',[])
    if not reviewers or not branch_policy.get('custom_branch_policies'):
        raise ValueError('Required reviewer and explicit deployment branch policy missing')
    if len(allowed)!=1 or allowed[0].get('name')!=BRANCH or allowed[0].get('type','branch')!='branch':
        raise ValueError('Publication environment must allow only the reviewed branch')


if __name__=='__main__':
    if os.environ.get('GITHUB_REF')!='refs/heads/'+BRANCH:
        raise SystemExit('Publication from this ref is not approved')
    headers={'Accept':'application/vnd.github+json','Authorization':'Bearer '+os.environ['GH_TOKEN']}
    def get(suffix):
        req=urllib.request.Request(f'https://api.github.com/repos/{REPO}/environments/{ENVIRONMENT}'+suffix,headers=headers)
        with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
    validate(get(''),get('/deployment-branch-policies'))
    print('Publication environment restrictions verified')
