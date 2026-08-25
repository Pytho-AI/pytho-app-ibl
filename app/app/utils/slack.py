"""Slack messaging utility for posting messages to Slack channels."""
import os
from typing import Dict, Any, Optional
from slack_sdk import WebClient
from slack_sdk.webhook import WebhookClient
from slack_sdk.errors import SlackApiError

def get_bot_token() -> Optional[str]:
    """Gets the Slack bot token from environment variables."""
    return os.getenv("SLACK_BOT_TOKEN")


def get_notification_channel() -> Optional[str]:
    """Gets the Slack notification channel from environment variables."""
    return os.getenv("SLACK_NOTIFICATION_CHANNEL")

def post_to_channel(
    channel: str,
    message: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Posts a JSON message to a specific Slack channel using Bot Token.

    Args:
        channel (str): The channel ID or name (e.g., "#general" or "C1234567890").
        message (dict): The Slack message payload with text and/or blocks.
        
    Returns:
        dict: Result with "success" boolean, optional "error", and "ts" (message timestamp).
    """
    token = get_bot_token()
    
    if not token:
        return {
            "success": False,
            "error": "SLACK_BOT_TOKEN environment variable not set"
        }
    
    try:
        client = WebClient(token=token)
        response = client.chat_postMessage(
            channel=channel,
            **message
        )
        
        return {
            "success": True,
            "ts": response["ts"],
            "channel": response["channel"]
        }
    except SlackApiError as e:
        print(f"Failed to post message to Slack channel: {e}")
        return {
            "success": False,
            "error": e.response["error"]
        }
    except Exception as e:
        print(f"Failed to post message to Slack: {e}")
        return {"success": False, "error": str(e)}


def post_notification(message: Dict[str, Any]) -> Dict[str, Any]:
    """
    Posts a notification to the configured Slack notification channel.

    Args:
        message (dict): The Slack message payload with text and/or blocks.

    Returns:
        dict: Result with "success" boolean and optional "error" message.
    """
    channel = get_notification_channel()
    if not channel:
        return {"success": False, "error": "SLACK_NOTIFICATION_CHANNEL not set"}
    
    return post_to_channel(channel, message)
