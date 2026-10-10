"""Accounting regressions for the isolated cellular measurement fixture."""

import unittest
from argparse import Namespace
from fastapi.testclient import TestClient

from tools.budget_link import Counter, budget_app, finite_report


class BudgetTests(unittest.TestCase):
    def test_counts_utf8_bytes_and_message_rates(self):
        meter = Counter()
        meter.add('/ws/detections', 'down', 'é')
        meter.add('/ws/detections', 'down', b'ab')
        row = meter.report(2)['/ws/detections:down']
        self.assertEqual(row, dict(bytes=4, messages=2, kbps=.016, messages_hz=1))
        self.assertIsNone(finite_report({'age': [float('inf') ]})['age'][0])

    def test_fixture_accounts_body_and_exposes_valid_workload(self):
        args = Namespace(serve_fixture='rgb15', scene=None, model=None)
        with TestClient(budget_app(args)) as client:
            client.delete('/__budget').raise_for_status()
            response = client.get('/tts/reply-budget.wav')
            self.assertEqual(len(response.content), 64044)
            self.assertTrue(response.content.startswith(b'RIFF'))
            report = client.get('/__budget').json()
            self.assertEqual(report['streams']['/tts/reply-budget.wav:down']['bytes'], 64044)
            self.assertIn('detector_error', report['perception'])
            client.delete('/__budget').raise_for_status()
            self.assertNotIn('/tts/reply-budget.wav:down', client.get('/__budget').json()['streams'])
