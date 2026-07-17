# Copyright 2026 Andhitia Rama
# License Apache-2.0 (http://www.apache.org/licenses/LICENSE-2.0).
from odoo.tests.common import tagged

from odoo_yaml_test import YamlTransactionCase


@tagged("post_install", "-at_install")
class TestRefreshProbe(YamlTransactionCase):
    def test_refresh_clears_stale_compute(self):
        self.run_yaml_scenario("scenarios/refresh_probe.yaml")
