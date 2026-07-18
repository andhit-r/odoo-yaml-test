# Copyright 2026 Andhitia Rama
# License Apache-2.0 (http://www.apache.org/licenses/LICENSE-2.0).
"""Throwaway models for the ``fake_models:`` probe.

DELIBERATELY NOT IMPORTED FROM ``tests/__init__.py``.

Odoo builds every model class that is imported while an addon loads. If this
module were imported there, ``yaml.test.fake.consumer`` would become a real
model with a real table in every database that installs the probe — and the
probe would then be proving nothing, because the model it "creates" would
already exist. ``fake_models:`` imports this module at the right moment, after
the registry has been snapshotted.

``case.py`` refuses to run when it detects that this leak has happened.
"""

from odoo import api, fields, models


class YamlTestFakeMixin(models.AbstractModel):
    """An abstract mixin — no table, cannot be instantiated directly.

    This is the whole point of the feature: before ``fake_models:`` the only
    way to exercise this was to ship a concrete consumer model in a real addon.
    """

    _name = "yaml.test.fake.mixin"
    _description = "Abstract mixin exercised by the fake_models probe"

    state = fields.Selection(
        [("draft", "Draft"), ("done", "Done")],
        default="draft",
        required=True,
    )
    done_ok = fields.Boolean(compute="_compute_done_ok", store=False)

    @api.depends("state")
    def _compute_done_ok(self):
        for record in self:
            record.done_ok = record.state == "done"

    def action_done(self):
        self.write({"state": "done"})
        return True


class YamlTestFakeConsumer(models.Model):
    """A concrete model that exists only inside a test transaction."""

    _name = "yaml.test.fake.consumer"
    _inherit = "yaml.test.fake.mixin"
    _description = "Concrete consumer of the fake mixin"

    name = fields.Char(required=True)
