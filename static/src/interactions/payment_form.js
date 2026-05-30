import { patch } from '@web/core/utils/patch';
import { PaymentForm } from '@payment/interactions/payment_form';
import { rpc } from "@web/core/network/rpc";

patch(PaymentForm.prototype, {

    // #=== DOM MANIPULATION ===#

    /**
     * Prepare the inline form for payment.
     * Set flow to direct for EcoCash, redirect for standard Paynow card.
     */
    async _prepareInlineForm(providerId, providerCode, paymentOptionId, paymentMethodCode, flow) {
        if (providerCode !== 'havano_payments') {
            await super._prepareInlineForm(...arguments);
            return;
        }
        
        if (paymentMethodCode === 'ecocash') {
            this._setPaymentFlow('direct');
        } else {
            this._setPaymentFlow('redirect');
        }
    },

    // #=== PAYMENT FLOW ===#

    /**
     * Process EcoCash direct flow.
     */
    async _processDirectFlow(providerCode, paymentOptionId, paymentMethodCode, processingValues) {
        if (providerCode !== 'havano_payments' || paymentMethodCode !== 'ecocash') {
            await super._processDirectFlow(...arguments);
            return;
        }

        const phoneInput = document.getElementById('o_havano_payments_phone');
        const phone = phoneInput ? phoneInput.value.trim() : '';
        if (!phone) {
            this._displayErrorDialog("Error", "Please enter a valid phone number.");
            this._enableButton();
            return;
        }

        try {
            const result = await this.waitFor(rpc('/payment/havano_payments/initiate_mobile', {
                'reference': processingValues.reference,
                'phone': phone,
            }));
            
            if (result.success) {
                // Redirect to Odoo payment status page
                window.location = '/payment/status';
            } else {
                this._displayErrorDialog("Payment Error", result.error || "Failed to initiate payment.");
                this._enableButton();
            }
        } catch (error) {
            this._displayErrorDialog("Connection Error", "Could not connect to payment server.");
            this._enableButton();
        }
    },

});
