"""Sign a public endpoint announcement locally; no user credentials are published."""
import argparse
import base64
import ctypes
from ctypes import wintypes
import ipaddress
import json
import os
import re
from pathlib import Path
import time
import urllib.parse
import urllib.request
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / 'tools/service-endpoint-public-key.txt'
SERVICE = 'veirune-main'


def origin(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None,443) or parsed.path not in ('','/') or parsed.query or parsed.fragment
            or parsed.hostname == 'localhost' or parsed.hostname.endswith(('.localhost','.local'))):
        raise ValueError('Use a public HTTPS origin without path or credentials')
    try: address = ipaddress.ip_address(parsed.hostname)
    except ValueError: address = None
    if address is not None and not address.is_global: raise ValueError('A public server address is required')
    return urllib.parse.urlunsplit(('https',parsed.netloc.lower(),'','',''))


def protect(data, *, decrypt=False):
    if os.name != 'nt': raise ValueError('The signing key belongs to the owner\'s Windows account')
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    raw=(ctypes.c_ubyte*len(data)).from_buffer_copy(data)
    entropy=b'Veirune service endpoint v1'
    salt=(ctypes.c_ubyte*len(entropy)).from_buffer_copy(entropy)
    source,extra,result=Blob(len(data),raw),Blob(len(entropy),salt),Blob()
    api=ctypes.WinDLL('crypt32',use_last_error=True)
    function=api.CryptUnprotectData if decrypt else api.CryptProtectData
    function.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    function.restype=wintypes.BOOL
    if not function(ctypes.byref(source),None,ctypes.byref(extra),None,None,1,ctypes.byref(result)):
        raise ValueError('Could not unlock the endpoint signing key with this Windows account')
    kernel=ctypes.WinDLL('kernel32');kernel.LocalFree.argtypes=[ctypes.c_void_p]
    try:return ctypes.string_at(result.data,result.size)
    finally:kernel.LocalFree(result.data)


def signed(key, target, serial, previous):
    payload=json.dumps({'service':SERVICE,'serial':serial,'origin':origin(target),'previous':previous[-16:],
                        'issuedAt':int(time.time())},separators=(',',':')).encode()
    signature=key.sign(payload,ec.ECDSA(hashes.SHA256()))
    return {'schema':1,'payload':base64.b64encode(payload).decode(),'signature':base64.b64encode(signature).decode()}


def verified(envelope, public):
    if envelope.get('schema') != 1: raise ValueError('Unsupported endpoint announcement')
    payload=base64.b64decode(envelope['payload'],validate=True)
    public.verify(base64.b64decode(envelope['signature'],validate=True),payload,ec.ECDSA(hashes.SHA256()))
    data=json.loads(payload)
    if data['service'] != SERVICE or not 0 < data['serial'] < 2**53: raise ValueError('Invalid endpoint identity or sequence')
    data['origin']=origin(data['origin'])
    return data


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin',required=True)
    parser.add_argument('--website',type=Path,default=ROOT.parent/'fastcloud-website/public')
    parser.add_argument('--key',type=Path,default=Path('D:/VeiruneBackups/service-endpoint-signing.dpapi'))
    parser.add_argument('--init',action='store_true')
    args=parser.parse_args()
    target=origin(args.origin)
    if not args.website.is_dir():raise ValueError('Website public directory is missing')
    if not args.key.exists():
        if not args.init or PUBLIC.exists():raise ValueError('Endpoint signing key is missing; never replace an existing trusted key')
        key=ec.generate_private_key(ec.SECP256R1())
        private=key.private_bytes(serialization.Encoding.DER,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        args.key.parent.mkdir(parents=True,exist_ok=True)
        with args.key.open('xb') as output:output.write(protect(private))
        PUBLIC.write_text(base64.b64encode(key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).decode()+'\n',encoding='ascii')
    key=serialization.load_der_private_key(protect(args.key.read_bytes(),decrypt=True),password=None)
    public=serialization.load_der_public_key(base64.b64decode(PUBLIC.read_text().strip(),validate=True))
    if key.public_key().public_numbers()!=public.public_numbers():raise ValueError('Signing key does not match the trusted public key')
    # Switch announcements only after a real certificate and both public probes pass.
    with urllib.request.urlopen(target+'/health',timeout=10) as response:health=json.load(response)
    with urllib.request.urlopen(target+'/v1/config',timeout=10) as response:config=json.load(response)
    if health.get('status')!='ok' or not config.get('client_id'):raise ValueError('New backend is not ready')
    path=args.website/'service.json'
    old=verified(json.loads(path.read_text()),public) if path.exists() else None
    previous=list(dict.fromkeys((old.get('previous',[])+[old['origin']]) if old else ['https://80.47.238.200']))
    value=signed(key,target,(old['serial']+1) if old else 1,previous)
    temporary=path.with_suffix('.json.part');temporary.write_text(json.dumps(value,indent=2)+'\n',encoding='ascii');os.replace(temporary,path)
    (args.website/'backend-origin.js').write_text('export const backendOrigin = '+json.dumps(target)+';\n',encoding='ascii')
    headers=args.website/'_headers'
    if headers.exists():
        text=headers.read_text()
        text=re.sub(r'connect-src\s+[^;\n]+',lambda match:match[0] if target in match[0].split() else match[0]+' '+target,text)
        headers.write_text(text,encoding='utf-8')
    print('Prepared signed endpoint announcement '+str((old['serial']+1) if old else 1)+'. No website deployment or live traffic change was performed.')


if __name__=='__main__':
    try:main()
    except Exception as error:raise SystemExit('Endpoint preparation failed: '+str(error))
