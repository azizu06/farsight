import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.pull_capture import pull_latest_run


class PullCaptureTests(unittest.TestCase):
    def test_latest_run_is_copied_to_mac_dataset_directory(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / 'farsight-captures'
            with patch('subprocess.check_output', return_value='/home/ubuntu/captures/20261011T062000Z-abcd\n') as ssh, \
                    patch('subprocess.run') as scp:
                result = pull_latest_run('ubuntu@jetson', destination)
            self.assertEqual(result, destination / '20261011T062000Z-abcd')
            self.assertTrue(destination.is_dir())
            self.assertEqual(ssh.call_args.args[0][:2], ['ssh', 'ubuntu@jetson'])
            scp.assert_called_once_with([
                'scp', '-r', 'ubuntu@jetson:/home/ubuntu/captures/20261011T062000Z-abcd',
                str(destination)], check=True)


if __name__ == '__main__':
    unittest.main()
