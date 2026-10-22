"""CI-only runtime check of the built server image, user and persistent volume."""
import json
import subprocess
import time
import urllib.request
import uuid
from werkzeug.security import generate_password_hash

name='fjordbudget-server-smoke-'+uuid.uuid4().hex[:12]
volume=name+'-data'


def docker(*args):
    return subprocess.run(['docker',*args],check=True,capture_output=True,text=True).stdout


try:
    docker('volume','create',volume)
    docker('run','-d','--name',name,'--read-only','--tmpfs','/tmp',
        '--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--mount',f'type=volume,source={volume},destination=/data',
        '-p','127.0.0.1::8088','-e','SERVER_URL=http://localhost:8088',
        '-e','ADMIN_PASSWORD_HASH='+generate_password_hash(uuid.uuid4().hex),
        'fjordbudget-server-test')
    info=json.loads(docker('inspect',name))[0]
    port=info['NetworkSettings']['Ports']['8088/tcp'][0]['HostPort']
    for attempt in range(30):
        try:
            with urllib.request.urlopen(f'http://localhost:{port}/api/health',timeout=2) as reply:
                assert json.load(reply)=={'ok':True}
            break
        except (OSError,AssertionError):
            if attempt==29:
                raise
            time.sleep(.5)
    assert docker('exec',name,'id','-u').strip()=='10001'
    public=docker('exec',name,'python','-m','fjordbudget_server.keys','/data').strip()
    docker('restart',name)
    # Keys and encrypted database must survive a process/container restart.
    assert docker('exec',name,'python','-m','fjordbudget_server.keys','/data').strip()==public
    print('Server image started as non-root with a read-only root, passed health and retained keys.')
finally:
    subprocess.run(['docker','rm','-f',name],check=False,capture_output=True)
    subprocess.run(['docker','volume','rm',volume],check=False,capture_output=True)
