"""
Firebase Cloud Messaging service.

Handles initialization of Firebase Admin SDK and sending push notifications.
The service account credentials are stored as base64-encoded JSON in the
FIREBASE_CREDENTIALS_BASE64 environment variable.
"""

import base64
import json
import logging
from typing import Optional

import firebase_admin
from firebase_admin import credentials, messaging

from app.core.config import settings

logger = logging.getLogger(__name__)

_firebase_app: Optional[firebase_admin.App] = None


def _init_firebase() -> Optional[firebase_admin.App]:
    """Initialize Firebase Admin SDK from base64-encoded credentials."""
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    if not settings.firebase_credentials_base64:
        logger.warning("⚠️ FIREBASE_CREDENTIALS_BASE64 not set — push notifications disabled")
        return None

    try:
        cred_json = base64.b64decode(settings.firebase_credentials_base64)
        cred_dict = json.loads(cred_json)
        cred = credentials.Certificate(cred_dict)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("🔥 Firebase Admin SDK initialized (project: %s)", cred_dict.get("project_id"))
        return _firebase_app
    except Exception as e:
        logger.error("❌ Failed to initialize Firebase Admin SDK: %s", e)
        return None


def get_firebase_app() -> Optional[firebase_admin.App]:
    """Get or initialize the Firebase app."""
    return _init_firebase()


async def send_to_token(
    token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> bool:
    """Send a notification to a single FCM token. Returns True on success."""
    app = get_firebase_app()
    if app is None:
        return False

    message = messaging.Message(
        notification=messaging.Notification(title=title, body=body),
        data=data or {},
        token=token,
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="default_channel",
                priority="max",
                default_sound=True,
            ),
        ),
    )

    try:
        messaging.send(message, app=app)
        return True
    except messaging.UnregisteredError:
        logger.info("🗑️ Token unregistered, should be removed: %s…", token[:20])
        return False
    except messaging.SenderIdMismatchError:
        logger.warning("⚠️ Sender ID mismatch for token: %s…", token[:20])
        return False
    except Exception as e:
        logger.error("❌ FCM send error: %s", e)
        return False


async def send_to_topic(
    topic: str,
    data: dict,
) -> bool:
    """Send a data-only message to an FCM topic. Returns True on success."""
    app = get_firebase_app()
    if app is None:
        return False

    # FCM data values must be strings
    str_data = {k: str(v) for k, v in data.items()}

    message = messaging.Message(
        data=str_data,
        topic=topic,
        android=messaging.AndroidConfig(priority="high"),
    )

    try:
        messaging.send(message, app=app)
        return True
    except Exception as e:
        logger.error("❌ FCM topic send error (%s): %s", topic, e)
        return False


async def send_to_tokens(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> dict:
    """
    Send a notification to multiple FCM tokens using batch.
    Returns {"success": int, "failure": int, "invalid_tokens": list[str]}.
    """
    app = get_firebase_app()
    if app is None:
        return {"success": 0, "failure": len(tokens), "invalid_tokens": []}

    if not tokens:
        return {"success": 0, "failure": 0, "invalid_tokens": []}

    message = messaging.MulticastMessage(
        notification=messaging.Notification(title=title, body=body),
        data=data or {},
        tokens=tokens,
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="default_channel",
                priority="max",
                default_sound=True,
            ),
        ),
    )

    try:
        response = messaging.send_each_for_multicast(message, app=app)
        invalid_tokens = []
        for i, send_response in enumerate(response.responses):
            if send_response.exception:
                if isinstance(send_response.exception, (
                    messaging.UnregisteredError,
                    messaging.SenderIdMismatchError,
                    messaging.InvalidArgumentError,
                )):
                    invalid_tokens.append(tokens[i])

        return {
            "success": response.success_count,
            "failure": response.failure_count,
            "invalid_tokens": invalid_tokens,
        }
    except Exception as e:
        logger.error("❌ FCM multicast error: %s", e)
        return {"success": 0, "failure": len(tokens), "invalid_tokens": []}
