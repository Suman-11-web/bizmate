# BizMate

BizMate is a Flask and SQLite multi-seller marketplace foundation. It has separate platform-owner and seller workspaces, reviewed seller applications, seller-owned product/inventory/order/expense records, product image uploads, a public marketplace, a cart, COD checkout, buyer order lookup, and manual seller-plan payment review.

## Run locally

Python 3.9 or newer is supported.

```bash
cd outputs/bizmate
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create the first platform owner interactively, then run the local development server:

```bash
python -m flask --app app create-owner
python app.py
```

The owner setup command creates the database schema and asks for an owner name, email, and password. Passwords are stored as hashes; do not put a production password in source control. The supplied plan catalog is installed on an empty database: Free/Starter (₹0/month, 25 active products), Business (₹299/month, unlimited active products), and Pro (₹699/month, unlimited active products). These are the configured BizMate plans, not sample sellers or catalogue data. BizMate does not create sample accounts, products, orders, expenses, or dashboard metrics. The marketplace stays empty until sellers are approved and publish their own products.

If the desired owner email already belongs to a seller and there is no owner yet, the `promote-owner --email seller@example.com` command can convert that account after explicit confirmation and a new password prompt. It preserves seller records but that account no longer has seller-workspace access.

For the paid-plan QR flow, place the owner’s UPI QR image at `static/images/qr.jpeg`. Sellers select a plan during registration, submit the UPI UTR/reference for a paid plan, and remain pending until the owner verifies the payment and approves the account. A submitted UTR is not treated as proof of payment.

## Account flow

1. Platform owner: sign in at `/login`, review seller details and UTRs, manually verify paid-plan payments against the UPI account, approve or reject applicants, and suspend or reactivate seller access.
2. Seller: choose Starter, Business, or Pro at `/register`; for paid plans, pay using the QR and provide the resulting UTR/reference. After owner verification and approval, manage that seller account’s products, stock, orders, expenses, customers, reports, and business insights.
3. Buyer: browse published, in-stock products, place a cash-on-delivery order, then verify its reference and checkout email at `/track/<order-reference>`.

Product images accept JPEG, PNG, or WebP up to 5 MB. New products are saved as drafts. Publishing requires an active owner-assigned plan and stock. Inventory is decremented at checkout and restored if an eligible order is cancelled; reopening a cancelled order checks and reserves stock again.

## Configuration

- `BIZMATE_SECRET_KEY`: required when `BIZMATE_ENV=production`; use a unique secret from a secret manager and keep it out of source control.
- `BIZMATE_ENV=production`: makes startup fail if the secret is missing.
- `BIZMATE_HTTPS=1`: marks session cookies secure and enables HSTS headers. It defaults to on in production; local HTTP development can leave it unset.
- `BIZMATE_DATABASE`: optional SQLite file path. The default is `instance/bizmate.db`.

The built-in Flask server is for local development only. For deployment, run behind a production WSGI server and HTTPS reverse proxy, set the production secret and secure-cookie setting, and configure backups and monitoring.

## Security and data boundaries

- Owner and seller roles have separate route guards and navigation; every seller query is scoped by seller account.
- Passwords use Werkzeug PBKDF2-SHA256 hashes. Passwords require at least 12 characters, including a letter and number. Five failed attempts for an IP/email pair trigger a 15-minute sign-in lock.
- Mutating forms and JSON actions require CSRF tokens. Session cookies are HTTP-only, SameSite=Lax, and can be HTTPS-only in deployment.
- Security headers include a same-origin Content Security Policy, clickjacking protection, MIME sniffing protection, and a restrictive referrer policy.
- SQL inputs are parameterized. Seller approval, plan changes, and account access changes are recorded in an owner-visible audit log.
- Buyer order lookup checks both the order reference and checkout email; the email is submitted in the POST body rather than put in the URL.

## Important scope before a public commercial launch

This build uses real persisted accounts and workflows, but it is not a complete hosted commerce operation by itself. Plan UPI payment verification is manual; there is no payment-provider integration, recurring plan collection, subscription renewal, shipping-carrier integration, or automated email. The plan catalogue only advertises capabilities implemented in this app; GST PDF invoices, staff logins, predictive AI, priority marketplace placement, and dedicated support are not enabled. Seller identity is collected as account details and a registration-number field; document/KYC review must be performed outside the app. Password reset, email verification, MFA, and external AI are not implemented. Uploaded images and data are stored on the local filesystem/SQLite file; a multi-instance deployment should move to managed PostgreSQL and object storage with migrations, access-controlled backups, and a retention policy.
