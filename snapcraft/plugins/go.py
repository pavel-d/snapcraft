# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright (C) 2015-2019 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""The go plugin can be used for go projects using `go get`.

This plugin uses the common plugin keywords as well as those for "sources".
For more information check the 'plugins' topic for the former and the
'sources' topic for the latter.

Additionally, this plugin uses the following plugin-specific keywords:

    - go-channel:
      (string, default: latest/stable)
      The Snap Store channel to install go from. If set to an empty string,
      go will be installed using the system's traditional package manager.

    - go-packages:
      (list of strings)
      Go packages to fetch, these must be a "main" package. Dependencies
      are pulled in automatically by `go get`.
      Packages that are not "main" will not cause an error, but would
      not be useful either.
      If the package is a part of the go-importpath the local package
      corresponding to those sources will be used.

    - go-importpath:
      (string)
      This entry tells the checked out `source` to live within a certain path
      within `GOPATH`.
      This entry is not required if `source` uses `go.mod`.

    - go-buildtags:
      (list of strings)
      Tags to use during the go build. Default is not to use any build tags.
"""

import logging
import os
import re
import shutil
from glob import iglob
from pkg_resources import parse_version
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import snapcraft
from snapcraft import common
from snapcraft.internal import elf, errors

if TYPE_CHECKING:
    from snapcraft.project import Project


logger = logging.getLogger(__name__)
_GO_MOD_REQUIRED_GO_VERSION = "1.13"


class GoModRequiredVersionError(errors.SnapcraftException):
    def __init__(self, *, go_version: str) -> None:
        self._go_version = go_version

    def get_brief(self) -> str:
        return "Use of go.mod requires Go {!r} or greater, found: {!r}".format(
            _GO_MOD_REQUIRED_GO_VERSION, self._go_version
        )

    def get_resolution(self) -> str:
        return "Set go-channel to a newer version of Go and try again."


def _get_cgo_ldflags(library_paths: List[str]) -> str:
    cgo_ldflags: List[str] = list()

    existing_cgo_ldflags = os.getenv("CGO_LDFLAGS")
    if existing_cgo_ldflags:
        cgo_ldflags.append(existing_cgo_ldflags)

    flags = common.combine_paths(library_paths, "-L", " ")
    if flags:
        cgo_ldflags.append(flags)

    ldflags = os.getenv("LDFLAGS")
    if ldflags:
        cgo_ldflags.append(ldflags)

    return " ".join(cgo_ldflags)


class GoPlugin(snapcraft.BasePlugin):
    @classmethod
    def schema(cls) -> Dict[str, Any]:
        schema = super().schema()
        schema["properties"]["go-channel"] = {
            "type": "string",
            "default": "latest/stable",
        }
        schema["properties"]["go-packages"] = {
            "type": "array",
            "minitems": 1,
            "uniqueItems": True,
            "items": {"type": "string"},
            "default": [],
        }
        schema["properties"]["go-importpath"] = {"type": "string", "default": ""}
        schema["properties"]["go-buildtags"] = {
            "type": "array",
            "minitems": 1,
            "uniqueItems": True,
            "items": {"type": "string"},
            "default": [],
        }
        schema["anyOf"] = [{"required": ["source"]}, {"required": ["go-packages"]}]

        return schema

    @classmethod
    def get_build_properties(cls) -> List[str]:
        # Inform Snapcraft of the properties associated with building. If these
        # change in the YAML Snapcraft will consider the build step dirty.
        return ["go-packages", "go-buildtags", "go-channel"]

    @classmethod
    def get_pull_properties(cls) -> List[str]:
        # Inform Snapcraft of the properties associated with pulling. If these
        # change in the YAML Snapcraft will consider the pull step dirty.
        return ["go-packages", "go-channel"]

    def __init__(self, name: str, options, project: "Project") -> None:
        super().__init__(name, options, project)

        self._setup_base_tools(options.go_channel, project.info.get_build_base())
        self._is_classic = project.info.confinement == "classic"

        self._install_bin_dir = os.path.join(self.installdir, "bin")

        self._gopath = os.path.join(self.partdir, "go")
        self._gopath_src = os.path.join(self._gopath, "src")
        self._gopath_bin = os.path.join(self._gopath, "bin")
        self._gopath_pkg = os.path.join(self._gopath, "pkg")

        self._version_regex = re.compile(r"^go version go(.*) .*$")
        self._go_version: Optional[str] = None

    def _setup_base_tools(self, go_channel: str, base: Optional[str]) -> None:
        if go_channel:
            self.build_snaps.append("go/{}".format(go_channel))
        elif base in ("core", "core16", "core18"):
            self.build_packages.append("golang-go")
        else:
            raise errors.PluginBaseError(part_name=self.name, base=base)

    def _is_using_go_mod(self, cwd: str) -> bool:
        if not os.path.exists(os.path.join(cwd, "go.mod")):
            return False

        if self._go_version is None:
            go_version_cmd_output: str = self._run_output(["go", "version"])
            version_match = self._version_regex.match(go_version_cmd_output)

            if version_match is None:
                raise RuntimeError(
                    "Unable to parse go version output: {!r}".format(
                        go_version_cmd_output
                    )
                )

            self._go_version = version_match.group(1)

        if parse_version(self._go_version) < parse_version(_GO_MOD_REQUIRED_GO_VERSION):
            raise GoModRequiredVersionError(go_version=self._go_version)

        return True

    def _pull_go_mod(self) -> None:
        self._run(["go", "mod", "download"], cwd=self.sourcedir)

    def _pull_go_packages(self) -> None:
        os.makedirs(self._gopath_src, exist_ok=True)

        # use -d to only download (build will happen later)
        # use -t to also get the test-deps
        # since we are not using -u the sources will stick to the
        # original checkout.
        if any(iglob("{}/**/*.go".format(self.sourcedir), recursive=True)):
            go_package = self._get_local_go_package()
            go_package_path = os.path.join(self._gopath_src, go_package)
            if os.path.islink(go_package_path):
                os.unlink(go_package_path)
            os.makedirs(os.path.dirname(go_package_path), exist_ok=True)
            os.symlink(self.sourcedir, go_package_path)
            self._run(["go", "get", "-t", "-d", "./{}/...".format(go_package)])

        for go_package in self.options.go_packages:
            self._run(["go", "get", "-t", "-d", go_package])

    def pull(self) -> None:
        super().pull()

        if self._is_using_go_mod(cwd=self.sourcedir):
            return self._pull_go_mod()
        else:
            self._pull_go_packages()

    def clean_pull(self) -> None:
        super().clean_pull()

        # Remove the gopath (if present)
        if os.path.exists(self._gopath):
            shutil.rmtree(self._gopath)

    def _get_local_go_package(self) -> str:
        if self.options.go_importpath:
            go_package = self.options.go_importpath
        else:
            logger.warning(
                "Please consider setting `go-importpath` for the {!r} "
                "part".format(self.name)
            )
            go_package = os.path.basename(os.path.abspath(self.options.source))
        return go_package

    def _get_local_main_packages(self) -> List[str]:
        search_path = "./{}/...".format(self._get_local_go_package())
        packages = self._run_output(
            ["go", "list", "-f", "{{.ImportPath}} {{.Name}}", search_path]
        )
        packages_split = [p.split() for p in packages.splitlines()]
        main_packages = [p[0] for p in packages_split if p[1] == "main"]
        return main_packages

    def _build(self, *, package: str = "") -> None:
        build_cmd = ["go", "build"]

        if self.options.go_buildtags:
            build_cmd.extend(["-tags={}".format(",".join(self.options.go_buildtags))])

        relink_cmd = build_cmd + ["-ldflags", "-linkmode=external"]

        if self._is_using_go_mod(self.builddir) and not package:
            work_dir = self.builddir
            build_cmd.extend(["-o", self._install_bin_dir])
            relink_cmd.extend(["-o", self._install_bin_dir])
        else:
            work_dir = self._install_bin_dir
            build_cmd.append(package)
            relink_cmd.append(package)

        pre_build_files = os.listdir(self._install_bin_dir)
        self._run(build_cmd, cwd=work_dir)
        post_build_files = os.listdir(self._install_bin_dir)

        new_files = set(post_build_files) - set(pre_build_files)
        if len(new_files) != 1:
            raise RuntimeError(f"Expected one binary to be built, found: {new_files!r}")
        binary_path = os.path.join(self._install_bin_dir, new_files.pop())

        # Relink with system linker if executable is dynamic in order to be
        # able to set rpath later on. This workaround can be removed after
        # https://github.com/NixOS/patchelf/issues/146 is fixed.
        if self._is_classic and elf.ElfFile(path=binary_path).is_dynamic:
            self._run(relink_cmd, cwd=work_dir)

    def _build_go_packages(self) -> None:
        if self.options.go_packages:
            packages = self.options.go_packages
        else:
            packages = self._get_local_main_packages()

        for package in packages:
            self._build(package=package)

    def build(self) -> None:
        super().build()

        # Ensure install directory exists.
        os.makedirs(self._install_bin_dir, exist_ok=True)

        if self._is_using_go_mod(cwd=self.builddir):
            self._build()
        else:
            self._build_go_packages()

    def clean_build(self) -> None:
        super().clean_build()

        if os.path.isdir(self._gopath_bin):
            shutil.rmtree(self._gopath_bin)

        if os.path.isdir(self._gopath_pkg):
            shutil.rmtree(self._gopath_pkg)

    def _run(self, cmd: List[str], cwd: str = None, **kwargs) -> None:
        env = self._build_environment()

        if cwd is None:
            cwd = self._gopath_src

        return self.run(cmd, cwd=cwd, env=env, **kwargs)

    def _run_output(self, cmd: List[str], **kwargs) -> str:
        env = self._build_environment()
        return self.run_output(cmd, cwd=self._gopath_src, env=env, **kwargs)

    def _build_environment(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["GOPATH"] = self._gopath
        env["GOBIN"] = self._gopath_bin

        library_paths: List[str] = []
        for root in [self.installdir, self.project.stage_dir]:
            library_paths.extend(
                common.get_library_paths(root, self.project.arch_triplet)
            )

        cgo_ldflags = _get_cgo_ldflags(library_paths)
        if cgo_ldflags:
            env["CGO_LDFLAGS"] = cgo_ldflags

        if self.project.is_cross_compiling:
            env["CC"] = "{}-gcc".format(self.project.arch_triplet)
            env["CXX"] = "{}-g++".format(self.project.arch_triplet)
            env["CGO_ENABLED"] = "1"
            # See https://golang.org/doc/install/source#environment
            go_archs = {"armhf": "arm", "i386": "386", "ppc64el": "ppc64le"}
            env["GOARCH"] = go_archs.get(self.project.deb_arch, self.project.deb_arch)
            if self.project.deb_arch == "armhf":
                env["GOARM"] = "7"
        return env

    def enable_cross_compilation(self) -> None:
        pass
