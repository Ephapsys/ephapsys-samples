#!/usr/bin/env python3
"""Provision a PKCS#11 token for the HelloWorld `hsm` anchor and print its enrollment record.

Creates (if absent) the two role-separated keys the Ephapsys SDK requires on any PKCS#11 token:
  - SIGN key (EC P-256): CKA_SIGN=true,   CKA_DERIVE=false  -> personalization evidence + device auth
  - KEM  key (EC P-256): CKA_DERIVE=true, CKA_SIGN=false    -> receives the model key, protects the cache
Both are sensitive and non-extractable. Many tokens grant sign and derive by default; the SDK rejects
keys that are not role-separated, so the flags are always set explicitly here.

Configuration comes from the same PKCS11_* variables the SDK uses (see .env.example).

  python3 pkcs11/provision_token.py                   # existing token (hardware or vendor module)
  python3 pkcs11/provision_token.py --init-softhsm    # DEV ONLY: create a SoftHSM token first

Writes the enrollment record (public keys + SPKI SHA-256 fingerprints; never the PIN) to stdout and,
with --out, to a file. Give it to an AOC administrator: see pkcs11/enroll_device.py.
SoftHSM is a software token for development and testing. It is NOT hardware-backed.
"""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path


def die(msg: str) -> None:
    sys.stderr.write(f"❌ {msg}\n")
    sys.exit(1)


def init_softhsm(token_label: str, pin_file: Path) -> None:
    """Create a per-user SoftHSM token (dev only). Writes SOFTHSM2_CONF-style config under ~/.softhsm."""
    if not shutil.which("softhsm2-util"):
        die("softhsm2-util not found (install the 'softhsm2' package)")
    conf = os.environ.get("SOFTHSM2_CONF")
    if not conf:
        die("set SOFTHSM2_CONF (e.g. ~/.softhsm/softhsm2.conf) before --init-softhsm")
    conf_path = Path(conf).expanduser()
    token_dir = conf_path.parent / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    if not conf_path.exists():
        conf_path.write_text(f"directories.tokendir = {token_dir}\nobjectstore.backend = file\nlog.level = ERROR\n")
    listing = subprocess.run(["softhsm2-util", "--show-slots"], capture_output=True, text=True).stdout
    if f"Label:            {token_label}" in listing or f"Label: {token_label}" in listing:
        print(f"ℹ️  SoftHSM token '{token_label}' already exists", file=sys.stderr)
        return
    pin_file.parent.mkdir(parents=True, exist_ok=True)
    if not pin_file.exists():
        pin_file.write_text(secrets.token_hex(8) + "\n")
        pin_file.chmod(0o600)
    pin = pin_file.read_text().strip()
    so_pin = secrets.token_hex(8)
    subprocess.run(["softhsm2-util", "--init-token", "--free", "--label", token_label, "--pin", pin, "--so-pin", so_pin],
                   check=True, capture_output=True)
    print(f"✅ Created SoftHSM token '{token_label}' (DEV ONLY, not hardware)", file=sys.stderr)


def provision_keys(cfg) -> None:
    import pkcs11
    from pkcs11 import Attribute, KeyType, ObjectClass
    from pkcs11.util.ec import encode_named_curve_parameters
    tok = pkcs11.lib(cfg.module).get_token(token_label=cfg.token_label)
    with tok.open(user_pin=cfg.resolve_pin(), rw=True) as s:
        params = s.create_domain_parameters(KeyType.EC, {Attribute.EC_PARAMS: encode_named_curve_parameters("secp256r1")}, local=True)
        base = {Attribute.SENSITIVE: True, Attribute.EXTRACTABLE: False, Attribute.TOKEN: True, Attribute.PRIVATE: True}
        for role, label, flags in (("sign", cfg.sign_key_label, {Attribute.SIGN: True, Attribute.DERIVE: False}),
                                   ("kem", cfg.kem_key_label, {Attribute.DERIVE: True, Attribute.SIGN: False})):
            if not label:
                die(f"set PKCS11_{role.upper()}_KEY_LABEL")
            if list(s.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY, Attribute.LABEL: label})):
                print(f"ℹ️  {role} key '{label}' already exists (unchanged)", file=sys.stderr)
                continue
            params.generate_keypair(store=True, label=label, public_template={Attribute.TOKEN: True},
                                    private_template={**base, **flags})
            print(f"✅ Created {role} key '{label}' with explicit role flags", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="Provision a PKCS#11 token for the HelloWorld hsm anchor.")
    ap.add_argument("--init-softhsm", action="store_true", help="DEV ONLY: create a SoftHSM token first")
    ap.add_argument("--out", help="also write the enrollment record to this file")
    args = ap.parse_args()
    try:
        from ephapsys.crypto.pkcs11 import Pkcs11Config, Pkcs11Provider, spki_sha256_hex
    except ImportError:
        die('install the SDK with PKCS#11 support: pip install "ephapsys[pkcs11]>=0.2.100"')
    if args.init_softhsm:
        pin_file = os.environ.get("PKCS11_PIN_FILE")
        if not pin_file:
            die("--init-softhsm requires PKCS11_PIN_FILE (the PIN is generated into it)")
        init_softhsm(os.environ.get("PKCS11_TOKEN_LABEL") or die("set PKCS11_TOKEN_LABEL"), Path(pin_file).expanduser())
    cfg = Pkcs11Config.from_env()
    provision_keys(cfg)
    prov = Pkcs11Provider(cfg)                              # re-validates role separation, mechanisms, policy
    sign_pem, kem_pem = prov.public_key_pem("sign"), prov.public_key_pem("kem")
    device_id = (os.environ.get("EPHAPSYS_DEVICE_ID") or "").strip()
    if not device_id:
        die("set EPHAPSYS_DEVICE_ID (a stable device identity)")
    record = {"device_id": device_id, "token": prov.token_info(),
              "sign_spki_sha256": spki_sha256_hex(sign_pem), "kem_spki_sha256": spki_sha256_hex(kem_pem),
              "pubkey_pem": sign_pem, "softhsm_dev_token": bool(args.init_softhsm)}
    text = json.dumps(record, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
