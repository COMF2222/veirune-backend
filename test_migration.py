import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from cryptography.hazmat.primitives.asymmetric import ec


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).parent/'tools'/path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
endpoint=module('endpoint_tool','service_endpoint.py')
restore=module('restore_tool','restore-server.py')
backup_target=module('backup_target','backup-target.py')


class MigrationTest(unittest.TestCase):
    def test_only_the_public_ip_changes_in_restored_private_configuration(self):
        original="SOUNDCLOUD_CLIENT_ID='same-app'\nSOUNDCLOUD_CLIENT_SECRET='same-secret'\nSOUNDCLOUD_REDIRECT_URI='http://127.0.0.1:41317/callback'\nFASTCLOUD_PUBLIC_IP='80.47.238.200'\nFASTCLOUD_RELEASE_NOTIFY_TOKEN='same-notification-key'\n"
        moved=restore.moved_environment(original,'1.1.1.1')
        self.assertEqual(moved,original.replace("FASTCLOUD_PUBLIC_IP='80.47.238.200'","FASTCLOUD_PUBLIC_IP='1.1.1.1'"))
        for ip in ('127.0.0.1','192.168.1.2','0.0.0.0'):
            with self.assertRaises(ValueError):restore.moved_environment(original,ip)
        with self.assertRaises(ValueError):restore.moved_environment('FASTCLOUD_PUBLIC_IP=x','1.1.1.1')

    def test_endpoint_signature_covers_the_destination_and_sequence(self):
        key=ec.generate_private_key(ec.SECP256R1())
        signed=endpoint.signed(key,'https://80.47.238.200',3,['https://77.223.107.226'])
        self.assertEqual(endpoint.verified(signed,key.public_key())['serial'],3)
        wrong=ec.generate_private_key(ec.SECP256R1())
        with self.assertRaises(Exception):endpoint.verified(signed,wrong.public_key())
        signed['payload']=endpoint.signed(key,'https://example.com',4,[])['payload']
        with self.assertRaises(Exception):endpoint.verified(signed,key.public_key())
        self.assertNotIn('PRIVATE KEY',json.dumps(signed))

    def test_announcements_reject_private_origins_and_credential_urls(self):
        for url in ('http://example.com','https://localhost','https://127.0.0.1','https://10.1.1.1','https://user:password@example.com','https://example.com/path','https://example.com:8080'):
            with self.assertRaises(ValueError):endpoint.origin(url)

    def test_hourly_backups_follow_the_owned_move_but_keep_custom_hosts(self):
        announcement={'origin':'https://new.example','previous':['https://80.47.238.200']}
        self.assertEqual(backup_target.target('root@80.47.238.200',announcement),'root@new.example')
        self.assertEqual(backup_target.target('root@custom.example',announcement),'root@custom.example')


if __name__=='__main__':unittest.main()
