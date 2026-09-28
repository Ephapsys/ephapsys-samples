#!/usr/bin/env python3
"""OPERATOR step: enroll a device's PKCS#11 signing key with the AOC for an agent template.

Run this as an AOC tenant administrator (`ephapsys login` first). The device itself never enrolls:
an operator verifies the fingerprint printed on the device (pkcs11/provision_token.py or
`ephapsys hsm show-key`) and registers it here. Personalization is rejected until this is done.

  python3 pkcs11/enroll_device.py --template <AGENT_TEMPLATE_ID> --record enrollment.json
  python3 pkcs11/enroll_device.py --template <AGENT_TEMPLATE_ID> --record enrollment.json --revoke

Re-enrolling a device with a new key replaces the previous pin; instances personalized with the old key
must personalize again.
"""
import argparse
import hashlib
import json
import sys

import requests
from cryptography.hazmat.primitives import serialization


def main() -> None:
    ap = argparse.ArgumentParser(description="Enroll (or revoke) a device PKCS#11 signing key with the AOC.")
    ap.add_argument("--template", required=True, help="agent template id the device will personalize from")
    ap.add_argument("--record", required=True, help="enrollment record JSON from provision_token.py")
    ap.add_argument("--reason", default="HelloWorld PKCS#11 device enrollment", help="audit reason")
    ap.add_argument("--revoke", action="store_true", help="revoke the device enrollment instead")
    ap.add_argument("--yes", action="store_true", help="skip the fingerprint confirmation prompt")
    args = ap.parse_args()

    from ephapsys.session import auth_headers, get_api_url, load_session
    rec = json.loads(open(args.record).read())
    device_id, pem = rec["device_id"], rec["pubkey_pem"]
    key = serialization.load_pem_public_key(pem.encode())
    fp = hashlib.sha256(key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    if rec.get("sign_spki_sha256") and rec["sign_spki_sha256"] != fp:
        sys.exit("❌ record fingerprint does not match its public key; refusing")
    base = (load_session().get("base_url") or get_api_url()).rstrip("/")
    print(f"AOC:          {base}\nTemplate:     {args.template}\nDevice:       {device_id}\nSign SPKI:    {fp}")
    if rec.get("softhsm_dev_token"):
        print("⚠️  This key lives in a SoftHSM DEV token (software, not hardware).")
    if not args.yes and input("Confirm this fingerprint matches the device output [y/N]: ").strip().lower() != "y":
        sys.exit("aborted")

    headers = {**auth_headers(), "Content-Type": "application/json"}
    url = f"{base}/agents/{args.template}/hsm-enrollments"
    if args.revoke:
        r = requests.delete(f"{url}/{device_id}", headers=headers, json={"reason": args.reason}, timeout=30)
    else:
        r = requests.put(url, headers=headers, json={"device_id": device_id, "pubkey_pem": pem, "reason": args.reason}, timeout=30)
    if r.status_code == 403:
        sys.exit("❌ 403: an AOC tenant administrator session is required (ephapsys login as an admin)")
    if r.status_code != 200:
        sys.exit(f"❌ {r.status_code}: {r.text[:300]}")
    print(("✅ Revoked " if args.revoke else "✅ Enrolled ") + f"device {device_id}: {json.dumps(r.json())}")


if __name__ == "__main__":
    main()
