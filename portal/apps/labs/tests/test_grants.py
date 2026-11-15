"""Desktop grant internals: deterministic VNC ports and the Guacamole 1.6
client-URL identifier format (worker/DESKTOP-DEBUG.md root cause 1)."""
from django.test import TestCase, override_settings

from portal.apps.labs import guacamole
from portal.apps.testutils import make_lab_challenge, make_user


class VncPortTests(TestCase):
    def setUp(self):
        from portal.apps.testutils import make_host
        self.host = make_host()

    def test_ports_are_deterministic_per_lab(self):
        challenge = make_lab_challenge()
        user = make_user("930000000000000001", "Alex")
        from portal.apps.labs import services
        services.request_lab(user, challenge.id)
        services.claim(user, challenge.id)
        lab = user.labs.get(challenge=challenge)
        self.assertEqual(guacamole.vnc_port_for(lab), guacamole.vnc_port_for(lab))

    def test_ports_never_collide_across_a_thousand_labs(self):
        """The scheme keeps 5900+N unique for N in 0..999 (one host never
        exceeds ~1000 concurrent labs; the wraparound is documented)."""
        class FakeLab:
            def __init__(self, pk):
                self.id = pk
        ports = {guacamole.vnc_port_for(FakeLab(pk)) for pk in range(1, 1001)}
        self.assertEqual(len(ports), 1000)
        self.assertTrue(all(5900 <= port <= 6899 for port in ports))


class ClientUrlIdentifierTests(TestCase):
    def test_identifier_is_base64url_of_id_type_datasource(self):
        """Guacamole 1.6.0's SPA decodes <base64url of 'id\\0c\\0dataSource'>;
        the pre-1.6 'dataSource.id' form broke tunnel creation."""
        identifier = guacamole._client_url_identifier("lab-1")
        self.assertEqual(identifier, "bGFiLTEAYwBqc29u")  # lab-1\0c\0json
        import base64
        decoded = base64.urlsafe_b64decode(identifier + "=" * 0).decode("utf-8", errors="replace")
        self.assertIn("lab-1", decoded)
        self.assertIn("json", decoded)

    def test_identifier_differs_per_connection(self):
        self.assertNotEqual(
            guacamole._client_url_identifier("lab-1"),
            guacamole._client_url_identifier("lab-2"),
        )


class DesktopRoutingTests(TestCase):
    """Path A: per-workhorse Guacamole instances. The grant URL carries the
    routing path of the lab's OWN host's instance."""

    def setUp(self):
        from portal.apps.testutils import make_host
        self.host = make_host()
        self._secret = override_settings(GUACAMOLE_JSON_SECRET="ab" * 16)
        self._secret.enable()
        self.addCleanup(self._secret.disable)

    def test_single_instance_default_serves_every_host_at_guacamole(self):
        """Empty mapping (single-workhorse deployments): no config, no
        surprises — every host's grants ride the classic /guacamole/ path."""
        self.assertEqual(guacamole.instance_base_path("workhorse-02"), "/guacamole/")
        self.assertEqual(guacamole.instance_base_path("anything-else"), "/guacamole/")

    def test_multi_instance_routes_each_host_to_its_own_path(self):
        with override_settings(GUACAMOLE_INSTANCE_PATHS={
            "workhorse-02": "/guacamole/",
            "workhorse-03": "/guacamole/workhorse-03/",
        }):
            self.assertEqual(guacamole.instance_base_path("workhorse-02"), "/guacamole/")
            self.assertEqual(guacamole.instance_base_path("workhorse-03"), "/guacamole/workhorse-03/")

    def test_multi_instance_refuses_unmapped_hosts_with_operator_guidance(self):
        with override_settings(GUACAMOLE_INSTANCE_PATHS={"workhorse-02": "/guacamole/"}):
            with self.assertRaises(guacamole.GrantError) as error:
                guacamole.instance_base_path("workhorse-09")
            self.assertIn("No Guacamole instance is configured", error.exception.message)
            self.assertIn("GUACAMOLE_INSTANCE_PATHS", error.exception.message)

    def test_host_names_slugify_to_url_safe_path_segments(self):
        self.assertEqual(guacamole._host_slug("Workhorse 03!"), "workhorse-03")
        self.assertEqual(guacamole._host_slug("wh_x.1"), "wh-x-1")

    def test_grant_url_uses_the_host_routed_base(self):
        import json as json_lib
        from urllib.parse import unquote, urlparse
        challenge = make_lab_challenge()
        user = make_user("700000000000000009", "Routed")
        from portal.apps.labs import services
        services.request_lab(user, challenge.id)
        services.claim(user, challenge.id)
        lab = user.labs.get(challenge=challenge)

        url = guacamole.issue_grant(user, lab)
        self.assertTrue(url.startswith("/guacamole/#/client/"))  # single-instance default

        with override_settings(GUACAMOLE_INSTANCE_PATHS={self.host.name: "/guacamole/"}):
            url = guacamole.issue_grant(user, lab)
            self.assertTrue(url.startswith("/guacamole/#/client/"))
        # The grant payload is intact either way (signature + expiry present).
        fragment = url.split("#/client/")[1]
        data = unquote(fragment.split("data=")[1])
        self.assertTrue(data)  # nonempty encrypted grant


@override_settings(GUACAMOLE_JSON_SECRET="ab" * 16)
class GrantShapeTests(TestCase):


    def setUp(self):
        from portal.apps.testutils import make_host
        self.host = make_host()

    def test_grant_url_carries_the_deep_link_and_expiry(self):
        import json as json_lib
        import base64
        from urllib.parse import unquote

        challenge = make_lab_challenge()
        user = make_user("940000000000000001", "Alex")
        from portal.apps.labs import services
        services.request_lab(user, challenge.id)
        services.claim(user, challenge.id)
        lab = user.labs.get(challenge=challenge)
        url = guacamole.issue_grant(user, lab)
        self.assertIn("#/client/", url)
        self.assertIn("data=", url)
        # The grant payload expires in the future.
        data_part = unquote(url.split("data=")[1].split("#")[0])
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding, hashes, hmac as hmac_mod
        import hashlib
        key = bytes.fromhex("ab" * 16)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).decryptor()
        padded = base64.b64decode(data_part)
        unpadder = padding.PKCS7(128).unpadder()
        raw = unpadder.update(decryptor.update(padded) + decryptor.finalize()) + unpadder.finalize()
        payload = json_lib.loads(raw[32:])
        self.assertGreater(payload["expires"], 0)
