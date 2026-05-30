# -*- coding: utf-8 -*-
{
    'name': 'Havano Payments',
    'version': '1.0.0',
    'category': 'Accounting/Payment Providers',
    'summary': 'Paynow and EcoCash Payment Gateway Integration',
    'description': """
        This module integrates Paynow and EcoCash payment solutions into Odoo.
    """,
    'depends': ['payment'],
    'data': [
        'views/payment_form_templates.xml',
        'views/payment_provider_views.xml',
        'data/payment_method_data.xml',
        'data/payment_provider_data.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'havano_payments/static/src/interactions/payment_form.js',
        ],
    },
    'license': 'LGPL-3',
    'installable': True,
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
}
