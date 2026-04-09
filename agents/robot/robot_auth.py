#!/usr/bin/env python3

import os
from typing import Any, Dict, Optional, Tuple

SOCIAL_LOGIN_ENABLED: bool = bool(int(os.getenv("SOCIAL_LOGIN_ENABLED", "0")))
GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")


def verify_google_token(token: str) -> Dict[str, Any]:
    """Validate a Google ID token and return user info."""
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests

        info = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception as exc:
        raise ValueError(f"Invalid Google token: {exc}")
    return {
        "user_id": info["sub"],
        "name": info.get("name", ""),
        "email": info.get("email", ""),
        "picture": info.get("picture", ""),
    }


def resolve_session(
    token: Optional[str],
    session_id: Optional[str],
) -> Tuple[str, Dict[str, Any]]:
    """Return (user_id, user_info).

    SOCIAL_LOGIN_ENABLED=0 (default): anonymous session keyed by session_id or "default".
    SOCIAL_LOGIN_ENABLED=1: Google ID token required; user_id = Google sub.
    """
    if not SOCIAL_LOGIN_ENABLED:
        uid = session_id or "default"
        return uid, {"user_id": uid, "name": "", "email": "", "picture": ""}
    if not token:
        raise ValueError("Authentication required: provide a Google ID token via ?token=")
    info = verify_google_token(token)
    return info["user_id"], info
