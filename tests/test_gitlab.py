import marge.gitlab as gitlab


class TestVersion(object):
    def test_parse(self):
        assert gitlab.Version.parse('9.2.2-ee') == gitlab.Version(release=(9, 2, 2), edition='ee')
