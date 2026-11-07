import json
import tempfile
import time
import unittest
import urllib.parse
from unittest.mock import patch

from media import MediaError
from web_playback import PublicWebPlayback, WebData
from media import MediaCache
from test_media import Response


class Accounts:
    def __init__(self):
        self.allowed = {42}
        self.track = {'id': 7, 'sharing': 'public', 'access': 'preview', 'streamable': True,
                      'duration': 127000, 'permalink_url': 'https://soundcloud.com/artist/song'}
        self.permitted = lambda user: user in self.allowed

    def identity(self, token):
        if token != 'listener': raise MediaError(401, 'Sign in')
        if not self.permitted(42): raise MediaError(403, 'Disabled')
        return 42

    def api_json(self, url, *, token): return dict(self.track)


class WebPlaybackTest(unittest.TestCase):
    def setUp(self):
        self.accounts = Accounts()
        self.calls = []
        self.web_track = {'id': 7, 'sharing': 'public', 'policy': 'MONETIZE', 'streamable': True,
                          'duration': 127000, 'full_duration': 127000, 'track_authorization': 'public-authorization',
                          'media': {'transcodings': [{'url': 'https://api-v2.soundcloud.com/media/soundcloud:tracks:7/stream',
                                    'preset': 'aac_160k', 'snipped': False, 'format': {'protocol': 'ctr-encrypted-hls'}}]}}
        self.playlist = '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,URI="data:text/plain;base64,AAAA",KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:127,\na.m4s\n#EXT-X-ENDLIST\n'
        self.playback = PublicWebPlayback(self.accounts, fetch=self.fetch)

    def fetch(self, url, body=None, headers=None):
        self.calls.append((url, body, headers or {}))
        path = urllib.parse.urlsplit(url).path
        if path == '/artist/song':
            page = '<script src="https://a-v2.sndcdn.com/assets/app.js"></script><script>window.__sc_hydration = '+json.dumps([{'hydratable': 'sound', 'data': self.web_track}])+';</script>'
            return WebData(page.encode(), 'text/html')
        if path.endswith('/app.js'): return WebData(b'client_id:"'+b'a'*32+b'"', 'text/javascript')
        if path.startswith('/media/'):
            return WebData(json.dumps({'url': 'https://cf-hls-media.sndcdn.com/stream.m3u8', 'licenseAuthToken': 'private-license-ticket'}).encode(), 'application/json')
        if path == '/stream.m3u8': return WebData(self.playlist.encode(), 'application/vnd.apple.mpegurl')
        if path == '/playback/widevine': return WebData(b'opaque-cdm-license', 'application/octet-stream')
        return WebData(b'encrypted-segment', 'application/octet-stream')

    def resolve(self):
        result = self.playback.resolve('soundcloud:tracks:7', 'listener')
        return result, result['playlist_path'].split('/')[4]

    def test_public_preview_gets_a_full_eme_lease_without_exposing_license_tokens(self):
        result, ticket = self.resolve()
        self.assertEqual(result['duration_ms'], 127000)
        self.assertNotIn('private-license-ticket', json.dumps(result))
        manifest = self.playback.resource(ticket, 'index.m3u8').data.decode()
        self.assertIn('data:text/plain;base64,AAAA', manifest)
        self.assertIn('/v1/media/web/'+ticket+'/asset/0', manifest)
        self.assertNotIn('sndcdn.com', manifest)

    def test_unapproved_or_signed_out_users_never_trigger_web_requests(self):
        for token in ('invalid', 'listener'):
            self.accounts.allowed.clear()
            with self.assertRaises(MediaError): self.playback.resolve('soundcloud:tracks:7', token)
        self.assertEqual(self.calls, [])

    def test_private_blocked_and_mismatched_web_recordings_are_rejected(self):
        for change in ({'sharing': 'private'}, {'policy': 'BLOCK'}, {'policy': 'SNIP'}, {'id': 8}):
            original = dict(self.web_track)
            self.web_track.update(change)
            with self.assertRaises(MediaError): self.resolve()
            self.web_track = original

    def test_thirty_second_stream_and_incomplete_playlist_are_rejected(self):
        for playlist in (self.playlist.replace('127,', '30,'), self.playlist.replace('#EXT-X-ENDLIST', '')):
            self.playlist = playlist
            with self.assertRaises(MediaError): self.resolve()

    def test_manifest_cannot_turn_the_broker_into_an_arbitrary_url_proxy(self):
        self.playlist = self.playlist.replace('a.m4s', 'https://127.0.0.1/internal')
        with self.assertRaises(MediaError): self.resolve()
        self.assertFalse(any('127.0.0.1' in call[0] for call in self.calls))

    def test_license_is_an_opaque_post_to_a_fixed_soundcloud_endpoint(self):
        _, ticket = self.resolve()
        result = self.playback.resource(ticket, 'license/widevine', body=b'cdm-challenge')
        self.assertEqual(result.data, b'opaque-cdm-license')
        url, body, headers = self.calls[-1]
        self.assertEqual(urllib.parse.urlsplit(url).hostname, 'license.media-streaming.soundcloud.cloud')
        self.assertEqual(body, b'cdm-challenge')
        self.assertNotIn('Authorization', headers)
        self.assertEqual(headers['X-SC-Application-Id'], 'a'*32)
        with self.assertRaises(MediaError): self.playback.resource(ticket, 'license/anything', body=b'x')

    def test_revocation_and_expiry_apply_to_existing_leases(self):
        _, ticket = self.resolve()
        self.accounts.allowed.clear()
        with self.assertRaises(MediaError) as error: self.playback.resource(ticket, 'asset/0')
        self.assertEqual(error.exception.status, 403)
        self.accounts.allowed.add(42)
        with patch('web_playback.time.monotonic', return_value=time.monotonic()+5*3600):
            with self.assertRaises(MediaError) as error: self.playback.resource(ticket, 'index.m3u8')
        self.assertEqual(error.exception.status, 410)

    def test_caller_cannot_resolve_a_private_or_blocked_original(self):
        for change in ({'sharing': 'private'}, {'access': 'blocked'}, {'streamable': False}):
            original = dict(self.accounts.track)
            self.accounts.track.update(change)
            with self.assertRaises(MediaError): self.resolve()
            self.accounts.track = original
        self.assertEqual(self.calls, [])

    def test_invalid_durations_and_partial_init_ranges_are_rejected(self):
        original = self.playlist
        for value in ('NaN', 'inf', '-1', '0', '90000'):
            self.playlist = original.replace('127,', value+',')
            with self.assertRaises(MediaError): self.resolve()
        self.playlist = original.replace('URI="init.mp4"', 'URI="init.mp4",BYTERANGE="12@0"')
        with self.assertRaises(MediaError): self.resolve()

    def test_platforms_get_their_own_encrypted_rendition_and_license_system(self):
        self.web_track['media']['transcodings'].append({**self.web_track['media']['transcodings'][0], 'format': {'protocol':'cbc-encrypted-hls'}})
        widevine, _ = self.resolve()
        fairplay = self.playback.resolve('soundcloud:tracks:7','listener',drm='fairplay')
        self.assertEqual(set(fairplay['license_paths']), {'com.apple.fps'})
        self.assertNotIn('com.apple.fps',widevine['license_paths'])
        self.assertIsNot(self.playback.sources[('soundcloud:tracks:7','widevine')],self.playback.sources[('soundcloud:tracks:7','fairplay')])
        with self.assertRaises(MediaError): self.playback.resource(fairplay['playlist_path'].split('/')[4], 'license/widevine', body=b'challenge')

    def test_shared_source_expiry_refreshes_assets_without_changing_the_lease(self):
        _, ticket = self.resolve()
        self.playback.sources.clear()
        self.playback.leases[ticket]['source']['assets'][0] += '?expired=1'
        original = self.playback.transport
        def fetch(url, body=None, headers=None):
            if 'expired=1' in url: raise MediaError(403,'expired')
            return original(url,body,headers)
        self.playback.transport = fetch
        self.assertEqual(self.playback.resource(ticket,'asset/0').data,b'encrypted-segment')
        self.assertNotIn('expired=1',self.playback.leases[ticket]['source']['assets'][0])


class WebCacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = WebPlaybackTest('runTest')
        self.fixture.setUp()
        self.track = {**self.fixture.accounts.track, 'user':{'id':9,'username':'Artist'}}
        self.allowed = {42,43}
        self.pages, self.downloads = [], []
        self.cache = MediaCache(self.temp.name,lambda token:(int(token),'Listener','listener'),lambda user:user in self.allowed,self.api,
                                min_free=0,max_bytes=8*1024**2,max_segment=1024)
        def fetch(url,token=None):
            self.downloads.append(url)
            self.assertIsNone(token)
            return Response(b'ciphertext',url)
        self.cache.fetch = fetch
        self.playback = PublicWebPlayback(self.cache,fetch=self.fixture.fetch)

    def tearDown(self): self.temp.cleanup()

    def api(self,url,*,token):
        if '/users/' in url:
            self.pages.append(url)
            return self.catalog.pop(0)
        return dict(self.track)

    def resolve(self,token='42'):
        result = self.playback.resolve('soundcloud:tracks:7',token)
        return result['playlist_path'].split('/')[4]

    def test_listeners_share_encrypted_disk_cache_but_licenses_are_never_saved(self):
        first, second = self.resolve(), self.resolve('43')
        for ticket in (first,second):
            self.assertEqual(self.playback.resource(ticket,'asset/0').data,b'ciphertext')
            self.playback.resource(ticket,'license/widevine',body=b'opaque-challenge')
        self.assertEqual(len(self.downloads),1)
        self.assertEqual(self.cache.stats()['cache_hits'],1)
        files = list(self.cache.objects.glob('*.seg'))
        self.assertEqual(len(files),1)
        self.assertEqual(files[0].read_bytes(),b'ciphertext')
        self.allowed.remove(43)
        with self.assertRaises(MediaError): self.playback.resource(second,'asset/0')

    def test_only_actual_playback_enqueues_catalog_once_and_capacity_is_bounded(self):
        first, second = self.resolve(), self.resolve('43')
        self.playback.leases[first]['source']['original']['user'] = {'urn':'soundcloud:users:9','username':'Artist'}
        self.assertTrue(self.playback.warm_queue.empty())
        self.playback.warm_worker = object()  # Inspect queue before a worker consumes it.
        self.playback.resource(first,'started',body=b'{}')
        self.playback.resource(second,'started',body=b'{}')
        self.assertEqual(self.playback.warm_queue.qsize(),1)
        self.assertFalse(any('/playback/' in call[0] for call in self.fixture.calls))
        for _ in range(3): self.playback.warm_queue.put_nowait(None)
        self.track['user'] = {'id':10,'username':'Other artist'}
        # A distinct artist needs a new job, even with the same recording fixture.
        self.playback.leases[first]['source']['original'] = dict(self.track)
        self.playback.started(first)
        self.assertEqual(self.playback.warm_queue.qsize(),4)

    def test_catalog_paginates_caches_only_public_previews_and_never_fetches_licenses(self):
        ticket = self.resolve()
        source = self.playback.leases[ticket]['source']
        row = {**self.track, 'urn':'soundcloud:tracks:7'}
        self.catalog = [
            {'collection':[row,{**row,'urn':'soundcloud:tracks:8','access':'playable'}], 'next_href':'https://api.soundcloud.com/users/soundcloud%3Ausers%3A9/tracks?offset=200'},
            {'collection':[row,{**row,'urn':'soundcloud:tracks:10','sharing':'private'}]},
        ]
        with patch('web_playback.time.sleep'):
            self.assertTrue(self.playback._warm((9,'widevine'),42,'42'))
        self.assertEqual(len(self.pages),2)
        self.assertEqual(len(self.downloads),len(source['assets']))
        self.assertFalse(any('/playback/' in call[0] for call in self.fixture.calls))
        self.assertEqual(set(self.cache.tickets),{ticket})
        self.allowed.clear()
        self.assertFalse(self.playback._warm((9,'widevine'),42,'42'))
        self.assertEqual(len(self.pages),2)


if __name__ == '__main__': unittest.main()
