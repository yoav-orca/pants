# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import dataclasses
import json
import os.path
from dataclasses import dataclass
from typing import Iterable, Mapping

from pants.backend.go.util_rules.assembly import (
    AssemblyPostCompilation,
    AssemblyPostCompilationRequest,
    AssemblyPreCompilationRequest,
    FallibleAssemblyPreCompilation,
)
from pants.backend.go.util_rules.import_analysis import ImportConfig, ImportConfigRequest
from pants.backend.go.util_rules.sdk import GoSdkProcess
from pants.engine.engine_aware import EngineAwareParameter, EngineAwareReturnType
from pants.engine.fs import EMPTY_DIGEST, AddPrefix, CreateDigest, Digest, FileContent, MergeDigests
from pants.engine.process import FallibleProcessResult
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.meta import frozen_after_init
from pants.util.strutil import path_safe


class BuildGoPackageRequest(EngineAwareParameter):
    def __init__(
        self,
        *,
        import_path: str,
        digest: Digest,
        dir_path: str,
        go_file_names: tuple[str, ...],
        s_file_names: tuple[str, ...],
        direct_dependencies: tuple[BuildGoPackageRequest, ...],
        minimum_go_version: str | None,
        for_tests: bool = False,
        embed_config: EmbedConfig | None = None,
    ) -> None:
        """Build a package and its dependencies as `__pkg__.a` files.

        Instances of this class form a structure-shared DAG, and so a hashcode is pre-computed for
        the recursive portion.
        """

        self.import_path = import_path
        self.digest = digest
        self.dir_path = dir_path
        self.go_file_names = go_file_names
        self.s_file_names = s_file_names
        self.direct_dependencies = direct_dependencies
        self.minimum_go_version = minimum_go_version
        self.for_tests = for_tests
        self.embed_config = embed_config
        self._hashcode = hash(
            (
                self.import_path,
                self.digest,
                self.dir_path,
                self.go_file_names,
                self.s_file_names,
                self.direct_dependencies,
                self.minimum_go_version,
                self.for_tests,
                self.embed_config,
            )
        )

    def __repr__(self) -> str:
        # NB: We must override the default `__repr__` so that `direct_dependencies` does not
        # traverse into transitive dependencies, which was pathologically slow.
        return (
            f"{self.__class__}("
            f"import_path={repr(self.import_path)}, "
            f"digest={self.digest}, "
            f"dir_path={self.dir_path}, "
            f"go_file_names={self.go_file_names}, "
            f"go_file_names={self.s_file_names}, "
            f"direct_dependencies={[dep.import_path for dep in self.direct_dependencies]}, "
            f"minimum_go_version={self.minimum_go_version}, "
            f"for_tests={self.for_tests}, "
            f"embed_config={self.embed_config}"
            ")"
        )

    def __hash__(self) -> int:
        return self._hashcode

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return NotImplemented
        return (
            self._hashcode == other._hashcode
            and self.import_path == other.import_path
            and self.digest == other.digest
            and self.dir_path == other.dir_path
            and self.go_file_names == other.go_file_names
            and self.s_file_names == other.s_file_names
            and self.minimum_go_version == other.minimum_go_version
            and self.for_tests == other.for_tests
            and self.embed_config == other.embed_config
            # TODO: Use a recursive memoized __eq__ if this ever shows up in profiles.
            and self.direct_dependencies == other.direct_dependencies
        )

    def debug_hint(self) -> str | None:
        return self.import_path


@dataclass(frozen=True)
class FallibleBuildGoPackageRequest(EngineAwareParameter, EngineAwareReturnType):
    """Request to build a package, but fallible if determining the request metadata failed.

    When creating "synthetic" packages, use `GoPackageRequest` directly. This type is only intended
    for determining the package metadata of user code, which may fail to be analyzed.
    """

    request: BuildGoPackageRequest | None
    import_path: str
    exit_code: int = 0
    stderr: str | None = None
    dependency_failed: bool = False

    def level(self) -> LogLevel:
        return (
            LogLevel.ERROR if self.exit_code != 0 and not self.dependency_failed else LogLevel.DEBUG
        )

    def message(self) -> str:
        message = self.import_path
        message += (
            " succeeded." if self.exit_code == 0 else f" failed (exit code {self.exit_code})."
        )
        if self.stderr:
            message += f"\n{self.stderr}"
        return message

    def cacheable(self) -> bool:
        # Failed compile outputs should be re-rendered in every run.
        return self.exit_code == 0


@dataclass(frozen=True)
class FallibleBuiltGoPackage(EngineAwareReturnType):
    """Fallible version of `BuiltGoPackage` with error details."""

    output: BuiltGoPackage | None
    import_path: str
    exit_code: int = 0
    stdout: str | None = None
    stderr: str | None = None
    dependency_failed: bool = False

    def level(self) -> LogLevel:
        return (
            LogLevel.ERROR if self.exit_code != 0 and not self.dependency_failed else LogLevel.DEBUG
        )

    def message(self) -> str:
        message = self.import_path
        message += (
            " succeeded." if self.exit_code == 0 else f" failed (exit code {self.exit_code})."
        )
        if self.stdout:
            message += f"\n{self.stdout}"
        if self.stderr:
            message += f"\n{self.stderr}"
        return message

    def cacheable(self) -> bool:
        # Failed compile outputs should be re-rendered in every run.
        return self.exit_code == 0


@dataclass(frozen=True)
class BuiltGoPackage:
    """A package and its dependencies compiled as `__pkg__.a` files.

    The packages are arranged into `__pkgs__/{path_safe(import_path)}/__pkg__.a`.
    """

    digest: Digest
    import_paths_to_pkg_a_files: FrozenDict[str, str]


@dataclass(unsafe_hash=True)
@frozen_after_init
class EmbedConfig:
    patterns: FrozenDict[str, tuple[str, ...]]
    files: FrozenDict[str, str]

    def __init__(self, patterns: Mapping[str, Iterable[str]], files: Mapping[str, str]) -> None:
        """Configuration passed to the Go compiler to configure file embedding. The compiler relies
        entirely on the caller to map embed patterns to actual filesystem paths. All embed patterns
        contained in the package must be mapped. Consult
        `FirstPartyPkgInfo.{EmbedPatterns,TestEmbedPatterns,XTestEmbedPatterns}` for the embed
        patterns obtained from analysis.

        :param patterns: Maps each pattern provided via a //go:embed directive to a list of file paths relative to
        the package directory for files to embed for that pattern. When the embedded variable is an `embed.FS`,
        those relative file paths define the virtual directory hierarchy exposed by the embed.FS filesystem
        abstraction. The relative file paths are resolved to actual filesystem paths for their content by consulting
        the `files` dictionary.

        :param files: Maps each virtual, relative file path used as a value in the `patterns` dictionary to the actual
        filesystem path with that file's content.
        """
        self.patterns = FrozenDict({k: tuple(v) for k, v in patterns.items()})
        self.files = FrozenDict(files)

    def to_embedcfg(self) -> bytes:
        data = {
            "Patterns": self.patterns,
            "Files": self.files,
        }
        return json.dumps(data).encode("utf-8")


@dataclass(frozen=True)
class RenderEmbedConfigRequest:
    embed_config: EmbedConfig | None


@dataclass(frozen=True)
class RenderedEmbedConfig:
    digest: Digest
    PATH = "./embedcfg"


# NB: We must have a description for the streaming of this rule to work properly
# (triggered by `FallibleBuiltGoPackage` subclassing `EngineAwareReturnType`).
@rule(desc="Compile with Go", level=LogLevel.DEBUG)
async def build_go_package(request: BuildGoPackageRequest) -> FallibleBuiltGoPackage:
    maybe_built_deps = await MultiGet(
        Get(FallibleBuiltGoPackage, BuildGoPackageRequest, build_request)
        for build_request in request.direct_dependencies
    )

    import_paths_to_pkg_a_files: dict[str, str] = {}
    dep_digests = []
    for maybe_dep in maybe_built_deps:
        if maybe_dep.output is None:
            return dataclasses.replace(
                maybe_dep, import_path=request.import_path, dependency_failed=True
            )
        dep = maybe_dep.output
        import_paths_to_pkg_a_files.update(dep.import_paths_to_pkg_a_files)
        dep_digests.append(dep.digest)

    merged_deps_digest, import_config, embedcfg = await MultiGet(
        Get(Digest, MergeDigests(dep_digests)),
        Get(ImportConfig, ImportConfigRequest(FrozenDict(import_paths_to_pkg_a_files))),
        Get(RenderedEmbedConfig, RenderEmbedConfigRequest(request.embed_config)),
    )

    input_digest = await Get(
        Digest,
        MergeDigests([merged_deps_digest, import_config.digest, embedcfg.digest, request.digest]),
    )

    assembly_digests = None
    symabis_path = None
    if request.s_file_names:
        assembly_setup = await Get(
            FallibleAssemblyPreCompilation,
            AssemblyPreCompilationRequest(input_digest, request.s_file_names, request.dir_path),
        )
        if assembly_setup.result is None:
            return FallibleBuiltGoPackage(
                None,
                request.import_path,
                assembly_setup.exit_code,
                stdout=assembly_setup.stdout,
                stderr=assembly_setup.stderr,
            )
        input_digest = assembly_setup.result.merged_compilation_input_digest
        assembly_digests = assembly_setup.result.assembly_digests
        symabis_path = "./symabis"

    compile_args = [
        "tool",
        "compile",
        "-o",
        "__pkg__.a",
        "-pack",
        "-p",
        request.import_path,
        "-importcfg",
        import_config.CONFIG_PATH,
        # See https://github.com/golang/go/blob/f229e7031a6efb2f23241b5da000c3b3203081d6/src/cmd/go/internal/work/gc.go#L79-L100
        # for why Go sets the default to 1.16.
        f"-lang=go{request.minimum_go_version or '1.16'}",
    ]

    if symabis_path:
        compile_args.extend(["-symabis", symabis_path])

    if embedcfg.digest != EMPTY_DIGEST:
        compile_args.extend(["-embedcfg", RenderedEmbedConfig.PATH])

    if not request.s_file_names:
        # If there are no non-Go sources, then pass -complete flag which tells the compiler that the provided
        # Go files are the entire package.
        compile_args.append("-complete")

    relativized_sources = (
        f"./{request.dir_path}/{name}" if request.dir_path else f"./{name}"
        for name in request.go_file_names
    )
    compile_args.extend(["--", *relativized_sources])
    compile_result = await Get(
        FallibleProcessResult,
        GoSdkProcess(
            input_digest=input_digest,
            command=tuple(compile_args),
            description=f"Compile Go package: {request.import_path}",
            output_files=("__pkg__.a",),
        ),
    )
    if compile_result.exit_code != 0:
        return FallibleBuiltGoPackage(
            None,
            request.import_path,
            compile_result.exit_code,
            stdout=compile_result.stdout.decode("utf-8"),
            stderr=compile_result.stderr.decode("utf-8"),
        )

    compilation_digest = compile_result.output_digest
    if assembly_digests:
        assembly_result = await Get(
            AssemblyPostCompilation,
            AssemblyPostCompilationRequest(
                compilation_digest,
                assembly_digests,
                request.s_file_names,
                request.dir_path,
            ),
        )
        if assembly_result.result.exit_code != 0:
            return FallibleBuiltGoPackage(
                None,
                request.import_path,
                assembly_result.result.exit_code,
                stdout=assembly_result.result.stdout.decode("utf-8"),
                stderr=assembly_result.result.stderr.decode("utf-8"),
            )
        assert assembly_result.merged_output_digest
        compilation_digest = assembly_result.merged_output_digest

    path_prefix = os.path.join("__pkgs__", path_safe(request.import_path))
    import_paths_to_pkg_a_files[request.import_path] = os.path.join(path_prefix, "__pkg__.a")
    output_digest = await Get(Digest, AddPrefix(compilation_digest, path_prefix))
    merged_result_digest = await Get(Digest, MergeDigests([*dep_digests, output_digest]))

    output = BuiltGoPackage(merged_result_digest, FrozenDict(import_paths_to_pkg_a_files))
    return FallibleBuiltGoPackage(output, request.import_path)


@rule
def required_built_go_package(fallible_result: FallibleBuiltGoPackage) -> BuiltGoPackage:
    if fallible_result.output is not None:
        return fallible_result.output
    raise Exception(
        f"Failed to compile {fallible_result.import_path}:\n"
        f"{fallible_result.stdout}\n{fallible_result.stderr}"
    )


@rule
async def render_embed_config(request: RenderEmbedConfigRequest) -> RenderedEmbedConfig:
    digest = EMPTY_DIGEST
    if request.embed_config:
        digest = await Get(
            Digest,
            CreateDigest(
                [FileContent(RenderedEmbedConfig.PATH, request.embed_config.to_embedcfg())]
            ),
        )
    return RenderedEmbedConfig(digest)


def rules():
    return collect_rules()
