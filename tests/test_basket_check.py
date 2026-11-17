import unittest

import numpy as np
import pandas as pd

from factor_rotation import basket_check


class Cohesion(unittest.TestCase):
    def test_shared_story_scores_high_and_stray_scores_low(self):
        rng = np.random.default_rng(0)
        n = 120
        hedge = rng.normal(0, 0.01, n)
        theme = rng.normal(0, 0.01, n)
        r = pd.DataFrame({"H": hedge, **{s: hedge + theme + rng.normal(0, 0.004, n) for s in ("A", "B", "C")},
                          "X": hedge + rng.normal(0, 0.012, n)})
        sc = basket_check.scores(r, "H", ["A", "B", "C", "X"])
        for s in ("A", "B", "C"):
            self.assertGreater(sc[s], 0.6)
        self.assertLess(sc["X"], basket_check.MEMBER_MIN)


if __name__ == "__main__":
    unittest.main()
