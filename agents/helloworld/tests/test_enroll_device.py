"""pkcs11/enroll_device.py record parsing: every device-side output format, fail-closed checks, dry run.

Run: pytest agents/helloworld/tests/test_enroll_device.py   (needs only `cryptography`; no token, no AOC)
"""
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

SCRIPT = Path(__file__).resolve().parents[1] / "pkcs11" / "enroll_device.py"
spec = importlib.util.spec_from_file_location("enroll_device", SCRIPT)
ed = importlib.util.module_from_spec(spec); spec.loader.exec_module(ed)


def _key():
    pem = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    der = serialization.load_pem_public_key(pem.encode()).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return pem, hashlib.sha256(der).hexdigest()


SIGN_PEM, SIGN_FP = _key()
KEM_PEM, KEM_FP = _key()

PROVISION = {"device_id": "dev-1", "token": {"label": "hw"}, "sign_spki_sha256": SIGN_FP,
             "kem_spki_sha256": KEM_FP, "pubkey_pem": SIGN_PEM, "softhsm_dev_token": False}
DEVICE_CHECK = {"device_id": "dev-1", "token": {"label": "hw", "model": "OP-TEE TA"},
                "sign_spki_sha256": SIGN_FP, "kem_spki_sha256": KEM_FP, "sign_pubkey_pem": SIGN_PEM}
SHOW_KEY = {"sign": {"key_id_hex": "01", "spki_sha256": SIGN_FP, "pubkey_pem": SIGN_PEM},
            "kem": {"key_id_hex": "02", "spki_sha256": KEM_FP, "pubkey_pem": KEM_PEM}}
CONSOLE = ("[PASS] TEE device node present\n"
           '[PASS] token selected by label/serial, config valid  — {"label": "hw", "model": "OP-TEE TA"}\n'
           "[PASS] binding-v1 evidence signature verifies\n\n"
           "Enrollment (give this to the AOC operator; `ephapsys hsm show-key` prints the same):\n"
           + json.dumps(DEVICE_CHECK, indent=2) + "\n\n11 passed, 0 failed, 4 manual\nexit=0\n")


@pytest.mark.parametrize("text", [json.dumps(PROVISION), json.dumps(DEVICE_CHECK), CONSOLE],
                         ids=["provision_token", "device_check", "device_check_console_log"])
def test_formats_with_device_id(text):
    assert ed.load_record(text) == {"device_id": "dev-1", "pubkey_pem": SIGN_PEM, "spki_sha256": SIGN_FP}


def test_show_key_needs_device_id_and_picks_the_sign_key():
    with pytest.raises(ed.RecordError, match="pass --device-id"):
        ed.load_record(json.dumps(SHOW_KEY))
    rec = ed.load_record(json.dumps(SHOW_KEY), device_id="dev-1")
    assert rec["pubkey_pem"] == SIGN_PEM and rec["spki_sha256"] == SIGN_FP


def test_empty_device_id_in_record_is_refused():
    with pytest.raises(ed.RecordError, match="no device id"):
        ed.load_record(json.dumps({**DEVICE_CHECK, "device_id": ""}))


def test_device_id_mismatch_is_refused():
    with pytest.raises(ed.RecordError, match="does not match the record"):
        ed.load_record(json.dumps(DEVICE_CHECK), device_id="dev-2")


def test_claimed_fingerprint_must_match_key():
    with pytest.raises(ed.RecordError, match="does not match its public key"):
        ed.load_record(json.dumps({**DEVICE_CHECK, "sign_spki_sha256": KEM_FP}))


def test_expect_fingerprint():
    assert ed.load_record(CONSOLE, expect_fingerprint=SIGN_FP.upper())["spki_sha256"] == SIGN_FP
    with pytest.raises(ed.RecordError, match="--expect-fingerprint"):
        ed.load_record(CONSOLE, expect_fingerprint=KEM_FP)


def test_no_record_and_conflicting_keys_are_refused():
    with pytest.raises(ed.RecordError, match="no enrollment record"):
        ed.load_record("[PASS] nothing here\n")
    other = {**DEVICE_CHECK, "sign_pubkey_pem": KEM_PEM, "sign_spki_sha256": KEM_FP}
    with pytest.raises(ed.RecordError, match="more than one different signing key"):
        ed.load_record(CONSOLE + json.dumps(other))


def test_bad_pem_is_refused():
    with pytest.raises(ed.RecordError, match="not a valid PEM"):
        ed.load_record(json.dumps({**DEVICE_CHECK, "sign_pubkey_pem": "garbage", "sign_spki_sha256": None}))


def test_cli_dry_run_sends_nothing(tmp_path):
    pytest.importorskip("ephapsys.session")
    rec = tmp_path / "check.txt"; rec.write_text(CONSOLE)
    r = subprocess.run([sys.executable, str(SCRIPT), "--template", "agent_temp_X", "--record", str(rec),
                        "--expect-fingerprint", SIGN_FP, "--dry-run"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert SIGN_FP in r.stdout and "dev-1" in r.stdout and "(dry run) would send: PUT" in r.stdout
