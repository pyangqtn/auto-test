#!/usr/bin/env python3
"""Slack notification test - send hello world to verify setup."""

import argparse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# TODO: Replace with your bot token
SLACK_TOKEN = "xoxb-your-bot-token-here"

def main():
    parser = argparse.ArgumentParser(description='Test Slack notification')
    parser.add_argument('-u', '--user', required=True, help='Slack user (username or email)')
    args = parser.parse_args()

    client = WebClient(token=SLACK_TOKEN)
    
    try:
        # Lookup user by email or username
        if '@' in args.user:
            response = client.users_lookupByEmail(email=args.user)
            user_id = response['user']['id']
        else:
            response = client.users_list()
            user_id = None
            for user in response['members']:
                if user.get('name') == args.user or user.get('profile', {}).get('display_name') == args.user:
                    user_id = user['id']
                    break
            if not user_id:
                print(f"User not found: {args.user}")
                return 1
        
        print(f"Found user ID: {user_id}")
        
        # Open DM and send message
        dm = client.conversations_open(users=[user_id])
        client.chat_postMessage(channel=dm["channel"]["id"], text="Hello World! 🎉 Slack integration working!")
        print("SUCCESS: Message sent!")
        return 0
        
    except SlackApiError as e:
        print(f"ERROR: {e.response['error']}")
        return 1

if __name__ == '__main__':
    exit(main())
