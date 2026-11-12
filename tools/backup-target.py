"""Follow only signed owned endpoint moves; SSH still checks the new host key."""
import argparse
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request


def target(server, announcement):
    previous='https://'+server.split('@',1)[1]
    if previous not in ['https://89.125.126.32','https://80.47.238.200','https://77.223.107.226',announcement['origin'],*announcement.get('previous',[])]:return server
    host=urllib.parse.urlsplit(announcement['origin']).hostname
    if not host or not re.fullmatch(r'[a-z0-9][a-z0-9.-]+',host):return server
    return server.split('@',1)[0]+'@'+host


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--server',required=True);parser.add_argument('--cache',type=Path,required=True);args=parser.parse_args()
    selected=args.server
    try:
        import base64
        from service_endpoint import verified,PUBLIC
        from cryptography.hazmat.primitives import serialization
        public=serialization.load_der_public_key(base64.b64decode(PUBLIC.read_text().strip(),validate=True))
        try:old=verified(json.loads(args.cache.read_text()),public) if args.cache.exists() else None
        except Exception:old=None
        if old:selected=target(selected,old)
        request=urllib.request.Request('https://veirune.comf.workers.dev/service.json',headers={'Cache-Control':'no-cache'})
        try:
            with urllib.request.urlopen(request,timeout=4) as response:
                raw=response.read(8193)
                if len(raw)>8192:raise ValueError('Too large')
            envelope=json.loads(raw);next=verified(envelope,public)
            if old and (next['serial']<old['serial'] or next['serial']==old['serial'] and next['origin']!=old['origin']):raise ValueError('Rollback')
            selected=target(args.server,next)
            temporary=args.cache.with_suffix('.json.part');temporary.write_text(json.dumps(envelope));os.replace(temporary,args.cache)
        except Exception:pass
    except Exception:pass
    print(selected)


if __name__=='__main__':main()
