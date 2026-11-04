"""Packaging checks: Dockerfile, compose, pyproject and the README stay in sync."""

from __future__ import annotations

import importlib.util
import os
import re
import unittest

import tfproxy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name: str) -> str:
    with open(os.path.join(ROOT, name), "r", encoding="utf-8") as handle:
        return handle.read()


def env_names_in_code() -> set[str]:
    return set(re.findall(r'"(TFP_[A-Z0-9_]+)"', read("tfproxy/config.py")))


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        self.text = read("Dockerfile")

    def test_base_image_and_layout(self):
        self.assertIn("FROM python:3.12-slim AS runtime", self.text)
        self.assertIn("COPY tfproxy/ /app/tfproxy/", self.text)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/python", "-m", "tfproxy", "serve"]', self.text)

    def test_runs_unprivileged_on_the_default_port(self):
        self.assertIn("EXPOSE 8080", self.text)
        user_lines = [line for line in self.text.splitlines() if line.startswith("USER ")]
        self.assertTrue(user_lines, "the container must not run as root")
        self.assertNotIn("USER root", self.text.split("USER tfproxy")[-1])

    def test_healthcheck_probes_healthz(self):
        self.assertIn("HEALTHCHECK", self.text)
        self.assertIn("/healthz", self.text)

    def test_test_stage_runs_the_suite(self):
        self.assertIn("FROM python:3.12-slim AS test", self.text)
        self.assertIn("unittest", self.text)

    def test_dockerignore_excludes_the_host_cache(self):
        self.assertIn("cache/", read(".dockerignore"))

    def test_dockerignore_keeps_everything_the_suite_needs(self):
        """The ``test`` stage does ``COPY .`` and runs the suite, so its inputs must be
        in the build context: excluding ``tests/`` or ``*.md`` breaks ``--target test``.
        """
        patterns = {
            line.strip()
            for line in read(".dockerignore").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        needed = ("LICENSE", "README.md", "CHANGELOG.md", "pyproject.toml", "docker-compose.yml", "examples", "tests", "tfproxy")
        for name in needed:
            with self.subTest(path=name):
                self.assertNotIn(name, patterns, f"{name} must stay in the build context")
                self.assertNotIn(name + "/", patterns, f"{name}/ must stay in the build context")
        self.assertNotIn("*.md", patterns, "*.md hides README.md and CHANGELOG.md")


class ComposeTest(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_compose_is_valid_and_uses_known_settings(self):
        import yaml

        document = yaml.safe_load(read("docker-compose.yml"))
        self.assertIn("services", document)
        services = document["services"]
        self.assertIn("tfproxy-a", services)
        self.assertIn("tfproxy-b", services)
        env = services["tfproxy-a"]["environment"]
        self.assertIn("TFP_S3_BUCKET", env)
        self.assertIn("TFP_LOCK_TTL", env)
        known = env_names_in_code()
        unknown = {key for key in env if key.startswith("TFP_") and key not in known}
        self.assertEqual(unknown, set(), f"compose sets unknown settings: {unknown}")
        self.assertIn("volumes", document)

    def test_two_services_share_one_bucket(self):
        text = read("docker-compose.yml")
        self.assertIn("extends: tfproxy-a", text, "the second instance must inherit the same bucket")
        self.assertEqual(text.count("TFP_S3_BUCKET:"), 1)


class ProjectMetadataTest(unittest.TestCase):
    def test_pyproject_version_matches_the_package(self):
        text = read("pyproject.toml")
        self.assertIn(f'version = "{tfproxy.__version__}"', text)

    def test_no_runtime_dependencies(self):
        self.assertIn("dependencies = []", read("pyproject.toml"))

    def test_console_script_is_declared(self):
        self.assertIn('tfproxy = "tfproxy.__main__:main"', read("pyproject.toml"))

    def test_no_third_party_imports_in_the_package(self):
        import sys as _sys

        allowed = set(getattr(_sys, "stdlib_module_names", set())) | {"tfproxy"}
        offenders = set()
        for dirpath, _dirnames, filenames in os.walk(os.path.join(ROOT, "tfproxy")):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, "r", encoding="utf-8") as handle:
                    source = handle.read()
                for match in re.finditer(r"^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_]*)", source, re.M):
                    module = match.group(1)
                    if module not in allowed and not module.startswith("tfproxy"):
                        offenders.add(f"{os.path.relpath(path, ROOT)}: {module}")
        self.assertEqual(offenders, set(), "the runtime must stay standard-library only")


class ReleaseEngineeringTest(unittest.TestCase):
    """The GitHub workflows that version and publish the image stay honest."""

    def workflow(self, name: str) -> str:
        path = os.path.join(ROOT, ".github", "workflows", name)
        if not os.path.exists(path):
            # ``--target test`` builds from a context without .github (see .dockerignore).
            self.skipTest(f"{name} is not in the build context")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_version_is_a_plain_semver(self):
        self.assertRegex(tfproxy.__version__, r"^\d+\.\d+\.\d+$")

    def test_license_is_shipped(self):
        self.assertIn("MIT License", read("LICENSE"))

    def test_changelog_has_an_entry_for_the_current_version(self):
        self.assertIn(f"## [{tfproxy.__version__}]", read("CHANGELOG.md"))

    def test_ci_builds_and_smoke_tests_the_image(self):
        text = self.workflow("ci.yml")
        self.assertIn("unittest discover", text)
        self.assertIn("docker/build-push-action", text)
        self.assertIn("/healthz", text)
        self.assertNotIn("packages: write", text, "CI must not publish anything")

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_release_publishes_to_ghcr_under_a_version_tag(self):
        import yaml

        document = yaml.safe_load(self.workflow("release.yml"))
        self.assertEqual(document["permissions"]["packages"], "write")
        # PyYAML resolves the bare key ``on`` as the boolean True.
        triggers = document["on"] if "on" in document else document[True]
        self.assertIn("v[0-9]+.[0-9]+.[0-9]+*", triggers["push"]["tags"])
        text = self.workflow("release.yml")
        self.assertIn("ghcr.io", text)
        self.assertIn("tfproxy.__version__", text, "the tag must be checked against the package version")
        self.assertIn("type=semver,pattern={{version}}", text)
        self.assertIn("type=raw,value=latest", text)


class ReadmeTest(unittest.TestCase):
    def test_every_setting_is_documented(self):
        readme = read("README.md")
        missing = sorted(name for name in env_names_in_code() if name not in readme)
        self.assertEqual(missing, [], f"undocumented settings: {missing}")

    def test_client_config_example_exists(self):
        example = read("examples/terraform-cli-config.tf")
        self.assertIn("modules.v1", example)
        self.assertIn("registry.terraform.io", example)

    def test_documented_endpoints_are_routed(self):
        from tfproxy.server import _ROUTES

        routes = {pattern.pattern for _verb, pattern, _name in _ROUTES}
        readme = read("README.md")
        for placeholder, needle in (
            ("/v1/modules/{ns}/{name}/{prov}/versions", "v1/modules"),
            ("/archive/{ns}/{name}/{prov}/{ver}.zip", "archive"),
            ("/.well-known/terraform.json", "well-known"),
            ("/api/v1/prefetch", "prefetch"),
            ("/healthz", "healthz"),
            ("/metrics", "metrics"),
        ):
            with self.subTest(route=placeholder):
                self.assertIn(needle, readme)
                self.assertTrue(any(needle.split("/")[0] in route for route in routes), routes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
