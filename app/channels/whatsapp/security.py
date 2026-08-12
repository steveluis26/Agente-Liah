"""Validación de la firma HMAC del webhook de Meta (X-Hub-Signature-256)."""
import hashlib
import hmac

from app.core.config import get_settings

settings = get_settings()


def verify_meta_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Devuelve True si el payload fue firmado por Meta con app_secret.

    La cabecera llega como: 'sha256=hexdigest'.
    """
    if not settings.whatsapp_verify_signature:
        return True  # sandbox/dev: omitir
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    provided = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, provided)
