# Copyright 2026 Andhitia Rama
# License Apache-2.0 (http://www.apache.org/licenses/LICENSE-2.0).
from odoo import api, fields, models


class YamlTestProbe(models.Model):
    _name = "yaml.test.probe"
    _description = "Probe model for odoo-yaml-test integration tests"

    name = fields.Char(
        string="Name",
        required=True,
        help="Free-text label; only used to make failures readable.",
    )
    source = fields.Integer(
        string="Source",
        default=0,
        help="The value mirror() doubles. Writing this is what makes mirror stale.",
    )
    mirror = fields.Integer(
        string="Mirror",
        compute="_compute_mirror",
        store=False,
        help=(
            "Twice source, computed on read and never invalidated by Odoo. "
            "Reading it after writing source returns the cached value unless "
            "the cache is dropped explicitly."
        ),
    )

    # The empty depends() is the whole point: Odoo is told this field depends on
    # nothing, so it installs no trigger that would invalidate `mirror` when
    # `source` changes. That reproduces the SSI policy-field staleness
    # (confirm_ok/approve_ok after a state transition) that
    # YamlTransactionCase._refresh() exists to clear — in a model that depends on
    # nothing but `base`.
    @api.depends()
    def _compute_mirror(self):
        for record in self:
            record.mirror = record.source * 2
