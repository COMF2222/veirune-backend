import json
import sqlite3
import unittest
import community
import personal
from datetime import datetime, timezone


class CommunityTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,status TEXT,updated_at INTEGER)")
        self.db.executemany("INSERT INTO users VALUES(?,?,?,0)", [(1,"Owner","approved"),(2,"Editor","approved"),(3,"Listener","approved"),(4,"Blocked","denied")])
        personal.initialize(self.db)
        community.initialize(self.db)

    def listen(self, uid, tid=10, plays=1, ms=30000, device="a", day="2026-10-19"):
        self.db.execute("INSERT OR REPLACE INTO listening VALUES(?,?,?,?,?,?,?,?,?,?)", (uid,device,day,tid,"Secret title","Artist","Rock",ms,plays,1792371600))

    def track(self, tid=10):
        return community.save_track(self.db, {"id":tid,"title":"Song","sharing":"public","duration":180000,"stream_url":"SECRET","user":{"id":9,"username":"Artist"}})

    def test_repeats_devices_and_days_count_one_listener_per_period(self):
        self.listen(1,plays=2000,ms=80000000)
        self.listen(1,device="b")
        self.assertEqual(community.ranking(self.db,"2026-10-13","2026-10-19"), [])
        self.listen(2)
        self.listen(2,day="2026-10-18")
        self.assertEqual(community.ranking(self.db,"2026-10-13","2026-10-19"), [{"trackId":10,"listeners":2}])
        self.listen(3,tid=11)
        self.listen(4)
        self.assertEqual(community.ranking(self.db,"2026-10-19","2026-10-19")[0]["listeners"],2)

    def test_short_attempts_and_outside_period_do_not_qualify(self):
        self.listen(1)
        self.listen(2,ms=29999)
        self.listen(3,day="2026-10-12")
        self.assertEqual(community.ranking(self.db,"2026-10-13","2026-10-19"),[])

    def test_roles_never_grant_owner_and_revocation_removes_picks(self):
        self.track()
        with self.assertRaises(PermissionError): community.pick(self.db,3,1,10,True)
        community.set_editor(self.db,2,True,1)
        self.assertEqual(community.role(self.db,2,1)["isOwner"],False)
        community.pick(self.db,2,1,10,True)
        community.pick(self.db,2,1,10,True)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM community_picks").fetchone()[0],1)
        community.set_editor(self.db,2,False,1)
        self.assertEqual(community.role(self.db,2,1)["pickedIds"],[])
        with self.assertRaises(PermissionError): community.pick(self.db,2,1,10,True)
        with self.assertRaises(ValueError): community.set_editor(self.db,4,True,1)

    def test_editors_cannot_remove_other_editors_votes_owner_can_moderate(self):
        self.track(); community.set_editor(self.db,2,True,1)
        community.pick(self.db,1,1,10,True); community.pick(self.db,2,1,10,True)
        community.pick(self.db,2,1,10,False)
        self.assertEqual(self.db.execute("SELECT user_id FROM community_picks").fetchall(),[(1,)])
        community.pick(self.db,2,1,10,True); community.pick(self.db,1,1,10,False)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM community_picks").fetchone()[0],0)

    def test_metadata_and_hidden_tracks_never_expose_private_history_or_streams(self):
        self.track(); self.listen(1); self.listen(2)
        data=community.snapshot(self.db,1,1,1792411200)
        hidden=community.present(data,{})
        self.assertEqual(hidden["today"],[])
        self.assertNotIn("Secret title",json.dumps(hidden))
        self.assertNotIn("SECRET",json.dumps(community.cached(self.db,[10])))
        for raw in ({"id":11,"title":"Private","sharing":"private"},{"id":True,"title":"bad","sharing":"public"}):
            with self.assertRaises(ValueError): community.save_track(self.db,raw)

    def test_pick_limits_and_boolean_preferences(self):
        for tid in range(1,11): self.track(tid); community.pick(self.db,1,1,tid,True)
        self.track(11)
        with self.assertRaises(ValueError): community.pick(self.db,1,1,11,True)
        with self.assertRaises(ValueError): personal.preference("show_community", "false")
        personal.preference("show_community", False)
        community.pick(self.db,1,1,1,False); community.pick(self.db,1,1,11,True)

    def test_public_urn_metadata_keeps_playback_policy_without_private_urls(self):
        row=community.public_track({"urn":"soundcloud:tracks:123","title":"Song","sharing":"public","streamable":True,"policy":"SNIP","access":"preview","stream_url":"SECRET","user":{"urn":"soundcloud:users:9","username":"Artist"}})
        self.assertEqual((row["id"],row["streamable"],row["policy"]),(123,True,"SNIP"))
        self.assertNotIn("SECRET", json.dumps(row))
        self.assertEqual(row["user"],{"id":9,"username":"Artist"})

    def test_hide_setting_syncs_and_legacy_clients_do_not_receive_unknown_fields(self):
        personal.write(self.db,1,{"preferences":{"show_community":False}},3)
        self.assertIs(personal.read(self.db,1,3)["preferences"]["show_community"],False)
        self.assertNotIn("show_community", personal.read(self.db,1,2)["preferences"])
        self.assertNotIn("show_community", personal.read(self.db,1,1)["preferences"])
        personal.write(self.db,1,{"preferences":{"autoplay":False}},1)
        self.assertIs(personal.read(self.db,1,3)["preferences"]["show_community"],False)

    def test_daily_and_weekly_windows_have_independent_unique_audiences(self):
        for uid in (1,2):
            self.listen(uid,tid=10,day="2026-10-18")
            self.listen(uid,tid=11)
        self.listen(3,tid=10)
        now=int(datetime(2026,10,19,12,tzinfo=timezone.utc).timestamp())
        data=community.snapshot(self.db,1,1,now)
        self.assertEqual(data["today"],[{"trackId":11,"listeners":2}])
        self.assertEqual(data["yesterday"],[{"trackId":10,"listeners":2}])
        self.assertEqual(data["week"],[{"trackId":10,"listeners":3},{"trackId":11,"listeners":2}])

    def test_shared_capacity_votes_and_blocked_editors(self):
        for uid in (2,3): community.set_editor(self.db,uid,True,1)
        self.db.execute("INSERT INTO users VALUES(5,'Other','approved',0)")
        community.set_editor(self.db,5,True,1)
        for tid in range(1,31):
            self.track(tid);community.pick(self.db,(tid-1)//10+1,1,tid,True)
        self.track(31)
        with self.assertRaises(ValueError): community.pick(self.db,5,1,31,True)
        community.pick(self.db,5,1,1,True)
        self.assertEqual(community.snapshot(self.db,5,1)["editorial"][0]["votes"],2)
        self.db.execute("UPDATE users SET status='denied' WHERE id=2")
        community.pick(self.db,5,1,31,True)
