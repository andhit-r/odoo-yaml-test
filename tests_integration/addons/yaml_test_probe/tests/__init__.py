from . import test_fake_models_probe, test_refresh_probe

# NOTE: `fake_models` is deliberately NOT imported here. Odoo builds every model
# class imported while the addon loads, so importing it would create the fake
# models for real — the exact failure this probe exists to rule out.
