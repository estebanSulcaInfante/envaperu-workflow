"""Focused local regression: inline config fails; file-backed guard works read-only."""
import json,subprocess,tempfile
from pathlib import Path
IMAGE='ghcr.io/estebansulcainfante/envaperu-scm-api@sha256:b5fdcba32bf9ccead3e7234adbdf4ac15f0ce0e33b9246aa2fda8532bdf65164'
GUARD=Path(__file__).resolve().parent/'start_guard.py'
ENV={'SCM_AUTH_MODE':'supabase','DATABASE_URL':'postgresql://scm_api:synthetic-only@db/scm_pilot','SUPABASE_URL':'https://example.invalid','SUPABASE_JWT_ISSUER':'https://example.invalid/auth/v1','SUPABASE_JWT_AUDIENCE':'authenticated','SUPABASE_S3_ENDPOINT':'https://example.invalid','SUPABASE_S3_REGION':'synthetic','SUPABASE_S3_ACCESS_KEY_ID':'synthetic','SUPABASE_S3_SECRET_ACCESS_KEY':'synthetic','SUPABASE_STORAGE_BUCKET':'synthetic','ALLOWED_ORIGINS':'https://example.invalid','KG009_RECOVERY_ON_STARTUP':'0','PYTHONDONTWRITEBYTECODE':'1'}
results={}
with tempfile.TemporaryDirectory(prefix='scm-config-regression-') as tmp:
 for kind in ['inline','file']:
  project='scm-guard-regression-'+kind
  cfg={'content':GUARD.read_text()} if kind=='inline' else {'file':GUARD.as_posix()}
  doc={'services':{'probe':{'image':IMAGE,'pull_policy':'never','read_only':True,'network_mode':'none','user':'10001:10001','cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],'mem_limit':'128m','cpus':0.5,'environment':ENV,'entrypoint':['python','/prep/start_guard.py'],'command':['python','-c','import os; assert os.getuid()==10001; print("GUARD_READONLY_PASS")'],'configs':[{'source':'guard','target':'/prep/start_guard.py'}]}},'configs':{'guard':cfg}}
  path=Path(tmp)/(kind+'.json');path.write_text(json.dumps(doc))
  cmd=['docker','compose','-p',project,'-f',str(path)]
  try:
   r=subprocess.run(cmd+['up','--abort-on-container-exit','--exit-code-from','probe'],capture_output=True,text=True,timeout=90)
   out=r.stdout+r.stderr
   if kind=='inline':
    assert r.returncode!=0 and '`file` is the sole supported option' in out,out
   else:
    assert r.returncode==0 and 'GUARD_READONLY_PASS' in out,out
    inspect=subprocess.run(['docker','inspect','--format','{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.Config.User}}',project+'-probe-1'],capture_output=True,text=True,check=True)
    assert inspect.stdout.strip()=='true|none|10001:10001',inspect.stdout
   results[kind]='EXPECTED_FAILURE' if kind=='inline' else 'PASS'
  finally:subprocess.run(cmd+['down','--remove-orphans'],capture_output=True,check=True)
print(json.dumps(results))
