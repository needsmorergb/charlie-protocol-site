import tempfile
import unittest
from pathlib import Path

from indexer import publish, site
from indexer.observe import Observation

MINT = '8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump'


class VerificationPublicationTests(unittest.TestCase):
    def test_error_kind_survives_durable_record(self):
        for kind in ('no_sharing_config', 'rpc_unavailable', 'rpc_error'):
            observation = Observation(MINT, 123, error='wording may change', error_kind=kind)
            record = publish.durable_record(observation)
            self.assertEqual(record['error_kind'], kind)
            self.assertNotIn('split', record)
            self.assertNotIn('sol_burn_balances', record)

    def test_partial_read_never_renders_figures(self):
        observation = Observation(MINT, 123, error='RPC unavailable', error_kind='rpc_unavailable')
        observation.config = object()  # A config was read before a later RPC failure.
        page = site.render(observation, now=123)
        self.assertIn('not about the coin', page)
        self.assertNotIn('data-figure=', page)
        self.assertNotIn('data-check=', page)

    def test_authored_pages_survive_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, writer in [('verify.html', site.write_verify), ('404.html', site.write_not_found), ('index.html', lambda d: site.write_landing(None, d))]:
                path = Path(directory) / name
                original = '<!-- charlie:authored-page -->Keep this page'
                path.write_text(original)
                writer(directory)
                self.assertEqual(path.read_text(), original)

    def test_human_status_wording_does_not_change_record(self):
        page = site._document('Test', '<p>FAIL: figure withheld</p>')
        self.assertIn('Needs review: figure withheld', page)
        self.assertNotIn('>FAIL', page)


if __name__ == '__main__':
    unittest.main()
