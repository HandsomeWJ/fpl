# main.py
from auth import get_access_token
from email_handler import fetch_and_filter_emails, extract_players
from transfer_handler import match_transfers, create_transfer_payload
from fpl_api import get_current_gameweek
from config import CLIENT_ID
import requests

def main():
    access_token = get_access_token()
    if access_token:
        emails = fetch_and_filter_emails(access_token)
        # print(f"Fetched {len(emails)} emails.")  # Add this line to check if emails are fetched
        headers = {'Authorization': f'Bearer {access_token}'}
        for email in emails:
            email_id = email['id']
            email_body_endpoint = f"https://graph.microsoft.com/v1.0/me/messages/{email_id}"

            email_response = requests.get(email_body_endpoint, headers=headers)
            if email_response.status_code == 200:
                email_data = email_response.json()
                body_content = email_data['body']['content']

                if "Paul Marshman" in body_content:
                    players_in, players_out = extract_players(body_content)

                    # Match players by position
                    transfer_pairs, unmatched_in = match_transfers(players_out, players_in)

                    if transfer_pairs:
                        print("Transfers (Out -> In):")
                        for out_player, out_position, in_player, in_position in transfer_pairs:
                            print(f"{out_player} ({out_position}) -> {in_player} ({in_position})")

                        # Create transfer payload
                        event = get_current_gameweek()
                        # print(f"Using Gameweek: {event}")
                        transfer_payload = create_transfer_payload(transfer_pairs, '10337428', event)
                        print(transfer_payload)

                    if unmatched_in:
                        print(f"Unmatched Transfers In: {unmatched_in}")

if __name__ == "__main__":
    main()

