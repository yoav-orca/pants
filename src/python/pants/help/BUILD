# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

python_sources()

python_tests(
    name="tests",
    sources=["*_test.py", "!*_integration_test.py"],
    dependencies=["//BUILD_ROOT:files"],
)

python_tests(name="integration", sources=["*_integration_test.py"], timeout=360)
