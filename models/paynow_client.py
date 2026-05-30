# -*- coding: utf-8 -*-
import logging
import hashlib
import requests
from urllib.parse import parse_qsl, quote_plus

_logger = logging.getLogger(__name__)

class PaynowClient:
    def __init__(self, integration_id, integration_key):
        self.integration_id = str(integration_id)
        self.integration_key = str(integration_key)
        self.initiate_url = "https://www.paynow.co.zw/interface/initiatetransaction"
        self.mobile_url = "https://www.paynow.co.zw/interface/remotetransaction"

    def generate_hash(self, data):
        """
        Concatenates values in the dictionary insertion order (excluding hash)
        and appends the integration key, then hashes with SHA512.
        """
        out = ""
        for key, value in data.items():
            if key.lower() == 'hash':
                continue
            out += str(value)
        out += self.integration_key
        return hashlib.sha512(out.encode('utf-8')).hexdigest().upper()

    def verify_hash(self, data):
        """
        Verifies the hash of incoming data.
        """
        received_hash = data.get('hash')
        if not received_hash:
            return False
        computed = self.generate_hash(data)
        return computed == received_hash.upper()

    def initiate_transaction(self, reference, amount, authemail, return_url, result_url, additional_info=None):
        """
        Initiates a standard redirection payment transaction.
        """
        # Build dictionary in the exact order required
        body = {
            "resulturl": result_url,
            "returnurl": return_url,
            "reference": reference,
            "amount": f"{amount:.2f}",
            "id": self.integration_id,
            "additionalinfo": additional_info or "",
            "authemail": authemail or "",
            "status": "Message"
        }

        # URL encode only values other than returnurl and resulturl
        for k, v in body.items():
            if k not in ("returnurl", "resulturl"):
                body[k] = quote_plus(str(v))

        # Sign the payload after encoding values (matching Paynow SDK logic)
        body["hash"] = self.generate_hash(body)

        _logger.info("Paynow initiation request body: %s", body)

        try:
            res = requests.post(self.initiate_url, data=body, timeout=30)
            res.raise_for_status()
            
            # Response is urlencoded string
            response_data = dict(parse_qsl(res.text))
            _logger.info("Paynow initiation response: %s", response_data)

            if response_data.get("status") == "Error":
                return {
                    "success": False,
                    "error": response_data.get("error", "Unknown Paynow error")
                }

            if not self.verify_hash(response_data):
                return {
                    "success": False,
                    "error": "Response hash verification failed"
                }

            return {
                "success": True,
                "browserurl": response_data.get("browserurl"),
                "pollurl": response_data.get("pollurl")
            }

        except Exception as e:
            _logger.exception("Failed to initiate Paynow transaction: %s", str(e))
            return {
                "success": False,
                "error": str(e)
            }

    def initiate_mobile_transaction(self, reference, amount, authemail, phone, method, result_url, additional_info=None):
        """
        Initiates a mobile payment prompt (EcoCash / OneMoney).
        Note: returnurl and resulturl are both required by the API even for remote transactions.
        """
        # For mobile, we must pass returnurl and resulturl as well
        body = {
            "resulturl": result_url,
            "returnurl": result_url, # Fallback to resulturl
            "reference": reference,
            "amount": f"{amount:.2f}",
            "id": self.integration_id,
            "additionalinfo": additional_info or "",
            "authemail": authemail or "",
            "phone": phone,
            "method": method,
            "status": "Message"
        }

        # URL encode only values other than returnurl, resulturl, and authemail (matching Paynow SDK logic)
        for k, v in body.items():
            if k not in ("returnurl", "resulturl", "authemail"):
                body[k] = quote_plus(str(v))

        # Sign the payload after encoding values
        body["hash"] = self.generate_hash(body)

        _logger.info("Paynow mobile request body: %s", body)

        try:
            res = requests.post(self.mobile_url, data=body, timeout=30)
            res.raise_for_status()

            response_data = dict(parse_qsl(res.text))
            _logger.info("Paynow mobile response: %s", response_data)

            if response_data.get("status") == "Error":
                return {
                    "success": False,
                    "error": response_data.get("error", "Unknown Paynow error")
                }

            if not self.verify_hash(response_data):
                return {
                    "success": False,
                    "error": "Response hash verification failed"
                }

            return {
                "success": True,
                "pollurl": response_data.get("pollurl"),
                "instructions": response_data.get("instructions")
            }

        except Exception as e:
            _logger.exception("Failed to initiate Paynow mobile transaction: %s", str(e))
            return {
                "success": False,
                "error": str(e)
            }

    def poll_transaction_status(self, pollurl):
        """
        Polls Paynow for transaction status update.
        """
        try:
            res = requests.get(pollurl, timeout=30)
            res.raise_for_status()

            response_data = dict(parse_qsl(res.text))
            _logger.info("Paynow status response: %s", response_data)

            if response_data.get("status") == "Error":
                return {
                    "success": False,
                    "error": response_data.get("error", "Unknown Paynow error")
                }

            if not self.verify_hash(response_data):
                return {
                    "success": False,
                    "error": "Response hash verification failed"
                }

            return {
                "success": True,
                "status": response_data.get("status"),
                "reference": response_data.get("reference"),
                "paynowreference": response_data.get("paynowreference"),
                "amount": float(response_data.get("amount", 0))
            }
        except Exception as e:
            _logger.exception("Failed to poll Paynow status: %s", str(e))
            return {
                "success": False,
                "error": str(e)
            }
