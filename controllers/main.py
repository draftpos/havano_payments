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

        # If this is a subscription or top-up payment, redirect straight to My Subscription
        if tx_sudo.subscription_payment_id or (tx_sudo.reference and (tx_sudo.reference.startswith('SUB-') or tx_sudo.reference.startswith('TOP-'))):
            action = request.env.ref('havanoposdesk_odoo.action_my_subscription', raise_if_not_found=False)
            action_id = action.id if action else 264
            return request.redirect(f"/odoo/action-{action_id}")

        return request.redirect('/payment/status')

    @http.route('/payment/havano_payments/webhook', type='http', auth='public', methods=['POST'], csrf=False)
    def havano_payments_webhook(self, **kwargs):
        """ Handles Paynow status update notification (webhook). """
        # We must use httprequest.form to preserve Paynow's exact POST payload order for hash verification.
        # kwargs mixes in GET params (like ?reference=...) which alters dictionary order and breaks the hash.
        payload = dict(request.httprequest.form)
        _logger.info("Paynow webhook notification received with payload: %s", payload)
        
        reference = payload.get('reference') or kwargs.get('reference')
        if not reference:
            return "Missing reference", 400

        tx_sudo = request.env['payment.transaction'].sudo().search([('reference', '=', reference)], limit=1)
        if not tx_sudo:
            return "Transaction not found", 404

        client = PaynowClient(
            tx_sudo.provider_id.paynow_integration_id,
            tx_sudo.provider_id.paynow_integration_key
        )

        # Verify hash using the exact POST payload
        if not client.verify_hash(payload):
            _logger.warning("Paynow webhook signature verification failed for reference: %s", reference)
            return "Invalid signature", 400

        # Process the update with the verified payload
        tx_sudo._process('havano_payments', payload)
        return "OK", 200

    @http.route('/payment/havano_payments/initiate_mobile', type='jsonrpc', auth='public', methods=['POST'])
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

    @http.route('/payment/havano_payments/check_status', type='http', auth='public', methods=['GET', 'POST'], csrf=False)
    def havano_payments_check_status(self, reference=None, **kwargs):
        """ Endpoint for browser to poll payment status and auto-process Paynow transactions. """
        import json
        ref = reference or kwargs.get('reference')
        if not ref:
            return request.make_response(
                json.dumps({'success': False, 'error': 'Missing reference'}),
                headers=[('Content-Type', 'application/json')]
            )

        tx_sudo = request.env['payment.transaction'].sudo().search([('reference', '=', ref)], limit=1)
        if not tx_sudo:
            return request.make_response(
                json.dumps({'success': False, 'error': 'Transaction not found'}),
                headers=[('Content-Type', 'application/json')]
            )

        action = request.env.ref('havanoposdesk_odoo.action_my_subscription', raise_if_not_found=False)
        redirect_url = f"/odoo/action-{action.id}" if action else "/odoo/action-264"

        # If already marked done
        if tx_sudo.state == 'done':
            return request.make_response(json.dumps({
                'success': True,
                'paid': True,
                'status': 'Paid',
                'redirect_url': redirect_url
            }), headers=[('Content-Type', 'application/json')])

        # Poll Paynow
        if tx_sudo.paynow_poll_url and tx_sudo.provider_id:
            try:
                client = PaynowClient(
                    tx_sudo.provider_id.paynow_integration_id,
                    tx_sudo.provider_id.paynow_integration_key
                )
                status_res = client.poll_transaction_status(tx_sudo.paynow_poll_url)
                status = (status_res.get('status') or '').lower()
                if status in ('paid', 'awaiting delivery'):
                    tx_sudo._process('havano_payments', status_res)
                    return request.make_response(json.dumps({
                        'success': True,
                        'paid': True,
                        'status': 'Paid',
                        'redirect_url': redirect_url
                    }), headers=[('Content-Type', 'application/json')])
                elif status in ('cancelled', 'canceled', 'failed'):
                    tx_sudo._process('havano_payments', status_res)
                    return request.make_response(json.dumps({
                        'success': True,
                        'paid': False,
                        'status': status_res.get('status'),
                        'redirect_url': redirect_url
                    }), headers=[('Content-Type', 'application/json')])
                else:
                    return request.make_response(json.dumps({
                        'success': True,
                        'paid': False,
                        'status': status_res.get('status') or 'Pending'
                    }), headers=[('Content-Type', 'application/json')])
            except Exception as e:
                _logger.warning("Paynow poll error for ref %s: %s", ref, e)

        return request.make_response(json.dumps({'success': True, 'paid': False, 'status': 'Pending'}), headers=[('Content-Type', 'application/json')])

    @http.route('/payment/havano_payments/ecocash_waiting', type='http', auth='public', methods=['GET'], csrf=False)
    def havano_payments_ecocash_waiting(self, **kwargs):
        """ Renders interactive EcoCash approval and auto-polling page. """
        reference = kwargs.get('reference')
        if not reference:
            return request.redirect('/payment/status')

        tx_sudo = request.env['payment.transaction'].sudo().search([('reference', '=', reference)], limit=1)
        if not tx_sudo:
            return request.redirect('/payment/status')

        action = request.env.ref('havanoposdesk_odoo.action_my_subscription', raise_if_not_found=False)
        subscription_url = f"/odoo/action-{action.id}" if action else "/odoo/action-264"

        # Check if already paid right now
        if tx_sudo.state == 'done':
            return request.redirect(subscription_url)

        if tx_sudo.paynow_poll_url and tx_sudo.provider_id:
            try:
                client = PaynowClient(
                    tx_sudo.provider_id.paynow_integration_id,
                    tx_sudo.provider_id.paynow_integration_key
                )
                status_res = client.poll_transaction_status(tx_sudo.paynow_poll_url)
                if (status_res.get('status') or '').lower() in ('paid', 'awaiting delivery'):
                    tx_sudo._process('havano_payments', status_res)
                    return request.redirect(subscription_url)
            except Exception:
                pass

        amount_str = f"{tx_sudo.amount:.2f}"

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Authorize EcoCash Payment</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        }}
        body {{
            background-color: #f1f5f9;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
            color: #0f172a;
        }}
        .card {{
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 36px 32px;
            max-width: 440px;
            width: 100%;
            text-align: center;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
        }}
        .spinner-container {{
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 48px;
            margin-bottom: 20px;
        }}
        .main-spinner {{
            width: 40px;
            height: 40px;
            border: 3px solid #e2e8f0;
            border-top-color: #0278F3;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }}
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
        h2 {{
            font-size: 18px;
            font-weight: 600;
            color: #0f172a;
            margin-bottom: 8px;
            letter-spacing: -0.01em;
        }}
        .amount-tag {{
            display: inline-block;
            background: #eff6ff;
            border: 1px solid #dbeafe;
            color: #0278F3;
            padding: 4px 12px;
            border-radius: 6px;
            font-weight: 600;
            font-size: 13px;
            margin-bottom: 16px;
        }}
        .instructions {{
            font-size: 14px;
            line-height: 1.5;
            color: #475569;
            margin-bottom: 20px;
        }}
        .instructions strong {{
            color: #0f172a;
        }}
        .status-box {{
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            padding: 11px 16px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            font-size: 13px;
            color: #64748b;
            font-weight: 500;
        }}
        .status-spinner {{
            width: 14px;
            height: 14px;
            border: 2px solid #cbd5e1;
            border-top-color: #0278F3;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }}
        .btn {{
            display: flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.15s ease;
            box-sizing: border-box;
        }}
        .btn-primary {{
            background-color: #0278F3;
            border: 1px solid #0278F3;
            color: #ffffff;
            height: 38px;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
            margin-bottom: 8px;
        }}
        .btn-primary:hover {{
            background-color: #0260c4;
            border-color: #0260c4;
        }}
        .btn-primary:disabled {{
            background-color: #93c5fd;
            border-color: #93c5fd;
            cursor: not-allowed;
            opacity: 0.7;
        }}
        .btn-secondary {{
            background: #ffffff;
            color: #64748b;
            border: 1px solid #cbd5e1;
            height: 36px;
        }}
        .btn-secondary:hover {{
            background: #f8fafc;
            color: #0f172a;
            border-color: #94a3b8;
        }}
        .ref-text {{
            font-size: 11px;
            color: #94a3b8;
            margin-top: 16px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="spinner-container">
            <div class="main-spinner" id="mainSpinner"></div>
        </div>
        <h2>Authorize EcoCash Payment</h2>
        <div class="amount-tag">Amount: ${amount_str}</div>
        <p class="instructions">
            A payment prompt has been sent to your phone. Please check your EcoCash phone and <strong>enter your PIN</strong> to authorize the payment.
        </p>

        <div class="status-box" id="statusBox">
            <div class="status-spinner" id="statusSpinner"></div>
            <span id="statusText">Waiting for EcoCash confirmation...</span>
        </div>

        <button class="btn btn-primary" id="btnCheck" onclick="checkStatus(true)">
            I Have Entered My PIN (Check Status)
        </button>
        <a href="{subscription_url}" class="btn btn-secondary">
            Return to Subscription
        </a>
        <div class="ref-text">Ref: {reference}</div>
    </div>

    <script>
        const reference = "{reference}";
        const redirectUrl = "{subscription_url}";
        let isChecking = false;

        async function checkStatus(isManual = false) {{
            if (isChecking) return;
            isChecking = true;
            
            const statusText = document.getElementById('statusText');
            const statusBox = document.getElementById('statusBox');
            const btnCheck = document.getElementById('btnCheck');
            const mainSpinner = document.getElementById('mainSpinner');
            const statusSpinner = document.getElementById('statusSpinner');

            if (isManual && btnCheck) {{
                btnCheck.innerText = 'Verifying with Paynow...';
                btnCheck.disabled = true;
            }}

            try {{
                const res = await fetch('/payment/havano_payments/check_status?reference=' + encodeURIComponent(reference));
                const result = await res.json();

                if (result.paid) {{
                    statusBox.style.background = '#f0fdf4';
                    statusBox.style.borderColor = '#bbf7d0';
                    statusBox.style.color = '#16a34a';
                    if (statusSpinner) statusSpinner.style.display = 'none';
                    if (mainSpinner) {{
                        mainSpinner.style.border = 'none';
                        mainSpinner.style.animation = 'none';
                        mainSpinner.innerHTML = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#16a34a" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>';
                    }}
                    statusText.innerHTML = '<strong>Payment Confirmed!</strong> Redirecting...';
                    setTimeout(() => {{
                        window.location.href = result.redirect_url || redirectUrl;
                    }}, 1000);
                    return;
                }} else if (result.status === 'Cancelled' || result.status === 'Failed') {{
                    statusBox.style.background = '#fef2f2';
                    statusBox.style.borderColor = '#fecaca';
                    statusBox.style.color = '#dc2626';
                    if (statusSpinner) statusSpinner.style.display = 'none';
                    if (mainSpinner) {{
                        mainSpinner.style.border = 'none';
                        mainSpinner.style.animation = 'none';
                        mainSpinner.innerHTML = '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="#dc2626" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>';
                    }}
                    statusText.innerText = 'Payment was cancelled or failed.';
                    if (btnCheck) btnCheck.style.display = 'none';
                    return;
                }}
            }} catch (err) {{
                console.error('Poll error:', err);
            }} finally {{
                isChecking = false;
                if (isManual && btnCheck) {{
                    btnCheck.innerText = 'I Have Entered My PIN (Check Status)';
                    btnCheck.disabled = false;
                }}
            }}
        }}

        // Auto poll every 3 seconds
        const pollInterval = setInterval(() => {{
            checkStatus(false);
        }}, 3000);
    </script>
</body>
</html>"""
        return request.make_response(html_content, headers=[('Content-Type', 'text/html; charset=utf-8')])
