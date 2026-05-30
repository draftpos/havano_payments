# -*- coding: utf-8 -*-
from odoo import fields, models

class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(selection_add=[('havano_payments', 'Havano Payments')], ondelete={'havano_payments': 'set default'})

    paynow_integration_id = fields.Char(
        string="Paynow Integration ID",
        required_if_provider='havano_payments',
        groups='base.group_system'
    )
    paynow_integration_key = fields.Char(
        string="Paynow Integration Key",
        required_if_provider='havano_payments',
        groups='base.group_system'
    )

    def _get_default_payment_method_codes(self):
        """ Override of `payment` to return the default payment method codes. """
        self.ensure_one()
        if self.code != 'havano_payments':
            return super()._get_default_payment_method_codes()
        return {'paynow', 'ecocash'}
