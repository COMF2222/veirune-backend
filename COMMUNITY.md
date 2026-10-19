# Community music

Deploy with the existing checked deployment script and Compose overlays. No new
environment variables are needed. Preflight and startup initialize
`community_editors`, `community_picks`, `community_tracks` and the discovery index.
Existing full SQLite backups include these tables.

Authenticated routes:

- `GET /v1/community/role`: current account's editor status and own picks.
- `GET /v1/community/home`: public popular tracks for today, yesterday and week,
  and the shared editorial collection.
- `POST /v1/community/picks`: `{trackId, active}`; editor or owner only.
- `POST /v1/admin/editors/{id}`: `{editor}`; configured owner only.

Editors cannot use the existing admin APIs. Revocation deletes their picks.
Only approved accounts contribute to rankings or editorial votes. Rankings count
one account per track after at least 30 seconds and one counted play across the
selected period; multiple devices and repeats are combined. A public ranked song
needs two listeners. Day buckets are UTC; week includes today and six prior days.
Top five songs per period are returned. Editorial limits are ten per editor and
thirty distinct tracks overall. Duplicate picks are idempotent and shared picks
appear once. Owners can remove all recommendations for a track.

Metadata is read server-side from SoundCloud, restricted to public tracks and
cached for five minutes. Missing metadata is omitted on upstream failure; private
history titles, credentials and stream URLs never become public fallbacks.

`X-Fastcloud-Preferences: 3` opts into the synchronized `show_community` boolean.
Old clients keep their previous preference fields and patch writes preserve it.
Update the backend before distributing desktop 0.4.22 / mobile 0.1.9.

Verify with `python -m unittest discover -q`. These are local fixture tests; they
do not verify production SoundCloud connectivity or VPS throughput.
