"""Paytm JS Checkout helpers. Secrets and checksum operations stay server-side."""
import json
import os

import requests
import paytmchecksum as PaytmChecksum


def config():
    environment = os.environ.get("PAYTM_ENV", "test").strip().lower()
    if environment not in {"test", "production"}:
        raise RuntimeError("PAYTM_ENV must be 'test' or 'production'.")
    mid = os.environ.get("PAYTM_MID", "").strip()
    key = os.environ.get("PAYTM_MERCHANT_KEY", "").strip()
    if not mid or not key or "[" in mid or "[" in key:
        raise RuntimeError("Set PAYTM_MID and PAYTM_MERCHANT_KEY in .env.")
    host = "https://securestage.paytmpayments.com" if environment == "test" else "https://secure.paytmpayments.com"
    website = "WEBSTAGING" if environment == "test" else "DEFAULT"
    return {"env": environment, "mid": mid, "key": key, "host": host, "website": website}


def signed_json_request(url, body, key):
    """Build Paytm's JSON API envelope using its official checksum utility."""
    body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    signature = PaytmChecksum.generateSignature(body_text, key)
    envelope = {"body": body, "head": {"signature": signature}}
    request_text = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    response = requests.post(url, data=request_text, headers={"Content-Type": "application/json"}, timeout=25)
    response.raise_for_status()
    return response.json()


def verify_json_response(response, key):
    """Verify Paytm's signed JSON body before trusting its fields."""
    try:
        body = response["body"]
        signature = response["head"]["signature"]
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        return bool(PaytmChecksum.verifySignature(body_text, key, signature))
    except (KeyError, TypeError, ValueError):
        return False


def verify_callback(fields, key):
    """Verify Paytm's form callback (CHECKSUMHASH is excluded from signed fields)."""
    params = dict(fields)
    signature = params.pop("CHECKSUMHASH", "")
    if not signature:
        return False
    return bool(PaytmChecksum.verifySignature(params, key, signature))


def initiate_transaction(order_id, amount, customer_id, callback_url):
    settings = config()
    body = {
        "requestType": "Payment",
        "mid": settings["mid"],
        "websiteName": settings["website"],
        "orderId": order_id,
        "callbackUrl": callback_url,
        "txnAmount": {"value": amount, "currency": "INR"},
        "userInfo": {"custId": str(customer_id)},
    }
    response = signed_json_request(
        f"{settings['host']}/theia/api/v1/initiateTransaction?mid={settings['mid']}&orderId={order_id}",
        body,
        settings["key"],
    )
    if not verify_json_response(response, settings["key"]):
        raise RuntimeError("Paytm initiate response signature did not verify.")
    response_body = response.get("body", {})
    if response_body.get("resultInfo", {}).get("resultStatus") != "S" or not response_body.get("txnToken"):
        raise RuntimeError(response_body.get("resultInfo", {}).get("resultMsg", "Paytm could not initiate this payment."))
    return response_body["txnToken"], settings


def fetch_payment_status(order_id, settings):
    response = signed_json_request(
        f"{settings['host']}/v3/order/status",
        {"mid": settings["mid"], "orderId": order_id},
        settings["key"],
    )
    if not verify_json_response(response, settings["key"]):
        raise RuntimeError("Paytm status response signature did not verify.")
    return response.get("body", {})
