#!/usr/bin/env python3
"""Provision a PKCS#11 token for the HelloWorld `hsm` anchor and print its enrollment record.

Creates (if absent) the two role-separated keys the Ephapsys SDK requires on any PKCS#11 token:
  - SIGN key (EC P-256): CKA_SIGN=true,   CKA_DERIVE=false  -> personalization evidence + device auth
  - KEM  key (EC P-256): CKA_DERIVE=true, CKA_SIGN=false    -> receives the model key, protects the cache
Both are sensitive and non-extractable. Many tokens grant sign and derive by default; the SDK rejects
keys that are not role-separated, so the flags are always set explicitly here.

Configuration comes from the same PKCS11_* variables the SDK uses (see .env.example). Token and keys are
selected exactly like the SDK selects them (token label and/or serial; key CKA_LABEL and/or CKA_ID), and
every selection is resolved BEFORE anything is created, so nothing is written to an unintended token.

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
import stat
import subprocess
import sys
from pathlib import Path

ROLES = ("sign", "kem")


def die(msg: str) -> None:
    sys.stderr.write(f"❌ {msg}\n")
    sys.exit(1)


def create_pin_file(path: Path) -> str:
    """Create the PIN file atomically with mode 0600 (no permissive-umask window); never overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pin = secrets.token_hex(8)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pin + "\n")
    return pin


def read_pin_file(path: Path) -> str:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        die(f"PIN file {path} is accessible by group/others (mode {oct(mode & 0o777)}); chmod 600 it")
    pin = path.read_text().strip()
    if not pin:
        die(f"PIN file {path} is empty")
    return pin


def init_softhsm(token_label: str, pin_file: Path) -> None:
    """Create a per-user SoftHSM token (dev only). Writes the SOFTHSM2_CONF config if it does not exist."""
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
    labels = [line.split(":", 1)[1].strip() for line in
              subprocess.run(["softhsm2-util", "--show-slots"], capture_output=True, text=True).stdout.splitlines()
              if line.strip().startswith("Label:")]
    if token_label in labels:
        print(f"ℹ️  SoftHSM token '{token_label}' already exists", file=sys.stderr)
        return
    pin = read_pin_file(pin_file) if pin_file.exists() else create_pin_file(pin_file)
    subprocess.run(["softhsm2-util", "--init-token", "--free", "--label", token_label, "--pin", pin,
                    "--so-pin", secrets.token_hex(8)], check=True, capture_output=True)
    print(f"✅ Created SoftHSM token '{token_label}' (DEV ONLY, not hardware)", file=sys.stderr)


def _selector_template(cfg, role, object_class):
    from pkcs11 import Attribute
    key_id, label = cfg.key_selector(role)                 # raises unless a label and/or ID is configured
    tmpl = {Attribute.CLASS: object_class}
    if key_id is not None:
        tmpl[Attribute.ID] = key_id
    if label is not None:
        tmpl[Attribute.LABEL] = label
    return tmpl


def provision_keys(cfg, token) -> None:
    """Resolve both roles first; create only missing keys, with the configured label and ID. No partial writes
    on ambiguity: if any role matches more than one private or public object, nothing is created."""
    from pkcs11 import Attribute, KeyType, ObjectClass
    from pkcs11.util.ec import encode_named_curve_parameters
    with token.open(user_pin=cfg.resolve_pin(), rw=True) as s:
        plan = {}
        for role in ROLES:
            priv = list(s.get_objects(_selector_template(cfg, role, ObjectClass.PRIVATE_KEY)))
            pub = list(s.get_objects(_selector_template(cfg, role, ObjectClass.PUBLIC_KEY)))
            if len(priv) > 1 or len(pub) > 1:
                die(f"{role} selector matches more than one key on the token; refusing to provision")
            if bool(priv) != bool(pub):
                die(f"{role} key is incomplete on the token (private and public objects must both exist)")
            plan[role] = bool(priv)
        params = s.create_domain_parameters(KeyType.EC, {Attribute.EC_PARAMS: encode_named_curve_parameters("secp256r1")}, local=True)
        base = {Attribute.SENSITIVE: True, Attribute.EXTRACTABLE: False, Attribute.TOKEN: True, Attribute.PRIVATE: True}
        role_flags = {"sign": {Attribute.SIGN: True, Attribute.DERIVE: False},
                      "kem": {Attribute.DERIVE: True, Attribute.SIGN: False}}
        for role in ROLES:
            key_id, label = cfg.key_selector(role)
            if plan[role]:
                print(f"ℹ️  {role} key already exists (unchanged)", file=sys.stderr)
                continue
            ident = {}
            if key_id is not None:
                ident["id"] = key_id
            if label is not None:
                ident["label"] = label
            params.generate_keypair(store=True, public_template={Attribute.TOKEN: True},
                                    private_template={**base, **role_flags[role]}, **ident)
            print(f"✅ Created {role} key ({', '.join(f'{k}={v.hex() if isinstance(v, bytes) else v}' for k, v in ident.items())}) "
                  f"with explicit role flags", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="Provision a PKCS#11 token for the HelloWorld hsm anchor.")
    ap.add_argument("--init-softhsm", action="store_true", help="DEV ONLY: create a SoftHSM token first")
    ap.add_argument("--out", help="also write the enrollment record to this file")
    args = ap.parse_args()
    try:
        from ephapsys.crypto.pkcs11 import Pkcs11Config, Pkcs11Provider, spki_sha256_hex
    except ImportError:
        die('install the SDK with PKCS#11 support: pip install "ephapsys[pkcs11]>=0.2.100"')
    device_id = (os.environ.get("EPHAPSYS_DEVICE_ID") or "").strip()
    if not device_id:
        die("set EPHAPSYS_DEVICE_ID (a stable device identity)")
    if args.init_softhsm:
        pin_file = os.environ.get("PKCS11_PIN_FILE")
        if not pin_file or os.environ.get("PKCS11_PIN"):
            die("--init-softhsm requires PKCS11_PIN_FILE (the PIN is generated into it) and no PKCS11_PIN")
        init_softhsm(os.environ.get("PKCS11_TOKEN_LABEL") or die("set PKCS11_TOKEN_LABEL"), Path(pin_file).expanduser())
    cfg = Pkcs11Config.from_env()                          # validates selectors for both roles before any write
    if cfg.pin_file:
        read_pin_file(Path(cfg.pin_file).expanduser())      # refuse group/world-readable PIN files
    # Resolve the exact token with the SDK's own rules (label and/or serial, exactly one match) BEFORE writing.
    selecting = Pkcs11Provider(cfg)
    provision_keys(cfg, selecting._token)
    prov = Pkcs11Provider(cfg)
    # public_key_pem() re-checks each key's policy (sensitive, non-extractable, role flags, curve, distinct
    # roles); it does not exercise sign/derive. The SDK checks mechanisms when it signs or derives.
    sign_pem, kem_pem = prov.public_key_pem("sign"), prov.public_key_pem("kem")
    record = {"device_id": device_id, "token": prov.token_info(),
              "sign_spki_sha256": spki_sha256_hex(sign_pem), "kem_spki_sha256": spki_sha256_hex(kem_pem),
              "pubkey_pem": sign_pem, "softhsm_dev_token": bool(args.init_softhsm)}
    text = json.dumps(record, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
