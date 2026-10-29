import shutil
import socket
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest
from unittest.mock import patch

from tests import test_ccswitch_maintenance as fixtures
from vibecue import ccswitch_maintenance as maintenance


class MaintenanceRaceTests(unittest.TestCase):
    setUp=fixtures.MaintenanceTests.setUp
    prepare_cache=fixtures.MaintenanceTests.prepare_cache

    def test_proxy_starting_requests_during_staging_is_not_asked_to_exit(self):
        self.native.running=True
        original_copy=shutil.copytree

        def copy_then_become_busy(source,destination,*args,**kwargs):
            result=original_copy(source,destination,*args,**kwargs)
            if '.CCSwitch-vibecue-stage-' in str(destination):self.manager.idle=False
            return result

        with patch.object(maintenance.shutil,'copytree',side_effect=copy_then_become_busy):
            self.assertFalse(self.manager.install(self.identity,self.cache))
        self.assertNotIn('quit',[action for action,_ in self.native.calls])
        self.assertTrue(maintenance.same_app(self.identity,maintenance.inspect_app(self.app)))

    def test_rollback_never_replaces_an_official_update_arriving_during_exit(self):
        backup=self.root/'original-backup/CC Switch.app'
        shutil.copytree(self.app,backup)
        fixtures.app_bundle(self.app,b'patched executable')
        replacement=maintenance.inspect_app(self.app)
        self.manager.update('waiting_retry',pending_rollback=str(backup),
                            rollback_sha256=replacement['binary_sha256'])
        self.native.running=True
        original_native=self.native

        def update_during_exit(action,payload=None):
            result=original_native(action,payload)
            if action=='quit' and result.get('status')=='stopped':
                fixtures.app_bundle(self.app,b'new official executable','4.0.22','4.0.22')
            return result

        self.manager.native=update_during_exit
        self.assertFalse(self.manager.rollback_pending())
        self.assertEqual(maintenance.inspect_app(self.app)['version'],'4.0.22')
        self.assertTrue(backup.exists())
        self.assertEqual(self.manager.state()['phase'],'detected')

    def test_ipv6_proxy_with_active_request_is_not_mistaken_for_stopped_listener(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body=b'{"running":true,"active_connections":1}'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*_):pass
        class Server(HTTPServer):address_family=socket.AF_INET6
        try:server=Server(('::1',0),Handler)
        except OSError:self.skipTest('IPv6 loopback is unavailable')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            database=self.user/'.cc-switch/cc-switch.db'
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE proxy_config (listen_address TEXT,listen_port INTEGER)')
                connection.execute('INSERT INTO proxy_config VALUES (?,?)',('::1',server.server_port))
            self.assertFalse(maintenance.Maintenance.proxy_idle(self.manager))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':unittest.main()
