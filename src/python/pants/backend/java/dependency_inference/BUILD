# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

python_sources(dependencies=[":java_resources"])
resources(name="java_resources", sources=["*.java", "java_parser.lockfile"])
python_tests(name="tests", timeout=240)

# Targets for developing on the Java parser outside of engine rules.
java_sources(
    name="java_parser",
    compatible_resolves=["java_parser"],
)
