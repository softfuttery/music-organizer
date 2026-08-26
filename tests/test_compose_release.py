import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parents[1]
RELEASE_SERVICES = ("web", "worker", "review-worker")


class ComposeReleaseTests(unittest.TestCase):
    def test_frontend_public_assets_are_included_in_the_image_build(self) -> None:
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

        self.assertIn("COPY frontend-vue/public ./public", dockerfile)
        self.assertIn("!frontend-vue/public/**", dockerignore)
        for name in ("manifest.webmanifest", "sw.js", "app-icon.svg"):
            self.assertTrue((PROJECT_ROOT / "frontend-vue" / "public" / name).is_file())

    def test_release_services_share_the_same_build_and_image(self) -> None:
        compose = yaml.safe_load(
            (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        shared_image = compose["x-app-image"]

        for service_name in RELEASE_SERVICES:
            with self.subTest(service=service_name):
                service = compose["services"][service_name]
                self.assertEqual(service["image"], shared_image["image"])
                self.assertEqual(service["build"], shared_image["build"])

    def test_frontend_build_bypasses_the_lan_dns_fake_ip(self) -> None:
        compose = yaml.safe_load(
            (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            compose["x-app-image"]["build"]["extra_hosts"],
            [
                "registry.npmjs.org:${NPM_REGISTRY_IP:-104.16.1.34}",
                "pypi.org:${PYPI_ORG_IP:-151.101.0.223}",
                "files.pythonhosted.org:${PYTHONHOSTED_IP:-151.101.0.223}",
            ],
        )

    def test_web_uses_threads_so_audio_does_not_block_control_requests(self) -> None:
        compose = yaml.safe_load(
            (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        web = compose["services"]["web"]

        self.assertIn("--worker-class gthread", web["command"])
        self.assertIn("--threads ${GUNICORN_THREADS:-4}", web["command"])
        self.assertEqual(web["environment"]["GUNICORN_THREADS"], "${GUNICORN_THREADS:-4}")

    def test_only_web_gets_the_synology_recycle_acl_group(self) -> None:
        compose = yaml.safe_load(
            (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]

        self.assertEqual(services["web"]["group_add"], ["${DSM_RECYCLE_GID:-101}"])
        self.assertNotIn("group_add", services["worker"])
        self.assertNotIn("group_add", services["review-worker"])

    def test_portable_compose_has_no_synology_host_assumptions(self) -> None:
        compose = yaml.safe_load(
            (PROJECT_ROOT / "compose.portable.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]

        self.assertNotIn("network_mode", services["worker"])
        self.assertNotIn("group_add", services["web"])
        self.assertEqual(
            services["web"]["ports"],
            ["${BIND_ADDRESS:-127.0.0.1}:${PORT:-15000}:${PORT:-15000}"],
        )
        for service in services.values():
            self.assertIn("${MEDIA_ROOT:-./media}:/media", service["volumes"])


if __name__ == "__main__":
    unittest.main()
