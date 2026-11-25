from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
import unittest

from lib.diagnostics import DiagnosticStore, explain_control
from monitor import Application, make_handler
from .test_serval import wait_until


AIM = '[AimTrace] rx_mode=AUTO_AIM rx_color=0 active_color=blue detected=2 red=0 blue=2 kept=2 tracker=tracking targets=1'
CONTROL = '[ControlTrace] target=true target_id=3 control=true planner_fire=false yaw=0.5 pitch=0.1 reason=control_ready'


class DiagnosticTests(unittest.TestCase):
    def test_actual_fps_for_both_entrypoints_and_zero_is_valid(self):
        store = DiagnosticStore()
        for program, fps in [('standard_mpc', 123.45), ('standard_debug_mpc', 0.)]:
            store.add_line(f'\x1b[32m[info] {program} fps: {fps:.2f}\x1b[0m')
            sample = store.snapshot()['latest']['fps']
            self.assertEqual(sample['fields'], dict(program=program, fps=fps))
            self.assertLess(sample['age_s'], 1)
        # Periodic FPS must not displace the useful control history.
        self.assertEqual(store.snapshot()['history'], [])

    def test_invalid_fps_and_old_fps_do_not_look_live(self):
        store = DiagnosticStore()
        prefix = '['+(datetime.now()-timedelta(seconds=10)).isoformat(sep=' ')+'] [info] '
        store.add_line(prefix+'standard_mpc fps: 98.76')
        for value in ['nan', 'inf', '-1', 'broken', '100 extra']:
            store.add_line('[info] standard_mpc fps: '+value)
        store.add_line('[info] camera fps: 300')
        snapshot = store.snapshot()
        self.assertEqual(snapshot['malformed'], 5)
        self.assertEqual(snapshot['ignored'], 1)
        self.assertEqual(snapshot['latest']['fps']['fields']['fps'], 98.76)
        self.assertGreater(snapshot['latest']['fps']['age_s'], 9)
        store.reset()
        self.assertNotIn('fps', store.snapshot()['latest'])

    def test_filters_timing_noise_and_parses_actual_color_and_control(self):
        store = DiagnosticStore()
        for _ in range(1000):
            store.add_line('[debug] YOLO26TRT timing [frame -1]: total 8.1 ms')
        store.add_line('\x1b[32m[info] '+AIM+'\x1b[0m')
        store.add_line(CONTROL)
        state = store.snapshot()
        self.assertEqual(state['ignored'], 1000)
        self.assertEqual(len(state['history']), 2)
        self.assertEqual(state['latest']['aim']['fields']['active_color'], 'blue')
        self.assertTrue(state['latest']['control']['fields']['control'])
        self.assertNotIn('YOLO26', store.export().decode())
        wire = {'tx':dict(age_s=0, values=dict(mode=1,target_id=3))}
        self.assertEqual(explain_control(state, wire)['code'], 'sent')
        wire['tx']['values']['mode'] = 0
        self.assertEqual(explain_control(state, wire)['code'], 'send_mismatch')

    def test_old_or_malformed_trace_does_not_appear_current(self):
        store = DiagnosticStore()
        prefix = '['+(datetime.now()-timedelta(seconds=10)).isoformat(sep=' ')+'] [info] '
        store.add_line(prefix+CONTROL)
        store.add_line('[ControlTrace] target=true control=true reason=broken')
        state = store.snapshot()
        self.assertEqual(state['malformed'], 1)
        self.assertGreater(state['latest']['control']['age_s'], 9)
        self.assertEqual(explain_control(state, {})['code'], 'stale')

    def test_missing_target_reports_detector_filter_or_tracker_stage(self):
        store = DiagnosticStore()
        no_target = '[ControlTrace] target=false target_id=0 control=false planner_fire=false yaw=0 pitch=0 reason=no_target'
        store.add_line(no_target)
        store.add_line(AIM.replace('detected=2', 'detected=0').replace('kept=2', 'kept=0').replace('targets=1', 'targets=0'))
        self.assertEqual(explain_control(store.snapshot(), {})['code'], 'no_detection')
        store.add_line(AIM.replace('kept=2', 'kept=0').replace('targets=1', 'targets=0'))
        self.assertEqual(explain_control(store.snapshot(), {})['code'], 'filtered')
        store.add_line(AIM.replace('targets=1', 'targets=0'))
        self.assertEqual(explain_control(store.snapshot(), {})['code'], 'tracking')

    def test_partial_appends_are_visible_in_http_and_export_without_log_writes(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            path = Path(directory)/'vision.log'
            path.write_bytes(b'')
            app = Application(diagnostics=path)
            cleanup.callback(app.close)
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def close_server():
                server.shutdown(); thread.join(); server.server_close()
            cleanup.callback(close_server)
            with path.open('ab', buffering=0) as output:
                output.write(AIM[:40].encode())
                output.write((AIM[40:]+'\n'+CONTROL+'\nstandard_mpc fps: 87.65\n').encode())
            wait_until(lambda:'fps' in app.diagnostic_store.snapshot()['latest'])
            base = 'http://127.0.0.1:'+str(server.server_port)
            with urlopen(base+'/api/diagnostics') as response:
                state = json.load(response)
            self.assertEqual(state['latest']['aim']['fields']['targets'], 1)
            self.assertEqual(state['latest']['fps']['fields'], dict(program='standard_mpc', fps=87.65))
            with urlopen(base+'/api/diagnostics.txt') as response:
                self.assertIn(CONTROL, response.read().decode())
            self.assertEqual(path.read_text(), AIM+'\n'+CONTROL+'\nstandard_mpc fps: 87.65\n')


if __name__ == '__main__':
    unittest.main()
