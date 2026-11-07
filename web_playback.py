"""Public SoundCloud web streams played by the device's licensed DRM engine.

The broker never extracts content keys or decodes protected audio. Licenses are
opaque CDM messages; short-lived capabilities are kept in memory, not the DB.
"""
import json
import math
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from media import MediaError, KeyedLocks, ExpiredMediaURL
from recordings import preview, urn as track_urn

MAX_RESOURCE = 8 * 1024**2
KEY_FORMATS = {'urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed', 'com.microsoft.playready', 'com.apple.streamingkeydelivery'}


@dataclass
class WebData:
    data: bytes
    content_type: str = 'application/octet-stream'
    status: int = 200


def checked_url(value):
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password
            or not (host in {'soundcloud.com', 'api-v2.soundcloud.com', 'playback.media-streaming.soundcloud.cloud',
                            'license.media-streaming.soundcloud.cloud'} or host.endswith('.sndcdn.com'))):
        raise MediaError(422, 'Unsupported SoundCloud playback host')
    if any(k.casefold() in {'access_token', 'oauth_token', 'client_secret'}
           for k, _ in urllib.parse.parse_qsl(parsed.query)):
        raise MediaError(422, 'Credential-bearing playback URL is not supported')
    return value


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        checked_url(newurl)
        # POST license challenges must never be redirected to another service.
        if request.get_method() != 'GET': raise MediaError(502, 'Unexpected license redirect')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def parse_manifest(text, base, expected_ms):
    if not text.lstrip().startswith('#EXTM3U') or '#EXT-X-ENDLIST' not in text:
        raise MediaError(422, 'A complete SoundCloud HLS playlist is required')
    lines, assets, seconds = [], [], 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(('#EXT-X-STREAM-INF', '#EXT-X-BYTERANGE')):
            raise MediaError(422, 'Unsupported SoundCloud HLS layout')
        if line.startswith('#EXT-X-KEY:'):
            fmt = re.search(r'KEYFORMAT="([^"]+)"', line)
            uri = re.search(r'URI="([^"]+)"', line)
            if (not fmt or fmt[1] not in KEY_FORMATS or not uri
                    or not uri[1].startswith(('data:', 'skd:'))):
                raise MediaError(422, 'Unsupported SoundCloud DRM signaling')
        if line.startswith('#EXTINF:'):
            try: length = float(line.split(':', 1)[1].split(',')[0])
            except ValueError: raise MediaError(422, 'Invalid HLS duration') from None
            if not math.isfinite(length) or length <= 0: raise MediaError(422, 'Invalid HLS duration')
            seconds += length
            if seconds > 24*3600: raise MediaError(422, 'Invalid HLS duration')
        if line.startswith('#EXT-X-MAP:') and 'BYTERANGE=' in line:
            raise MediaError(422, 'Unsupported SoundCloud HLS layout')
        match = re.search(r'URI="([^"]+)"', line) if line.startswith('#EXT-X-MAP:') else None
        if match or line and not line.startswith('#'):
            url = checked_url(urllib.parse.urljoin(base, match[1] if match else line))
            if urllib.parse.urlsplit(url).hostname in {'soundcloud.com', 'api-v2.soundcloud.com', 'license.media-streaming.soundcloud.cloud'}:
                raise MediaError(422, 'Audio assets must be on a SoundCloud media host')
            marker = '@WEBASSET'+str(len(assets))+'@'
            assets.append(url)
            line = line[:match.start(1)]+marker+line[match.end(1):] if match else marker
        lines.append(line)
    duration = round(seconds * 1000)
    if not 0 < duration <= 24*3600*1000 or not assets or len(assets) > 4096:
        raise MediaError(422, 'Invalid full recording')
    if expected_ms and duration + 10000 < expected_ms:
        raise MediaError(422, 'SoundCloud returned only a preview')
    return lines, assets, duration


class PublicWebPlayback:
    def __init__(self, accounts, *, fetch=None):
        self.accounts = accounts
        self.lock = threading.RLock()
        self.source_locks = KeyedLocks()
        self.resolves = threading.BoundedSemaphore(4)
        self.leases, self.sources = {}, {}
        self.client_id, self.client_time = None, 0
        self.warm_queue = queue.Queue(maxsize=4)
        self.warm_jobs, self.warm_worker = {}, None
        self.opener = urllib.request.build_opener(Redirect())
        self.transport = fetch or self.fetch

    def fetch(self, url, body=None, headers=None):
        checked_url(url)
        request = urllib.request.Request(url, data=body, headers={'User-Agent': 'Veirune-playback/1', 'Accept-Encoding': 'identity', **(headers or {})})
        try:
            with self.opener.open(request, timeout=12) as response:
                checked_url(response.geturl())
                data = response.read(MAX_RESOURCE+1)
                if len(data) > MAX_RESOURCE: raise MediaError(422, 'Playback resource exceeds the size limit')
                return WebData(data, response.headers.get('Content-Type', 'application/octet-stream'), response.status)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise MediaError(status if status in (401,403,404,410,429) else 502, 'SoundCloud playback request failed') from None
        except (OSError, urllib.error.URLError):
            raise MediaError(502, 'Could not reach SoundCloud playback') from None

    def read(self, url, body=None, headers=None):
        checked_url(url)
        result = self.transport(url, body, headers)
        if len(result.data) > MAX_RESOURCE: raise MediaError(422, 'Playback resource exceeds the size limit')
        return result

    def text(self, url):
        return self.read(url).data.decode('utf-8')

    def source(self, urn, original, *, force=False, drm='widevine'):
        # Single flight per recording/platform; background catalog scans never
        # hold a global lock ahead of a listener starting a different recording.
        with self.source_locks.hold('web-source:'+urn+':'+drm):
            now = time.monotonic()
            found = self.sources.get((urn,drm))
            if not force and found and found['created']+30 > now: return found
            permalink = urllib.parse.urlsplit(original.get('permalink_url') or '')
            if permalink.scheme != 'https' or permalink.hostname != 'soundcloud.com' or permalink.username or permalink.password or permalink.port not in (None,443) or not re.fullmatch(r'/[^/]+/[^/]+/?', permalink.path):
                raise MediaError(422, 'A public SoundCloud track address is required')
            page = self.text(urllib.parse.urlunsplit(('https','soundcloud.com',permalink.path,'','')))
            match = re.search(r'window\.__sc_hydration\s*=\s*(\[.*?\]);', page, re.S)
            try:
                entries = json.loads(match[1]) if match else []
                track = next(x['data'] for x in entries if x.get('hydratable') == 'sound' and x.get('data', {}).get('id') == int(urn.rsplit(':',1)[1]))
            except (ValueError, TypeError, StopIteration, KeyError):
                raise MediaError(422, 'SoundCloud web recording could not be verified') from None
            if track.get('sharing') != 'public' or track.get('policy') not in ('ALLOW','MONETIZE') or track.get('streamable') is not True:
                raise MediaError(422, 'This recording is not available for full web playback')
            if not self.client_id or self.client_time+6*3600 < now:
                scripts = re.findall(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/[^"]+)"', page)[-3:]
                for script in reversed(scripts):
                    match = re.search(r'client_id\s*:\s*"([a-zA-Z0-9]{32})"', self.text(script))
                    if match:
                        self.client_id, self.client_time = match[1], now
                        break
                else: raise MediaError(502, 'SoundCloud playback client is unavailable')
            protocols = ('hls','cbc-encrypted-hls') if drm == 'fairplay' else ('hls','ctr-encrypted-hls')
            candidates = [t for t in (track.get('media') or {}).get('transcodings', [])
                          if isinstance(t,dict) and t.get('snipped') is False and (t.get('format') or {}).get('protocol') in protocols]
            candidates.sort(key=lambda t: ((t.get('format') or {}).get('protocol') != 'hls', 'aac_160k' not in str(t.get('preset')), (t.get('format') or {}).get('protocol') != 'ctr-encrypted-hls'))
            if not candidates: raise MediaError(422, 'No full SoundCloud web stream is available')
            transcoding = candidates[0]
            target = checked_url(transcoding.get('url',''))
            target_parts = urllib.parse.urlsplit(target)
            if target_parts.hostname != 'api-v2.soundcloud.com' or not target_parts.path.startswith('/media/'):
                raise MediaError(422, 'Invalid SoundCloud transcoding address')
            params = urllib.parse.urlencode({'client_id': self.client_id, 'track_authorization': track.get('track_authorization','')})
            stream = json.loads(self.text(target+('&' if target_parts.query else '?')+params))
            manifest_url = checked_url(stream.get('url',''))
            expected = track.get('full_duration') or track.get('duration') or original.get('duration') or 0
            if not isinstance(expected,int) or isinstance(expected,bool): raise MediaError(422, 'Invalid recording duration')
            lines, assets, duration = parse_manifest(self.text(manifest_url), manifest_url, expected)
            license_token = stream.get('licenseAuthToken')
            encrypted = (transcoding.get('format') or {}).get('protocol') != 'hls'
            if encrypted and (not isinstance(license_token,str) or not 0 < len(license_token) < 16384):
                raise MediaError(422, 'SoundCloud did not provide a playback license token')
            found = {'created': now, 'lines': lines, 'assets': assets, 'duration': duration, 'token': license_token if encrypted else None,
                     'client_id': self.client_id, 'original': original, 'urn': urn, 'system': drm, 'version': track.get('last_modified') or original.get('last_modified') or '',
                     'bitrate': 160 if '160k' in str(transcoding.get('preset')) else 96 if '96k' in str(transcoding.get('preset')) else 128}
            with self.lock:
                self.sources = {k:v for k,v in self.sources.items() if v['created']+60 > now}
                if len(self.sources) >= 32: self.sources.pop(next(iter(self.sources)))
                self.sources[(urn,drm)] = found
            return found

    def resolve(self, urn, token, *, drm='widevine'):
        if not isinstance(urn,str) or not re.fullmatch(r'soundcloud:tracks:[1-9][0-9]{0,19}',urn): raise MediaError(400,'Invalid track URN')
        if drm not in ('widevine','fairplay'): raise MediaError(400,'Unsupported DRM platform')
        user = self.accounts.identity(token)
        if not self.resolves.acquire(timeout=3): raise MediaError(503,'Playback resolution is busy')
        try:
            original = self.accounts.api_json('https://api.soundcloud.com/tracks/'+urllib.parse.quote(urn,safe=''), token=token)
            if original.get('sharing') != 'public' or original.get('access') not in ('playable','preview') or original.get('streamable') is not True:
                raise MediaError(422,'This track is not available for web playback')
            source = self.source(urn, original, drm=drm)
            ticket = secrets.token_urlsafe(32)
            with self.lock:
                now = time.monotonic()
                self.leases = {k:v for k,v in self.leases.items() if v['expires'] > now}
                if len(self.leases) >= 1024: raise MediaError(503,'Playback capacity reached')
                self.leases[ticket] = {'expires': now+4*3600, 'user': user, 'source': source, 'oauth': token}
                self.register_cache(ticket, self.leases[ticket])
            base = '/v1/media/web/'+ticket
            systems = [('com.apple.fps','fairplay')] if drm == 'fairplay' else [('com.widevine.alpha','widevine'),('com.microsoft.playready','playready')]
            return {'playlist_path': base+'/index.m3u8', 'license_paths': {name:base+'/license/'+kind for name,kind in systems} if source['token'] else {},
                    'duration_ms': source['duration'], 'bitrate_kbps': source['bitrate']}
        finally: self.resolves.release()

    def lease(self, ticket):
        with self.lock: lease = self.leases.get(ticket)
        if not lease or lease['expires'] <= time.monotonic(): raise MediaError(410,'Playback link expired')
        if not self.accounts.permitted(lease['user']): raise MediaError(403,'Access to Veirune was disabled by the owner')
        return lease

    def started(self, ticket):
        lease = self.lease(ticket)
        original = lease['source']['original']
        user = original.get('user') or {}
        artist_id = user.get('id')
        artist_urn = user.get('urn')
        if isinstance(artist_urn,str) and re.fullmatch(r'soundcloud:users:[1-9][0-9]{0,19}',artist_urn):
            parsed_id = int(artist_urn.rsplit(':',1)[1])
            if artist_id is not None and artist_id != parsed_id: return
            artist_id = parsed_id
        if not isinstance(artist_id,int) or isinstance(artist_id,bool) or artist_id <= 0: return
        key = (artist_id, lease['source']['system'])
        with self.lock:
            now = time.monotonic()
            self.warm_jobs = {k:v for k,v in self.warm_jobs.items() if v > now}
            if key in self.warm_jobs: return
            try: self.warm_queue.put_nowait((key,lease['user'],lease['oauth']))
            except queue.Full: return
            self.warm_jobs[key] = now+24*3600
            if self.warm_worker is None:
                self.warm_worker = threading.Thread(target=self._warm_loop,name='veirune-catalog-cache',daemon=True)
                self.warm_worker.start()

    def _warm_loop(self):
        while True:
            key, user, token = self.warm_queue.get()
            complete = False
            try: complete = self._warm(key,user,token)
            except Exception: pass  # No upstream URLs or credentials in logs.
            finally:
                if not complete:
                    with self.lock: self.warm_jobs[key] = time.monotonic()+15*60
                self.warm_queue.task_done()
                token = None

    def _warm(self, key, user, token):
        if not hasattr(self.accounts,'register_web_manifest'): return False
        artist_id, drm = key
        path = '/users/'+urllib.parse.quote('soundcloud:users:'+str(artist_id),safe='')+'/tracks'
        url = 'https://api.soundcloud.com'+path+'?'+urllib.parse.urlencode({'limit':200,'access':'playable,preview','linked_partitioning':'true'})
        deadline = time.monotonic()+20*60
        budget = min(2*1024**3,max(0,self.accounts.max_bytes//4))
        downloaded, seen = 0, set()
        for _ in range(100):
            if not self.accounts.permitted(user) or time.monotonic() >= deadline or downloaded >= budget: return False
            data = self.accounts.api_json(url,token=token)
            rows = data.get('collection',[]) if isinstance(data,dict) else data if isinstance(data,list) else []
            for track in rows[:200]:
                target = track_urn(track) if isinstance(track,dict) else None
                if (not target or target in seen or not preview(track) or track.get('sharing') != 'public'
                        or track.get('streamable') is not True): continue
                seen.add(target)
                if not self.accounts.permitted(user) or time.monotonic() >= deadline or downloaded >= budget: return False
                try: source = self.source(target,track,drm=drm)
                except MediaError as error:
                    if error.status in (429,503,504): return False
                    continue
                ticket = secrets.token_urlsafe(32)
                lease = {'user':user,'source':source}
                try:
                    self.register_cache(ticket,lease)
                    for cache_key in lease['cache_keys']:
                        if self.accounts.contains(cache_key): continue
                        if downloaded >= budget or time.monotonic() >= deadline or not self.accounts.permitted(user): return False
                        stream, size = self.accounts.web_segment(ticket,cache_key)
                        with stream: pass  # Only encrypted bytes reach disk.
                        downloaded += size
                        time.sleep(.05)
                except MediaError as error:
                    if error.status in (429,503,504): return False
                finally:
                    with self.accounts.state_lock: self.accounts.tickets.pop(ticket,None)
                time.sleep(.15)
            next_url = data.get('next_href') if isinstance(data,dict) else None
            if not next_url: return True
            parsed = urllib.parse.urlsplit(next_url)
            if (parsed.scheme != 'https' or parsed.hostname != 'api.soundcloud.com' or parsed.username or parsed.password
                    or parsed.port not in (None,443) or urllib.parse.unquote(parsed.path) not in ('/users/soundcloud:users:'+str(artist_id)+'/tracks','/users/'+str(artist_id)+'/tracks')): return False
            url = next_url
        return False

    def register_cache(self, ticket, lease):
        if hasattr(self.accounts, 'register_web_manifest'):
            source = lease['source']
            lease['cache_keys'] = self.accounts.register_web_manifest(ticket, lease['user'], source['urn'], source['assets'], source['version']+'|'+source['system'], source['bitrate'])

    def asset(self, ticket, lease, index):
        if 'cache_keys' not in lease: return self.read(lease['source']['assets'][index])
        stream, size = self.accounts.web_segment(ticket, lease['cache_keys'][index])
        with stream:
            data = stream.read(MAX_RESOURCE+1)
        if len(data) != size or len(data) > MAX_RESOURCE: raise MediaError(502,'Incomplete playback segment')
        return WebData(data)

    def resource(self, ticket, name, *, body=None):
        lease = self.lease(ticket)
        if name == 'started' and body is not None:
            self.started(ticket)
            return WebData(b'{}','application/json')
        source = lease['source']
        if name == 'index.m3u8' and body is None:
            source = self.source(source['urn'], source['original'], drm=source['system'])
            lease['source'] = source
            self.register_cache(ticket, lease)
            text = '\n'.join(source['lines'])+'\n'
            for i in range(len(source['assets'])): text = text.replace('@WEBASSET'+str(i)+'@','/v1/media/web/'+ticket+'/asset/'+str(i))
            return WebData(text.encode(),'application/vnd.apple.mpegurl')
        match = re.fullmatch(r'asset/([0-9]{1,4})',name)
        if match and body is None:
            index = int(match[1])
            if index >= len(source['assets']): raise MediaError(404,'Unknown playback asset')
            try: return self.asset(ticket, lease, index)
            except MediaError as error:
                if not isinstance(error,ExpiredMediaURL) and error.status not in (401,403,410): raise
                renewed = self.source(source['urn'],source['original'],force=True,drm=source['system'])
                if len(renewed['assets']) != len(source['assets']): raise MediaError(410,'Recording changed; start it again') from None
                lease['source'] = renewed
                self.register_cache(ticket, lease)
                return self.asset(ticket, lease, index)
        match = re.fullmatch(r'license/(widevine|playready|fairplay)',name)
        if match and (body is not None or match[1] == 'fairplay'):
            if (match[1] == 'fairplay') != (source['system'] == 'fairplay'):
                raise MediaError(404,'License system does not match this recording')
            if body is not None and not 0 < len(body) <= 1024**2: raise MediaError(413,'Invalid CDM request size')
            source = self.source(source['urn'],source['original'],drm=source['system'])
            lease['source'] = source
            if not source['token']: raise MediaError(422,'This stream does not require a license')
            url = 'https://license.media-streaming.soundcloud.cloud/playback/'+match[1]+'?'+urllib.parse.urlencode({'license_token':source['token']})
            return self.read(url,body,{'Content-Type':'application/json' if match[1]=='fairplay' else 'application/octet-stream','Origin':'https://soundcloud.com','Referer':'https://soundcloud.com/','X-SC-Application-Id':source['client_id']})
        raise MediaError(404,'Unknown playback resource')
