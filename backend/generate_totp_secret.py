#!/usr/bin/env python3
"""Run this locally (not on the server) to enroll TOTP login for Brief AI.

Generates a random secret, prints it, and renders the QR code as ASCII art
in the terminal. Scan it with your authenticator app, then paste the printed
secret into .env as TOTP_SECRET before deploying. The secret never touches
the network or the running app.
"""
import pyotp
import qrcode

ISSUER = "Brief AI"
ACCOUNT = "brief-ai"

secret = pyotp.random_base32()
uri = pyotp.TOTP(secret).provisioning_uri(name=ACCOUNT, issuer_name=ISSUER)

qr = qrcode.QRCode(border=1)
qr.add_data(uri)
qr.make()
qr.print_ascii(invert=True)

print(f"\nTOTP_SECRET={secret}\n")
print("Scan the QR code above with your authenticator app, then add the")
print("line above to your .env file (replacing APP_PIN if present).")
