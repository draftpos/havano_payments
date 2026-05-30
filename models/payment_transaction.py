# -*- coding: utf-8 -*-
import logging
from odoo import fields, models, _
from odoo.exceptions import ValidationError
from odoo.addons.havano_payments.models.paynow_client import PaynowClient

_logger = logging.getLogger(__name__)

class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    paynow_poll_url = fields.Char(string="Paynow Poll URL")

    def _get_specific_rendering_values(self, processing_values):
        """ Override of payment to return Paynow-specific rendering values. """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'havano_payments':
            return res

        # We only do this for standard paynow redirection flow
        if self.payment_method_code != 'paynow':
            return res

        # Setup URLs
        base_url = self.provider_id.get_base_url()
        return_url = f"{base_url}/payment/havano_payments/return?reference={self.reference}"
        result_url = f"{base_url}/payment/havano_payments/webhook?reference={self.reference}"

        # Initialize Paynow Client
        client = PaynowClient(
            self.provider_id.paynow_integration_id,
            self.provider_id.paynow_integration_key
        )

        # Call Paynow API to initiate standard transaction
        init_res = client.initiate_transaction(
            reference=self.reference,
            amount=self.amount,
            authemail=self.partner_email or self.partner_id.email or "customer@example.com",
            return_url=return_url,
            result_url=result_url,
            additional_info=f"Odoo Order {self.reference}"
        )

        if not init_res.get('success'):
            raise ValidationError(_("Could not initiate Paynow payment: %s", init_res.get('error')))

        # Store poll URL
        self.paynow_poll_url = init_res['pollurl']

        return {
            'api_url': init_res['browserurl']
        }

    def _extract_amount_data(self, payment_data):
        """ Override of `payment` to parse amount data. """
        if self.provider_code != 'havano_payments':
            return super()._extract_amount_data(payment_data)
        
        # We can extract the amount from the status update payload
        # Note: Paynow sends amount as a string decimal
        amount = float(payment_data.get('amount', 0.0))
        return {
            'amount': amount,
            'currency_code': self.currency_id.name
        }

    def _apply_updates(self, payment_data):
        """ Override of `payment` to update state based on Paynow status. """
        if self.provider_code != 'havano_payments':
            return super()._apply_updates(payment_data)

        # Map Paynow statuses to Odoo transaction states
        # Paynow status can be: "Paid", "Awaiting Delivery", "Sent", "Awaiting Payment", "Cancelled", "Failed"
        status = payment_data.get('status', '').lower()
        self.provider_reference = payment_data.get('paynowreference')

        if status in ("paid", "awaiting delivery"):
            self._set_done()
        elif status in ("awaiting payment", "sent", "pending"):
            self._set_pending()
        elif status in ("cancelled", "canceled"):
            self._set_canceled()
        else:
            self._set_error(_("Paynow transaction failed with status: %s", payment_data.get('status')))
