# -*- coding: utf-8 -*-
import logging
from odoo import http, _
from odoo.http import request
from odoo.addons.havano_payments.models.paynow_client import PaynowClient

_logger = logging.getLogger(__name__)

class HavanoPaymentsController(http.Controller):

    @http.route('/payment/havano_payments/return', type='http', auth='public', methods=['GET'], csrf=False)
    def havano_payments_return(self, **kwargs):
        """ Handles redirect return from Paynow. """
        _logger.info("Paynow return callback received with params: %s", kwargs)
        reference = kwargs.get('reference')
        if not reference:
            return request.redirect('/payment/status')

        tx_sudo = request.env['payment.transaction'].sudo().search([('reference', '=', reference)], limit=1)
        if not tx_sudo or not tx_sudo.paynow_poll_url:
            return request.redirect('/payment/status')

        # Poll status
        client = PaynowClient(
            tx_sudo.provider_id.paynow_integration_id,
            tx_sudo.provider_id.paynow_integration_key
        )
        status_res = client.poll_transaction_status(tx_sudo.paynow_poll_url)
        if status_res.get('success'):
            tx_sudo._process('havano_payments', status_res)
        else:
            tx_sudo._set_error(_("Failed to verify transaction status: %s", status_res.get('error')))

        return request.redirect('/payment/status')

    @http.route('/payment/havano_payments/webhook', type='http', auth='public', methods=['POST'], csrf=False)
    def havano_payments_webhook(self, **kwargs):
        """ Handles Paynow status update notification (webhook). """
        _logger.info("Paynow webhook notification received with params: %s", kwargs)
        reference = kwargs.get('reference')
        if not reference:
            return "Missing reference", 400

        tx_sudo = request.env['payment.transaction'].sudo().search([('reference', '=', reference)], limit=1)
        if not tx_sudo:
            return "Transaction not found", 404

        client = PaynowClient(
            tx_sudo.provider_id.paynow_integration_id,
            tx_sudo.provider_id.paynow_integration_key
        )

        # Verify hash
        if not client.verify_hash(kwargs):
            _logger.warning("Paynow webhook signature verification failed for reference: %s", reference)
            return "Invalid signature", 400

        # Process the update
        tx_sudo._process('havano_payments', kwargs)
        return "OK", 200

    @http.route('/payment/havano_payments/initiate_mobile', type='json', auth='public', methods=['POST'])
    def havano_payments_initiate_mobile(self, reference, phone):
        """ RPC endpoint to initiate EcoCash mobile prompt (USSD push). """
        _logger.info("EcoCash initiation request for transaction reference %s, phone %s", reference, phone)
        
        tx_sudo = request.env['payment.transaction'].sudo().search([('reference', '=', reference)], limit=1)
        if not tx_sudo or not tx_sudo.exists():
            return {
                "success": False,
                "error": "Transaction not found"
            }

        # Initialize Paynow Client
        client = PaynowClient(
            tx_sudo.provider_id.paynow_integration_id,
            tx_sudo.provider_id.paynow_integration_key
        )

        base_url = tx_sudo.provider_id.get_base_url()
        result_url = f"{base_url}/payment/havano_payments/webhook?reference={tx_sudo.reference}"

        # Call Paynow to initiate mobile payment (EcoCash)
        mobile_res = client.initiate_mobile_transaction(
            reference=tx_sudo.reference,
            amount=tx_sudo.amount,
            authemail=tx_sudo.partner_email or tx_sudo.partner_id.email or "customer@example.com",
            phone=phone,
            method="ecocash",
            result_url=result_url,
            additional_info=f"Odoo EcoCash Order {tx_sudo.reference}"
        )

        if not mobile_res.get('success'):
            tx_sudo._set_error(_("EcoCash initiation failed: %s", mobile_res.get('error')))
            return {
                "success": False,
                "error": mobile_res.get('error')
            }

        # Success - store poll url and set to pending
        tx_sudo.paynow_poll_url = mobile_res['pollurl']
        tx_sudo._set_pending()

        return {
            "success": True,
            "instructions": mobile_res.get('instructions')
        }
