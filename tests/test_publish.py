import unittest

from factor_rotation import publish


class SiteUrl(unittest.TestCase):
    def test_https_and_ssh_remotes_give_the_pages_address(self):
        want = "https://owner.github.io/my-repo/"
        self.assertEqual(publish.site_url("https://github.com/Owner/my-repo.git"), want)
        self.assertEqual(publish.site_url("git@github.com:Owner/my-repo.git"), want)

    def test_the_stamp_goes_in_the_head_once(self):
        from datetime import datetime
        html = publish.stamp("<head></head><body></head>", datetime(2026, 11, 18, 9, 5))
        self.assertEqual(html.count("fr-published"), 1)
        self.assertIn('content="Nov 18, 9:05 AM CT"', html)


if __name__ == "__main__":
    unittest.main()
