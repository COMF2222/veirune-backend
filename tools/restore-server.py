"""Restore a PC backup onto a NEW VPS; never replace an existing deployment."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request


def moved_environment(text, address):
    ip=ipaddress.IPv4Address(address)
    if not ip.is_global:raise ValueError('Use the new public VPS IPv4')
    if not all(re.search(r'^'+name+r'\s*=\s*\S+',text,re.M) for name in ('SOUNDCLOUD_CLIENT_ID','SOUNDCLOUD_CLIENT_SECRET','SOUNDCLOUD_REDIRECT_URI')):
        raise ValueError('Restore the original SoundCloud application configuration')
    replacement="FASTCLOUD_PUBLIC_IP='"+str(ip)+"'"
    return re.sub(r'^FASTCLOUD_PUBLIC_IP=.*$',replacement,text,flags=re.M) if re.search(r'^FASTCLOUD_PUBLIC_IP=',text,re.M) else text.rstrip()+'\n'+replacement+'\n'


def run(arguments, cwd=None, capture=False):
    return subprocess.run(arguments,cwd=cwd,check=True,text=True,capture_output=capture)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staging',type=Path,required=True)
    parser.add_argument('--ip',required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:raise ValueError('Run the restore as root on the new VPS')
    staging=args.staging.resolve()
    if staging.parent!=Path('/root') or not re.fullmatch(r'veirune-migration-[a-f0-9]{32}',staging.name):raise ValueError('Invalid private staging directory')
    target=Path('/root/fastcloud-backend')
    if target.exists():raise ValueError('Backend directory already exists; this command only restores a new VPS')
    for name in ('source.bundle','approvals.sqlite3','server.env','manifest.json'):
        if not (staging/name).is_file() or (staging/name).is_symlink():raise ValueError('Incomplete migration bundle')
    env=moved_environment((staging/'server.env').read_text(),args.ip)
    if not shutil.which('git'):
        run(['apt-get','update']);run(['apt-get','install','-y','git','ca-certificates'])
    # Clone a local Git bundle: recovery works even for a private GitHub repo.
    run(['git','clone','--branch','main',str(staging/'source.bundle'),str(target)])
    run(['git','remote','set-url','origin','https://github.com/COMF2222/veirune-backend.git'],target)
    with os.fdopen(os.open(target/'.env',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as output:output.write(env)
    sys.path.insert(0,str(target))
    import backup
    counts=backup.inspect(staging/'approvals.sqlite3')
    if counts!=json.loads((staging/'manifest.json').read_text())['counts']:raise ValueError('Migration row counts do not match the checked backup')
    if not shutil.which('docker'):run(['bash','tools/bootstrap-ubuntu.sh'],target)
    compose=['docker','compose','-f','compose.ip.yaml']
    run(compose+['build'],target)
    # A root-only staging directory keeps the readable bind source private.
    (staging/'approvals.sqlite3').chmod(0o644)
    restore="import backup,os,json; print(json.dumps(backup.restore('/restore.sqlite3','/data/approvals.sqlite3'))); os.chown('/data/approvals.sqlite3',10001,10001)"
    restored=run(compose+['run','--rm','--no-deps','--user','0:0','-v',str(staging/'approvals.sqlite3')+':/restore.sqlite3:ro','fastcloud','python','-c',restore],target,True)
    if json.loads(restored.stdout)!=counts:raise ValueError('Restored database failed its row-count check')
    run(['bash','deploy_checked.sh','-f','compose.ip.yaml'],target)
    run(compose+['up','-d','ip-proxy'],target)
    run(compose+['exec','-T','fastcloud','python','-c',"import os,shutil; assert shutil.disk_usage('/media').free > int(os.environ.get('FASTCLOUD_MEDIA_MIN_FREE_BYTES',3*1024**3))+32*1024**2, 'Insufficient free disk for playback'"],target)
    outbound="""import urllib.request,urllib.error
with urllib.request.urlopen('https://soundcloud.com/',timeout=12) as response: assert response.status==200
try:
    urllib.request.urlopen('https://api.soundcloud.com/me',timeout=12)
except urllib.error.HTTPError as error:
    assert error.code==401, 'SoundCloud API is not reachable as expected'
    error.close()
print('SoundCloud website and API TLS reachability verified')
"""
    run(compose+['exec','-T','fastcloud','python','-c',outbound],target)
    run(['python3','tools/check-deployment.py','--url','https://'+args.ip],target)
    # Do not silently restore a different API app: existing rotating grants
    # must continue to belong to the same Client ID/secret and redirect URI.
    with urllib.request.urlopen('https://'+args.ip+'/v1/config',timeout=10) as response:config=json.load(response)
    import shlex
    expected=re.search(r'^SOUNDCLOUD_CLIENT_ID=(.*)$',env,re.M)
    if not expected or config.get('client_id')!=shlex.split(expected[1])[0]:raise ValueError('The restored server has a different SoundCloud application')
    print('Restored original database and SoundCloud application; HTTPS and health passed. Endpoint announcement has NOT changed.')
    # Only these known sensitive staging files are removed after verification.
    for name in ('approvals.sqlite3','server.env','manifest.json','source.bundle'):(staging/name).unlink()


if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Restore failed; the public endpoint was not changed. Inspect the private staging directory on the new VPS.')
