"""RealmRaid regressions using the offline real-method breakthrough harness."""
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tasks.RyouToppa import test_settlement_safe as suite


class SamplingTests(suite.SamplingTests):
    TASK = 'RealmRaid'


class IntegrationTests(suite.IntegrationTests):
    TASK = 'RealmRaid'


if __name__ == '__main__':
    unittest.main(verbosity=2)
