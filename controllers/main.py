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
    <title>Authorize EcoCash Payment - Havano ERP</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        }}
        body {{
            background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 50%, #0f172a 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
            color: #f8fafc;
        }}
        .card {{
            background: rgba(30, 41, 59, 0.85);
            border: 1px solid rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(16px);
            border-radius: 24px;
            padding: 40px;
            max-width: 480px;
            width: 100%;
            text-align: center;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5), 0 0 40px rgba(99, 102, 241, 0.15);
            animation: fadeIn 0.4s ease-out;
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(16px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .phone-pulse-container {{
            position: relative;
            width: 90px;
            height: 90px;
            margin: 0 auto 24px auto;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .phone-circle {{
            width: 76px;
            height: 76px;
            background: linear-gradient(135deg, #4f46e5, #7c3aed);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 10px 25px -5px rgba(99, 102, 241, 0.5);
            z-index: 2;
            transition: all 0.3s ease;
        }}
        .phone-circle svg {{
            width: 38px;
            height: 38px;
            fill: #ffffff;
        }}
        .pulse-ring {{
            position: absolute;
            width: 100%;
            height: 100%;
            border-radius: 50%;
            border: 2px solid rgba(129, 140, 248, 0.6);
            animation: pulse 2s cubic-bezier(0.24, 0, 0.38, 1) infinite;
            z-index: 1;
        }}
        @keyframes pulse {{
            0% {{ transform: scale(0.85); opacity: 0.8; }}
            50% {{ transform: scale(1.3); opacity: 0.2; }}
            100% {{ transform: scale(1.5); opacity: 0; }}
        }}
        h2 {{
            font-size: 24px;
            font-weight: 700;
            color: #ffffff;
            margin-bottom: 8px;
            letter-spacing: -0.02em;
        }}
        .amount-tag {{
            display: inline-block;
            background: rgba(99, 102, 241, 0.15);
            border: 1px solid rgba(99, 102, 241, 0.3);
            color: #a5b4fc;
            padding: 6px 16px;
            border-radius: 20px;
            font-weight: 600;
            font-size: 15px;
            margin-bottom: 18px;
        }}
        .instructions {{
            font-size: 14px;
            line-height: 1.6;
            color: #94a3b8;
            margin-bottom: 24px;
        }}
        .instructions strong {{
            color: #e2e8f0;
        }}
        .status-box {{
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 14px;
            padding: 14px 18px;
            margin-bottom: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 12px;
            font-size: 14px;
            color: #cbd5e1;
            font-weight: 500;
        }}
        .spinner {{
            width: 18px;
            height: 18px;
            border: 2px solid rgba(255, 255, 255, 0.2);
            border-top-color: #818cf8;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }}
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
        .btn {{
            display: block;
            width: 100%;
            padding: 14px 20px;
            border-radius: 12px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.2s ease;
            border: none;
            margin-bottom: 12px;
        }}
        .btn-primary {{
            background: linear-gradient(135deg, #4f46e5, #6366f1);
            color: #ffffff;
            box-shadow: 0 4px 14px rgba(79, 70, 229, 0.4);
        }}
        .btn-primary:hover {{
            background: linear-gradient(135deg, #4338ca, #4f46e5);
            transform: translateY(-1px);
        }}
        .btn-secondary {{
            background: transparent;
            color: #94a3b8;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }}
        .btn-secondary:hover {{
            background: rgba(255, 255, 255, 0.05);
            color: #f8fafc;
        }}
        .ref-text {{
            font-size: 12px;
            color: #64748b;
            margin-top: 10px;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="phone-pulse-container">
            <div class="pulse-ring"></div>
            <div class="phone-circle" id="iconCircle">
                <svg viewBox="0 0 24 24">
                    <path d="M17 1.01L7 1c-1.1 0-2 .9-2 2v18c0 1.1.9 2 2 2h10c1.1 0 2-.9 2-2V3c0-1.1-.9-1.99-2-1.99zM17 19H7V5h10v14z"/>
                </svg>
            </div>
        </div>
        <h2>Authorize Payment</h2>
        <div class="amount-tag">Amount: ${amount_str}</div>
        <p class="instructions">
            A payment prompt has been sent to your phone. Please check your EcoCash phone and <strong>enter your PIN</strong> to authorize the payment.
        </p>

        <div class="status-box" id="statusBox">
            <div class="spinner" id="statusSpinner"></div>
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
            const iconCircle = document.getElementById('iconCircle');
            const statusSpinner = document.getElementById('statusSpinner');

            if (isManual) {{
                btnCheck.innerText = 'Verifying with Paynow...';
                btnCheck.disabled = true;
            }}

            try {{
                const res = await fetch('/payment/havano_payments/check_status?reference=' + encodeURIComponent(reference));
                const result = await res.json();

                if (result.paid) {{
                    statusBox.style.background = 'rgba(16, 185, 129, 0.15)';
                    statusBox.style.borderColor = 'rgba(16, 185, 129, 0.3)';
                    statusBox.style.color = '#34d399';
                    if (statusSpinner) statusSpinner.style.display = 'none';
                    if (iconCircle) iconCircle.style.background = 'linear-gradient(135deg, #059669, #10b981)';
                    statusText.innerHTML = '<strong>Payment Confirmed!</strong> Redirecting to your subscription...';
                    setTimeout(() => {{
                        window.location.href = result.redirect_url || redirectUrl;
                    }}, 1000);
                    return;
                }} else if (result.status === 'Cancelled' || result.status === 'Failed') {{
                    statusBox.style.background = 'rgba(239, 68, 68, 0.15)';
                    statusBox.style.borderColor = 'rgba(239, 68, 68, 0.3)';
                    statusBox.style.color = '#f87171';
                    if (statusSpinner) statusSpinner.style.display = 'none';
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
