"""A running engineering command keeps the managed executable identity stable."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from vibecue.engineering_install import tool_use_lock


class EngineeringLeaseIntegrationTests(unittest.TestCase):
    def test_cli_does_not_start_tools_during_atomic_version_switch(self):
        with tempfile.TemporaryDirectory(prefix='vcgo-tool-lease-') as temporary:
            home=Path(temporary)
            with tool_use_lock(home,exclusive=True,nonblocking=True) as acquired:
                self.assertTrue(acquired)
                env=dict(os.environ,VIBECUE_HOME=str(home),PYTHONDONTWRITEBYTECODE='1')
                run=subprocess.run([sys.executable,'-m','vibecue.engineering_tools','capabilities'],input='{}',capture_output=True,text=True,env=env,timeout=4)
            self.assertEqual(run.returncode,0)
            report=json.loads(run.stdout)
            self.assertEqual(report['status'],'error')
            self.assertIn('切换版本',report['summary'])
            self.assertFalse(report.get('capabilities'))
            with tool_use_lock(home,exclusive=True,nonblocking=True) as acquired:
                self.assertTrue(acquired)

    def test_multiple_operations_can_share_identity_but_publication_waits(self):
        with tempfile.TemporaryDirectory(prefix='vcgo-tool-lease-') as temporary:
            home=Path(temporary)
            with tool_use_lock(home,nonblocking=True) as first:
                with tool_use_lock(home,nonblocking=True) as second:
                    self.assertTrue(first and second)
                    with tool_use_lock(home,exclusive=True,nonblocking=True) as publish:
                        self.assertFalse(publish)
            with tool_use_lock(home,exclusive=True,nonblocking=True) as publish:
                self.assertTrue(publish)
