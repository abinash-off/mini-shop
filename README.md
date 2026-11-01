# mini-shop
# Common Goods — Flask + Paytm JS Checkout

A small Flask/SQLite storefront with account signup, catalog, persistent cart, stock-aware orders, order history, admin tools, and Paytm JS Checkout. Payment credentials and checksum operations stay on the server. An order is marked **paid** only after a valid Paytm callback checksum and a successful, signed Transaction Status API response that matches the merchant, order, and amount.

## 1. Paytm sandbox credentials

Create or sign in to your Paytm merchant account, then open **Developer Settings → API Keys → Test API details** and generate/view the staging credentials. Paytm provides a sandbox MID and Merchant Key; do not paste the key into source code, frontend JavaScript, or Git. Production credentials become available after account activation. See [Paytm's getting started guide](https://www.paytmpayments.com/docs/getting-started) and [official API key instructions](https://www.paytmpayments.com/docs/support/questions?category=Testing-and-Generic-Queries&query=2).

Copy `.env.example` to `.env` and replace the bracketed values:

```dotenv
SECRET_KEY=[GENERATE_A_LONG_RANDOM_VALUE]
PAYTM_ENV=test
PAYTM_MID=[TEST_MERCHANT_ID]
PAYTM_MERCHANT_KEY=[TEST_MERCHANT_KEY]
PUBLIC_BASE_URL=https://[YOUR_NGROK_SUBDOMAIN].ngrok-free.app
PORT=5000
```

Generate a Flask secret key in PowerShell with:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

## 2. Run locally

In PowerShell, change into the folder that contains `app.py` (in this workspace it is `mini shop`):

```powershell
cd "D:\KARUNYA\Projects\HOD\mini shop"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python app.py
```

If PowerShell blocks environment activation, run `Set-ExecutionPolicy -Scope Process Bypass` in that terminal, then activate again. The database `store.db` and demo products are created automatically. Open <http://127.0.0.1:5000> and sign in or create an account.

### Expose the callback for local testing

Paytm's server must be able to reach the callback endpoint. A plain `localhost` URL is private to your computer, so use a temporary HTTPS tunnel while the Flask app runs. For example, install/run ngrok in a second terminal:

```powershell
ngrok http 5000
```

Copy the HTTPS forwarding address from ngrok into `PUBLIC_BASE_URL` in `.env` (no trailing slash), then restart Flask. If the ngrok URL changes, update `.env` and restart the app. The app passes `PUBLIC_BASE_URL/payment/callback` to Paytm as the callback URL.

## 3. Test a payment

1. Add an item to the bag and select **Pay with Paytm**. The server creates a pending order, reserves stock, signs the Initiate Transaction request, and gets a transaction token.
2. Paytm CheckoutJS opens. Use the staging payment instrument values from [Paytm's current test instruments page](https://www.paytmpayments.com/docs/test-instruments/testing-integration):
   - Card: any Visa or Mastercard number, a future expiry, CVV `123`. Paytm's staging page lets you choose a successful or failed result.
   - Test wallet: mobile `77777 77777`, password `Paytm12345`, OTP `489871` (the page may indicate OTP is not required).
3. Paytm posts the signed callback. The app verifies `CHECKSUMHASH`, then independently calls Transaction Status API and checks its signed response, MID, order ID, `TXN_SUCCESS`, and exact amount.
4. Only then does the order payment status become `paid` and fulfillment status change to `processing`. A failed final status marks it failed/cancelled and releases the reserved stock. A pending response stays unpaid.

For customer-visible orders, use **Orders** after returning to the shop. For admin tools, sign up first, stop Flask, and promote that account from the project folder:

```powershell
python -c "import sqlite3; c=sqlite3.connect('store.db'); c.execute('UPDATE users SET is_admin=1 WHERE email=?', ('you@example.com',)); c.commit(); print('Admin account updated:', c.total_changes)"
```

## 4. Payment flow and API shapes

- `POST /checkout` (signed-in customer): creates an order from the server's cart and current database prices. The browser cannot choose the amount.
- `GET /pay/<order_id>`: renders CheckoutJS with the transaction token. The token and MID are used by CheckoutJS; the Merchant Key is never sent to the browser.
- `POST /payment/callback`: receives Paytm's form post, verifies its checksum, calls the server-to-server Transaction Status API, and updates the order only from that final response.
- `GET /payment/return/<order_id>`: shows the stored payment outcome to the customer.

Initiate Transaction is signed over the JSON body and sent server-to-server. The relevant shape is:

```json
{
  "body": {
    "requestType": "Payment",
    "mid": "[TEST_MERCHANT_ID]",
    "websiteName": "WEBSTAGING",
    "orderId": "CG42-a1b2c3d4e5f6",
    "callbackUrl": "https://[YOUR_NGROK_SUBDOMAIN].ngrok-free.app/payment/callback",
    "txnAmount": {"value": "64.00", "currency": "INR"},
    "userInfo": {"custId": "[CUSTOMER_ID]"}
  },
  "head": {"signature": "[SERVER_GENERATED_SIGNATURE]"}
}
```

Paytm returns a signed response containing `body.txnToken` and `body.resultInfo.resultStatus`. The token is short-lived (15 minutes per Paytm docs). The callback is a form post with fields such as `MID`, `ORDERID`, `TXNAMOUNT`, `STATUS`, `TXNID`, and `CHECKSUMHASH`. The callback is not treated as the final payment authority: the app signs a `/v3/order/status` request and validates Paytm's signed response before changing `payment_status` to `paid`.

Implementation lives in [app.py](./app.py), [paytm_gateway.py](./paytm_gateway.py), and [templates/paytm_checkout.html](./templates/paytm_checkout.html). The checksum package and API request format follow Paytm's [checksum documentation](https://www.paytmpayments.com/docs/checksum-implementation/), [Initiate Transaction API](https://www.paytmpayments.com/docs/api/initiate-transaction-api), [JS Checkout guide](https://www.paytmpayments.com/docs/js-checkout/getting-started/), and [Transaction Status API](https://www.paytmpayments.com/docs/api/v3/transaction-status-api/).

## 5. Switch to production

After Paytm activates the merchant account:

1. Set `PAYTM_ENV=production`.
2. Replace `PAYTM_MID` and `PAYTM_MERCHANT_KEY` with production credentials from the dashboard.
3. Set `PUBLIC_BASE_URL` to the deployed site's HTTPS origin; configure that callback URL with Paytm if required.
4. Deploy behind HTTPS and set a strong production `SECRET_KEY` through the host's secret manager. Do not commit `.env` or log credentials/tokens.
5. The app switches `websiteName` from `WEBSTAGING` to `DEFAULT` and Paytm host endpoints from `https://securestage.paytmpayments.com` to `https://secure.paytmpayments.com`; CheckoutJS uses the corresponding environment host as well.
6. Run a controlled production smoke payment and confirm the amount, callback, status API response, and order state before opening checkout to customers.

Paytm's [test-to-live guide](https://www.paytmpayments.com/docs/jscheckout-test-go-live) requires separate production MID/key, `DEFAULT` website name, and production API hosts. Do not use staging cards or credentials against production.

## Security notes

- `.env` is ignored by Git; only `.env.example` with placeholders is committed.
- `PAYTM_MERCHANT_KEY` is used only in `paytm_gateway.py` on the server.
- Order totals come from the stored cart and product rows, not submitted browser values.
- The app verifies callback checksum and signed Status API response, then cross-checks MID, order ID, amount, and final status.
- Checkout reserves stock in a transaction; failed payment releases it. Production shops should also add reservation expiry/reconciliation for abandoned or pending payments, rate limits, operational monitoring, and a proper production WSGI server.

## Project structure

```text
.
├── app.py
├── paytm_gateway.py
├── requirements.txt
├── .env.example
├── templates/
│   ├── paytm_checkout.html
│   ├── payment_result.html
│   ├── cart.html
│   ├── orders.html
│   └── admin.html
└── static/
    ├── style.css
    └── payments.css
```
