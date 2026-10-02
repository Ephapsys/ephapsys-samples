#!/usr/bin/env python3
"""OPERATOR step: enroll a device's PKCS#11 signing key with the AOC for an agent template.

Run this as an AOC tenant administrator (`ephapsys login` first). The device itself never enrolls:
an operator verifies the fingerprint printed on the device and registers it here. Personalization is
rejected ("device key is not enrolled or is revoked") until this is done.

  python3 pkcs11/enroll_device.py --template <AGENT_TEMPLATE_ID> --record enrollment.json
  python3 pkcs11/enroll_device.py --template <AGENT_TEMPLATE_ID> --record enrollment.json --revoke
  python3 pkcs11/enroll_device.py --template <AGENT_TEMPLATE_ID> --device-id <ID> --revoke

--record accepts the output of any of the device-side tools, as a JSON file or the saved console output:
  - pkcs11/provision_token.py --out FILE        ({"device_id", "pubkey_pem", "sign_spki_sha256", ...})
  - pkcs11_device_check.py (platform repo)      ({"device_id", "sign_pubkey_pem", "sign_spki_sha256", ...})
  - ephapsys hsm show-key                       ({"sign": {"pubkey_pem", "spki_sha256"}, "kem": {...}})
    show-key does not print the device id, so pass --device-id (it must equal EPHAPSYS_DEVICE_ID on the device).

Use --expect-fingerprint with the SPKI SHA-256 the device owner reported out of band; the enrollment is
refused if the key in the record does not match it. --dry-run prints the request without sending it.

Re-enrolling a device with a new key replaces the previous pin; instances personalized with the old key
must personalize again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys

from cryptography.hazmat.primitives import serialization


class RecordError(ValueError):
    pass


def spki_sha256(pem: str) -> str:
    try:
        key = serialization.load_pem_public_key(pem.encode())
    except Exception as e:  # noqa: BLE001 - any parse failure is a bad record
        raise RecordError(f"signing public key is not a valid PEM public key ({e})") from None
    return hashlib.sha256(key.public_bytes(serialization.Encoding.DER,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def _json_objects(text: str):
    """The whole text as JSON if it parses, else every top-level JSON object embedded in it (console logs)."""
    try:
        yield json.loads(text)
        return
    except json.JSONDecodeError:
        pass
    dec, i = json.JSONDecoder(), 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        yield obj
        i = end


def _extract(obj):
    """(device_id or None, signing PEM, claimed fingerprint or None) for a recognised record shape, else None."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("pubkey_pem"), str):                       # provision_token.py
        return obj.get("device_id"), obj["pubkey_pem"], obj.get("sign_spki_sha256")
    if isinstance(obj.get("sign_pubkey_pem"), str):                  # pkcs11_device_check.py
        return obj.get("device_id"), obj["sign_pubkey_pem"], obj.get("sign_spki_sha256")
    sign = obj.get("sign")
    if isinstance(sign, dict) and isinstance(sign.get("pubkey_pem"), str):   # ephapsys hsm show-key
        return obj.get("device_id"), sign["pubkey_pem"], sign.get("spki_sha256")
    return None


def load_record(text: str, device_id: str | None = None, expect_fingerprint: str | None = None) -> dict:
    """Parse an enrollment record and return {"device_id", "pubkey_pem", "spki_sha256"}. Fails closed."""
    found = [r for r in map(_extract, _json_objects(text)) if r]
    if not found:
        raise RecordError("no enrollment record found (expected output of provision_token.py, "
                          "pkcs11_device_check.py or `ephapsys hsm show-key`)")
    pems = {spki_sha256(pem) for _, pem, _ in found}
    if len(pems) != 1:
        raise RecordError("the input contains more than one different signing key; pass a single record")
    rec_device, pem, claimed = found[0]
    fp = spki_sha256(pem)
    for _, _, c in found:
        if c and c.lower() != fp:
            raise RecordError("record fingerprint does not match its public key; refusing")
    if expect_fingerprint:
        want = re.sub(r"[\s:]", "", expect_fingerprint).lower()
        if want != fp:
            raise RecordError(f"key fingerprint {fp} does not match --expect-fingerprint {want}; refusing")
    rec_device = (rec_device or "").strip() or None
    if device_id and rec_device and device_id != rec_device:
        raise RecordError(f"--device-id {device_id!r} does not match the record's device_id {rec_device!r}")
    dev = device_id or rec_device
    if not dev:
        raise RecordError("no device id: the record has none (EPHAPSYS_DEVICE_ID was unset on the device, "
                          "or the record came from `ephapsys hsm show-key`); pass --device-id")
    return {"device_id": dev, "pubkey_pem": pem, "spki_sha256": fp}


def main() -> None:
    ap = argparse.ArgumentParser(description="Enroll (or revoke) a device PKCS#11 signing key with the AOC.")
    ap.add_argument("--template", required=True, help="agent template id the device will personalize from")
    ap.add_argument("--record", help="enrollment record: JSON file or saved console output of a device-side tool")
    ap.add_argument("--device-id", help="device id (required if the record has none; must match it otherwise)")
    ap.add_argument("--expect-fingerprint", help="SPKI SHA-256 reported by the device owner; refuse on mismatch")
    ap.add_argument("--reason", default="HelloWorld PKCS#11 device enrollment", help="audit reason")
    ap.add_argument("--revoke", action="store_true", help="revoke the device enrollment instead")
    ap.add_argument("--dry-run", action="store_true", help="print the request without sending it")
    ap.add_argument("--yes", action="store_true", help="skip the fingerprint confirmation prompt")
    args = ap.parse_args()

    if args.record:
        try:
            rec = load_record(open(args.record).read(), args.device_id, args.expect_fingerprint)
        except (OSError, RecordError) as e:
            sys.exit(f"❌ {e}")
    elif args.revoke and args.device_id:
        rec = {"device_id": args.device_id, "pubkey_pem": None, "spki_sha256": None}
    else:
        sys.exit("❌ --record is required (or --device-id with --revoke)")

    from ephapsys.session import auth_headers, get_api_url, load_session
    base = (load_session().get("base_url") or get_api_url()).rstrip("/")
    print(f"AOC:          {base}\nTemplate:     {args.template}\nDevice:       {rec['device_id']}")
    if rec["spki_sha256"]:
        print(f"Sign SPKI:    {rec['spki_sha256']}")
    if args.record and re.search(r'"softhsm_dev_token"\s*:\s*true', open(args.record).read()):
        print("⚠️  This key lives in a SoftHSM DEV token (software, not hardware).")
    url = f"{base}/agents/{args.template}/hsm-enrollments"
    if args.dry_run:
        action = f"DELETE {url}/{rec['device_id']}" if args.revoke else f"PUT {url}"
        print(f"(dry run) would send: {action}")
        return
    if not args.yes:
        prompt = "Confirm revocation of this device" if args.revoke else "Confirm this fingerprint matches the device output"
        if input(f"{prompt} [y/N]: ").strip().lower() != "y":
            sys.exit("aborted")

    import requests
    headers = {**auth_headers(), "Content-Type": "application/json"}
    if args.revoke:
        r = requests.delete(f"{url}/{rec['device_id']}", headers=headers, json={"reason": args.reason}, timeout=30)
    else:
        r = requests.put(url, headers=headers, json={"device_id": rec["device_id"], "pubkey_pem": rec["pubkey_pem"],
                                                     "reason": args.reason}, timeout=30)
    if r.status_code == 403:
        sys.exit("❌ 403: an AOC tenant administrator session is required (ephapsys login as an admin)")
    if r.status_code != 200:
        sys.exit(f"❌ {r.status_code}: {r.text[:300]}")
    print(("✅ Revoked " if args.revoke else "✅ Enrolled ") + f"device {rec['device_id']}: {json.dumps(r.json())}")


if __name__ == "__main__":
    main()
