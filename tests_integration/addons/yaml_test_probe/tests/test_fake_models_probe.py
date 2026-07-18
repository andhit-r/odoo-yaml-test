# Copyright 2026 Andhitia Rama
# License Apache-2.0 (http://www.apache.org/licenses/LICENSE-2.0).
from odoo.tests.common import tagged

from odoo_yaml_test import YamlTransactionCase


@tagged("post_install", "-at_install")
class TestFakeModelsProbe(YamlTransactionCase):
    """Proves ``fake_models:`` against a real Odoo 19 registry.

    The unit tests in ``tests/`` drive a fake registry and can only prove the
    call sequence. Only this job can prove that Odoo actually builds the
    tables, reflects ``ir.model`` rows, honours the generated ACL, and puts
    the registry back the way it found it.
    """

    def test_mixin_is_exercised_through_a_throwaway_model(self):
        self.run_yaml_scenario("scenarios/fake_models_probe.yaml")

    def test_the_fake_model_is_absent_before_and_after(self):
        """Guards the teardown.

        This method may run before or after the one above — unittest orders by
        name, but the guarantee must hold either way, so this asserts absence
        rather than a specific ordering. If ``addCleanup`` did not fire, the
        model leaks out of the other method's transaction and this fails.
        """
        self.assertNotIn("yaml.test.fake.consumer", self.env.registry)
        self.assertNotIn("yaml.test.fake.mixin", self.env.registry)
