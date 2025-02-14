# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os
import unittest

import pytest

from pants.fs.fs import safe_filename


class SafeFilenameTest(unittest.TestCase):
    class FixedDigest:
        def __init__(self, size):
            self._size = size

        def update(self, value):
            pass

        def hexdigest(self):
            return self._size * "*"

    def test_bad_name(self):
        with pytest.raises(ValueError):
            safe_filename(os.path.join("more", "than", "a", "name.game"))

    def test_noop(self):
        assert "jack.jill" == safe_filename("jack", ".jill", max_length=9)
        assert "jack.jill" == safe_filename("jack", ".jill", max_length=100)

    def test_shorten(self):
        assert "**.jill" == safe_filename("jack", ".jill", digest=self.FixedDigest(2), max_length=8)

    def test_shorten_readable(self):
        assert "j.**.e.jill" == safe_filename(
            "jackalope", ".jill", digest=self.FixedDigest(2), max_length=11
        )

    def test_shorten_fail(self):
        with pytest.raises(ValueError):
            safe_filename("jack", ".beanstalk", digest=self.FixedDigest(3), max_length=12)
